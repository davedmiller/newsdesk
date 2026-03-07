# ABOUTME: Unified notification hub CLI — send, watch, and init subcommands.
# ABOUTME: Polls JSONL queues (local + remote via SSH), displays in terminal, forwards to Pushover.

import argparse
import json
import os
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

DEFAULT_CONFIG = {
    "queue_file": "~/.local/share/newsdesk/queue.jsonl",
    "history_file": "~/.local/share/newsdesk/history.jsonl",
    "remote_machines": [],
    "pushover_enabled": True,
    "pushover_projects_default": True,
}

PRIORITY_ICONS = {
    -2: None,   # silent — skipped entirely
    -1: "",     # quiet — no icon
    0: "\u2705",  # ✅
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

    # Expand tildes in path fields
    for key in ("queue_file", "history_file"):
        config[key] = os.path.expanduser(config[key])

    # Expand tildes in remote machine queue_file paths
    for machine in config.get("remote_machines", []):
        if "queue_file" in machine:
            machine["queue_file"] = os.path.expanduser(machine["queue_file"])

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
# CLI
# ---------------------------------------------------------------------------
def cmd_send(args):
    """Handle the 'send' subcommand."""
    config = load_config(
        os.path.expanduser("~/.config/newsdesk/config.json")
    )
    try:
        send_notification(
            config["queue_file"],
            args.title,
            args.message,
            args.priority,
            args.project,
        )
    except Exception:
        pass  # best-effort
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

    # Check Keychain for Pushover tokens
    import subprocess

    for key_name in ("pcm-app-token", "pcm-user-key"):
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "pushover", "-s", key_name, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"  Keychain: {key_name} ✓")
            else:
                print(f"  Keychain: {key_name} ✗ (not found)")
        except Exception:
            print(f"  Keychain: {key_name} ✗ (error)")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="newsdesk", description="Unified notification hub")
    sub = parser.add_subparsers(dest="command")

    # send
    p_send = sub.add_parser("send", help="Send a notification")
    p_send.add_argument("title", help="Notification title")
    p_send.add_argument("message", help="Notification message")
    p_send.add_argument("--priority", type=int, default=0, help="Priority (-2 to 2)")
    p_send.add_argument("--project", default=None, help="Project tag")

    # watch
    p_watch = sub.add_parser("watch", help="Watch for notifications")
    p_watch.add_argument("--pushover", action="store_true", default=None)
    p_watch.add_argument("--no-pushover", action="store_true")

    # init
    sub.add_parser("init", help="Initialize config")

    args = parser.parse_args()
    if args.command == "send":
        return cmd_send(args)
    elif args.command == "init":
        return cmd_init(args)
    elif args.command == "watch":
        print("Watch not yet implemented")
        return 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
