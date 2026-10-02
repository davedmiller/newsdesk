# Newsdesk

Unified notification hub for macOS. Every machine queues notifications locally with one CLI; an always-on hub gathers them, keeps one history, forwards the important ones to Pushover, and serves a web page you can read from any browser on the tailnet.

## How it fits together

```
sender (any machine)            hub (always-on Mac)                 you
────────────────────            ───────────────────                 ───
newsdesk send ─▶ queue.jsonl    newsdesk relay (launchd)
                  │               ├─ claims the hub's own queue
                  └─ push ──────▶ ├─ claims batches pushed to it
                     (ssh)        ├─ pulls machines it can reach by ssh
                                  ├─▶ history.jsonl ──▶ web page ──▶ 🌐 browser
                                  ├─▶ Pushover (priority ≥ threshold) ──▶ 📱
                                  └─▶ Healthchecks.io ping ──(dead-man)──▶ 📱
```

- **Send** never touches the network. It appends one JSON line to the local queue and returns.
- **Push** runs as a detached child of `send` on machines that have a `hub` configured. It ships the queue to the hub over ssh and deletes nothing the hub has not taken.
- **Relay** is the only consumer. It writes history before deleting anything, drops entries it has already seen (every entry carries an id), retries failed Pushover forwards for up to an hour, and pings Healthchecks.io every minute so a dead relay pages you through a path that does not depend on the relay.
- **The page** polls the relay every 3 seconds. Newest first, filter box, bell, silent entries hidden by default.

## Install on a sender

```bash
git clone https://github.com/davedmiller/newsdesk.git ~/Developer/newsdesk
cd ~/Developer/newsdesk
./scripts/setup.sh
source ~/.zshrc
```

Then point it at the hub in `~/.config/newsdesk/config.json`:

```json
{
  "queue_file": "~/.local/share/newsdesk/queue.jsonl",
  "history_file": "~/.local/share/newsdesk/history.jsonl",
  "remote_machines": [],
  "hub": {"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}
}
```

`host` is an alias in `~/.ssh/config` that works with `BatchMode=yes`. With a `hub` set, the local queue is never rotated: a backlog waits, unbounded, until the hub is reachable again.

## Install on the hub

Same clone and `setup.sh`, then:

1. **Config** (`~/.config/newsdesk/config.json`): no `hub`; set `pushover_min_priority` (2 = emergencies only) and `web_bind` to the machine's Tailscale address so the page is reachable from the tailnet and nothing else. `web_port` defaults to 5556.
2. **Keychain**, in a Terminal on the hub itself (the Keychain is not usable over ssh):
   ```bash
   security add-generic-password -a pushover -s newsdesk-app-token -w <APP_TOKEN>
   security add-generic-password -a pushover -s newsdesk-user-key -w <USER_KEY>
   security add-generic-password -a dave -s newsdesk-hc-url -w <HEALTHCHECKS_PING_URL>
   ```
   The Healthchecks URL is optional; without it the page shows `healthchecks off`.
3. **Check**: `newsdesk init` shows both Pushover ticks; `newsdesk relay --once --no-pushover` runs one cycle and prints its summary.
4. **Run it**: `scripts/install-launchd.sh` installs `com.dave.newsdesk-relay` as a LaunchAgent (KeepAlive, log at `~/.local/share/newsdesk/relay.log`). Re-run it after pulling new code, or `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay`.

The page is then at `http://<hub tailscale ip>:5556`.

## Usage

```bash
newsdesk send "Title" "Message"
newsdesk send "Deploy Done" "All tests passed" --priority 1 --project myapp
# --url adds a tappable link in the Pushover notification and on the page
newsdesk send "Backup degraded" "Ann TM stale, NAS 91%" --priority 1 \
  --url "http://100.70.51.21:5555/" --url-title "Open status page"

newsdesk relay                 # the hub loop; normally run by launchd
newsdesk relay --once          # one cycle, print the summary, exit
newsdesk relay --no-pushover   # relay without forwarding to the phone
newsdesk push                  # ship the local queue to the hub (send does this for you)
newsdesk init                  # create the config, check the Keychain
```

### Priority levels

| Priority | Page | Pushover |
|----------|------|----------|
| -2 silent | hidden unless "show silent" | never |
| -1 quiet | no icon | if ≥ threshold |
| 0 normal | ✅ | if ≥ threshold |
| 1 high | 🔔 | if ≥ threshold |
| 2 emergency | 🔔 | if ≥ threshold; re-alerts until acknowledged |

`pushover_min_priority` on the hub is the one forwarding switch (default `-1` = everything but silent). Priority `-2` is never forwarded.

## Pulled senders

A machine the hub can reach by ssh but that cannot reach the hub (for example a Raspberry Pi with no key to it) is listed in the hub's `remote_machines` instead of pushing:

```json
{
  "remote_machines": [
    {"name": "lake", "host": "lake-agent", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}
  ]
}
```

The relay pulls each one every 30 seconds, writes what it got to history, and only then tells the machine to delete it. `host` must work with `ssh -o BatchMode=yes` from the hub, so populate `known_hosts` once by hand.

## Claude Code integration

A global Stop hook sends a priority-0 "Turn complete" through newsdesk at the end of every turn, and a Notification hook sends permission prompts at priority 1:

```bash
# ~/.claude/hooks/newsdesk-notify.sh
"$HOME/bin/newsdesk" send "Claude: Permission" "$MESSAGE" --priority 1
```

With the hub's threshold at 2, these stay on the page and off the phone.

## Queue safety

- A sender's queue is moved aside (`queue.jsonl.processing`) before it is read, so a `send` racing a push or the relay never loses a line.
- Push ships `.processing` before anything newer and keeps it until the hub has the batch, however old it is.
- On the hub, a pushed batch lands as its own `queue.jsonl.in.<id>` file by an atomic rename, never appended to the live queue.
- The relay writes history before deleting any source, and drops entries whose id it has already accepted, so a replay after a crash or a lost acknowledgement adds nothing.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The web tests bind a local port, so they fail under a sandbox that blocks local binding.

## Requirements

- macOS (uses Keychain and launchd)
- Python 3.9 or newer, stdlib only
- ssh between senders and the hub
- Pushover account; Healthchecks.io account for the dead-man ping (optional)
