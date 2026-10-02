# ABOUTME: Unified notification hub CLI — send, push, relay, watch, and init subcommands.
# ABOUTME: Senders queue JSONL locally; the relay gathers every queue, forwards to Pushover, serves a web viewer.

import argparse
import curses
import fcntl
import glob
import http.server
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

# ---------------------------------------------------------------------------
# Constants (adjust as needed)
# ---------------------------------------------------------------------------
QUEUE_MAX_LINES = 200
QUEUE_ROTATE_TO = 100
HISTORY_MAX_ENTRIES = 5000  # the hub's history is the web viewer's whole reachable past
POLL_INTERVAL_S = 2
SSH_CONNECT_TIMEOUT = 2
SSH_COMMAND_TIMEOUT = 3
DEFAULT_PROJECT = "general"
STALE_PROCESSING_AGE = 86400  # 1 day in seconds
DISPLAY_FIELD_WIDTH = 15
LINK_MARKER = "\U0001f517"  # 🔗 — shown in the watch UI when an entry carries a url
# Pushover priority-2 (emergency) re-alerts until acknowledged and REQUIRES these.
PUSHOVER_EMERGENCY_RETRY_S = 60     # re-alert interval (Pushover minimum 30)
PUSHOVER_EMERGENCY_EXPIRE_S = 3600  # stop re-alerting after this long (Pushover max 10800)
# Relay (hub) cadences and bounds
REMOTE_POLL_INTERVAL_S = 30    # how often the relay pulls each remote machine
REMOTE_PULL_TIMEOUT_S = 15     # overall bound on one pull; a timeout loses nothing
HEARTBEAT_INTERVAL_S = 60      # state file + Healthchecks ping cadence
RELAY_STALE_AFTER_S = 180      # the page shows its red banner past this
PUSH_SSH_TIMEOUT_S = 20        # overall bound on one push's ssh
FORWARD_RETRY_INTERVAL_S = 30  # gap between attempts for a failed Pushover forward
FORWARD_RETRY_EXPIRE_S = 3600  # a failed forward older than this is dropped
INCOMING_TEMP_MAX_AGE_S = 86400  # orphaned push temp files older than this are removed
WEB_PORT = 5556
WEB_FEED_ENTRIES = 200         # default number of entries the feed returns
WEB_BIND_RETRY_S = 10          # gap between attempts to bind the web port

DEFAULT_CONFIG = {
    "queue_file": "~/.local/share/newsdesk/queue.jsonl",
    "history_file": "~/.local/share/newsdesk/history.jsonl",
    "remote_machines": [],
    "pushover_min_priority": -1,  # only forward priority >= this to Pushover (-2 always silent)
    "web_bind": "127.0.0.1",      # address the relay's web viewer binds
    "web_port": WEB_PORT,
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
    _atomic_write(history_path, "".join(json.dumps(entry) + "\n" for entry in combined))


def _atomic_write(path, text):
    """Replace a file's contents whole, so a concurrent reader never sees a partial file."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        f.write(text)
    os.replace(tmp_path, path)


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
# Queue claim / commit (the relay and push delete only after their work is safe)
# ---------------------------------------------------------------------------
def claim_queue(queue_path):
    """Move the queue aside as .processing and return its entries. Deletes nothing.

    A .processing file left by an earlier claim is returned as it stands, and the
    queue is left for the next claim, so an uncommitted batch is never overwritten.
    """
    processing_path = queue_path + ".processing"
    if not os.path.exists(processing_path):
        try:
            os.rename(queue_path, processing_path)
        except OSError:
            return []
    return parse_jsonl(processing_path)


def commit_queue(queue_path):
    """Delete the .processing file once its entries are safely stored elsewhere."""
    try:
        os.unlink(queue_path + ".processing")
    except FileNotFoundError:
        pass


def claim_spool(queue_path):
    """Read every batch a push has dropped beside the queue. Returns (entries, paths).

    The caller unlinks the paths once the entries are in history.
    """
    def arrival(path):
        try:
            return (os.path.getmtime(path), path)
        except OSError:
            return (0.0, path)

    # Oldest first: the names are random ids, so sorting by name would shuffle
    # batches that arrived within one cycle.
    entries, paths = [], []
    for path in sorted(glob.glob(glob.escape(queue_path) + ".in.*"), key=arrival):
        entries.extend(parse_jsonl(path))
        paths.append(path)
    return entries, paths


def remote_path(queue_file):
    """Rewrite a leading ~ as $HOME so it expands on the remote machine, not here."""
    return queue_file.replace("~", "$HOME", 1) if queue_file.startswith("~") else queue_file


# ---------------------------------------------------------------------------
# Push (a sender ships its queue to the hub)
# ---------------------------------------------------------------------------
def build_push_command(remote_queue, batch_id):
    """Shell command the hub runs to receive one batch on stdin.

    The batch is staged, then renamed into a uniquely named spool file. It is never
    appended to the hub's queue: the relay's rename could fall between an append's
    open and its write and lose the batch, whereas a rename is all-or-nothing.
    """
    return (
        f'f="{remote_queue}"; t="$f.incoming.{batch_id}"; '
        f'mkdir -p "$(dirname "$f")" && cat > "$t" && mv "$t" "$f.in.{batch_id}"'
    )


def ship_batch(hub, data):
    """Send one batch of queue bytes to the hub over ssh. Returns True on success."""
    cmd = build_push_command(remote_path(hub["queue_file"]), uuid.uuid4().hex)
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2",
                hub["host"],
                cmd,
            ],
            input=data, capture_output=True,
            timeout=PUSH_SSH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def push_queue(config):
    """Ship the local queue to the configured hub. Returns 0 when nothing is left to ship.

    Nothing is deleted until the hub has it: a failed batch stays in .processing,
    whatever its age, and the next push ships that file before anything newer.
    """
    hub = config.get("hub")
    if not hub:
        return 0
    queue_path = config["queue_file"]
    processing_path = queue_path + ".processing"
    os.makedirs(os.path.dirname(queue_path), exist_ok=True)
    with open(queue_path + ".push.lock", "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another push holds the lock and will drain the queue
        while True:
            if not os.path.exists(processing_path):
                try:
                    os.rename(queue_path, processing_path)
                except OSError:
                    return 0
            with open(processing_path, "rb") as f:
                data = f.read()
            if data and not ship_batch(hub, data):
                return 1
            os.unlink(processing_path)


# ---------------------------------------------------------------------------
# Remote pull (the hub reads a sender that cannot push)
# ---------------------------------------------------------------------------
def build_pull_command(remote_queue, batch_id):
    """Shell command a remote runs to hand over its queue. Deletes nothing.

    The queue is renamed to a uniquely named .out file, then every .out file is
    printed after a '#<path>' line. Files stay until build_ack_command removes them,
    so a pull whose output is lost is simply repeated.
    """
    return (
        f'f="{remote_queue}"; [ -f "$f" ] && mv "$f" "$f.out.{batch_id}"; '
        f'for o in "$f".out.*; do [ -f "$o" ] || continue; '
        f'printf "#%s\\n" "$o"; cat "$o"; echo; done; true'
    )


def build_ack_command(files):
    """Shell command that removes exactly the .out files a pull returned."""
    for path in files:
        if ".out." not in os.path.basename(path) or "\n" in path:
            raise ValueError(f"not a pulled queue file: {path!r}")
    return "rm -f -- " + " ".join(shlex.quote(path) for path in files)


def parse_pull_output(text):
    """Split a pull's output into (entries, file paths)."""
    entries, files = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            files.append(line[1:])
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries, files


def _remote_ssh(host, cmd):
    """Run a command on a remote machine. Returns the CompletedProcess, or None if unreachable."""
    try:
        return subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
                host,
                cmd,
            ],
            capture_output=True, text=True,
            timeout=REMOTE_PULL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def pull_remote_queue(host, queue_file):
    """Read a remote machine's queue. Returns (entries, reached, files).

    reached is True whenever the remote shell ran, even with nothing to read.
    files are the remote paths to pass to ack_remote_queue once the entries are stored.
    """
    result = _remote_ssh(host, build_pull_command(remote_path(queue_file), uuid.uuid4().hex))
    if result is None or result.returncode != 0:
        return [], False, []
    entries, files = parse_pull_output(result.stdout)
    return entries, True, files


def ack_remote_queue(host, files):
    """Remove pulled files from a remote machine. Best-effort: a lost ack means a repeat pull."""
    try:
        _remote_ssh(host, build_ack_command(files))
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Pushover forwarding
# ---------------------------------------------------------------------------
def forward_to_pushover(entry, app_token, user_key):
    """Forward a notification to Pushover via curl. Returns True if Pushover accepted it."""
    try:
        priority = entry.get("priority", 0)
        cmd = [
            "curl", "-s",
            "--form-string", f"token={app_token}",
            "--form-string", f"user={user_key}",
            "--form-string", f"title={entry['title']}",
            "--form-string", f"message={entry['message']}",
            "--form-string", f"priority={priority}",
        ]
        # Priority 2 is a Pushover "emergency": it re-alerts until acknowledged and
        # the API rejects the whole message unless retry/expire are supplied.
        if priority >= 2:
            cmd += [
                "--form-string", f"retry={PUSHOVER_EMERGENCY_RETRY_S}",
                "--form-string", f"expire={PUSHOVER_EMERGENCY_EXPIRE_S}",
            ]
        # Optional supplementary link → a tappable button in the notification.
        # url_title is meaningless to Pushover without url, so gate it on url.
        if entry.get("url"):
            cmd += ["--form-string", f"url={entry['url']}"]
            if entry.get("url_title"):
                cmd += ["--form-string", f"url_title={entry['url_title']}"]
        cmd.append("https://api.pushover.net/1/messages.json")
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        # curl exits 0 on an HTTP error too; Pushover's body says whether it took the message
        return result.returncode == 0 and b'"status":1' in (getattr(result, "stdout", b"") or b"")
    except Exception:
        return False


def read_keychain_token(service, account="pushover"):
    """Read a token from macOS Keychain. Returns None on failure."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
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
                      url=None, url_title=None, rotate=True):
    """Append a notification to the queue file. Creates parent dirs if needed.

    url/url_title are an optional supplementary link forwarded to Pushover as a
    tappable button. They are persisted only when set, so existing entries that
    carry no link keep their minimal shape.

    rotate=False leaves the queue to grow: a sender with a hub must not drop
    entries that are waiting for the hub to become reachable.
    """
    if project is None:
        project = DEFAULT_PROJECT

    entry = {
        "id": uuid.uuid4().hex,  # lets the hub drop a batch that was delivered twice
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

    if rotate:
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
# Relay (headless hub: consume every queue, keep history, forward to Pushover)
# ---------------------------------------------------------------------------
HEALTHCHECKS_KEYCHAIN_SERVICE = "newsdesk-hc-url"
HEALTHCHECKS_KEYCHAIN_ACCOUNT = "dave"


def _data_dir(config):
    """Directory holding the queue, and beside it the relay's lock and state files."""
    return os.path.dirname(config["queue_file"])


def acquire_relay_lock(data_dir):
    """Take the single-instance lock. Returns the open lock file, or None if a relay holds it."""
    os.makedirs(data_dir, exist_ok=True)
    lock_file = open(os.path.join(data_dir, "relay.lock"), "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def _read_relay_secrets(rt):
    """Read whichever Keychain values the relay still lacks. Safe to call repeatedly."""
    if not rt["no_pushover"]:
        if not rt["app_token"]:
            rt["app_token"] = read_keychain_token("newsdesk-app-token")
        if not rt["user_key"]:
            rt["user_key"] = read_keychain_token("newsdesk-user-key")
    if not rt["hc_url"]:
        rt["hc_url"] = read_keychain_token(
            HEALTHCHECKS_KEYCHAIN_SERVICE, account=HEALTHCHECKS_KEYCHAIN_ACCOUNT)


def new_relay(config, no_pushover=False, now=None):
    """Build the relay's runtime state. Seeds the seen-id set from history."""
    if now is None:
        now = time.time()
    rt = {
        "config": config,
        "no_pushover": bool(no_pushover),
        "app_token": None,
        "user_key": None,
        "hc_url": None,
        "seen": set(),             # ids already accepted into history
        "retry": [],               # failed forwards: {"entry", "first", "next"}
        "started": now,
        "last_cycle": now,
        "last_heartbeat": None,
        "last_remote_poll": None,
        "remotes": {m["host"]: None for m in config.get("remote_machines") or []},
        "remotes_reached": 0,      # how many answered at the last poll
        "forwarded_total": 0,
        "forward_failed_total": 0,
        "web_server": None,
    }
    for entry in parse_jsonl(config["history_file"]):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            rt["seen"].add(entry["id"])
    _read_relay_secrets(rt)
    rt["state"] = build_state(rt)
    return rt


def build_state(rt):
    """The relay's public state: served to the web page and written to relay.state."""
    return {
        "last_cycle_ts": rt["last_cycle"],
        "started_ts": rt["started"],
        "pushover": pushover_status_label(
            rt["no_pushover"], rt["app_token"], rt["user_key"],
            rt["config"].get("pushover_min_priority", -1)),
        "healthchecks": bool(rt["hc_url"]),
        "remotes": dict(rt["remotes"]),
        "forwarded_total": rt["forwarded_total"],
        "forward_failed_total": rt["forward_failed_total"],
        "retrying": len(rt["retry"]),
    }


def ping_healthchecks(url):
    """Ping Healthchecks from a detached curl, so a slow network never stalls the relay."""
    try:
        subprocess.Popen(
            ["curl", "-fsS", "-m", "10", url],
            start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _try_forward(rt, entry):
    """Forward one entry; any failure, including an exception, is just False."""
    try:
        return bool(forward_to_pushover(entry, rt["app_token"], rt["user_key"]))
    except Exception:
        return False


def _remove_stale_incoming(queue_path, now):
    """Delete temp files left by a push whose remote shell died before its rename."""
    for path in glob.glob(glob.escape(queue_path) + ".incoming.*"):
        try:
            if now - os.path.getmtime(path) > INCOMING_TEMP_MAX_AGE_S:
                os.unlink(path)
        except OSError:
            pass


def relay_cycle(rt, now, once=False):
    """Run one relay cycle at time `now`. Returns a summary dict of what it did.

    Order matters: entries are written to history before anything they came from is
    deleted, so a crash at any point replays them into the seen-id check, not into a loss.
    """
    config = rt["config"]
    queue_path = config["queue_file"]
    summary = {"consumed": 0, "dupes": 0, "forwarded": 0, "forward_failed": 0,
               "expired": 0, "retrying": 0, "remotes_reached": 0, "remotes_total": 0,
               "pinged": False}

    # Claim: local queue, batches pushed by other machines, and (on its cadence) remote pulls
    candidates = claim_queue(queue_path)
    spool_entries, spool_paths = claim_spool(queue_path)
    candidates.extend(spool_entries)

    remotes = config.get("remote_machines") or []
    acks = []
    if remotes and (rt["last_remote_poll"] is None
                    or now - rt["last_remote_poll"] >= REMOTE_POLL_INTERVAL_S):
        rt["last_remote_poll"] = now
        rt["remotes_reached"] = 0
        for machine in remotes:
            entries, reached, files = pull_remote_queue(machine["host"], machine["queue_file"])
            if not reached:
                continue
            rt["remotes"][machine["host"]] = now
            rt["remotes_reached"] += 1
            candidates.extend(entries)
            if files:
                acks.append((machine["host"], files))
    summary["remotes_total"] = len(remotes)
    summary["remotes_reached"] = rt["remotes_reached"]

    # Accept each id once, however many times it was delivered
    accepted, batch_ids = [], set()
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            if entry_id in rt["seen"] or entry_id in batch_ids:
                summary["dupes"] += 1
                continue
            batch_ids.add(entry_id)
        accepted.append(entry)

    # Store, then release the sources
    append_to_history(config["history_file"], accepted)
    rt["seen"].update(batch_ids)
    commit_queue(queue_path)
    for path in spool_paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    for host, files in acks:
        ack_remote_queue(host, files)
    summary["consumed"] = len(accepted)

    # Forward: earlier failures that are due, then this cycle's entries
    can_forward = bool(not rt["no_pushover"] and rt["app_token"] and rt["user_key"])
    min_priority = config.get("pushover_min_priority", -1)
    still_waiting = []
    for item in rt["retry"]:
        if now - item["first"] > FORWARD_RETRY_EXPIRE_S:
            summary["expired"] += 1
            continue
        if can_forward and now >= item["next"]:
            if _try_forward(rt, item["entry"]):
                summary["forwarded"] += 1
                continue
            item["next"] = now + FORWARD_RETRY_INTERVAL_S
        still_waiting.append(item)
    rt["retry"] = still_waiting

    for entry in accepted:
        try:
            wanted = should_forward_pushover(entry, min_priority)
        except Exception:
            wanted = False  # e.g. a non-integer priority
        if not (can_forward and wanted):
            continue
        if _try_forward(rt, entry):
            summary["forwarded"] += 1
        else:
            summary["forward_failed"] += 1
            rt["retry"].append(
                {"entry": entry, "first": now, "next": now + FORWARD_RETRY_INTERVAL_S})

    summary["retrying"] = len(rt["retry"])
    rt["forwarded_total"] += summary["forwarded"]
    rt["forward_failed_total"] += summary["forward_failed"]
    rt["last_cycle"] = now

    # Heartbeat: last, so a cycle that raised above never reports itself healthy
    if once or rt["last_heartbeat"] is None or now - rt["last_heartbeat"] >= HEARTBEAT_INTERVAL_S:
        rt["last_heartbeat"] = now
        _read_relay_secrets(rt)
        _remove_stale_incoming(queue_path, now)
        rt["state"] = build_state(rt)
        _atomic_write(os.path.join(_data_dir(config), "relay.state"), json.dumps(rt["state"]) + "\n")
        # No ping until the relay has stayed up a full interval: a crash loop must go red
        if rt["hc_url"] and not once and now - rt["started"] >= HEARTBEAT_INTERVAL_S:
            tokens_missing = not rt["no_pushover"] and not (rt["app_token"] and rt["user_key"])
            ping_healthchecks(rt["hc_url"] + ("/fail" if tokens_missing else ""))
            summary["pinged"] = True
    else:
        rt["state"] = build_state(rt)

    return summary


def _relay_log_line(summary):
    """One log line for a cycle."""
    return (
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} consumed={summary['consumed']} "
        f"dupes={summary['dupes']} forwarded={summary['forwarded']} "
        f"forward_failed={summary['forward_failed']} retrying={summary['retrying']} "
        f"expired={summary['expired']} "
        f"remotes={summary['remotes_reached']}/{summary['remotes_total']}"
    )


# ---------------------------------------------------------------------------
# Web viewer (served by the relay)
# ---------------------------------------------------------------------------
WEB_PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")


def build_feed(rt, limit):
    """What the page polls: relay state plus the newest `limit` history entries, oldest first."""
    entries = parse_jsonl(rt["config"]["history_file"])[-limit:]
    return {
        "state": rt["state"],
        "entries": entries,
        "now": time.time(),
        "stale_after_s": RELAY_STALE_AFTER_S,
    }


def make_web_server(rt, bind, port):
    """Build the viewer's HTTP server: the page, the feed, and nothing else."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                try:
                    with open(WEB_PAGE_PATH, "rb") as f:
                        body = f.read()
                except OSError:
                    self._send(500, "text/plain; charset=utf-8", b"page file missing\n")
                    return
                self._send(200, "text/html; charset=utf-8", body)
            elif url.path == "/api/feed":
                limit = WEB_FEED_ENTRIES
                try:
                    limit = int(urllib.parse.parse_qs(url.query)["limit"][0])
                except (KeyError, IndexError, ValueError):
                    pass
                limit = max(1, min(limit, HISTORY_MAX_ENTRIES))
                body = json.dumps(build_feed(rt, limit)).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def log_message(self, format, *args):
            pass  # the relay log is for relay cycles, not page polls

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((bind, port), Handler)


def web_server_loop(rt, bind, port, stop, retry_s=WEB_BIND_RETRY_S):
    """Serve the viewer until `stop` is set, retrying the bind while the address is unavailable."""
    reported = False
    while not stop.is_set():
        try:
            server = make_web_server(rt, bind, port)
        except OSError as e:
            if not reported:
                print(f"web: cannot bind {bind}:{port} ({e}); retrying", flush=True)
                reported = True
            stop.wait(retry_s)
            continue
        reported = False
        rt["web_server"] = server
        try:
            server.serve_forever()
        finally:
            server.server_close()
            rt["web_server"] = None


def cmd_relay(args):
    """Handle the 'relay' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))
    lock_file = acquire_relay_lock(_data_dir(config))
    if lock_file is None:
        print("newsdesk relay: another relay is already running", file=sys.stderr)
        return 1
    try:
        rt = new_relay(config, no_pushover=args.no_pushover)
        if args.once:
            print(_relay_log_line(relay_cycle(rt, time.time(), once=True)), flush=True)
            return 0

        stop = threading.Event()
        threading.Thread(
            target=web_server_loop,
            args=(rt, config.get("web_bind", "127.0.0.1"), config.get("web_port", WEB_PORT), stop),
            daemon=True,
        ).start()
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} relay started "
              f"pushover={rt['state']['pushover']} healthchecks={rt['state']['healthchecks']}",
              flush=True)
        while True:
            try:
                summary = relay_cycle(rt, time.time())
                if any(summary[k] for k in ("consumed", "dupes", "forwarded",
                                            "forward_failed", "expired")):
                    print(_relay_log_line(summary), flush=True)
            except Exception as e:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} cycle error: {e!r}", flush=True)
            time.sleep(POLL_INTERVAL_S)
    finally:
        lock_file.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_send(args):
    """Handle the 'send' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))
    project = args.project if args.project else detect_project()
    hub = config.get("hub")
    try:
        send_notification(
            config["queue_file"],
            args.title,
            args.message,
            args.priority,
            project,
            url=args.url,
            url_title=args.url_title,
            rotate=not hub,
        )
    except Exception:
        pass  # best-effort
    if hub:
        # Ship to the hub from a detached child, so send never waits on the network
        # and the hook that ran it can exit. Addressed by path: hooks have no PATH setup.
        try:
            subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "push"],
                start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass  # the entry is queued; the next send's push ships it
    return 0


def cmd_push(args):
    """Handle the 'push' subcommand."""
    config = load_config(os.path.expanduser("~/.config/newsdesk/config.json"))
    return push_queue(config)


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

    p_relay = sub.add_parser(
        "relay", help="Headless hub: consume queues, keep history, forward to Pushover, serve the web viewer")
    p_relay.add_argument("--once", action="store_true", default=False,
                         help="Run one cycle, print its summary, and exit")
    p_relay.add_argument("--no-pushover", action="store_true", default=False,
                         help="Suppress all Pushover forwarding for this run")

    sub.add_parser("push", help="Ship the local queue to the configured hub (run by send)")

    sub.add_parser("init", help="Initialize config")

    args = parser.parse_args()
    if args.command == "send":
        return cmd_send(args)
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "watch":
        return cmd_watch(args)
    elif args.command == "relay":
        return cmd_relay(args)
    elif args.command == "push":
        return cmd_push(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
