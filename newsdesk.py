# ABOUTME: Unified notification hub CLI — send, watch, and init subcommands.
# ABOUTME: Polls JSONL queues (local + remote via SSH), displays in terminal, forwards to Pushover.

import argparse
import curses
import json
import os
import socket
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants (adjust as needed)
# ---------------------------------------------------------------------------
QUEUE_MAX_LINES = 200
QUEUE_ROTATE_TO = 100
HISTORY_MAX_ENTRIES = 1000
POLL_INTERVAL_S = 2
SSH_CONNECT_TIMEOUT = 2
SSH_COMMAND_TIMEOUT = 3
DEFAULT_PROJECT = "general"
STALE_PROCESSING_AGE = 86400  # 1 day in seconds
DISPLAY_FIELD_WIDTH = 15
LINK_MARKER = "\U0001f517"  # 🔗 — shown in the watch UI when an entry carries a url

DEFAULT_CONFIG = {
    "queue_file": "~/.local/share/newsdesk/queue.jsonl",
    "history_file": "~/.local/share/newsdesk/history.jsonl",
    "remote_machines": [],
    "pushover_min_priority": -1,  # only forward priority >= this to Pushover (-2 always silent)
}

PRIORITY_ICONS = {
    -2: None,       # silent — skipped entirely
    -1: "",         # quiet — no icon
    0: "\u2705",    # ✅
    1: "\U0001f514",  # 🔔
    2: "\U0001f514",  # 🔔
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path):
    """Load config from JSON file, falling back to defaults. Expands ~ in paths."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(path) as f:
            user = json.load(f)
        config.update(user)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    for key in ("queue_file", "history_file"):
        config[key] = os.path.expanduser(config[key])

    # Don't expand ~ for remote paths — tilde refers to the remote user's home,
    # not the local user's. consume_remote_queue handles ~ -> $HOME substitution.

    return config


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------
def parse_jsonl(path):
    """Parse a JSONL file, skipping malformed lines. Returns [] for missing/empty files."""
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return entries


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def append_to_history(history_path, new_entries):
    """Append entries to history, capping at HISTORY_MAX_ENTRIES."""
    if not new_entries:
        return

    existing = parse_jsonl(history_path)
    combined = existing + new_entries

    if len(combined) > HISTORY_MAX_ENTRIES:
        combined = combined[-HISTORY_MAX_ENTRIES:]

    os.makedirs(os.path.dirname(history_path), exist_ok=True)
    with open(history_path, "w") as f:
        for entry in combined:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Priority helpers
# ---------------------------------------------------------------------------
def priority_icon(priority):
    """Return the display icon for a priority level."""
    return PRIORITY_ICONS.get(priority, PRIORITY_ICONS[0])


DEFAULT_BELL_THRESHOLD = 1
BELL_THRESHOLD_CYCLE = [1, 0, -1, None]


def should_bell(priority, threshold):
    """Return True if this priority should ring the terminal bell.

    Priority -2 (silent) never bells. threshold=None disables bell entirely.
    """
    if threshold is None or priority == -2:
        return False
    return priority >= threshold


def cycle_bell_threshold(current):
    """Cycle to the next bell threshold: 1 -> 0 -> -1 -> off (None) -> 1."""
    try:
        idx = BELL_THRESHOLD_CYCLE.index(current)
    except ValueError:
        return DEFAULT_BELL_THRESHOLD
    return BELL_THRESHOLD_CYCLE[(idx + 1) % len(BELL_THRESHOLD_CYCLE)]


def bell_threshold_label(threshold):
    """Human-readable label for a bell threshold value."""
    if threshold is None:
        return "off"
    if threshold == 1:
        return "high+ (default)"
    if threshold == 0:
        return "normal+"
    if threshold == -1:
        return "quiet+"
    return f"≥{threshold}"


def should_display(entry, show_silent=False):
    """Return True if this entry should be shown. Priority -2 hidden unless show_silent."""
    if entry.get("priority", 0) == -2:
        return show_silent
    return True


def should_forward_pushover(entry, min_priority=-1):
    """Return True if this entry should be forwarded to Pushover.

    Priority -2 is never forwarded (hard rule). Otherwise the entry must meet
    min_priority — lets the watcher keep low-value chatter (e.g. priority-0
    end-of-turn pings) on the console/feed without pushing it to the phone.
    """
    priority = entry.get("priority", 0)
    return priority != -2 and priority >= min_priority


def pushover_status_label(suppressed, app_token, user_key, min_priority):
    """Compact Pushover-forwarding state for the watch header / help screen."""
    if suppressed:
        return "off (--no-pushover)"
    if not (app_token and user_key):
        return "no keychain tokens"
    return f"≥ {min_priority}"  # e.g. "≥ 1"


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------
def _fit_field(value, width=DISPLAY_FIELD_WIDTH):
    """Pad short values or truncate long values to a fixed width."""
    if len(value) > width:
        return value[:width - 1] + "\u2026"
    return f"{value:<{width}}"


def format_entry(entry):
    """Format a notification entry as a display line."""
    ts = time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0)))
    project = _fit_field(entry.get("project", DEFAULT_PROJECT))
    icon = priority_icon(entry.get("priority", 0))
    title = entry.get("title", "")
    message = entry.get("message", "")
    machine = entry.get("machine", "")

    parts = [ts, project]
    if machine:
        parts.append(_fit_field(machine))
    if icon:
        parts.append(icon)
    if entry.get("url"):
        parts.append(LINK_MARKER)  # left of the message so width-clipping keeps it visible
    parts.append(f"{title} \u2014 {message}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Queue consumption (rename-read-delete)
# ---------------------------------------------------------------------------
def consume_local_queue(queue_path):
    """Consume the local queue using rename-read-delete. Returns list of entries."""
    processing_path = queue_path + ".processing"
    entries = []

    # Step 1: Handle existing .processing file (crash recovery)
    if os.path.exists(processing_path):
        age = time.time() - os.path.getmtime(processing_path)
        if age < STALE_PROCESSING_AGE:
            entries.extend(parse_jsonl(processing_path))
        os.unlink(processing_path)

    # Step 2-4: Rename queue, read it, delete it
    if os.path.exists(queue_path):
        try:
            os.rename(queue_path, processing_path)
        except OSError:
            return entries
        entries.extend(parse_jsonl(processing_path))
        try:
            os.unlink(processing_path)
        except FileNotFoundError:
            pass

    return entries


def consume_remote_queue(host, queue_file):
    """Consume a remote queue via SSH rename-read-delete. Returns list of entries."""
    # Replace ~ with $HOME so tilde expands on the remote machine
    remote_path = queue_file.replace("~", "$HOME", 1) if queue_file.startswith("~") else queue_file
    cmd = (
        f'f="{remote_path}"; p="$f.processing"; '
        f'if [ -f "$p" ]; then '
        f'  if [ "$(find "$p" -mtime -1 2>/dev/null)" ]; then cat "$p"; fi; '
        f'  rm -f "$p"; '
        f'fi; '
        f'mv "$f" "$p" 2>/dev/null && cat "$p" && rm "$p"'
    )
    try:
        result = subprocess.run(
            [
                "ssh",
                f"-o ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                "-o BatchMode=yes",
                host,
                cmd,
            ],
            capture_output=True, text=True,
            timeout=SSH_COMMAND_TIMEOUT,
        )
        if result.returncode != 0 and not result.stdout:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    entries = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Pushover forwarding
# ---------------------------------------------------------------------------
def forward_to_pushover(entry, app_token, user_key):
    """Forward a notification to Pushover via curl. Best-effort."""
    try:
        cmd = [
            "curl", "-s",
            "--form-string", f"token={app_token}",
            "--form-string", f"user={user_key}",
            "--form-string", f"title={entry['title']}",
            "--form-string", f"message={entry['message']}",
            "--form-string", f"priority={entry.get('priority', 0)}",
        ]
        # Optional supplementary link → a tappable button in the notification.
        # url_title is meaningless to Pushover without url, so gate it on url.
        if entry.get("url"):
            cmd += ["--form-string", f"url={entry['url']}"]
            if entry.get("url_title"):
                cmd += ["--form-string", f"url_title={entry['url_title']}"]
        cmd.append("https://api.pushover.net/1/messages.json")
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass


def read_keychain_token(service):
    """Read a token from macOS Keychain. Returns None on failure."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "pushover", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Auto-detection helpers
# ---------------------------------------------------------------------------
def get_machine_name():
    """Return short lowercase hostname."""
    return socket.gethostname().split(".")[0].lower()


def detect_project(directory=None):
    """Walk up from directory looking for a .git dir. Return repo name or DEFAULT_PROJECT."""
    if directory is None:
        directory = os.getcwd()
    path = os.path.abspath(directory)
    while True:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.basename(path).lower()
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return DEFAULT_PROJECT


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_notification(queue_path, title, message, priority, project=None,
                      url=None, url_title=None):
    """Append a notification to the queue file. Creates parent dirs if needed.

    url/url_title are an optional supplementary link forwarded to Pushover as a
    tappable button. They are persisted only when set, so existing entries that
    carry no link keep their minimal shape.
    """
    if project is None:
        project = DEFAULT_PROJECT

    entry = {
        "ts": time.time(),
        "title": title,
        "message": message,
        "priority": priority,
        "project": project,
        "machine": get_machine_name(),
    }
    if url:
        entry["url"] = url
        if url_title:
            entry["url_title"] = url_title

    os.makedirs(os.path.dirname(queue_path), exist_ok=True)

    with open(queue_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    _maybe_rotate(queue_path)


def _maybe_rotate(queue_path):
    """If queue exceeds QUEUE_MAX_LINES, keep only the last QUEUE_ROTATE_TO lines."""
    try:
        with open(queue_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    if len(lines) > QUEUE_MAX_LINES:
        with open(queue_path, "w") as f:
            f.writelines(lines[-QUEUE_ROTATE_TO:])


# ---------------------------------------------------------------------------
# Watch (curses UI)
# ---------------------------------------------------------------------------
def cmd_watch_curses(stdscr, config, no_pushover):
    """Main watch loop inside curses."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(POLL_INTERVAL_S * 1000))

    # Forwarding is governed entirely by config["pushover_min_priority"]; the only
    # session control is --no-pushover, which suppresses all forwarding this run.
    pushover_suppressed = bool(no_pushover)
    min_priority = config.get("pushover_min_priority", -1)

    app_token = user_key = None
    if not pushover_suppressed:
        app_token = read_keychain_token("newsdesk-app-token")
        user_key = read_keychain_token("newsdesk-user-key")

    display_lines = []
    mode = "latest"  # latest | history | help
    history_offset = 0
    help_page = 0           # 0 = keys, 1 = priority reference
    show_silent = False     # whether to display priority -2 entries
    bell_threshold = DEFAULT_BELL_THRESHOLD  # priority >= this rings the bell; None = off
    status_msg = ""         # transient message shown on status line
    status_msg_until = 0    # timestamp when status_msg expires
    poll_blink = False      # toggles each loop for activity indicator

    while True:
        # Poll queues
        new_entries = consume_local_queue(config["queue_file"])
        for machine in config["remote_machines"]:
            new_entries.extend(
                consume_remote_queue(machine["host"], machine["queue_file"])
            )

        if new_entries:
            append_to_history(config["history_file"], new_entries)
            for entry in new_entries:
                if should_display(entry, show_silent):
                    display_lines.append(format_entry(entry))
                    if should_bell(entry.get("priority", 0), bell_threshold):
                        curses.beep()
                # tokens are None when --no-pushover suppressed forwarding for this run
                if app_token and user_key and should_forward_pushover(entry, min_priority):
                    forward_to_pushover(entry, app_token, user_key)

        # Clear expired status message
        if status_msg and time.time() > status_msg_until:
            status_msg = ""

        # Draw
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Header
        silent_indicator = " [silent ON]" if show_silent else ""
        bell_indicator = f" [bell: {bell_threshold_label(bell_threshold)}]" if bell_threshold != DEFAULT_BELL_THRESHOLD else ""
        mode_label = {"latest": "LATEST", "history": "HISTORY", "help": "HELP"}.get(mode, mode.upper())
        header = f"newsdesk [{mode_label}] \u2014 (L)atest (H)istory (C)lear (S)ave (B)ell si(V)lent (Q)uit (?)help{silent_indicator}{bell_indicator}"
        remotes = config["remote_machines"]
        if len(remotes) == 1:
            poll_info = f"polling local + {remotes[0]['host']}"
        elif remotes:
            poll_info = f"polling local + {len(remotes)} remote"
        else:
            poll_info = "polling local"
        poll_blink = not poll_blink
        blink_char = "\u25cf" if poll_blink else " "
        po_status = pushover_status_label(pushover_suppressed, app_token, user_key, min_priority)
        if status_msg:
            status = status_msg
        else:
            status = f"{blink_char} Pushover: {po_status}  \u23f3 {poll_info}"

        try:
            stdscr.addnstr(0, 0, header, width - 1)
            stdscr.addnstr(1, 0, status, width - 1)
            stdscr.addnstr(2, 0, "\u2500" * min(width - 1, 60), width - 1)
        except curses.error:
            pass

        content_start = 3
        content_height = height - content_start

        if mode == "latest":
            visible = display_lines[-(content_height):]
            for i, line in enumerate(visible):
                try:
                    padded = line.ljust(width - 1)[:width - 1]
                    stdscr.addnstr(content_start + i, 0, padded, width - 1)
                except curses.error:
                    pass

        elif mode == "history":
            history = parse_jsonl(config["history_file"])
            history_lines = [format_entry(e) for e in history if should_display(e, show_silent)]
            total = len(history_lines)
            history_offset = max(0, min(history_offset, total - content_height))
            visible = history_lines[history_offset:history_offset + content_height]
            for i, line in enumerate(visible):
                try:
                    padded = line.ljust(width - 1)[:width - 1]
                    stdscr.addnstr(content_start + i, 0, padded, width - 1)
                except curses.error:
                    pass

        elif mode == "help":
            help_pages = []
            # Page 1: keyboard shortcuts & polling
            page1 = [
                "Keyboard shortcuts:",
                "",
                "  L   Latest view — live tail of incoming notifications",
                "  H   History view — browse all processed notifications",
                "  C   Clear the latest view",
                "  S   Save history snapshot to a .log file",
                "  V   Toggle visibility of silent (priority -2) messages",
                "  B   Cycle bell threshold (high+ → normal+ → quiet+ → off)",
                "  Q   Quit the watcher",
                "  ?   This help screen",
                "",
                "History view navigation:",
                "",
                "  Up/k       Scroll up one line",
                "  Down/j     Scroll down one line",
                "  PgUp       Scroll up one page",
                "  PgDn       Scroll down one page",
                "  Home       Jump to oldest",
                "  End        Jump to newest",
                "",
                "Polling:",
                "",
                "  Local queue: " + config["queue_file"],
            ]
            for m in config["remote_machines"]:
                page1.append(f"  Remote: {m['host']}:{m['queue_file']}")
            if not config["remote_machines"]:
                page1.append("  No remote machines configured")
            help_pages.append(page1)
            # Page 2: priority reference & Pushover status
            app_status = "✓ found" if app_token else "✗ missing"
            key_status = "✓ found" if user_key else "✗ missing"
            def bell_cell(p):
                return "yes" if should_bell(p, bell_threshold) else "no "
            def push_cell(p):
                return "yes" if (p != -2 and p >= min_priority) else "no "
            help_pages.append([
                "Priority reference:",
                "",
                f"Bell threshold: {bell_threshold_label(bell_threshold)} (B key cycles)",
                "",
                "  Priority   Icon   Bell   Display          Pushover",
                "  ────────   ────   ────   ───────          ────────",
                f"  -2 silent  (none)  {bell_cell(-2)}   hidden (V key)   never",
                f"  -1 quiet   (none)  {bell_cell(-1)}   yes              {push_cell(-1)}",
                f"   0 normal  \u2705      {bell_cell(0)}   yes              {push_cell(0)}",
                f"   1 high    \U0001f514      {bell_cell(1)}   yes              {push_cell(1)}",
                f"   2 urgent  \U0001f514      {bell_cell(2)}   yes              {push_cell(2)}",
                "",
                "Use --priority N with 'newsdesk send' to set priority.",
                f"Pushover forwards priority ≥ {min_priority} (config: pushover_min_priority).",
                "",
                f"Pushover status: {po_status}",
                f"  Keychain newsdesk-app-token: {app_status}",
                f"  Keychain newsdesk-user-key:  {key_status}",
            ])
            help_page = min(help_page, len(help_pages) - 1)
            help_lines = help_pages[help_page]
            nav = f"Page {help_page + 1}/{len(help_pages)} — Left/Right to switch, Esc to return"
            help_lines = help_lines + ["", nav]
            for i, line in enumerate(help_lines):
                if i >= content_height:
                    break
                try:
                    padded = line.ljust(width - 1)[:width - 1]
                    stdscr.addnstr(content_start + i, 0, padded, width - 1)
                except curses.error:
                    pass

        stdscr.refresh()

        # Input
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch == -1:
            continue

        if mode == "help":
            if ch == 27:  # Esc
                mode = "latest"
                help_page = 0
            elif ch == curses.KEY_RIGHT or ch == ord("l"):
                help_page += 1
            elif ch == curses.KEY_LEFT or ch == ord("h"):
                help_page = max(0, help_page - 1)
        else:
            if ch in (ord("q"), ord("Q")):
                break
            elif ch in (ord("l"), ord("L")):
                mode = "latest"
            elif ch in (ord("h"), ord("H")):
                mode = "history"
                history_offset = 999999  # will be clamped to end
            elif ch in (ord("c"), ord("C")):
                display_lines.clear()
            elif ch in (ord("s"), ord("S")):
                save_path = os.path.join(
                    os.path.dirname(config["history_file"]),
                    time.strftime("newsdesk-%Y-%m-%d-%H%M.log"),
                )
                history = parse_jsonl(config["history_file"])
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w") as f:
                    for entry in history:
                        f.write(format_entry(entry) + "\n")
                status_msg = f"Saved to {save_path}"
                status_msg_until = time.time() + 3
            elif ch in (ord("v"), ord("V")):
                show_silent = not show_silent
                status_msg = f"Silent messages: {'shown' if show_silent else 'hidden'}"
                status_msg_until = time.time() + 3
            elif ch in (ord("b"), ord("B")):
                bell_threshold = cycle_bell_threshold(bell_threshold)
                status_msg = f"Bell: {bell_threshold_label(bell_threshold)}"
                status_msg_until = time.time() + 3
            elif ch == ord("?"):
                mode = "help"
            elif mode == "history":
                if ch == curses.KEY_UP or ch == ord("k"):
                    history_offset = max(0, history_offset - 1)
                elif ch == curses.KEY_DOWN or ch == ord("j"):
                    history_offset += 1
                elif ch == curses.KEY_PPAGE:  # Page Up
                    history_offset = max(0, history_offset - content_height)
                elif ch == curses.KEY_NPAGE:  # Page Down
                    history_offset += content_height
                elif ch == curses.KEY_HOME:
                    history_offset = 0
                elif ch == curses.KEY_END:
                    history_offset = 999999  # clamped on next draw


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_send(args):
    """Handle the 'send' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))
    project = args.project if args.project else detect_project()
    try:
        send_notification(
            config["queue_file"],
            args.title,
            args.message,
            args.priority,
            project,
            url=args.url,
            url_title=args.url_title,
        )
    except Exception:
        pass  # best-effort
    return 0


def cmd_watch(args):
    """Handle the 'watch' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))

    curses.wrapper(cmd_watch_curses, config, args.no_pushover)
    return 0


def cmd_init(args):
    """Handle the 'init' subcommand."""
    cfg_path = os.path.expanduser("~/.config/newsdesk/config.json")
    if os.path.exists(cfg_path):
        print(f"Config already exists: {cfg_path}")
    else:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
            f.write("\n")
        print(f"Config created: {cfg_path}")

    for key_name in ("newsdesk-app-token", "newsdesk-user-key"):
        token = read_keychain_token(key_name)
        if token:
            print(f"  Keychain: {key_name} \u2713")
        else:
            print(f"  Keychain: {key_name} \u2717 (not found)")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="newsdesk", description="Unified notification hub")
    sub = parser.add_subparsers(dest="command")

    p_send = sub.add_parser("send", help="Send a notification")
    p_send.add_argument("title", help="Notification title")
    p_send.add_argument("message", help="Notification message")
    p_send.add_argument("--priority", type=int, default=0, help="Priority (-2 to 2)")
    p_send.add_argument("--project", default=None, help="Project tag")
    p_send.add_argument("--url", default=None,
                        help="Supplementary URL — a tappable link in the Pushover notification")
    p_send.add_argument("--url-title", default=None,
                        help="Label for --url (ignored without --url)")

    p_watch = sub.add_parser("watch", help="Watch for notifications")
    p_watch.add_argument("--no-pushover", action="store_true", default=False,
                         help="Suppress all Pushover forwarding for this session")

    sub.add_parser("init", help="Initialize config")

    args = parser.parse_args()
    if args.command == "send":
        return cmd_send(args)
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "watch":
        return cmd_watch(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
