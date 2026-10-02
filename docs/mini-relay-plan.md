*Last updated: 2026-10-02 09:47 MDT*

# Newsdesk — Hub-on-the-Mini Relay + Web Viewer

**Status: DRAFT for study. Nothing built.** Phase 1 (relay, push, web viewer) is the work to do now. Phase 2 (pull from the Lewiston boxes) is planned here in full and **deferred** — no Pi-side work until Dave says so. Decisions in §9 are open unless marked otherwise.

Reviewed 2026-10-02 (`docs/mini-relay-plan-review.md`, 48 findings). The findings are applied here; where a row cites an ID like A2 or OR1 with "review", it points into that file.

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
| M4 | Any browser on the tailnet — laptop, phone | **Viewer.** Reads the hub's history live | `https://micro-mac-mini.<tailnet>.ts.net` (`http://100.70.51.21:5556` answers only if `web_bind` is set to the tailnet IP, F10) |

### 2.2 Data flow

```
micro-m4                          micro-mac-mini                          phone / any browser
────────                          ──────────────                          ───────────────────
send ──▶ queue.jsonl
           │ push (ssh mini: stage, verify, rename; under flock)
           └──────────────────▶ queue.jsonl.in.* ─┐
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
| N3 | Queues are durable through outages | Push never deletes an entry it has not shipped: `.processing` and the queue both stay on disk through any number of failed pushes, with no age limit, and rotation is off on a machine that has a `hub` (D15). The cost is a queue that grows for as long as the mini is unreachable. |
| N4 | Each entry reaches history and the phone once | One consumer (the relay); nothing else touches a queue. Push is at-least-once — a batch whose success reply is lost is shipped again — so every entry carries an `id` and the relay drops ids already in history (D14). The relay commits a claimed batch only after the history write, so a crash in between replays the batch into the same dedupe. Entries with no `id` (a sender on older code) pass through undeduped. |
| N5 | stdlib only | `subprocess` for ssh and curl; `http.server` + `threading` + `queue` for the viewer. No framework, no build step. |
| N6 | The mini's listening surface stays deliberate (#101 audit) | Web server binds loopback; `tailscale serve` exposes it tailnet-only over HTTPS (D9). Fallback binds the tailnet IP, never `0.0.0.0`. |

### 2.5 Phasing

| Phase | Scope | Code | Config / deploy |
|-------|-------|------|-----------------|
| **1 — now** | Relay on the mini; push from micro-m4; web viewer; Healthchecks; curses TUI removed once the relay is proven | C1–C11 | SP1–SP6, then S1–S13 |
| **2 — deferred** | Pull from the three boxes | **None expected.** `consume_remote_queue` exists today; Phase 1 changes it to report whether the box was reached (C11) and ships the relay's remote cadence (C1) with `remote_machines = []`, inert and tested (T2). The one open item is its 3 s timeout, measured at Q0 | Q0–Q5 |

Phase 2 is config plus verification, provided Q0's measurement fits the timeout.

## 3. Components

### 3.1 C1 — `newsdesk relay`

Headless loop, intended for launchd. Replaces the consume/forward half of today's `watch`, and hosts the web server (C4).

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk relay [--once] [--no-pushover]` |
| Shape | The loop calls a step function, `relay_cycle(state, now)`, so tests drive cycles and the clock directly (T1, T2, T10) |
| Single instance | Non-blocking `flock` on `~/.local/share/newsdesk/relay.lock` at startup; if held, print a message and exit 1. A hand-run `relay` beside the launchd one would otherwise be a second writer of history and state |
| Each cycle (every `POLL_INTERVAL_S` = 2 s) | 1. `claim_queue` the local queue and every completed spool file `queue.jsonl.in.*` (C2, C11). 2. Drop entries whose `id` is already in history (D14). 3. Stamp each survivor with the next `seq` (C6). 4. `append_to_history`. 5. Commit the claim (delete `.processing` and the spool files). 6. Fan out each entry to SSE subscribers (C4). 7. `forward_to_pushover` for entries passing `should_forward_pushover(entry, pushover_min_priority)`. Commit comes after the history write so a crash between them replays into step 2, not into a loss |
| Every `REMOTE_POLL_INTERVAL_S` = 30 s | `consume_remote_queue` for each `remote_machines` entry; its entries join the same cycle at step 2. Phase 1 has none; the code path and its test ship anyway |
| Every `HEARTBEAT_INTERVAL_S` = 60 s | Write `relay.state` (C6); publish a `state` SSE event; ping Healthchecks if a URL is configured (`/fail` suffix when Keychain tokens are missing, D5; never `/fail` under `--no-pushover`, where tokens are deliberately unread). **The ping is issued by the relay loop itself, at the end of a cycle that completed** — never from the web thread, a timer thread or a second launchd job, so a wedged consume loop goes red instead of staying green on process liveness alone. **The first ping waits for `HEARTBEAT_INTERVAL_S` of uptime**, so a relay that crashes 20 s after every launchd restart never pings and goes red. **It is fire-and-forget:** `Popen(["curl", "-fsS", "-m", "10", url], start_new_session=True)` with output to `/dev/null`, not waited on, so an internet blip cannot stall consumption for the curl timeout |
| Web server | Started in a daemon thread at startup (not under `--once`). `socketserver` already contains per-request exceptions, so the one realistic failure is the bind; the thread retries the bind every 10 s until it succeeds (covers Tailscale not yet up when `web_bind` is the tailnet IP). It can never take the relay loop down |
| Tokens | Read from Keychain at startup. Missing tokens → log once, keep relaying (history and the viewer still work), and **retry the read on each heartbeat** until they appear, so a Keychain that was not ready at login heals without a restart |
| Forwarding | `forward_to_pushover` returns whether curl succeeded (today it swallows every failure). The cycle counts `forwarded` and `forward_failed` separately. Still synchronous, up to 10 s per forwarded entry; at threshold 2 that is rare. Retrying a failed forward stays out of scope (X3) |
| Logging | One line per cycle that did anything: `2026-09-19T07:28:00 consumed=3 dupes=0 forwarded=1 forward_failed=0 remotes=0/0 sse=2` to stdout, flushed per line (launchd routes to `relay.log`). Silent cycles log nothing |
| `--once` | One full cycle including remotes, then exit 0. No web server, **no Healthchecks ping**; always prints the summary line, even for an empty cycle, and always writes `relay.state`. For manual verification and tests |
| Housekeeping | On each heartbeat, delete `queue.jsonl.incoming.*` temp files older than a day — leftovers of a push whose remote shell was killed (F12). They are never claimed as entries |
| Robustness | Per-entry try/except around forward and fan-out; per-remote try/except already inside `consume_remote_queue`. The loop must not die on a bad line, an entry with a non-integer priority, a bad host, a Pushover 4xx, or a slow browser |

### 3.2 C2 — `newsdesk push`

Ships the local queue to the hub. It never appends to the hub's `queue.jsonl`; it drops a complete, verified file beside it for the relay to claim.

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk push` — reads `config.get("hub")`; exits 0 silently if no hub configured. Spawned by every `send` (C3); never scheduled |
| Lock | Exclusive `fcntl.flock` on `queue.jsonl.push.lock`, taken with `LOCK_NB` in a short sleep loop for up to `PUSH_LOCK_TIMEOUT_S`, then give up and exit 0. (A blocking `flock` under `SIGALRM` is retried by Python unless the handler raises.) Serializes concurrent pushes: a burst of sends spawns a burst of pushes, the first holds the lock and drains everything, the rest wake to an empty queue and exit. A burst is normally two ssh connections — the first push ships what was there when it started, then loops once for the rest — and no two pushes ever touch `.processing` at once |
| Steps, under the lock | 1. **If `queue.jsonl.processing` exists, ship it as it stands** (step 3), whatever its age. On success delete it; on failure release and exit, touching nothing else. 2. Rename `queue.jsonl` → `queue.jsonl.processing`; if there was no queue, release and exit 0. 3. Ship: read the file's bytes, run the remote command with them on stdin. 4. On exit 0: delete `.processing`, go to 2 (a send may have landed while shipping). On any failure or timeout: leave `.processing` in place, release, exit — the next send's push starts at step 1 |
| What is on disk after a failure | Everything not yet confirmed shipped: the failed batch in `.processing`, later sends in `queue.jsonl`. Nothing is held only in memory, `.processing` is never overwritten by a rename (step 1 clears it first), and the 1-day stale rule that `consume_local_queue` applies does **not** apply here — an undelivered backlog is never too old to ship |
| Remote command | Built by one function so the test can run it under `sh -c` with no ssh (T4). With `N` = the batch's byte count and `ID` = a fresh uuid: `f=<queue>; t="$f.incoming.ID"; if cat > "$t" && [ "$(wc -c < "$t")" -eq N ]; then mv "$t" "$f.in.ID"; else rm -f "$t"; exit 1; fi` |
| ssh | `ssh -o BatchMode=yes -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 <hub> '<remote command>'`, run with `timeout=PUSH_SSH_TIMEOUT_S`. Without the overall bound, a connection that stalls after connecting holds the lock until TCP gives up |
| Why verify, then rename | A dropped connection looks like end-of-input to the remote `cat`, so staging alone would commit a truncated batch; the byte count is what tells a whole batch from a cut one. And the batch is never appended to `queue.jsonl`: an append opens the file and writes a moment later, and the relay's rename can fall in that gap and lose the batch. `mv` into a uniquely named spool file is atomic — the relay sees the whole file or no file |
| Duplicates | If the remote `mv` succeeds but the exit status never arrives, the batch ships again. Each entry's `id` lets the relay drop the second copy (N4, D14) |
| Backlog after an outage | If the mini was unreachable, entries wait in `.processing`/queue until the *next send* spawns a push. There is no timer to ship them sooner. Accepted (D3): micro-m4 sends constantly when in use, sends nothing that clears the phone threshold today, and nothing is lost — only delayed until the next send. The queue is not rotated while it waits (D15) |

### 3.3 C3 — `send` spawns a push

After the local append, if `config.get("hub")` is set: `subprocess.Popen([sys.executable, os.path.abspath(__file__), "push"], start_new_session=True, stdin/stdout/stderr=DEVNULL)`. Fire-and-forget; `send` still returns in milliseconds; a Claude Code hook that spawned it is not blocked and cannot kill it (SP3 proves this before anything is built on it). On the boxes there is no `hub`, so nothing changes there.

Where it goes: in `cmd_send`, which has the config, inside its own try/except — a `Popen` that raises leaves the entry queued and `send` still returns 0. `send_notification` gains only the `id` field (a `uuid4` hex, D14). On a machine with a `hub`, `send` also skips `_maybe_rotate` (D15). The child is addressed by interpreter and file path because hooks do not source `.zshrc` and cannot rely on `newsdesk` being on PATH.

This is the **only** push trigger — every send on micro-m4 ships to the mini, and there is no launchd job on micro-m4 (D3). Why detached rather than a synchronous `ssh` inside `send`: a cold ssh over Tailscale is 0.5–2 s and an unreachable mini costs the full `SSH_CONNECT_TIMEOUT` — inside every Claude Code hook, on every turn, for as long as the mini is down. The detached child is what lets "each send writes to the mini" and N1 both hold.

### 3.4 C4 — Web viewer, server side

A `ThreadingHTTPServer` in a daemon thread inside the relay. This is A1 + A2 + A3 from `docs/alternate-architectures.md` §2.1, built together.

| ID | Route | Returns | Notes |
|----|-------|---------|-------|
| W1 | `GET /` | `web/index.html` | Read from disk per request, `Cache-Control: no-store` — edit the page, reload, no relay restart |
| W2 | `GET /api/history?since=<seq>&limit=<n>` | JSON array of entries, oldest→newest | `since` filters `seq > since` (default 0); `limit` caps from the newest end (default `WEB_INITIAL_ENTRIES`). Reads `history.jsonl` via `parse_jsonl` — no in-memory cache to get stale. Safe against a concurrent history write because that write is a temp file plus `os.replace` (C11) |
| W3 | `GET /api/state` | The relay state dict (C6) | From memory, not the file |
| W4 | `GET /events` | SSE stream | `event: entry` per consumed entry; `event: state` per heartbeat; `: keepalive` comment every `SSE_KEEPALIVE_S` so proxies and `tailscale serve` never see an idle stream. Headers `Content-Type: text/event-stream`, `Cache-Control: no-cache`; flush after every write |
| W5 | anything else | 404 | No POST, no auth, no query beyond W2. Tailnet-only reachability *is* the auth, as for 5555/8766/8767 |

Relay-to-web hand-off: the relay loop and the request threads share a subscriber list under one `threading.Lock`. Each subscriber is a `queue.Queue(maxsize=SSE_QUEUE_MAX)`; the loop fans out with `put_nowait` and **drops a subscriber whose queue is full** or whose write fails, so a stalled browser costs the loop nothing. The server sets `daemon_threads = True`. A dropped page recovers the same way as any disconnect, below.

Gap handling: `EventSource` reconnects on its own. On every `open`, the page refetches W2 with `since=` its highest seen `seq`, so nothing consumed during a disconnect is missed. `seq` is the hub's arrival counter (C6), not the sender's `ts`: an entry that arrives late — a micro-m4 backlog after an outage, a box whose clock was not yet set — carries an old `ts` but a new `seq`, and a `ts` cursor would skip exactly those.

Bind: `web_bind` (default `127.0.0.1`) and `web_port` (default 5556). Loopback plus `tailscale serve` is the recommended exposure (D9).

### 3.5 C5 — Web viewer, the page

One static file, `web/index.html` (located relative to `newsdesk.py`'s own path, not the working directory), vanilla JS, no build, mobile-first. Titles and messages come from arbitrary senders, so the page sets them with `textContent` only, and renders a U8 link only for an `http:` or `https:` url. Parity with the curses TUI, mapped key by key:

| ID | Curses today | Page | Notes |
|----|--------------|------|-------|
| U1 | `L` latest / `H` history | One list, **newest first** by arrival (`seq`, D10); each row still shows the sender's own time. Entries that arrived since the page opened are highlighted and counted in the tab title: `(3) newsdesk` | Newest-first because on a phone you open it and want the latest without scrolling. A scroll-to-oldest is a scroll, not a mode |
| U2 | `C` clear | "Mark read" — clears highlights and the title count | Client-side only |
| U3 | `S` save snapshot | Link to `/api/history` | The JSONL *is* the snapshot |
| U4 | `B` bell threshold | 🔔 toggle + threshold select (≥1 / ≥0 / ≥−1 / off), persisted in `localStorage` | Browsers block audio until one user gesture — the toggle *is* the gesture, then `AudioContext` beeps. Same semantics as `should_bell` |
| U5 | `V` show silent | Checkbox, persisted | Priority −2 hidden by default, as today |
| U6 | `?` help pages | Priority legend in a collapsible footer | Keychain status moves to U7 |
| U7 | Header status line | `relay 3s ago · pushover ≥ 2 · remotes 0/0` from `state` events, plus `healthchecks off` when the state says so. **Red banner** when `EventSource` is disconnected or the state is older than `RELAY_STALE_AFTER_S` | A viewer that cannot tell the relay is dead is the same hole as today |
| U8 | `🔗` link marker | The url rendered as an actual link, labelled with `url_title` | Strictly better |
| U9 | — | Filter box matching project / machine / title / message | next-steps F1, free on the client |
| U10 | — | Each row: local time, project, machine, priority icon, title — message | Same fields as `format_entry`; `machine` is what distinguishes senders in a unified feed |

Nothing is stored server-side by the page. Web Notifications are out of scope (X7) — Pushover already does phone push; this is a viewer.

### 3.6 C6 — Relay state

Held in memory, served at W3, published on the SSE `state` event, and also written to `~/.local/share/newsdesk/relay.state` (JSON, temp-write + rename) on every heartbeat so `--once` runs and shell checks can read it:

```json
{"last_cycle_ts": 1758281280.1, "started_ts": 1758200000.0, "pushover": "≥ 2", "healthchecks": true, "remotes": {}, "forwarded_total": 14, "forward_failed_total": 0, "last_seq": 4812, "sse_clients": 2}
```

- `pushover` is the string from `pushover_status_label` (`≥ 2`, `no keychain tokens`, or `off (--no-pushover)`), which is why that helper stays (C10).
- `healthchecks` is false when no URL is in the Keychain. The page header shows it (U7), because a check that has never been pinged does not alert and would otherwise be silent.
- `remotes` maps host → last time the box was reached (`null` = never since start). Empty in Phase 1.
- `last_seq` is the arrival counter. The relay stamps every entry it writes to history with `seq = last_seq + 1`. At startup it resumes from the highest `seq` in `history.jsonl` (0 if none), so the counter survives restarts.

### 3.7 C7 — Config

| ID | Key | micro-m4 | mini | box (Phase 2) | Notes |
|----|-----|----------|------|---------------|-------|
| K1 | `hub` | `{"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}` | absent | absent | Presence turns on C3. `~` is remote — same rule as `remote_machines` |
| K2 | `remote_machines` | `[]` (**remove** the `mini` entry) | `[]` in Phase 1; one per box in Phase 2 | `[]` | `host` is an `~/.ssh/config` alias on the mini |
| K3 | `pushover_min_priority` | ignored | **`2`, set explicitly at S3** | ignored | Only the relay reads it now. 2 is micro-m4's value today; the code default is −1, which on the hub would page the phone for every turn-complete ping. D12 revisits the value at the cutover |
| K4 | `web_bind`, `web_port` | ignored | `"127.0.0.1"`, `5556` | ignored | Added to `DEFAULT_CONFIG`; override `web_bind` to the tailnet IP if D9's fallback is needed |
| K6 | Pushover tokens | no longer read | Keychain `newsdesk-app-token`, `newsdesk-user-key`, account `pushover` | — | Today they are set up on micro-m4. The mini needs its own copies (S3) or the relay starts in F5 and pages `/fail` |
| K5 | Healthchecks URL | — | Keychain `newsdesk-hc-url`, account `dave` | — | Same pattern as `backup-monitor-hc-url`. `read_keychain_token` hardcodes account `pushover` today, so it gains an `account` parameter (C11). Absent → relay logs once, no ping, and the state and page say `healthchecks off` (C6). ✅ **The healthchecks.io account exists (2026-10-02): davedmiller79@gmail.com, default project, with email and Pushover (down = Emergency, up = Normal) already subscribed** — set up for hvac_monitor's freeze watch. S3 is just "add a check `newsdesk-relay` there"; both integrations attach to it |

### 3.8 C8 — launchd and exposure

| ID | File / command | Machine | Key settings |
|----|----------------|---------|--------------|
| L1 | `launchd/com.dave.newsdesk-relay.plist` | mini | `KeepAlive true`, `RunAtLoad true`, `ThrottleInterval 10`, `StandardOutPath`/`StandardErrorPath` → `~/.local/share/newsdesk/relay.log`, `ProgramArguments` = absolute path to `~/bin/newsdesk relay`. `EnvironmentVariables`: `PATH` (so the wrapper's `exec python3` finds the same Python as an interactive shell — launchd's default PATH gives `/usr/bin/python3`; SP2 records which one launchd sees) and `PYTHONUNBUFFERED=1` (stdout to a file is block-buffered otherwise, and `relay.log` would sit empty). The file in the repo is a template with the literal token `__HOME__` wherever the home directory appears |
| L2 | `scripts/install-launchd.sh` | mini | Replaces `__HOME__` in the template with `$HOME`, writes the result to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u)`. Idempotent (bootout first if loaded) |
| L3 | `tailscale serve --bg --https=443 http://127.0.0.1:5556` | mini, once | Tailnet-only HTTPS at `https://micro-mac-mini.<tailnet>.ts.net`. Needs MagicDNS + HTTPS certs enabled in the admin console. Persists across reboots (`tailscale serve status` to check) |

One plist, one machine. The relay is a user agent (`gui/` domain), not a system daemon — it needs the login Keychain, which is what the backup monitor already relies on. micro-m4 gets nothing: its push is spawned per send (C3).

### 3.9 C9 — New constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `REMOTE_POLL_INTERVAL_S` | 30 | Relay pull cadence for `remote_machines` (inert until Phase 2) |
| `HEARTBEAT_INTERVAL_S` | 60 | State file + `state` event + Healthchecks ping cadence |
| `RELAY_STALE_AFTER_S` | 180 | Page shows the red banner past this |
| `PUSH_LOCK_TIMEOUT_S` | 30 | A push waiting on the lock gives up after this (a wedged ssh must not pile up children) |
| `PUSH_SSH_TIMEOUT_S` | 20 | Overall bound on one push's ssh, so the lock holder cannot stall indefinitely |
| `SSE_QUEUE_MAX` | 100 | Per-subscriber queue bound; a full queue drops the subscriber |
| `HISTORY_MAX_ENTRIES` | 5000 (was 1000) | The hub's history is now the unified feed for every machine and the page's whole reachable past (D16) |
| `WEB_PORT` | 5556 | Default `web_port` — register in OpenBrain #101 |
| `WEB_INITIAL_ENTRIES` | 200 | Default `limit` for W2 on page load |
| `SSE_KEEPALIVE_S` | 15 | Comment ping cadence on idle streams |

### 3.10 C10 — Removed with the curses TUI

**Not part of the build.** The TUI stays, working, until the relay has passed S11; it is removed at S12 (D8). `~/bin/newsdesk` on micro-m4 runs the working tree, so deleting `watch` any earlier would leave no way to reopen the only proven relay.

Deleted at S12, with their tests: `cmd_watch`, `cmd_watch_curses`, the `watch` subparser, `import curses`, and the helpers only the TUI used — `format_entry`, `_fit_field`, `should_display`, `priority_icon` / `PRIORITY_ICONS`, `should_bell`, `cycle_bell_threshold`, `bell_threshold_label`, `DISPLAY_FIELD_WIDTH`, `LINK_MARKER`, `DEFAULT_BELL_THRESHOLD`, `BELL_THRESHOLD_CYCLE`. About 345 of the 731 lines of today's `newsdesk.py` and 27 of today's 72 tests (nine test classes). The page carries its own icon map and bell rule.

`should_forward_pushover` and `pushover_status_label` stay, with their tests: the relay uses both (C1, C6). `newsdesk` with no arguments keeps printing help. If a terminal view is ever missed, `newsdesk tail` (X6) is fifteen lines.

### 3.11 C11 — Changes to existing functions

The build reuses these, but not unchanged:

| Function | Change | Why |
|----------|--------|-----|
| `consume_local_queue` | Split into `claim_queue(path)` (merge into `.processing`, return entries, delete nothing) and `commit_queue(path)` (unlink). `consume_local_queue` becomes claim + commit and keeps its tests; while `watch` still exists it is the caller. The relay calls claim, writes history, then commits; it also claims and commits the spool files `queue.jsonl.in.*` | Commit-after-write (N4); push needs a claim that does not delete (C2) |
| `consume_remote_queue` | Returns `(entries, reached)`. The remote command ends with `; true`, so exit 0 means the box answered — today a missing queue file, a timeout and an ssh failure all look the same | `remotes` in the state, the `remotes=3/3` log field and Q3's check all need "reached" |
| `append_to_history` | Writes a temp file and `os.replace`s it, through one atomic-write helper shared with `relay.state` | Web threads now read the file while the relay rewrites it (W2) |
| `read_keychain_token` | Gains `account="pushover"` | K5's URL is stored under account `dave` |
| `forward_to_pushover` | Returns whether curl succeeded | `forward_failed` in the log and state (C1) |
| `send_notification` | Adds `id` (uuid4 hex) to each entry | Dedupe (D14) |
| remote-path helper | The `~` → `$HOME` substitution, inline in `consume_remote_queue` today, moves to one function used by it and by push | K1 follows the same rule |

## 4. Failure modes

| ID | Failure | Effect | Detected by | Recovery |
|----|---------|--------|-------------|----------|
| F1 | Relay process dies | Nothing forwarded; page shows red banner | launchd restarts within 10 s; Healthchecks fires if it keeps dying | Automatic |
| F2 | Mini off / Tailscale down | Nothing forwarded, page unreachable | **Healthchecks dead-man → Pushover directly** (HC has a native Pushover integration; it must not route through newsdesk) | Manual — but *known* |
| F3 | Box unreachable (Phase 2) | Its entries wait in its queue | hvac staleness alert (hvac W6) covers the box being dark; the queue drains when it returns, timestamped | Automatic |
| F4 | micro-m4 can't reach mini | Entries wait in micro-m4's `.processing` / queue, unrotated and with no age limit (D15) | The next send's push ships the backlog. If micro-m4 goes idle first, the backlog waits for the next send — delayed, never lost (D3) | Automatic on next send |
| F5 | Keychain locked / tokens missing on mini | History and page work, no Pushover | Relay logs once; page header shows `no keychain tokens`; HC pinged with `/fail` so it **pages** (D5) | Manual |
| F6 | Old `watch` on micro-m4 still polling the mini | Races the relay for the mini's queue; items it wins never hit the hub's history or Pushover | Nothing | **Deploy order S4 before S5** |
| F7 | Pushover rejects a message, or curl cannot reach it | Entry is in history, not on phone | `forward_failed=N` in `relay.log` and `forward_failed_total` in the state. Logged, not retried (#24) | Retry is out of scope here; next-steps R4 |
| F8 | Web server cannot bind (port taken, tailnet IP not up yet) | Page unreachable; relay keeps forwarding | Relay logs it and retries the bind every 10 s | Automatic |
| F11 | A push succeeds on the mini but its exit status is lost (lid closed, ssh timeout) | The batch ships twice | The relay drops entries whose `id` is already in history; `dupes=N` in the log | Automatic |
| F12 | A push is cut mid-transfer | Truncated bytes reach the mini | The remote byte-count check fails; the temp file is removed and the batch stays in micro-m4's `.processing`. A temp file orphaned by a killed remote shell is never claimed (the relay claims only `queue.jsonl.in.*`); the relay deletes `queue.jsonl.incoming.*` older than a day | Automatic on next send |
| F13 | A second `relay` is started by hand | It would double-write history and state | `relay.lock` is held; the second process exits 1 with a message | Automatic |
| F9 | SSE stream dropped by an idle proxy or a phone sleeping | Page misses entries while disconnected | `EventSource` reconnects; page refetches `since=` its highest seen `seq` (C4) | Automatic |
| F10 | `tailscale serve` not persisted after a reboot | HTTPS URL dead; relay fine | S9 checks `tailscale serve status`; fallback URL `http://100.70.51.21:5556` only if `web_bind` was set to the tailnet IP | Manual, once |

## 5. Deployment — Phase 1

### 5.1 Spikes — before S1

Six assumptions the design rests on cannot be settled by reading. Each spike is small and throwaway, and each can change the design, so all but SP6 run before any of S1 is written. None has been run.

| ID | Assumption | Spike | If it fails |
|----|------------|-------|-------------|
| SP1 | `tailscale serve` carries a long-lived SSE stream without buffering or idle disconnects | First `tailscale serve status` on the mini (is 443 already in use?) and confirm MagicDNS and HTTPS certificates are enabled in the admin console. Then a 20-line `http.server` on the mini emitting one event a minute and a comment every 15 s, behind `tailscale serve`. Watch from micro-m4 and the phone for 30 minutes: events arrive singly, the stream survives idle | D9 falls back to binding the tailnet IP (`web_bind`); N6 and K4 change |
| SP2 | A LaunchAgent on the headless mini can read the Keychain, and starts without anyone at the console | Needs the Pushover tokens in the mini's Keychain, so do S3's two token lines ahead of it. A throwaway plist that logs the exit code of `security find-generic-password … -w`, plus `python3 --version` and `echo $PATH`. Reboot the mini with nobody logged in at the screen | The relay cannot be a `gui/` agent as designed (C8); S9's criterion depends on the answer |
| SP3 | The detached push outlives the Claude Code hook that ran `send` | A temporary Stop hook that `Popen`s `sh -c 'sleep 20; date >> file'` with `start_new_session=True`; check the file. Repeat with `ssh -o BatchMode=yes mini true` as the child, to confirm ssh auth works from that context | C3 and D3 are reopened |
| SP4 | A `send` run from Claude Code's sandboxed Bash tool can spawn a working push | The same child as SP3, launched from a sandboxed Bash call. The child probably inherits the sandbox and cannot reach the mini; if so, confirm the failed push leaves `.processing` intact for the next hook-spawned push | Sandboxed sends rely on the next unsandboxed send to ship them — acceptable if nothing is lost; record it in F4 |
| SP5 | The byte-count check catches a cut transfer over real ssh | `(printf partial; sleep 5) \| ssh mini '<remote command with N too large>'`, kill the client mid-sleep, inspect the mini: no `queue.jsonl.in.*` file, no temp file. (The race against the relay's claim is covered locally by T4, not here) | C2's remote command is redesigned before S1 |
| SP6 | Healthchecks behaves as D5 assumes | On the new check: a never-pinged check does not alert, and `/fail` every 60 s produces one Pushover Emergency, not one per ping | D5's `/fail` cadence changes (ping `/fail` once per transition). Runs at S3, when the check exists |

### 5.2 Steps

Order matters because of F6. Each step is complete before the next starts. **`watch` keeps working until S12**, so until then the rollback from any failed step is: on micro-m4 remove `hub`, restore the `mini` entry in `remote_machines`, reopen `watch`; on the mini `launchctl bootout` the relay.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| S1 | micro-m4 | Implement C1–C9 and C11 with tests (§8), **leaving `watch` and its tests in place**; commit on branch `relay-hub`; push | `.venv/bin/python -m pytest tests/ -q` green: today's 72 plus the new tests |
| S2 | mini | `git fetch && git checkout relay-hub` in `~/Developer/newsdesk` (also retires the stale clone that lacks `--url`, #42) | `newsdesk send --help` shows `--url`; `newsdesk relay --help` exists |
| S3 | mini | Keychain: both Pushover tokens (`security add-generic-password -a pushover -s newsdesk-app-token -w <token>`, same for `newsdesk-user-key`) and `security add-generic-password -a dave -s newsdesk-hc-url -w <url>`. Config: set `pushover_min_priority` to 2 explicitly (K3); `web_bind`/`web_port` only if not defaults. Create the Healthchecks check `newsdesk-relay` (period 5 min, grace 5 min, Pushover integration); run SP6 | `newsdesk init` shows both token ticks. `newsdesk relay --once --no-pushover` prints its summary line and writes `relay.state` — `--no-pushover` because micro-m4's `watch` is still the live consumer of this queue |
| S4 | micro-m4 | Config: delete `mini` from `remote_machines`; add `hub`. **Quit the running `watch`.** Then one `newsdesk send "Push check" "S4" --priority 0`. From here until S5 completes, the mini's queue is unconsumed — minutes, and it is durable | `ssh mini 'ls ~/.local/share/newsdesk/'` shows one `queue.jsonl.in.*` file holding that entry — proof the detached push, the ssh auth and the remote command all work before the relay is involved |
| S5 | mini | `scripts/install-launchd.sh` | `launchctl list \| grep newsdesk`; `relay.log` shows the S4 entry consumed; `curl -s localhost:5556/api/state` returns JSON with `"healthchecks": true` and `pushover` `≥ 2`; the HC dashboard shows the check green within two minutes |
| S6 | mini | `tailscale serve --bg --https=443 http://127.0.0.1:5556` (prerequisites confirmed in SP1) | `tailscale serve status`; the HTTPS URL loads the page on micro-m4 **and on the phone** |
| S7 | mini | Real end-to-end with no terminal left open: from an ssh session, `(sleep 60; newsdesk send "Relay test" "priority 2 via hub" --priority 2) &`, then close the session | Phone buzzes (emergency; acknowledge it). Row appears on the open page within 2 s of the relay consuming it, without reload |
| S8 | micro-m4 | `newsdesk send "Push test" "from micro-m4" --priority 0`; then five sends in a loop | Row appears on the page within ~5 s with machine `micro-m4`; all five burst rows appear, each once |
| S9 | mini | `sudo reboot` (⚠️ disrupts OpenBrain / hvac services for ~2 min — Dave's call on timing) | Relay running with no action at the mini beyond what SP2 established is needed; HC never went red; `tailscale serve status` still lists the route; page loads |
| S10 | phone | Lock the phone with the page open for 10 min; send from micro-m4 meanwhile; unlock | Row is there on unlock (F9 recovery) |
| S11 | — | Kill the relay (`launchctl bootout`, then confirm no process) | Phone gets the Healthchecks alert within 12 min (period 5 + grace 5 + margin). Reinstall |
| S12 | micro-m4, then mini | Remove the TUI (C10) on `relay-hub`; push. On the mini: `git pull`, `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` | Suite green at 45 surviving tests plus the new ones; `newsdesk watch` no longer exists; the page still updates after the kickstart |
| S13 | — | Merge `relay-hub` to main. On the mini: `git checkout main && git pull`, kickstart the relay again. Docs (O4); register 5556 (O5) | `git status` on the mini shows `main`, up to date; page loads |

## 6. Deployment — Phase 2 (deferred; planned only)

Prerequisites: hvac plan W3/W7 landed, so `newsdesk` is installed on each box and its units call `newsdesk send`.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| Q0 | mini | Measure a cold `ssh <box> true` to each box, timed from a LaunchAgent (this is also where Local Network Privacy shows up, R6). `consume_remote_queue` gives up after `SSH_COMMAND_TIMEOUT` = 3 s, and its remote command deletes the box's batch before the output is back — a timeout that lands in between loses the batch (review RK3). If a cold connection can exceed 3 s, raise the timeout for the relay's 30 s cadence, or change the pull to read first and delete on a second, acknowledging command | Three timings recorded; the timeout decision made before Q3 |
| Q1 | boxes | Confirm which user the box's systemd units send as — the queue path in K2 must be that user's `~/.local/share/newsdesk/queue.jsonl` (root's home is `/root`) | `ls -la` the queue after a unit has sent |
| Q2 | mini | `~/.ssh/config` alias per box with matching `User`; one manual `ssh <box> true` each to populate `known_hosts` — BatchMode refuses unknown hosts **silently** (the poll just returns nothing) | `ssh -o BatchMode=yes <box> true` exits 0 |
| Q3 | mini | Add the three `remote_machines` entries; `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` (config is read at startup) | `relay.log` shows `remotes=3/3`; page header shows `remotes 3/3` |
| Q4 | a box | `newsdesk send "Pull test" "from lake-agent-1" --priority 0` | Row on the page within 30 s with the box's machine name |
| Q5 | hvac_monitor | Amend hvac W7 (O1) | — |

Verify-don't-assume at Q3: launchd on the mini reaching Tailscale peers under Local Network Privacy (#698 was LAN; 100.x should be exempt — confirm on first run).

## 7. Verification criteria (falsifiable)

| ID | Claim | Test |
|----|-------|------|
| V1 | The relay survives a reboot of the mini | S9 |
| V2 | A relay that stops is reported to the phone without newsdesk's help | S11 |
| V3 | A priority-2 reaches the phone with no terminal window open anywhere | S7 (delayed send from a session that is then closed) |
| V4 | The hub being unreachable does not slow `send` | With Tailscale off on micro-m4, the median of 10 `newsdesk send x y` runs is no more than 20 ms above the median with no `hub` configured. (A bare `./newsdesk` already takes about 55 ms, so an absolute figure would measure Python startup, not this change) |
| V5 | No entry is lost or torn when a push is interrupted mid-transfer | T4 (truncated input leaves the hub untouched and exits non-zero), T3 (the batch is still in `.processing`), and SP5 over real ssh |
| V6 | Two consumers can no longer exist | After S12, `claim_queue` and `consume_remote_queue` are each called from exactly one place that feeds history: the relay cycle. `push` claims micro-m4's own queue only to ship it |
| V7 | A new entry is on the phone's page within 2 s of the relay consuming it, with no reload | S7 |
| V8 | A page that was disconnected shows what it missed | S10 |
| V9 | The mini has no new all-interfaces listener | `lsof -nP -iTCP -sTCP:LISTEN` on the mini shows 5556 on `127.0.0.1` only |
| V10 | Nothing is lost or doubled under concurrency | 500 sends on micro-m4 with distinct titles in a tight loop while the relay runs; the hub's history then holds exactly 500 of those titles, none twice |

## 8. Tests (TDD — written first)

Cycle tests call `relay_cycle(state, now)` with an explicit `now`; nothing sleeps and nothing mocks `time`.

| ID | Test | Pins |
|----|------|------|
| T1 | `test_relay_cycle_consumes_and_forwards` | One cycle: local queue and a spool file → history, each entry with a `seq`; forward called only for entries ≥ threshold and ≠ −2; `.processing` and the spool file gone only after history is written. `--once` prints the summary for an empty cycle, writes state, spawns no ping |
| T2 | `test_relay_remote_cadence` | Remotes pulled on the first cycle and again only after `REMOTE_POLL_INTERVAL_S`; `remote_machines = []` pulls nothing; an unreached remote leaves its state entry unchanged |
| T3 | `test_push_recovery` | (a) ssh fails → `.processing` holds the batch. (b) `.processing` exists, new sends land in the queue, ssh fails again → every entry is still on disk across the two files. (c) A `.processing` older than `STALE_PROCESSING_AGE` is still shipped. (d) Success after two failures ships each entry once and leaves no file. (e) An ssh that exceeds `PUSH_SSH_TIMEOUT_S` counts as a failure |
| T4 | `test_remote_command_behaviour` | The remote command, built by its function, run under `sh -c` against a `tmp_path` queue — no ssh, no mocks. Full input → one `queue.jsonl.in.*` file with exactly the bytes sent, no temp file. Byte-count mismatch → no spool file, no temp file, non-zero exit, `queue.jsonl` untouched. A loop doing claim-and-commit concurrently with 1,000 runs loses no line and tears none |
| T5 | `test_push_drains_under_lock` | An entry appended while a push is mid-ship is shipped by the same push's next loop iteration; a second push started concurrently exits having shipped nothing; a push that cannot get the lock within `PUSH_LOCK_TIMEOUT_S` exits 0 |
| T6 | `test_send_spawns_push_only_with_hub` | `Popen` called iff `config.get("hub")`, with `start_new_session=True` and `[sys.executable, <newsdesk.py>, "push"]`; a `Popen` that raises leaves the entry queued and `send` returns 0; every entry carries an `id`; rotation is skipped iff `hub` is set |
| T7 | `test_history_since_and_limit` | W2's filter: `since` excludes `seq <= since`; `limit` keeps the newest; an entry with an old `ts` but a new `seq` is returned |
| T8 | `test_web_routes` | Real `ThreadingHTTPServer` on an ephemeral port: `/` serves the file with `no-store` and contains the element ids the page script depends on; `/api/state` is JSON; unknown path is 404; a second bind on a taken port retries without raising into the loop |
| T9 | `test_sse_fanout_and_keepalive` | A subscriber receives `event: entry` for a consumed entry, `event: state` on a heartbeat, and a `: keepalive` (with `SSE_KEEPALIVE_S` monkeypatched small); a subscriber whose write fails, or whose queue is full, is dropped |
| T10 | `test_heartbeat_pings_when_url_present` | curl spawned iff URL; none before `HEARTBEAT_INTERVAL_S` of uptime; `/fail` suffix when tokens missing, never under `--no-pushover`; spawned detached and not waited on (a mock curl that never returns does not delay the next cycle); no ping from a cycle that raised before completing; missing tokens are re-read on the next heartbeat |
| T11 | Existing tests | All 72 stay green through S1–S11 (`consume_local_queue` keeps its behaviour as claim + commit). At S12, 27 TUI-only tests go with the TUI and 45 remain (D8) |
| T12 | `test_relay_dedupes_by_id` | An entry whose `id` is already in history is not written, fanned out or forwarded again; an entry with no `id` passes through; a batch replayed after a crash between history write and commit adds nothing |
| T13 | `test_history_write_is_atomic` | `append_to_history` goes through the atomic-write helper (temp file, `os.replace`); a reader during the write sees the old file or the new one; the cap is `HISTORY_MAX_ENTRIES` |
| T14 | `test_relay_survives_bad_entries` | A forward that raises, a forward that returns failure (counted in `forward_failed`), and an entry with a non-integer priority each leave the cycle running and the other entries handled |
| T15 | `test_relay_single_instance` | A second relay started while `relay.lock` is held exits 1 |
| T16 | `test_consume_remote_reports_reached` | `(entries, True)` for an answering host with no queue file; `(…, False)` on ssh failure and on timeout |
| T17 | `test_read_keychain_token_account` | The `account` argument reaches the `security` command; the default is `pushover` |

## 9. Decisions

| ID | Question | Recommendation | Status |
|----|----------|----------------|--------|
| D1 | Does any consume path survive outside `relay`? | **No, once S12 removes `watch`.** One consumer, one code path. `push` claims micro-m4's own queue to ship it, which is not consuming (V6). | Open |
| D2 | ~~Viewer on micro-m4: `ssh -t mini` or `--hub`?~~ | Superseded by the web viewer (C4/C5). | Moot |
| D3 | Push trigger: spawn-on-send, launchd backstop, or both? | **Spawn only.** Every send ships; no launchd on micro-m4. The backstop covered only "mini unreachable at send time, then micro-m4 idle" — and even then the entry ships on the next send. Not worth a plist. | **Decided 2026-09-19 (Dave)** |
| D4 | Relay remote cadence | 30 s. Box messages are boot/update/throttle reports; nothing there needs 2 s. Ships inert in Phase 1. | Open |
| D5 | Relay pings Healthchecks `/fail` when Keychain tokens are missing? | **Yes.** It turns F5 from silent into paged, at the cost of three lines. | **Decided 2026-10-02 (Dave)** |
| D6 | Keep `--no-pushover` on `relay`? | Yes — it is the only session-level escape and costs nothing. | Open |
| D7 | Branch | `relay-hub`; the mini runs the branch from S2, and it is merged to main at S13, after the TUI removal. | Open |
| D8 | Remove the curses TUI, with its tests (C10)? And when? | **Yes, at S12 — after the relay has passed S11, not during the build.** Two viewers is two things to keep at parity, and the page reaches strictly more places. Keeping the TUI as a viewer of the hub was considered and is not in this plan: today's `watch` consumes queues and forwards, so it would have to be rewritten as a read-only client of the hub's page API, about 250 lines of curses kept at parity with the page for a view the browser already gives. `newsdesk tail` (X6) covers a terminal glance if it is ever missed. This deletes 27 of today's 72 tests (nine classes) and about half of `newsdesk.py`. | **Decided 2026-10-02 (Dave): remove, after S11** |
| D9 | Exposure: `tailscale serve` (loopback bind, tailnet HTTPS, hostname) or bind the tailnet IP on 5556 (plain http)? | **`tailscale serve` first.** No new all-interfaces listener, a real URL, HTTPS. Fallback is one config key (`web_bind`). Untried on this tailnet; SP1 settles it before anything is built. | Open — pending SP1 |
| D10 | Row order on the page | **Newest first, by arrival at the hub (`seq`).** The phone case decides it; arrival order keeps a late backlog from sorting into the past where nobody looks. | Open |
| D11 | Web server inside the relay process, or a separate `newsdesk web`? | **Inside.** The relay has each entry in hand the instant it is consumed, so SSE is zero-latency with no file watching; one plist, one log, one process to keep alive. The thread is fenced (F8). | Open |
| D12 | **`pushover_min_priority` on the hub once it relays 24/7 (K3): keep 2, or lower to 1?** Added 2026-10-02 from hvac_monitor's Lake House freeze watch (`docs/freeze-watch-plan.md` §9 N6), for Dave to revisit at the cutover. | **Decide at S4/S5 with these facts.** At **2** (today) only emergencies page: the freeze watch's first stages — Lake house dark at 90 min, Nest offline at 30 min, can't see the Nest, collector down, Longmont offline, Longmont on battery with > 2 h of runway — stay on the page and in the hvac check-in, and the phone hears a Lake power cut at 4 h (if Gaylord < 40 °F) and a furnace that lost 24 V at 2 h 15 m. At **1** those first stages page too, ~2½ h and ~1½ h sooner. ⚠️ The threshold is global: `~/.claude/hooks/newsdesk-notify.sh` sends **every Claude Code permission prompt at priority 1**, so at 1 each one from every session pages the phone; backup-migration, Memex and PostCardMaker choose per message. A per-project threshold (or the hook at 0) would remove the trade-off. Related: **F7** — the relay's fire-and-forget forwarding loses an alarm raised during a Longmont internet blip; the freeze watch carries a second path for its emergencies through healthchecks.io (`HEALTHCHECKS_FREEZE_ALARM_URL`). | **Open — Dave, at the cutover** |
| D13 | Healthchecks on any machine other than the mini? | **No — one check, `newsdesk-relay`, on the hub.** micro-m4 sleeps, so a dead-man there pages for nothing; a failed push loses nothing (F4); and a periodic ping would need the launchd job D3 rejected. The boxes are pulled and carry no outbound path or daemon (P1); a dark box is reported by hvac_monitor's staleness alert (hvac W6, F3) — the relay only records when each box was last reached (C6) and raises nothing itself — and hvac_monitor's freeze watch covers the urgent case with its own Healthchecks path. A dead mini pages twice if the backup-monitor check (R5) is live — accepted: one says the mini is gone, the other says the relay is. | **Decided 2026-10-02 (Dave)** |
| D14 | A push whose success reply is lost ships its batch again. Dedupe, or accept duplicates? | **Dedupe.** `send` adds an `id` to each entry; the relay drops ids already in history (N4, T12). Otherwise an emergency can page twice. | **Decided 2026-10-02 (Dave)** |
| D15 | micro-m4's queue is rotated to the newest 100 once it passes 200 entries. During a long mini outage, cap the backlog or keep everything? | **Keep everything: no rotation on a machine with a `hub`.** The queue grows without bound while the mini is unreachable; push drains it when the mini returns. (The review recommended accepting the cap; Dave chose no loss.) | **Decided 2026-10-02 (Dave)** |
| D16 | `HISTORY_MAX_ENTRIES` on the hub | **5000, up from 1000.** It is the page's whole reachable past, now fed by every machine. The file stays under about 2 MB and the whole-file rewrite per append stays cheap. | **Decided 2026-10-02 (Dave)** |

## 10. Deferred / out of scope

| ID | Item | Why not now |
|----|------|-------------|
| X1 | Phase 2 box pull | Dave's call — planned in §6, no Pi-side work yet |
| X2 | SSH backoff for unreachable remotes (next-steps R2) | 30 s cadence × 3 s `SSH_COMMAND_TIMEOUT` × 3 boxes is bounded at 9 s per cycle; tolerable |
| X3 | Retrying or alerting on a failed Pushover forward (#24, next-steps R4) | The relay now logs and counts failures (C1, F7); acting on them is a separate fix |
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
| O4 | `README.md`, `CLAUDE.md`, `docs/newsdesk-plan.md` | New subcommands, the relay/viewer split, launchd install, the page URL; drop `watch`. Also: `CLAUDE.md`'s "single-file CLI", "no server" and "curses for watch UI" statements and `newsdesk.py`'s two ABOUTME lines become false; the config path in `README.md` and `CLAUDE.md` is already wrong (`~/.config/newsdesk/config.json` is what the code uses); the test command there needs `.venv/bin/python`. `web/index.html`, the plist and the install script each get ABOUTME headers | 1 |
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
