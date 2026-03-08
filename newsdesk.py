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

DEFAULT_CONFIG = {
    "queue_file": "~/.local/share/newsdesk/queue.jsonl",
    "history_file": "~/.local/share/newsdesk/history.jsonl",
    "remote_machines": [],
    "pushover_enabled": True,
    "pushover_projects_default": True,
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


def should_bell(priority):
    """Return True if this priority should ring the terminal bell."""
    return priority >= 1


def should_display(entry, show_silent=False):
    """Return True if this entry should be shown. Priority -2 hidden unless show_silent."""
    if entry.get("priority", 0) == -2:
        return show_silent
    return True


def should_forward_pushover(entry):
    """Return True if this entry should be forwarded to Pushover. Priority -2 is never forwarded."""
    return entry.get("priority", 0) != -2


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
# Pushover state
# ---------------------------------------------------------------------------
class PushoverState:
    """Tracks per-project Pushover forwarding toggles."""

    def __init__(self, all_enabled=True, projects_default=True):
        self.all_enabled = all_enabled
        self.projects_default = projects_default
        self._projects = {}  # project_name -> bool

    def ensure_project(self, project):
        """Register a project if not already known."""
        if project not in self._projects:
            self._projects[project] = self.projects_default

    def toggle_project(self, project):
        """Toggle a project's forwarding state."""
        self.ensure_project(project)
        self._projects[project] = not self._projects[project]

    def toggle_all(self):
        """Toggle the ALL master switch."""
        self.all_enabled = not self.all_enabled

    def should_forward(self, project):
        """Return True if a notification for this project should be forwarded."""
        if not self.all_enabled:
            return False
        self.ensure_project(project)
        return self._projects[project]

    def known_projects(self):
        """Return list of (project_name, enabled) tuples."""
        return sorted(self._projects.items())

    def status_line(self):
        """Return a status string for the header."""
        parts = [f"ALL {'✓' if self.all_enabled else '✗'}"]
        for name, enabled in self.known_projects():
            parts.append(f"{name} {'✓' if enabled else '✗'}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Pushover forwarding
# ---------------------------------------------------------------------------
def forward_to_pushover(entry, app_token, user_key):
    """Forward a notification to Pushover via curl. Best-effort."""
    try:
        subprocess.run(
            [
                "curl", "-s",
                "--form-string", f"token={app_token}",
                "--form-string", f"user={user_key}",
                "--form-string", f"title={entry['title']}",
                "--form-string", f"message={entry['message']}",
                "--form-string", f"priority={entry.get('priority', 0)}",
                "https://api.pushover.net/1/messages.json",
            ],
            capture_output=True, timeout=10,
        )
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
def send_notification(queue_path, title, message, priority, project=None):
    """Append a notification to the queue file. Creates parent dirs if needed."""
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
def cmd_watch_curses(stdscr, config, pushover_override):
    """Main watch loop inside curses."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(POLL_INTERVAL_S * 1000))

    pushover_enabled = config["pushover_enabled"]
    if pushover_override is not None:
        pushover_enabled = pushover_override

    po_state = PushoverState(
        all_enabled=pushover_enabled,
        projects_default=config["pushover_projects_default"],
    )

    # Read Pushover tokens if enabled
    app_token = user_key = None
    if pushover_enabled:
        app_token = read_keychain_token("pcm-app-token")
        user_key = read_keychain_token("pcm-user-key")
        if not app_token or not user_key:
            pushover_enabled = False

    display_lines = []
    mode = "latest"  # latest | history | pushover
    history_offset = 0
    show_silent = False     # whether to display priority -2 entries
    status_msg = ""         # transient message shown on status line
    status_msg_until = 0    # timestamp when status_msg expires
    poll_blink = False      # toggles each loop for activity indicator

    while True:
        # Poll queues
        if mode != "pushover":
            new_entries = consume_local_queue(config["queue_file"])
            for machine in config["remote_machines"]:
                new_entries.extend(
                    consume_remote_queue(machine["host"], machine["queue_file"])
                )

            if new_entries:
                append_to_history(config["history_file"], new_entries)
                for entry in new_entries:
                    po_state.ensure_project(entry.get("project", DEFAULT_PROJECT))
                    if should_display(entry, show_silent):
                        display_lines.append(format_entry(entry))
                        if should_bell(entry.get("priority", 0)):
                            curses.beep()
                    if should_forward_pushover(entry) and po_state.should_forward(entry.get("project", DEFAULT_PROJECT)):
                        if app_token and user_key:
                            forward_to_pushover(entry, app_token, user_key)

        # Clear expired status message
        if status_msg and time.time() > status_msg_until:
            status_msg = ""

        # Draw
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Header
        silent_indicator = " [silent ON]" if show_silent else ""
        mode_label = {"latest": "LATEST", "history": "HISTORY", "pushover": "PUSHOVER", "help": "HELP"}.get(mode, mode.upper())
        header = f"newsdesk [{mode_label}] \u2014 (L)atest (H)istory (C)lear (S)ave (P)ushover si(V)lent (Q)uit (?)help{silent_indicator}"
        n_remote = len(config["remote_machines"])
        poll_info = f"polling local + {n_remote} remote" if n_remote else "polling local"
        poll_blink = not poll_blink
        blink_char = "\u25cf" if poll_blink else " "
        if status_msg:
            status = status_msg
        else:
            status = f"{blink_char} Pushover: {po_state.status_line()}  \u23f3 {poll_info}"

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
            help_lines = [
                "Keyboard shortcuts:",
                "",
                "  L   Latest view — live tail of incoming notifications",
                "  H   History view — browse all processed notifications",
                "  C   Clear the latest view",
                "  S   Save history snapshot to a .log file",
                "  V   Toggle visibility of silent (priority -2) messages",
                "  P   Pushover settings — toggle forwarding per project",
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
                "Press Esc to return.",
            ]
            for i, line in enumerate(help_lines):
                if i >= content_height:
                    break
                try:
                    padded = line.ljust(width - 1)[:width - 1]
                    stdscr.addnstr(content_start + i, 0, padded, width - 1)
                except curses.error:
                    pass

        elif mode == "pushover":
            try:
                stdscr.addnstr(content_start, 0, "Pushover forwarding:", width - 1)
                stdscr.addnstr(content_start + 1, 0,
                               f"  [A] ALL {'✓' if po_state.all_enabled else '✗'}", width - 1)
                for idx, (name, enabled) in enumerate(po_state.known_projects()):
                    label = f"  [{idx + 1}] {name} {'✓' if enabled else '✗'}"
                    stdscr.addnstr(content_start + 2 + idx, 0, label, width - 1)
                prompt_row = content_start + 2 + len(po_state.known_projects())
                stdscr.addnstr(prompt_row, 0,
                               "Press number to toggle, A for ALL, Esc to close", width - 1)
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
        elif mode == "pushover":
            if ch == 27:  # Esc
                mode = "latest"
            elif ch in (ord("a"), ord("A")):
                po_state.toggle_all()
            elif ord("1") <= ch <= ord("9"):
                idx = ch - ord("1")
                projects = po_state.known_projects()
                if idx < len(projects):
                    po_state.toggle_project(projects[idx][0])
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
            elif ch in (ord("p"), ord("P")):
                mode = "pushover"
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
        )
    except Exception:
        pass  # best-effort
    return 0


def cmd_watch(args):
    """Handle the 'watch' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))

    pushover_override = None
    if args.pushover:
        pushover_override = True
    elif args.no_pushover:
        pushover_override = False

    curses.wrapper(cmd_watch_curses, config, pushover_override)
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

    for key_name in ("pcm-app-token", "pcm-user-key"):
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

    p_watch = sub.add_parser("watch", help="Watch for notifications")
    p_watch.add_argument("--pushover", action="store_true", default=False)
    p_watch.add_argument("--no-pushover", action="store_true", default=False)

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
