*Last updated: 2026-10-02 09:25 MDT*

# Newsdesk — Hub-on-the-Mini Relay + Web Viewer

**Status: DRAFT for study. Nothing built.** Phase 1 (relay, push, web viewer) is the work to do now. Phase 2 (pull from the Lewiston boxes) is planned here in full and **deferred** — no Pi-side work until Dave says so. Decisions in §9 are open unless marked otherwise.

## 1. Problem

Today the Pushover relay is the interactive `newsdesk watch` TUI on micro-m4 — `forward_to_pushover` has one call site, inside `cmd_watch_curses`. Nothing reaches a phone unless that iTerm window is open. During the Sept 2026 Lewiston absence the alerting path for two Michigan houses was one terminal window for fifteen days (OpenBrain #42). The window survives neither a reboot nor a closed lid, and micro-m4 is the machine that reboots, sleeps, and travels.

A second, smaller problem: the only way to *see* notification history is a curses TUI in a terminal on a Mac. From a phone, or from any machine without newsdesk installed, there is no view at all.

Four things change together:

1. The relay lives on the always-on machine (micro-mac-mini), launchd-managed, not a TUI.
2. Senders get entries to the mini in whichever direction the existing trust already runs.
3. The viewer is a web page served by the relay, readable from any browser on the tailnet. The curses TUI is removed.
4. The relay is observed by something that does not depend on the relay working.

## 2. Design

### 2.1 Roles per machine

| ID | Machine | Role | Runs |
|----|---------|------|------|
| M1 | micro-mac-mini (`davidmiller`) | **Hub.** Sole consumer of every queue; sole Pushover forwarder; owns the unified history; serves the viewer | `newsdesk relay` under launchd (KeepAlive), which also hosts the web server |
| M2 | micro-m4 (`dave`) | **Pushing sender.** Writes locally, ships its queue to the hub | `newsdesk send` (unchanged callers) → spawns a detached `newsdesk push`. No launchd on this machine |
| M3 | Lewiston agent boxes ×3 (`tag:agent`) — **Phase 2** | **Pulled sender.** Writes locally, nothing else | `newsdesk send` only (hvac plan W7). No daemon, no key, no outbound path |
| M4 | Any browser on the tailnet — laptop, phone | **Viewer.** Reads the hub's history live | `https://micro-mac-mini.<tailnet>.ts.net` (or `http://100.70.51.21:5556`) |

### 2.2 Data flow

```
micro-m4                          micro-mac-mini                          phone / any browser
────────                          ──────────────                          ───────────────────
send ──▶ queue.jsonl
           │ push (ssh mini, stage+append, under flock)
           └──────────────────▶ queue.jsonl ──┐
                                              │
agent box ×3  (Phase 2)                       ▼
──────────                              relay (launchd)
send ──▶ queue.jsonl ◀── ssh pull ──── every 30 s
                                              │
                                              ├─▶ history.jsonl
                                              ├─▶ web server thread ── SSE ──▶ 🌐 viewer page
                                              ├─▶ Pushover (≥ threshold) ─────▶ 📱
                                              ├─▶ relay.state
                                              └─▶ Healthchecks.io ping (every 60 s)
                                                          │
                                               dead-man fires → Pushover ────▶ 📱
```

### 2.3 Direction per sender — the decision and why

| ID | Sender | Direction | Reason | Phase |
|----|--------|-----------|--------|-------|
| P1 | Agent boxes → mini | **Mini pulls** | Matches hvac #821: a pulled box has no outbound path into the tailnet, holds no key to the mini, keeps no delivery state, runs no push daemon. The grant `autogroup:member → tag:agent` (all ports) plus the Tailscale SSH `accept` rule already lets the mini in. Cost: one `remote_machines` entry per box. | **2** |
| P2 | micro-m4 → mini | **micro-m4 pushes** | The mini cannot reach micro-m4 (no Remote Login — deliberate, #30), and micro-m4 is the intermittent machine, so pulling from it would mostly hit a sleeping laptop. micro-m4 → mini SSH already exists. | 1 |
| P3 | Mini's own jobs → mini | Local | Already the case. | 1 |

Dave's original framing — "nobody's being reached into" — is not a property worth buying for the boxes; #821 found the opposite is *better* for them. It *is* the right property for micro-m4, for the reason in P2. So direction follows existing trust, per sender, rather than being uniform.

### 2.4 Invariants preserved

| ID | Invariant | How |
|----|-----------|-----|
| N1 | `send` has zero network dependency (#39) | Local append first, always. The push is a *detached* child; `send` returns before it connects. |
| N2 | No Pushover secret leaves the hub | Keychain read only in `relay`, only on the mini. `push` carries entries, never tokens. |
| N3 | Queues are durable through outages | Unchanged rename-read-delete; push retains `.processing` on failure and retries. |
| N4 | Each entry is consumed exactly once | There is exactly one consumer (the relay). Nothing else touches a queue. |
| N5 | stdlib only | `subprocess` for ssh and curl; `http.server` + `threading` + `queue` for the viewer. No framework, no build step. |
| N6 | The mini's listening surface stays deliberate (#101 audit) | Web server binds loopback; `tailscale serve` exposes it tailnet-only over HTTPS (D9). Fallback binds the tailnet IP, never `0.0.0.0`. |

### 2.5 Phasing

| Phase | Scope | Code | Config / deploy |
|-------|-------|------|-----------------|
| **1 — now** | Relay on the mini; push from micro-m4; web viewer; Healthchecks; curses TUI removed | C1–C10 | S1–S12 |
| **2 — deferred** | Pull from the three boxes | **None.** `consume_remote_queue` exists today and the relay's remote cadence (C1) ships in Phase 1 with `remote_machines = []`, inert and tested (T2) | Q1–Q5 |

Phase 2 is config plus verification. Nothing in Phase 1 has to be revisited to do it.

## 3. Components

### 3.1 C1 — `newsdesk relay`

Headless loop, intended for launchd. Replaces the consume/forward half of today's `watch`, and hosts the web server (C4).

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk relay [--once] [--no-pushover] [--no-web]` |
| Each cycle (every `POLL_INTERVAL_S` = 2 s) | `consume_local_queue` → `append_to_history` → fan out each entry to SSE subscribers (C4) → `forward_to_pushover` for entries passing `should_forward_pushover(entry, pushover_min_priority)` |
| Every `REMOTE_POLL_INTERVAL_S` = 30 s | `consume_remote_queue` for each `remote_machines` entry. Phase 1 has none; the code path and its test ship anyway |
| Every `HEARTBEAT_INTERVAL_S` = 60 s | Write `relay.state` (C6); publish a `state` SSE event; `curl -fsS -m 10 <healthchecks_url>` if configured (`/fail` suffix when Keychain tokens are missing, D5) |
| Web server | Started in a daemon thread at startup unless `--no-web`. A crash in the thread is logged and the server restarted; it can never take the relay loop down |
| Tokens | Read from Keychain once at startup. Missing tokens → log once, keep relaying (history and the viewer still work) |
| Logging | One line per cycle that did anything: `2026-09-19T07:28:00 consumed=3 forwarded=1 remotes=0/0 sse=2` to stdout (launchd routes to `relay.log`). Silent cycles log nothing |
| `--once` | One full cycle including remotes, no web server, then exit 0. For manual verification and tests |
| Robustness | Per-entry try/except around forward and fan-out; per-remote try/except already inside `consume_remote_queue`. The loop must not die on a bad line, a bad host, a Pushover 4xx, or a slow browser |

### 3.2 C2 — `newsdesk push`

Ships the local queue to the hub. Mirror image of `consume_remote_queue`.

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk push` — reads `config["hub"]`; exits 0 silently if no hub configured. Spawned by every `send` (C3); never scheduled |
| Lock | Exclusive `fcntl.flock` on `queue.jsonl.push.lock`, **blocking**, with a `PUSH_LOCK_TIMEOUT_S` alarm. Serializes concurrent pushes: a burst of sends spawns a burst of pushes, the first holds the lock and drains everything, the rest wake to an empty queue and exit. One ssh connection per burst, and no two pushes ever touch `.processing` at once |
| Steps, under the lock, looped until the queue is empty | 1. Recover any `.processing` left by a crashed prior push (fresh, < `STALE_PROCESSING_AGE`). 2. Rename `queue.jsonl` → `queue.jsonl.processing`, read entries. 3. If nothing to ship, release and exit 0. 4. `ssh -o BatchMode=yes -o ConnectTimeout=2 <hub> 'f=...; t="$f.incoming.$$"; cat > "$t" && cat "$t" >> "$f" && rm "$t"'` with the entries on stdin. 5. On exit 0: delete `.processing`, go to 2 (a send may have landed while shipping). On any failure: leave `.processing` in place, release, exit — the next send's push recovers it |
| Why stage-then-append | A dropped connection mid-stream must not leave a half line in the hub's queue. The remote `cat "$t" >> "$f"` is a local operation on the mini and completes or doesn't. `parse_jsonl` would skip a torn line silently — that is a lost notification, and staging makes it impossible |
| Concurrency with the relay | The relay renames the hub's queue; an append that races it either lands before the rename (consumed now) or opens a fresh file after (consumed next cycle). No loss either way |
| Backlog after an outage | If the mini was unreachable, entries wait in `.processing`/queue until the *next send* spawns a push. There is no timer to ship them sooner. Accepted (D3): micro-m4 sends constantly when in use, sends nothing that clears the phone threshold today, and nothing is lost — only delayed until the next send |

### 3.3 C3 — `send` spawns a push

After the local append, if `config["hub"]` is set: `subprocess.Popen([newsdesk, "push"], start_new_session=True, stdin/stdout/stderr=DEVNULL)`. Fire-and-forget; `send` still returns in milliseconds; a Claude Code hook that spawned it is not blocked and cannot kill it. On the boxes there is no `hub`, so nothing changes there.

This is the **only** push trigger — every send on micro-m4 ships to the mini, and there is no launchd job on micro-m4 (D3). Why detached rather than a synchronous `ssh` inside `send`: a cold ssh over Tailscale is 0.5–2 s and an unreachable mini costs the full `SSH_CONNECT_TIMEOUT` — inside every Claude Code hook, on every turn, for as long as the mini is down. The detached child is what lets "each send writes to the mini" and N1 both hold.

### 3.4 C4 — Web viewer, server side

A `ThreadingHTTPServer` in a daemon thread inside the relay. This is A1 + A2 + A3 from `docs/alternate-architectures.md` §2.1, built together.

| ID | Route | Returns | Notes |
|----|-------|---------|-------|
| W1 | `GET /` | `web/index.html` | Read from disk per request, `Cache-Control: no-store` — edit the page, reload, no relay restart |
| W2 | `GET /api/history?since=<ts>&limit=<n>` | JSON array of entries, oldest→newest | `since` filters `ts > since` (default 0); `limit` caps from the newest end (default `WEB_INITIAL_ENTRIES`). Reads `history.jsonl` via `parse_jsonl` — no in-memory cache to get stale |
| W3 | `GET /api/state` | The relay state dict (C6) | From memory, not the file |
| W4 | `GET /events` | SSE stream | `event: entry` per consumed entry (fanned out from the relay loop through a per-subscriber `queue.Queue`); `event: state` per heartbeat; `: keepalive` comment every `SSE_KEEPALIVE_S` so proxies and `tailscale serve` never see an idle stream. Subscriber removed on write error |
| W5 | anything else | 404 | No POST, no auth, no query beyond W2. Tailnet-only reachability *is* the auth, as for 5555/8766/8767 |

Gap handling: `EventSource` reconnects on its own. On every `open`, the page refetches W2 with `since=` its newest seen `ts`, so nothing consumed during a disconnect is missed. That is the whole consistency story — no sequence numbers, no server-side cursor.

Bind: `web_bind` (default `127.0.0.1`) and `web_port` (default 5556). Loopback plus `tailscale serve` is the recommended exposure (D9).

### 3.5 C5 — Web viewer, the page

One static file, `web/index.html`, vanilla JS, no build, mobile-first. Parity with the curses TUI, mapped key by key:

| ID | Curses today | Page | Notes |
|----|--------------|------|-------|
| V1 | `L` latest / `H` history | One list, **newest first** (D10). Entries that arrived since the page opened are highlighted and counted in the tab title: `(3) newsdesk` | Newest-first because on a phone you open it and want the latest without scrolling. A scroll-to-oldest is a scroll, not a mode |
| V2 | `C` clear | "Mark read" — clears highlights and the title count | Client-side only |
| V3 | `S` save snapshot | Link to `/api/history` | The JSONL *is* the snapshot |
| V4 | `B` bell threshold | 🔔 toggle + threshold select (≥1 / ≥0 / ≥−1 / off), persisted in `localStorage` | Browsers block audio until one user gesture — the toggle *is* the gesture, then `AudioContext` beeps. Same semantics as `should_bell` |
| V5 | `V` show silent | Checkbox, persisted | Priority −2 hidden by default, as today |
| V6 | `?` help pages | Priority legend in a collapsible footer | Keychain status moves to V7 |
| V7 | Header status line | `relay 3s ago · pushover ≥ 2 · remotes 0/0` from `state` events. **Red banner** when `EventSource` is disconnected or the state is older than `RELAY_STALE_AFTER_S` | A viewer that cannot tell the relay is dead is the same hole as today |
| V8 | `🔗` link marker | The url rendered as an actual link, labelled with `url_title` | Strictly better |
| V9 | — | Filter box matching project / machine / title / message | next-steps F1, free on the client |
| V10 | — | Each row: local time, project, machine, priority icon, title — message | Same fields as `format_entry`; `machine` is what distinguishes senders in a unified feed |

Nothing is stored server-side by the page. Web Notifications are out of scope (X7) — Pushover already does phone push; this is a viewer.

### 3.6 C6 — Relay state

Held in memory, served at W3, published on the SSE `state` event, and also written to `~/.local/share/newsdesk/relay.state` (JSON, temp-write + rename) on every heartbeat so `--once` runs and shell checks can read it:

```json
{"last_cycle_ts": 1758281280.1, "started_ts": 1758200000.0, "pushover": "≥ 2", "remotes": {}, "forwarded_total": 14, "sse_clients": 2}
```

`remotes` maps host → last successful pull time (`null` = never reached since start). Empty in Phase 1.

### 3.7 C7 — Config

| ID | Key | micro-m4 | mini | box (Phase 2) | Notes |
|----|-----|----------|------|---------------|-------|
| K1 | `hub` | `{"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}` | absent | absent | Presence turns on C3. `~` is remote — same rule as `remote_machines` |
| K2 | `remote_machines` | `[]` (**remove** the `mini` entry) | `[]` in Phase 1; one per box in Phase 2 | `[]` | `host` is an `~/.ssh/config` alias on the mini |
| K3 | `pushover_min_priority` | ignored | `2` (current) | ignored | Only the relay reads it now |
| K4 | `web_bind`, `web_port` | ignored | `"127.0.0.1"`, `5556` | ignored | Defaults; override `web_bind` to the tailnet IP if D9's fallback is needed |
| K5 | Healthchecks URL | — | Keychain `newsdesk-hc-url`, account `dave` | — | Same pattern as `backup-monitor-hc-url`. Absent → relay logs once, no ping. ✅ **The healthchecks.io account exists (2026-10-02): davedmiller79@gmail.com, default project, with email and Pushover (down = Emergency, up = Normal) already subscribed** — set up for hvac_monitor's freeze watch. S3 is just "add a check `newsdesk-relay` there"; both integrations attach to it |

### 3.8 C8 — launchd and exposure

| ID | File / command | Machine | Key settings |
|----|----------------|---------|--------------|
| L1 | `launchd/com.dave.newsdesk-relay.plist` | mini | `KeepAlive true`, `RunAtLoad true`, `ThrottleInterval 10`, `StandardOutPath`/`StandardErrorPath` → `~/.local/share/newsdesk/relay.log`, `ProgramArguments` = absolute path to `~/bin/newsdesk relay` |
| L2 | `scripts/install-launchd.sh` | mini | Substitutes `__HOME__`, copies to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u)`. Idempotent (bootout first if loaded) |
| L3 | `tailscale serve --bg --https=443 http://127.0.0.1:5556` | mini, once | Tailnet-only HTTPS at `https://micro-mac-mini.<tailnet>.ts.net`. Needs MagicDNS + HTTPS certs enabled in the admin console. Persists across reboots (`tailscale serve status` to check) |

One plist, one machine. The relay is a user agent (`gui/` domain), not a system daemon — it needs the login Keychain, which is what the backup monitor already relies on. micro-m4 gets nothing: its push is spawned per send (C3).

### 3.9 C9 — New constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `REMOTE_POLL_INTERVAL_S` | 30 | Relay pull cadence for `remote_machines` (inert until Phase 2) |
| `HEARTBEAT_INTERVAL_S` | 60 | State file + `state` event + Healthchecks ping cadence |
| `RELAY_STALE_AFTER_S` | 180 | Page shows the red banner past this |
| `PUSH_LOCK_TIMEOUT_S` | 30 | A push waiting on the lock gives up after this (a wedged ssh must not pile up children) |
| `WEB_PORT` | 5556 | Default `web_port` — register in OpenBrain #101 |
| `WEB_INITIAL_ENTRIES` | 200 | Default `limit` for W2 on page load |
| `SSE_KEEPALIVE_S` | 15 | Comment ping cadence on idle streams |

### 3.10 C10 — Removed with the curses TUI

Deleted outright, with their tests (D8): `cmd_watch`, `cmd_watch_curses`, the `watch` subparser, `import curses`, and the helpers only the TUI used — `format_entry`, `_fit_field`, `should_display`, `priority_icon` / `PRIORITY_ICONS`, `should_bell`, `cycle_bell_threshold`, `bell_threshold_label`, `pushover_status_label`, `DISPLAY_FIELD_WIDTH`, `LINK_MARKER`, `DEFAULT_BELL_THRESHOLD`, `BELL_THRESHOLD_CYCLE`. About 300 lines of `newsdesk.py` and ~9 of the 72 tests. The page carries its own icon map and bell rule.

`should_forward_pushover` stays (the relay uses it). `newsdesk` with no arguments keeps printing help. If a terminal view is ever missed, `newsdesk tail` (X6) is fifteen lines.

## 4. Failure modes

| ID | Failure | Effect | Detected by | Recovery |
|----|---------|--------|-------------|----------|
| F1 | Relay process dies | Nothing forwarded; page shows red banner | launchd restarts within 10 s; Healthchecks fires if it keeps dying | Automatic |
| F2 | Mini off / Tailscale down | Nothing forwarded, page unreachable | **Healthchecks dead-man → Pushover directly** (HC has a native Pushover integration; it must not route through newsdesk) | Manual — but *known* |
| F3 | Box unreachable (Phase 2) | Its entries wait in its queue | hvac staleness alert (W6) covers the box being dark; the queue drains when it returns, timestamped | Automatic |
| F4 | micro-m4 can't reach mini | Entries wait in micro-m4's `.processing` / queue | The next send's push ships the backlog. If micro-m4 goes idle first, the backlog waits for the next send — delayed, never lost (D3) | Automatic on next send |
| F5 | Keychain locked / tokens missing on mini | History and page work, no Pushover | Relay logs once; page header shows `no keychain tokens`; HC pinged with `/fail` so it **pages** (D5) | Manual |
| F6 | Old `watch` on micro-m4 still polling the mini | Races the relay for the mini's queue; items it wins never hit the hub's history or Pushover | Nothing | **Deploy order S4 before S5** |
| F7 | Pushover rejects a message | Entry is in history, not on phone | Not detected (fire-and-forget, #24) | Out of scope here; next-steps R4 |
| F8 | Web server thread crashes | Page unreachable; relay keeps forwarding | Relay logs it and restarts the server; page banner meanwhile | Automatic |
| F9 | SSE stream dropped by an idle proxy or a phone sleeping | Page misses entries while disconnected | `EventSource` reconnects; page refetches `since=` newest seen (C4) | Automatic |
| F10 | `tailscale serve` not persisted after a reboot | HTTPS URL dead; relay fine | S9 checks `tailscale serve status`; fallback URL `http://100.70.51.21:5556` only if `web_bind` was set to the tailnet IP | Manual, once |

## 5. Deployment — Phase 1

Order matters because of F6. Each step is complete before the next starts.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| S1 | micro-m4 | Implement C1–C10 with tests (§8); commit on branch `relay-hub`; push | `python -m pytest tests/ -q` green |
| S2 | mini | `git pull` in `~/Developer/newsdesk` (also retires the stale clone that lacks `--url`, #42) | `newsdesk send --help` shows `--url`; `newsdesk watch` no longer exists |
| S3 | mini | Keychain: `security add-generic-password -a dave -s newsdesk-hc-url -w <url>`. Create the Healthchecks check `newsdesk-relay` (period 5 min, grace 5 min, Pushover integration). Config: `web_bind`/`web_port` only if not defaults | `newsdesk relay --once` prints `consumed=… remotes=0/0` |
| S4 | micro-m4 | Config: delete `mini` from `remote_machines`; add `hub`. **Quit the running `watch`.** From here until S5 completes, the mini's queue is unconsumed — minutes, and it is durable | `cat ~/.config/newsdesk/config.json` |
| S5 | mini | `scripts/install-launchd.sh` | `launchctl list \| grep newsdesk`; `relay.log` shows cycles; HC dashboard shows the check green; `curl -s localhost:5556/api/state` returns JSON |
| S6 | mini | `tailscale serve --bg --https=443 http://127.0.0.1:5556` | `tailscale serve status`; the HTTPS URL loads the page on micro-m4 **and on the phone** |
| S7 | mini | Real end-to-end: `newsdesk send "Relay test" "priority 2 via hub" --priority 2` on the mini | Phone buzzes (emergency; acknowledge it). Row appears on the open page within 2 s without reload |
| S8 | micro-m4 | `newsdesk send "Push test" "from micro-m4" --priority 0`; then five sends in a loop | Row appears on the page within ~5 s with machine `micro-m4`; `relay.log` shows one batch for the burst, not five |
| S9 | mini | `sudo reboot` (⚠️ disrupts OpenBrain / hvac services for ~2 min — Dave's call on timing) | Relay running after login without a hand; HC never went red; `tailscale serve status` still lists the route; page loads |
| S10 | phone | Lock the phone with the page open for 10 min; send from micro-m4 meanwhile; unlock | Row is there on unlock (F9 recovery) |
| S11 | — | Kill the relay (`kill -9` the pid and `launchctl bootout`) and wait 10 min | Phone gets the Healthchecks alert. Reinstall |
| S12 | — | Merge `relay-hub` to main; docs (O4); register 5556 (O5) | — |

## 6. Deployment — Phase 2 (deferred; planned only)

Prerequisites: hvac plan W3/W7 landed, so `newsdesk` is installed on each box and its units call `newsdesk send`.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| Q1 | boxes | Confirm which user the box's systemd units send as — the queue path in K2 must be that user's `~/.local/share/newsdesk/queue.jsonl` (root's home is `/root`) | `ls -la` the queue after a unit has sent |
| Q2 | mini | `~/.ssh/config` alias per box with matching `User`; one manual `ssh <box> true` each to populate `known_hosts` — BatchMode refuses unknown hosts **silently** (the poll just returns nothing) | `ssh -o BatchMode=yes <box> true` exits 0 |
| Q3 | mini | Add the three `remote_machines` entries; `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` (config is read at startup) | `relay.log` shows `remotes=3/3`; page header shows `remotes 3/3` |
| Q4 | a box | `newsdesk send "Pull test" "from lake-agent-1" --priority 0` | Row on the page within 30 s with the box's machine name |
| Q5 | hvac_monitor | Amend W7 (O1) | — |

Verify-don't-assume at Q3: launchd on the mini reaching Tailscale peers under Local Network Privacy (#698 was LAN; 100.x should be exempt — confirm on first run).

## 7. Verification criteria (falsifiable)

| ID | Claim | Test |
|----|-------|------|
| V1 | The relay survives a reboot of the mini | S9 |
| V2 | A relay that stops is reported to the phone without newsdesk's help | S11 |
| V3 | A priority-2 reaches the phone with no terminal window open anywhere | S7 with every terminal closed |
| V4 | `send` on micro-m4 still returns in < 50 ms with the hub unreachable | `time newsdesk send x y` with Tailscale off |
| V5 | No entry is lost when a push is interrupted mid-transfer | T3 (kill ssh mid-stream in a test harness; entry is in `.processing`, not in the hub's queue as a torn line) |
| V6 | Two consumers can no longer exist | `grep -n consume_ newsdesk.py` shows call sites only in `relay` and `push` |
| V7 | A new entry is on the phone's page within 2 s of the relay consuming it, with no reload | S7 |
| V8 | A page that was disconnected shows what it missed | S10 |
| V9 | The mini has no new all-interfaces listener | `lsof -nP -iTCP -sTCP:LISTEN` on the mini shows 5556 on `127.0.0.1` only |

## 8. Tests (TDD — written first)

| ID | Test | Pins |
|----|------|------|
| T1 | `test_relay_once_consumes_and_forwards` | One cycle: local queue → history; forward called only for entries ≥ threshold and ≠ −2; state written |
| T2 | `test_relay_remote_cadence` | Remotes pulled on the first cycle and again only after `REMOTE_POLL_INTERVAL_S` (mock clock); `remote_machines = []` pulls nothing |
| T3 | `test_push_retains_processing_on_failure` | ssh returncode ≠ 0 → `.processing` still exists with all entries; returncode 0 → deleted |
| T4 | `test_push_command_stages_before_append` | The remote command string writes to `$f.incoming.$$` before `>> "$f"` |
| T5 | `test_push_drains_under_lock` | An entry appended while a push is mid-ship is shipped by the same push's next loop iteration; a second push started concurrently exits having shipped nothing |
| T6 | `test_send_spawns_push_only_with_hub` | `Popen` called iff `config["hub"]`; called with `start_new_session=True` |
| T7 | `test_history_since_and_limit` | W2's filter: `since` excludes `ts <= since`; `limit` keeps the newest |
| T8 | `test_web_routes` | Real `ThreadingHTTPServer` on an ephemeral port: `/` serves the file with `no-store`; `/api/state` is JSON; unknown path is 404 |
| T9 | `test_sse_fanout_and_keepalive` | A subscriber receives `event: entry` for a consumed entry and a `: keepalive` after `SSE_KEEPALIVE_S` (mock clock); a subscriber whose write fails is dropped |
| T10 | `test_heartbeat_pings_when_url_present` | curl invoked iff URL; `/fail` suffix when tokens missing |
| T11 | Existing tests | The ~63 that survive C10 still green; the ~9 TUI-only tests go with the TUI (D8) |

## 9. Decisions

| ID | Question | Recommendation | Status |
|----|----------|----------------|--------|
| D1 | Does any consume path survive outside `relay`? | **No.** One consumer, one code path. | Open |
| D2 | ~~Viewer on micro-m4: `ssh -t mini` or `--hub`?~~ | Superseded by the web viewer (C4/C5). | Moot |
| D3 | Push trigger: spawn-on-send, launchd backstop, or both? | **Spawn only.** Every send ships; no launchd on micro-m4. The backstop covered only "mini unreachable at send time, then micro-m4 idle" — and even then the entry ships on the next send. Not worth a plist. | **Decided 2026-09-19 (Dave)** |
| D4 | Relay remote cadence | 30 s. Box messages are boot/update/throttle reports; nothing there needs 2 s. Ships inert in Phase 1. | Open |
| D5 | Relay pings Healthchecks `/fail` when Keychain tokens are missing? | **Yes.** It turns F5 from silent into paged, at the cost of three lines. | Open |
| D6 | Keep `--no-pushover` on `relay`? | Yes — it is the only session-level escape and costs nothing. | Open |
| D7 | Branch | `relay-hub`, merged to main after S11 passes. | Open |
| D8 | Remove the curses TUI entirely, with its tests (C10)? | **Yes.** Two viewers is two things to keep at parity. The page reaches strictly more places. `newsdesk tail` (X6) covers a terminal glance if it is ever missed. ⚠️ This deletes ~9 passing tests along with the code they test — flagged here so it is an explicit call, not a quiet one. | Open |
| D9 | Exposure: `tailscale serve` (loopback bind, tailnet HTTPS, hostname) or bind the tailnet IP on 5556 (plain http)? | **`tailscale serve` first.** No new all-interfaces listener, a real URL, HTTPS. Fallback is one config key (`web_bind`). Untried on this tailnet; S6 is where it proves itself. | Open |
| D10 | Row order on the page | **Newest first.** The phone case decides it. | Open |
| D11 | Web server inside the relay process, or a separate `newsdesk web`? | **Inside.** The relay has each entry in hand the instant it is consumed, so SSE is zero-latency with no file watching; one plist, one log, one process to keep alive. The thread is fenced (F8). | Open |
| D12 | **`pushover_min_priority` on the hub once it relays 24/7 (K3): keep 2, or lower to 1?** Added 2026-10-02 from hvac_monitor's Lake House freeze watch (`docs/freeze-watch-plan.md` §9 N6), for Dave to revisit at the cutover. | **Decide at S4/S5 with these facts.** At **2** (today) only emergencies page: the freeze watch's first stages — Lake house dark at 90 min, Nest offline at 30 min, can't see the Nest, collector down, Longmont offline, Longmont on battery with > 2 h of runway — stay on the page and in the hvac check-in, and the phone hears a Lake power cut at 4 h (if Gaylord < 40 °F) and a furnace that lost 24 V at 2 h 15 m. At **1** those first stages page too, ~2½ h and ~1½ h sooner. ⚠️ The threshold is global: `~/.claude/hooks/newsdesk-notify.sh` sends **every Claude Code permission prompt at priority 1**, so at 1 each one from every session pages the phone; backup-migration, Memex and PostCardMaker choose per message. A per-project threshold (or the hook at 0) would remove the trade-off. Related: **F7** — the relay's fire-and-forget forwarding loses an alarm raised during a Longmont internet blip; the freeze watch carries a second path for its emergencies through healthchecks.io (`HEALTHCHECKS_FREEZE_ALARM_URL`). | **Open — Dave, at the cutover** |

## 10. Deferred / out of scope

| ID | Item | Why not now |
|----|------|-------------|
| X1 | Phase 2 box pull | Dave's call — planned in §6, no Pi-side work yet |
| X2 | SSH backoff for unreachable remotes (next-steps R2) | 30 s cadence × 2 s ConnectTimeout × 3 boxes is bounded at 6 s per cycle; tolerable |
| X3 | Checking the Pushover response (#24, next-steps R4) | Separate fix, separate commit |
| X4 | Replacing SSH+JSONL with an HTTP ingest on the mini (next-steps I1) | #821 already rejected a push API on the mini for hvac for reimplementing concerns the pull path gets free; same argument here. The web server here is read-only |
| X5 | ntfy (#206) | Deferred there; this closes the "unified web view" gap that was ntfy's main draw |
| X6 | `newsdesk tail` — print recent history to a terminal | Only if the TUI is missed after D8 |
| X7 | Web Notifications / PWA install | Pushover already does phone push; iOS web push needs a home-screen install and HTTPS; not worth it for a viewer |
| X8 | Auth on the web viewer | Tailnet reachability is the auth, as for every other mini service |

## 11. Cross-project follow-ups

| ID | Where | Change | Phase |
|----|-------|--------|-------|
| O1 | `hvac_monitor/docs/lewiston-collection-agents-plan.md` W7 | "micro-m4's `newsdesk watch` polls that queue" → the mini's relay polls it; the ssh-config/known_hosts prerequisite moves to the mini | 2 |
| O2 | OpenBrain #42 | The "no headless relay mode" correction becomes history once S9 passes; amend rather than leave a future session designing around a hole that closed | 1 |
| O3 | OpenBrain #211 | "restart `watch` to apply a threshold change" → restart the *relay* (`launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay`) | 1 |
| O4 | `README.md`, `CLAUDE.md`, `docs/newsdesk-plan.md` | New subcommands, the relay/viewer split, launchd install, the page URL; drop `watch` | 1 |
| O5 | OpenBrain #101 port registry | Add 5556 newsdesk viewer, loopback-bound behind `tailscale serve` | 1 |
| O6 | `docs/alternate-architectures.md` | Note that A1–A3 were built (this plan) and why the ntfy deferral held | 1 |

## 12. References

| ID | Ref | What it establishes |
|----|-----|---------------------|
| R1 | OpenBrain #42 | Relay = open TUI window; the two real fixes; boxes can be senders |
| R2 | OpenBrain #821 | Pull from the boxes; why a pulled box is the safer box; repair channel ≠ data channel |
| R3 | OpenBrain #30 | micro-m4 ↔ mini SSH is one-directional, by choice |
| R4 | OpenBrain #211 | `pushover_min_priority` = 2; read once at startup |
| R5 | OpenBrain #547 / `backup-migration/docs/monitoring-design.md` | Healthchecks.io as the mini's dead-man; Keychain URL pattern |
| R6 | OpenBrain #698 | Local Network Privacy blocks launchd processes from the *LAN*; Tailscale 100.x is not LAN — verify at Q3, don't assume |
| R7 | hvac plan §5.1, W7 | ACL grant `autogroup:member → tag:agent`, Tailscale SSH `accept`; the box-originated messages |
| R8 | `docs/alternate-architectures.md` §2.1 | A1 headless poller, A2 stdlib http.server + SSE + static page, A3 Tailscale-only — scoped in April at "half a day to two days", deferred behind ntfy |
| R9 | OpenBrain #101 | Mini port registry; deliberate all-interfaces listeners are 5555/8766/8767 only |
