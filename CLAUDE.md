# Newsdesk

Unified notification hub CLI for macOS (Tahoe only). Replaces scattered Pushover scripts with a single tool.

## Architecture

- **Single-file CLI**: `newsdesk.py` contains all logic
- **Shell wrapper**: `newsdesk` resolves symlinks and execs `newsdesk.py`
- **No dependencies beyond stdlib** (except `curses` for watch UI)
- **File-based queues**: JSONL format, no server or database
- **macOS Keychain** for Pushover tokens (not env vars or config)

## Subcommands

- `newsdesk send "Title" "Message" --priority 0 --project name` — append to local JSONL queue
- `newsdesk watch` — curses TUI polling local + remote queues, optional Pushover forwarding
- `newsdesk init` — create config, check Keychain status

## Key Files

| File | Purpose |
|------|---------|
| `newsdesk.py` | All CLI logic, constants, queue/history functions |
| `newsdesk` | Shell wrapper (symlink target) |
| `scripts/setup.sh` | Per-machine setup: symlink, PATH, init |
| `tests/test_newsdesk.py` | pytest unit tests (TDD) |
| `docs/newsdesk-plan.md` | Full project plan with IDs |

## Running Tests

```
python -m pytest tests/ -q
```

## Constants

All magic numbers are named constants at the top of `newsdesk.py`:
`QUEUE_MAX_LINES`, `QUEUE_ROTATE_TO`, `HISTORY_MAX_ENTRIES`, `POLL_INTERVAL_S`, `SSH_CONNECT_TIMEOUT`, `SSH_COMMAND_TIMEOUT`, `DEFAULT_PROJECT`, `STALE_PROCESSING_AGE`

## Data Files

Default location: `~/.local/share/newsdesk/`
- `queue.jsonl` — pending notifications (consumed by watcher)
- `history.jsonl` — processed notifications (capped at HISTORY_MAX_ENTRIES)
- `config.json` — machine-specific config
- `newsdesk-*.log` — saved snapshots from watch UI (S key)

## Queue Safety

- Rename-read-delete pattern prevents race conditions between senders and watcher
- Stale `.processing` files (> 1 day) are deleted without reading
- Fresh `.processing` files are recovered (crash recovery)

## Setup on a New Machine

```
git clone <repo> ~/Developer/newsdesk
cd ~/Developer/newsdesk
./scripts/setup.sh
source ~/.zshrc
```

Then store Pushover credentials in Keychain:
```
security add-generic-password -a pushover -s newsdesk-app-token -w <TOKEN>
security add-generic-password -a pushover -s newsdesk-user-key -w <KEY>
```
