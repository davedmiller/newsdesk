# Newsdesk

Unified notification hub for macOS. Senders queue locally with `newsdesk send`; one always-on hub (`newsdesk relay`, on micro-mac-mini) gathers every queue, forwards to Pushover, and serves a web page.

## Architecture

- **Single-file CLI**: `newsdesk.py` contains all logic; `web/index.html` is the page it serves
- **Shell wrapper**: `newsdesk` resolves symlinks and execs `newsdesk.py`
- **No dependencies beyond stdlib**; must run on Python 3.9 (the hub's `/usr/bin/python3`)
- **File-based queues**: JSONL, no database. The relay is the only consumer
- **Direction follows trust**: micro-m4 pushes to the hub (spawned by each `send`); machines the hub can ssh into are pulled
- **macOS Keychain** for Pushover tokens and the Healthchecks URL (not env vars or config)

## Subcommands

- `newsdesk send "Title" "Message" --priority 0 --project name` — append to the local queue; with a `hub` configured, spawn a detached `push`
- `newsdesk push` — ship the local queue to the hub over ssh (run by `send`, never scheduled)
- `newsdesk relay [--once] [--no-pushover]` — the hub loop: claim queues, dedupe by id, history, Pushover with retry, Healthchecks ping, web server
- `newsdesk init` — create config, check Keychain status

## Key Files

| File | Purpose |
|------|---------|
| `newsdesk.py` | All CLI logic, constants, queue/history/relay/web functions |
| `web/index.html` | The viewer page (vanilla JS, polls `/api/feed`) |
| `launchd/com.dave.newsdesk-relay.plist` | LaunchAgent template for the hub (`__HOME__` substituted) |
| `scripts/install-launchd.sh` | Installs/reloads the relay agent on the hub |
| `scripts/setup.sh` | Per-machine setup: symlink, PATH, init |
| `tests/test_newsdesk.py` | send, config, parsing, forwarding tests |
| `tests/test_relay.py` | relay cycle, push, pull, web tests |
| `docs/mini-relay-plan.md` | The hub design, decisions D1–D19, deployment record |

## Running Tests

```
.venv/bin/python -m pytest tests/ -q
```

The web tests bind a local port; run outside a sandbox that blocks local binding.

## Constants

All magic numbers are named constants at the top of `newsdesk.py`: queue rotation, `HISTORY_MAX_ENTRIES`, `POLL_INTERVAL_S`, ssh timeouts, the relay cadences (`REMOTE_POLL_INTERVAL_S`, `HEARTBEAT_INTERVAL_S`, `FORWARD_RETRY_*`), `WEB_PORT`, `WEB_FEED_ENTRIES`.

## Data Files

Config: `~/.config/newsdesk/config.json`. Data: `~/.local/share/newsdesk/`
- `queue.jsonl` — pending notifications; `queue.jsonl.processing` while claimed
- `queue.jsonl.in.<id>` — a batch pushed to the hub, waiting for the relay
- `queue.jsonl.push.lock` — one push at a time on a sender
- `history.jsonl` — what the hub accepted (capped at `HISTORY_MAX_ENTRIES`)
- `relay.state`, `relay.lock`, `relay.log` — hub only

## Queue Safety

- Claim (rename to `.processing`) → write history → commit (delete). Nothing is deleted before it is stored
- Push keeps `.processing` through any number of failures, with no age limit; no rotation on a machine with a `hub`
- Every entry carries an `id`; the relay drops ids it has already accepted, so retries and replays never duplicate

## Deploying a change to the hub

`git pull` on the mini, then `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay`. The Keychain on the mini is not usable over ssh: token changes need a Terminal there.
