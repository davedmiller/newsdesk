# Newsdesk

Unified notification hub CLI for macOS. Centralizes notifications from multiple machines into a single terminal UI with optional Pushover relay.

## Features

- **Send** notifications from any project or script via CLI
- **Watch** a curses TUI that polls local and remote queues via SSH
- **Relay** notifications to Pushover for mobile alerts
- **Priority levels** from silent (-2) to emergency (2) with configurable display, bell, and relay behavior
- **Per-project toggles** for Pushover forwarding
- **Auto-detection** of project name (from git repo) and machine name (from hostname)
- **File-based queues** using JSONL — no server, no database
- **macOS Keychain** for Pushover credentials

## Install

```bash
git clone https://github.com/davedmiller/newsdesk.git ~/Developer/newsdesk
cd ~/Developer/newsdesk
./scripts/setup.sh
source ~/.zshrc
```

Then optionally store Pushover credentials for mobile relay:

```bash
security add-generic-password -a pushover -s newsdesk-app-token -w <APP_TOKEN>
security add-generic-password -a pushover -s newsdesk-user-key -w <USER_KEY>
```

## Usage

### Send a notification

```bash
newsdesk send "Title" "Message"
newsdesk send "Deploy Done" "All tests passed" --priority 1 --project myapp
```

### Watch for notifications

```bash
newsdesk watch              # local only, Pushover on if tokens exist
newsdesk watch --pushover   # force Pushover relay on
newsdesk watch --no-pushover
```

### Watcher keyboard shortcuts

| Key | Action |
|-----|--------|
| L | Latest view — live tail of incoming notifications |
| H | History view — browse processed notifications |
| C | Clear the latest view |
| S | Save history snapshot to a log file |
| V | Toggle visibility of silent (priority -2) messages |
| P | Pushover settings — toggle forwarding per project |
| ? | Help (two pages: shortcuts + priority reference) |
| Q | Quit |

### Priority levels

| Priority | Icon | Bell | Display | Pushover |
|----------|------|------|---------|----------|
| -2 silent | — | no | hidden (V to show) | never |
| -1 quiet | — | no | yes | yes |
| 0 normal | ✅ | no | yes | yes |
| 1 high | 🔔 | yes | yes | yes |
| 2 emergency | 🔔 | yes | yes | yes |

### Initialize config

```bash
newsdesk init
```

Creates `~/.local/share/newsdesk/config.json` and checks Keychain status.

## Remote polling

The watcher can poll queues on other machines via SSH. Add remote machines to your config:

```json
{
  "remote_machines": [
    {"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}
  ]
}
```

The host value should match an entry in your `~/.ssh/config`. Queue paths use `~` which expands to the remote user's home directory.

## Claude Code integration

Newsdesk integrates with Claude Code via a global Notification hook. When Claude needs permission or goes idle, the hook sends a notification through newsdesk:

```bash
# ~/.claude/hooks/newsdesk-notify.sh
"$HOME/bin/newsdesk" send "Claude: Permission" "$MESSAGE" --priority 1
```

See `~/.claude/CLAUDE.md` for cross-project setup instructions.

## Queue safety

- Rename-read-delete pattern prevents race conditions between senders and watcher
- Stale `.processing` files (> 1 day) are cleaned up automatically
- Fresh `.processing` files are recovered on restart (crash recovery)

## Tests

```bash
python -m pytest tests/ -q
```

## Requirements

- macOS (uses Keychain, curses)
- Python 3 (stdlib only)
- SSH access for remote polling
- Pushover account for mobile relay (optional)
