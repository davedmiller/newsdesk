*Last updated: 2026-10-02 12:22 MDT*

# Newsdesk — Hub-on-the-Mini Relay + Web Viewer

**Status: DRAFT for study. Nothing built.** Phase 1 (relay, push, web viewer) is the work to do now. Phase 2 (pull from the Lewiston boxes) is deferred — no Pi-side work until Dave says so — but its code ships and is tested in Phase 1, so that Phase 2 is config and verification only (Appendix A). All decisions in §9 are made except D12, which waits for the cutover.

§3, §5 and §8 are the build spec. Rationale that is not needed to build sits in the appendices. The plan was reviewed and assessed on 2026-10-02; Appendix C says what each changed.

## 1. Problem

Today the Pushover relay is the interactive `newsdesk watch` TUI on micro-m4 — `forward_to_pushover` has one call site, inside `cmd_watch_curses`. Nothing reaches a phone unless that iTerm window is open. During the Sept 2026 Lewiston absence the alerting path for two Michigan houses was one terminal window for fifteen days (OpenBrain #42). The window survives neither a reboot nor a closed lid, and micro-m4 is the machine that reboots, sleeps, and travels.

A second, smaller problem: the only way to *see* notification history is a curses TUI in a terminal on a Mac. From a phone, or from any machine without newsdesk installed, there is no view at all.

Four things change together:

1. The relay lives on the always-on machine (micro-mac-mini), launchd-managed, not a TUI.
2. Senders get entries to the mini in whichever direction the existing trust already runs.
3. The viewer is a web page served by the relay, readable from any browser on the tailnet. The curses TUI is removed once the relay is proven.
4. The relay is observed by something that does not depend on the relay working.

## 2. Design

### 2.1 Roles per machine

| ID | Machine | Role | Runs |
|----|---------|------|------|
| M1 | micro-mac-mini (`davidmiller`) | **Hub.** Sole consumer of every queue; sole Pushover forwarder; owns the unified history; serves the viewer | `newsdesk relay` under launchd (KeepAlive), which also hosts the web server |
| M2 | micro-m4 (`dave`) | **Pushing sender.** Writes locally, ships its queue to the hub | `newsdesk send` (unchanged callers) → spawns a detached `newsdesk push`. No launchd on this machine |
| M3 | Lewiston agent boxes ×3 (`tag:agent`) — **Phase 2** | **Pulled sender.** Writes locally, nothing else | `newsdesk send` only (hvac plan W7). No daemon, no key, no outbound path |
| M4 | Any browser on the tailnet — laptop, phone | **Viewer.** Reads the hub's history | `https://micro-mac-mini.tailbf38e2.ts.net` (`http://100.70.51.21:5556` answers only if `web_bind` is set to the tailnet IP, F10) |

### 2.2 Data flow

```
micro-m4                          micro-mac-mini                          phone / any browser
────────                          ──────────────                          ───────────────────
send ──▶ queue.jsonl
           │ push (ssh mini: stage, rename into a spool file)
           └──────────────────▶ queue.jsonl.in.* ──┐
                                                   │
agent box ×3  (Phase 2)                            ▼
──────────                                   relay (launchd)
send ──▶ queue.jsonl ◀── ssh pull + ack ──── every 30 s
                                                   │
                                                   ├─▶ history.jsonl ◀── web thread ◀── poll ── 🌐 viewer page
                                                   ├─▶ Pushover (≥ threshold, retried) ───────▶ 📱
                                                   ├─▶ relay.state
                                                   └─▶ Healthchecks.io ping (every 60 s)
                                                               │
                                                    dead-man fires → Pushover ────────────────▶ 📱
```

### 2.3 Direction per sender

| ID | Sender | Direction | Reason | Phase |
|----|--------|-----------|--------|-------|
| P1 | Agent boxes → mini | **Mini pulls** | Matches hvac #821: a pulled box has no outbound path into the tailnet, holds no key to the mini, keeps no delivery state, runs no push daemon. The grant `autogroup:member → tag:agent` (all ports) plus the Tailscale SSH `accept` rule already lets the mini in. Cost: one `remote_machines` entry per box. | **2** |
| P2 | micro-m4 → mini | **micro-m4 pushes** | The mini cannot reach micro-m4 (no Remote Login — deliberate, #30), and micro-m4 is the intermittent machine, so pulling from it would mostly hit a sleeping laptop. micro-m4 → mini SSH already exists. | 1 |
| P3 | Mini's own jobs → mini | Local | Already the case. | 1 |

Direction follows existing trust, per sender, rather than being uniform.

### 2.4 Invariants

| ID | Invariant | How |
|----|-----------|-----|
| N1 | `send` has zero network dependency (#39) | Local append first, always. The push is a *detached* child; `send` returns before it connects. |
| N2 | No Pushover secret leaves the hub | Keychain read only in `relay`, only on the mini. `push` carries entries, never tokens. |
| N3 | Nothing unshipped is ever deleted | Push keeps `.processing` and the queue on disk through any number of failures, with no age limit, and rotation is off on a machine that has a `hub` (D15). The relay deletes a box's batch only after writing it to history (C11). The cost is a queue that grows for as long as the hub is unreachable. |
| N4 | Each entry reaches history once | One consumer (the relay). Transport is at-least-once in both directions — a batch whose acknowledgement is lost is delivered again — so every entry carries an `id` and the relay drops ids it has already accepted (D14). Entries with no `id` (a sender on older code) pass through undeduped. |
| N5 | stdlib only, Python 3.9 | The mini runs `/usr/bin/python3` 3.9.6 (SP2), so nothing newer than 3.9 syntax or library. `subprocess` for ssh and curl; `http.server` + `threading` for the viewer. No framework, no build step. |
| N6 | The mini's listening surface stays deliberate (#101 audit) | Web server binds loopback; `tailscale serve` exposes it tailnet-only over HTTPS (D9). Fallback binds the tailnet IP, never `0.0.0.0`. |

### 2.5 Phasing

| Phase | Scope | Code | Config / deploy |
|-------|-------|------|-----------------|
| **1 — now** | Relay on the mini; push from micro-m4; web viewer; Healthchecks; curses TUI removed once the relay is proven | C1–C11, including the box-pull code | SP1–SP6, then S1–S13 |
| **2 — deferred** | Pull from the three boxes | **None.** The pull and its acknowledgement (C11) and the relay's remote cadence (C1) ship in Phase 1 with `remote_machines = []`, inert and tested (T2, T16) | Appendix A, Q0–Q5 |

## 3. Components

### 3.1 C1 — `newsdesk relay`

Headless loop for launchd. Replaces the consume/forward half of today's `watch`, and hosts the web server (C4).

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk relay [--once] [--no-pushover]` |
| Shape | The loop calls a step function, `relay_cycle(state, now)`, so tests drive cycles and the clock directly |
| Single instance | Non-blocking `flock` on `~/.local/share/newsdesk/relay.lock` at startup; if held, print a message and exit 1 |
| Seen ids | A set seeded at startup from the ids in `history.jsonl`. An id is added the moment its entry is accepted, so a duplicate later in the same cycle is caught too |
| Each cycle (every `POLL_INTERVAL_S` = 2 s) | 1. `claim_queue` the local queue and every spool file `queue.jsonl.in.*` (C2, C11). 2. Drop entries whose `id` is in the seen set; add the rest to it. 3. `append_to_history`. 4. Commit the claim (delete `.processing` and the spool files). 5. `forward_to_pushover` for entries passing `should_forward_pushover(entry, pushover_min_priority)`. 6. Retry due failed forwards (below). Commit comes after the history write, so a crash between them replays into step 2, not into a loss |
| Every `REMOTE_POLL_INTERVAL_S` = 30 s | `pull_remote_queue` for each `remote_machines` entry; its entries join the cycle at step 2, and `ack_remote_queue` runs after step 3 (C11). Phase 1 has no remotes |
| Forwarding and retry | `forward_to_pushover` returns whether curl succeeded. A failed forward goes on an in-memory retry list and is tried again every `FORWARD_RETRY_INTERVAL_S` until it succeeds or is `FORWARD_RETRY_EXPIRE_S` old, then logged as expired. The list does not survive a relay restart. A forward that timed out after Pushover accepted it can page twice — accepted over losing it |
| Every `HEARTBEAT_INTERVAL_S` = 60 s | Write `relay.state` (C6). Ping Healthchecks if a URL is configured: `/fail` suffix when Keychain tokens are missing (D5), never under `--no-pushover`. The ping is issued by the loop itself at the end of a completed cycle, the first one only after `HEARTBEAT_INTERVAL_S` of uptime, and is fire-and-forget: `Popen(["curl", "-fsS", "-m", "10", url], start_new_session=True)`, output to `/dev/null`, not waited on. Also delete `queue.jsonl.incoming.*` temp files older than a day (F12) |
| Web server | Started in a daemon thread at startup (not under `--once`). The one realistic failure is the bind; the thread retries it every 10 s. It can never take the relay loop down |
| Tokens | Read from Keychain at startup. Missing → log once, keep relaying, and retry the read on each heartbeat until they appear |
| Logging | One line per cycle that did anything, flushed per line: `2026-09-19T07:28:00 consumed=3 dupes=0 forwarded=1 forward_failed=0 retrying=0 remotes=0/0` to stdout (launchd routes to `relay.log`). Silent cycles log nothing |
| `--once` | One full cycle including remotes, then exit 0. No web server, no Healthchecks ping; always prints the summary line and writes `relay.state` |
| Robustness | Per-entry try/except around forward. The loop must not die on a bad line, an entry with a non-integer priority, a bad host, or a Pushover 4xx |

### 3.2 C2 — `newsdesk push`

Ships the local queue to the hub. It never appends to the hub's `queue.jsonl`; it drops a complete file beside it for the relay to claim.

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk push` — reads `config.get("hub")`; exits 0 silently if no hub configured. Spawned by every `send` (C3); never scheduled |
| Lock | Exclusive `fcntl.flock` on `queue.jsonl.push.lock`, non-blocking: if it is held, exit 0. The holder loops until the queue is empty, so it ships what the later sends wrote. No two pushes ever touch `.processing` at once |
| Steps, under the lock | 1. **If `queue.jsonl.processing` exists, ship it as it stands** (step 3), whatever its age. On success delete it; on failure release and exit, touching nothing else. 2. Rename `queue.jsonl` → `queue.jsonl.processing`; if there was no queue, release and exit 0. 3. Ship: run the remote command with the file's bytes on stdin. 4. On exit 0: delete `.processing`, go to 2. On any failure or timeout: leave `.processing` in place, release, exit — the next send's push starts at step 1 |
| On disk after a failure | Everything not confirmed shipped: the failed batch in `.processing`, later sends in `queue.jsonl`. Nothing is held only in memory, `.processing` is never overwritten by a rename, and the 1-day stale rule of `consume_local_queue` does **not** apply |
| Remote command | Built by one function so the test can run it under `sh -c` with no ssh (T4). With `ID` a fresh uuid: `f=<queue>; t="$f.incoming.ID"; cat > "$t" && mv "$t" "$f.in.ID"` |
| ssh | `ssh -o BatchMode=yes -o ConnectTimeout=2 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 <hub> '<remote command>'`, run with `timeout=PUSH_SSH_TIMEOUT_S` |
| Why a spool file | Appending to the hub's queue opens the file and writes a moment later; the relay's rename can fall in that gap and lose the batch. `mv` into a uniquely named file is atomic — the relay sees the whole file or none |
| A cut transfer | The remote `cat` cannot tell a dropped connection from end of input, so a truncated batch can land as a spool file. Its last line is torn and `parse_jsonl` skips it; the complete lines are accepted. micro-m4 saw the ssh fail, so it ships the whole batch again, and the seen set drops the lines that already arrived (F12) |
| Backlog after an outage | Entries wait in `.processing`/queue until the *next send* spawns a push. There is no timer (D3), and the queue is not rotated while it waits (D15) |

### 3.3 C3 — `send` spawns a push

After the local append, if `config.get("hub")` is set: `subprocess.Popen([sys.executable, os.path.abspath(__file__), "push"], start_new_session=True, stdin/stdout/stderr=DEVNULL)`. Fire-and-forget; a Claude Code hook that ran `send` is not blocked and cannot kill the child (SP3 proves this first).

- It goes in `cmd_send`, which has the config, inside its own try/except: a `Popen` that raises leaves the entry queued and `send` returns 0.
- `send_notification` adds an `id` (`uuid4` hex) to every entry (D14).
- On a machine with a `hub`, `send` skips `_maybe_rotate` (D15).
- The child is addressed by interpreter and file path because hooks do not source `.zshrc`.

This is the only push trigger (D3). A synchronous ssh inside `send` would cost 0.5–2 s per Claude Code hook, and the full connect timeout whenever the mini is down.

### 3.4 C4 — Web viewer, server side

A `ThreadingHTTPServer` (`daemon_threads = True`) in a daemon thread inside the relay.

| ID | Route | Returns | Notes |
|----|-------|---------|-------|
| W1 | `GET /` | `web/index.html` | Located relative to `newsdesk.py`. Read from disk per request, `Cache-Control: no-store` — edit the page, reload, no relay restart |
| W2 | `GET /api/feed?limit=<n>` | `{"state": {…}, "entries": […]}` | `state` is the relay state (C6), from memory. `entries` is the newest `limit` lines of `history.jsonl` (default `WEB_FEED_ENTRIES`), oldest → newest. File order is the order the hub accepted them. Safe against a concurrent history write because that write is a temp file plus `os.replace` (C11) |
| W3 | anything else | 404 | No POST, no auth. Tailnet-only reachability *is* the auth, as for 5555/8766/8767 |

There is no stream. The page polls W2 (C5). The web threads share nothing with the relay loop except a reference to the state dict, which the loop replaces whole on each heartbeat.

Bind: `web_bind` (default `127.0.0.1`) and `web_port` (default 5556). Loopback plus `tailscale serve` is the recommended exposure (D9).

### 3.5 C5 — Web viewer, the page

One static file, `web/index.html`, vanilla JS, no build, mobile-first. It fetches W2 every `POLL_MS` (3000, a constant in the page) and re-renders. Titles and messages come from arbitrary senders, so the page sets them with `textContent` only, and renders a link only for an `http:` or `https:` url.

| ID | Curses today | Page | Notes |
|----|--------------|------|-------|
| U1 | `L` latest / `H` history | One list, **newest first** in the order the hub accepted them (D10); each row shows the sender's own time. Entries whose `id` the page had not seen on an earlier poll are highlighted and counted in the tab title: `(3) newsdesk` | A late backlog shows up at the top, where it will be seen |
| U2 | `C` clear | "Mark read" — clears highlights and the title count | Client-side only |
| U3 | `S` save snapshot | Link to `/api/feed?limit=5000` | The JSON *is* the snapshot |
| U4 | `B` bell threshold | 🔔 toggle + threshold select (≥1 / ≥0 / ≥−1 / off), persisted in `localStorage`. Beeps for a newly seen entry at or above the threshold | Browsers block audio until one user gesture — the toggle *is* the gesture, then `AudioContext` beeps |
| U5 | `V` show silent | Checkbox, persisted | Priority −2 hidden by default, as today |
| U6 | `?` help pages | Priority legend in a collapsible footer | — |
| U7 | Header status line | `relay 3s ago · pushover ≥ 2 · remotes 0/0`, plus `healthchecks off` and `retrying N` when the state says so. **Red banner** when a poll fails or the state is older than `RELAY_STALE_AFTER_S` | A viewer that cannot tell the relay is dead is the same hole as today |
| U8 | `🔗` link marker | The url rendered as a link, labelled with `url_title` | — |
| U9 | — | Filter box matching project / machine / title / message | Free on the client |
| U10 | — | Each row: local time, project, machine, priority icon, title — message | `machine` distinguishes senders in a unified feed |

Nothing is stored server-side by the page. A page that slept (a locked phone) simply catches up on its next poll, as long as fewer than `WEB_FEED_ENTRIES` entries arrived meanwhile.

### 3.6 C6 — Relay state

Held in memory, served inside W2, and written to `~/.local/share/newsdesk/relay.state` (JSON, atomic write) on every heartbeat and by `--once`:

```json
{"last_cycle_ts": 1758281280.1, "started_ts": 1758200000.0, "pushover": "≥ 2", "healthchecks": true, "remotes": {}, "forwarded_total": 14, "forward_failed_total": 0, "retrying": 0}
```

- `pushover` is the string from `pushover_status_label` (`≥ 2`, `no keychain tokens`, or `off (--no-pushover)`), which is why that helper stays (C10).
- `healthchecks` is false when no URL is in the Keychain — a check that has never been pinged does not alert, so the page says so.
- `remotes` maps host → last time the box was reached (`null` = never since start). Empty in Phase 1.

### 3.7 C7 — Config

| ID | Key | micro-m4 | mini | box (Phase 2) | Notes |
|----|-----|----------|------|---------------|-------|
| K1 | `hub` | `{"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}` | absent | absent | Presence turns on C3. `~` is remote — same rule as `remote_machines` |
| K2 | `remote_machines` | `[]` (**remove** the `mini` entry) | `[]` in Phase 1; one per box in Phase 2 | `[]` | `host` is an `~/.ssh/config` alias on the mini |
| K3 | `pushover_min_priority` | ignored | **`2`, set explicitly at S3** | ignored | The code default is −1, which on the hub would page the phone for every turn-complete ping. D12 revisits the value at the cutover |
| K4 | `web_bind`, `web_port` | ignored | `"127.0.0.1"`, `5556` | ignored | Added to `DEFAULT_CONFIG` |
| K5 | Healthchecks URL | — | Keychain `newsdesk-hc-url`, account `dave` | — | The healthchecks.io account exists (davedmiller79@gmail.com, default project, email and Pushover subscribed: down = Emergency, up = Normal); S3 adds a check `newsdesk-relay` to it |
| K6 | Pushover tokens | no longer read | Keychain `newsdesk-app-token`, `newsdesk-user-key`, account `pushover` | — | Today they are set up on micro-m4. The mini needs its own copies (S3) |

### 3.8 C8 — launchd and exposure

| ID | File / command | Machine | Key settings |
|----|----------------|---------|--------------|
| L1 | `launchd/com.dave.newsdesk-relay.plist` | mini | A template with the literal token `__HOME__`. `KeepAlive true`, `RunAtLoad true`, `ThrottleInterval 10`, `StandardOutPath`/`StandardErrorPath` → `~/.local/share/newsdesk/relay.log`, `ProgramArguments` = absolute path to `~/bin/newsdesk relay`. `EnvironmentVariables`: `PYTHONUNBUFFERED=1` (no `PATH` entry needed: the mini has one `python3`, in `/usr/bin`, SP2) |
| L2 | `scripts/install-launchd.sh` | mini | Replaces `__HOME__` with `$HOME`, writes to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u)`. Idempotent (bootout first if loaded) |
| L3 | `tailscale serve --bg --https=443 http://127.0.0.1:5556` | mini, once | Tailnet-only HTTPS at `https://micro-mac-mini.tailbf38e2.ts.net`. Needs MagicDNS + HTTPS certs enabled in the admin console (SP1) |

The relay is a user agent (`gui/` domain), not a system daemon — it needs the login Keychain, which SP2 showed a LaunchAgent can read and an ssh session cannot.

### 3.9 C9 — Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `REMOTE_POLL_INTERVAL_S` | 30 | Relay pull cadence for `remote_machines` |
| `REMOTE_PULL_TIMEOUT_S` | 15 | Overall bound on one box pull. Safe to be generous: a timeout loses nothing (C11) |
| `HEARTBEAT_INTERVAL_S` | 60 | State file + Healthchecks ping cadence |
| `RELAY_STALE_AFTER_S` | 180 | Page shows the red banner past this |
| `PUSH_SSH_TIMEOUT_S` | 20 | Overall bound on one push's ssh |
| `FORWARD_RETRY_INTERVAL_S` | 30 | Gap between attempts for a failed Pushover forward |
| `FORWARD_RETRY_EXPIRE_S` | 3600 | A failed forward older than this is dropped and logged |
| `WEB_PORT` | 5556 | Default `web_port` |
| `WEB_FEED_ENTRIES` | 200 | Default `limit` for W2 |
| `HISTORY_MAX_ENTRIES` | 5000 (was 1000) | D16 |

### 3.10 C10 — Removed with the curses TUI, at S12

**Not part of the build.** The TUI stays, working, until the relay has passed S11 (D8). `~/bin/newsdesk` on micro-m4 runs the working tree, so deleting `watch` earlier would leave no way to reopen the only proven relay.

Deleted at S12, with their tests: `cmd_watch`, `cmd_watch_curses`, the `watch` subparser, `import curses`, `consume_remote_queue` (its only caller is `watch`; the relay uses C11's pull), and the helpers only the TUI used — `format_entry`, `_fit_field`, `should_display`, `priority_icon` / `PRIORITY_ICONS`, `should_bell`, `cycle_bell_threshold`, `bell_threshold_label`, `DISPLAY_FIELD_WIDTH`, `LINK_MARKER`, `DEFAULT_BELL_THRESHOLD`, `BELL_THRESHOLD_CYCLE`. About half of today's 731 lines and 27 of today's 72 tests (nine classes).

`should_forward_pushover` and `pushover_status_label` stay, with their tests.

### 3.11 C11 — Existing functions that change, and the box pull

| Function | Change | Why |
|----------|--------|-----|
| `consume_local_queue` | Split into `claim_queue(path)` (merge into `.processing`, return entries, delete nothing) and `commit_queue(path)` (unlink). `consume_local_queue` becomes claim + commit and keeps its tests and its stale rule; `watch` is its caller until S12 | The relay commits after the history write; push needs a claim that does not delete |
| `append_to_history` | Writes a temp file and `os.replace`s it, through one atomic-write helper shared with `relay.state` | Web threads read the file while the relay rewrites it |
| `read_keychain_token` | Gains `account="pushover"` | K5's URL is stored under account `dave` |
| `forward_to_pushover` | Returns whether curl succeeded | Retry and the failure counters (C1) |
| `send_notification` | Adds `id` to each entry | D14 |
| remote-path helper | The `~` → `$HOME` substitution moves to one function, used by push and the pull | K1 and K2 follow the same rule |
| `pull_remote_queue(host, queue_file)` — new | One ssh, bounded by `REMOTE_PULL_TIMEOUT_S`. Remote: rename the queue, if present, to `queue.jsonl.out.<ID>`; then for every `queue.jsonl.out.*` print a `#<filename>` line followed by its contents. Returns `(entries, reached, filenames)`; `reached` is true whenever the remote shell ran. Nothing is deleted | The old `consume_remote_queue` deletes the box's batch before its output is back, so a timeout in between loses it; and it cannot tell "reached, nothing to read" from "unreachable" |
| `ack_remote_queue(host, filenames)` — new | A second ssh that removes exactly the files the pull returned, run after their entries are in history. Skipped when there were none | If the ack is lost the files are pulled again and the seen set drops them (N4) |

`consume_remote_queue` is left untouched for `watch` and deleted with it at S12.

## 4. Failure modes

| ID | Failure | Effect | Detected by | Recovery |
|----|---------|--------|-------------|----------|
| F1 | Relay process dies | Nothing forwarded; page shows red banner | launchd restarts within 10 s; Healthchecks fires if it keeps dying (the first ping waits 60 s of uptime) | Automatic |
| F2 | Mini off / Tailscale down | Nothing forwarded, page unreachable | **Healthchecks dead-man → Pushover directly**; it must not route through newsdesk | Manual — but *known* |
| F3 | Box unreachable (Phase 2) | Its entries wait in its queue | hvac staleness alert (hvac W6) covers the box being dark; the relay only records when it was last reached | Automatic |
| F4 | micro-m4 can't reach mini | Entries wait in micro-m4's `.processing` / queue, unrotated, no age limit | The next send's push ships the backlog — delayed, never lost (D3) | Automatic on next send |
| F5 | Keychain locked / tokens missing on mini | History and page work, no Pushover | Page header shows `no keychain tokens`; HC pinged with `/fail` so it **pages** (D5); the relay re-reads on each heartbeat | Automatic once the Keychain answers |
| F6 | Old `watch` on micro-m4 still polling the mini | Races the relay for the mini's queue | Nothing | **Deploy order S4 before S5** |
| F7 | A Pushover forward fails (internet blip, 5xx, rejection) | Entry is in history, not yet on phone | `forward_failed` in the log; `retrying N` on the page | Retried every 30 s for up to an hour. Lost if the relay restarts meanwhile or the hour runs out |
| F8 | Web server cannot bind | Page unreachable; relay keeps forwarding | Relay logs it and retries the bind every 10 s | Automatic |
| F9 | Phone sleeps with the page open | Page shows nothing new while asleep | — | The next poll shows everything, up to `WEB_FEED_ENTRIES` |
| F10 | `tailscale serve` not persisted after a reboot | HTTPS URL dead; relay fine | S9 checks `tailscale serve status` | Manual, once |
| F11 | A push or pull succeeds but its acknowledgement is lost | The batch is delivered twice | The seen set drops the repeats; `dupes=N` in the log | Automatic |
| F12 | A push is cut mid-transfer | A truncated spool file or an orphaned temp file on the mini | Torn last line skipped; micro-m4 resends the batch; repeats deduped. Temp files older than a day are deleted on the heartbeat | Automatic on next send |
| F13 | A second `relay` is started by hand | It would double-write history and state | `relay.lock` is held; the second process exits 1 | Automatic |

## 5. Deployment — Phase 1

### 5.1 Spikes — before S1

Assumptions that cannot be settled by reading. Each spike is small and throwaway. SP1–SP4 were run on 2026-10-02; results are in §5.1.1.

| ID | Assumption | Spike | If it fails |
|----|------------|-------|-------------|
| SP1 | `tailscale serve` can front a loopback port on this tailnet | `tailscale serve status` on the mini (is 443 already in use?); confirm MagicDNS and HTTPS certificates in the admin console; serve a one-line page with `python3 -m http.server` behind it and load it on micro-m4 and the phone | D9 falls back to binding the tailnet IP (`web_bind`) |
| SP2 | A LaunchAgent on the headless mini can read the Keychain, and starts with nobody at the console | Do S3's two Pushover token lines first. A throwaway plist that logs the exit code of `security find-generic-password … -w`, plus `python3 --version` and `echo $PATH`. Reboot the mini with nobody logged in at the screen | The relay cannot be a `gui/` agent as designed (C8) |
| SP3 | The detached push outlives the Claude Code hook that ran `send` | A temporary Stop hook that `Popen`s `sh -c 'sleep 20; date >> file'` with `start_new_session=True`; check the file. Repeat with `ssh -o BatchMode=yes mini true` as the child | C3 and D3 are reopened |
| SP4 | A `send` from Claude Code's sandboxed Bash tool can spawn a working push | The same child as SP3 from a sandboxed Bash call. It probably cannot reach the mini; if so, confirm the failed push leaves `.processing` intact for the next hook-spawned push | Sandboxed sends ship on the next unsandboxed send; record it in F4 |
| SP6 | Healthchecks behaves as D5 assumes | On the new check: a never-pinged check does not alert, and `/fail` every 60 s produces one Pushover Emergency, not one per ping. Runs at S3 | `/fail` is sent once per transition instead |

(SP5 was withdrawn with the byte-count check it tested; Appendix C.)

#### 5.1.1 Results — 2026-10-02

| ID | Result | What it changes |
|----|--------|-----------------|
| SP1 | **Blocked on one click.** Nothing is on 443 and there is no serve config. `tailscale serve` run from a LaunchAgent answered "Serve is not enabled on your tailnet" with the enable link `https://login.tailscale.com/f/serve?node=na2hPq5tUr11CNTRL`; the node reports no certificate domains yet. Run over ssh, the same command fails outright ("The Tailscale GUI failed to start", CLIError 3) — the mini runs the standalone GUI build (1.102.3), whose CLI needs the GUI session. The machine name is confirmed: `micro-mac-mini.tailbf38e2.ts.net` | Dave enables Serve at that link. **S6 must run in a Terminal on the mini (screen share) or from a LaunchAgent, not over ssh.** The page-on-the-phone half is still to do once Serve is enabled |
| SP2 | **Passed with a console user logged in; reboot half pending.** A LaunchAgent (`com.dave.newsdesk-spike`) added a Keychain item and read its secret back, both exit 0. The mini auto-logs in as `davidmiller` and FileVault is off, so a `gui/` agent should start after a reboot with no one present — the agent is still installed and logs on every load, so the next reboot answers it (`~/.local/share/newsdesk/spike/agent.log`). **Over ssh the Keychain is not usable:** adding an item fails ("User interaction is not allowed", exit 36) and reading a secret fails (exit 36); only an existence check works. launchd's `PATH` is `/usr/bin:/bin:/usr/sbin:/sbin`, and the mini's only `python3` is `/usr/bin/python3` 3.9.6, the same one an interactive shell gets | **S3's Keychain lines and `newsdesk init` must run in a Terminal on the mini, not over ssh.** `relay --once` over ssh will always report `no keychain tokens`. **The code must run on Python 3.9.** L1 does not need a `PATH` entry. None of the four Keychain items exists on the mini yet, including `backup-monitor-hc-url` |
| SP3 | **Passed.** With a throwaway spawn added to `cmd_send`, another session's real Stop hook ran `send` at 12:21:35; its detached child logged 20 s later, after the hook had exited, and `ssh -o BatchMode=yes mini true` from it exited 0 | C3 and D3 stand |
| SP4 | **Passed, unexpectedly.** A `send` from the sandboxed Bash tool spawned a child whose ssh to the mini exited 0, twice, although a plain `ssh mini true` in the same sandbox fails with "Operation not permitted". Why the child is not confined is not understood | Nothing: the design does not depend on it either way. If a future sandbox does block it, the push fails and the next hook-spawned push ships the batch (F4) |
| SP6 | Not run — needs the Healthchecks check, at S3 | — |

Also found on the mini: its clone is on `main` at `735539c` with an uncommitted change to `.claude/settings.json`. S2's checkout has to deal with that file first; it is not this plan's change, so ask before touching it.

### 5.2 Steps

Order matters because of F6. **`watch` keeps working until S12**, so until then the rollback from any failed step is: on micro-m4 remove `hub`, restore the `mini` entry in `remote_machines`, reopen `watch`; on the mini `launchctl bootout` the relay.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| S1 | micro-m4 | Implement C1–C9 and C11 with tests (§8), **leaving `watch` and its tests in place**; commit on branch `relay-hub`; push | `.venv/bin/python -m pytest tests/ -q` green: today's 72 plus the new tests |
| S2 | mini | First resolve the uncommitted change to `.claude/settings.json` in the clone (ask Dave; §5.1.1). Then `git fetch && git checkout relay-hub` in `~/Developer/newsdesk` (also retires the stale clone that lacks `--url`, #42) | `newsdesk send --help` shows `--url`; `newsdesk relay --help` exists |
| S3 | mini, **in a Terminal on the mini (screen share) — the Keychain is not usable over ssh** | Keychain: both Pushover tokens (`security add-generic-password -a pushover -s newsdesk-app-token -w <token>`, same for `newsdesk-user-key`) and `security add-generic-password -a dave -s newsdesk-hc-url -w <url>`. Config: `pushover_min_priority` 2, explicitly. Create the Healthchecks check `newsdesk-relay` (period 5 min, grace 5 min, Pushover integration); run SP6 | `newsdesk init` shows both token ticks. `newsdesk relay --once --no-pushover` prints its summary line and writes `relay.state` — `--no-pushover` because micro-m4's `watch` is still the live consumer of this queue |
| S4 | micro-m4 | Config: delete `mini` from `remote_machines`; add `hub`. **Quit the running `watch`.** Then `newsdesk send "Push check" "S4" --priority 0`. Until S5 completes the mini's queue is unconsumed — minutes, and it is durable | `ssh mini 'ls ~/.local/share/newsdesk/'` shows one `queue.jsonl.in.*` file holding that entry: the detached push, the ssh auth and the remote command all work before the relay is involved |
| S5 | mini | `scripts/install-launchd.sh` | `launchctl list \| grep newsdesk`; `relay.log` shows the S4 entry consumed; `curl -s localhost:5556/api/feed` returns JSON whose state has `"healthchecks": true` and `pushover` `≥ 2`; the HC check goes green within two minutes |
| S6 | mini, in a Terminal on the mini, after Serve is enabled for the tailnet (SP1) | `tailscale serve --bg --https=443 http://127.0.0.1:5556` | `tailscale serve status`; the HTTPS URL loads the page on micro-m4 **and on the phone** |
| S7 | mini | End-to-end with no terminal left open: from an ssh session, `(sleep 60; newsdesk send "Relay test" "priority 2 via hub" --priority 2) &`, then close the session | Phone buzzes (emergency; acknowledge it). Row appears on the open page within about 5 s, without reload |
| S8 | micro-m4 | `newsdesk send "Push test" "from micro-m4" --priority 0`; then five sends in a loop | Rows appear within ~10 s with machine `micro-m4`; all five burst rows appear, each once |
| S9 | mini | `sudo reboot` (⚠️ disrupts OpenBrain / hvac services for ~2 min — Dave's call on timing) | Relay running with no action at the mini beyond what SP2 established is needed; HC never went red; `tailscale serve status` still lists the route; page loads |
| S10 | phone | Lock the phone with the page open for 10 min; send from micro-m4 meanwhile; unlock | Row is there within a poll of unlocking (F9) |
| S11 | — | Kill the relay (`launchctl bootout`, confirm no process) | Phone gets the Healthchecks alert within 12 min. Reinstall |
| S12 | micro-m4, then mini | Remove the TUI (C10) on `relay-hub`; push. On the mini: `git pull`, `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` | Suite green at 45 surviving tests plus the new ones; `newsdesk watch` no longer exists; the page still updates after the kickstart |
| S13 | — | Merge `relay-hub` to main. On the mini: `git checkout main && git pull`, kickstart again. Docs (O4); register 5556 (O5) | `git status` on the mini shows `main`, up to date; page loads |

## 6. Deployment — Phase 2

Deferred, and config-only. The steps (Q0–Q5) are in Appendix A.

## 7. Verification criteria (falsifiable)

| ID | Claim | Test |
|----|-------|------|
| V1 | The relay survives a reboot of the mini | S9 |
| V2 | A relay that stops is reported to the phone without newsdesk's help | S11 |
| V3 | A priority-2 reaches the phone with no terminal window open anywhere | S7 |
| V4 | The hub being unreachable does not slow `send` | With Tailscale off on micro-m4, the median of 10 `newsdesk send x y` runs is no more than 20 ms above the median with no `hub` configured |
| V5 | No entry is lost when a push is interrupted mid-transfer | T3 (the batch is still in `.processing` and is shipped again) and T12 (the lines that did arrive are not doubled) |
| V6 | Two consumers can no longer exist | After S12, only the relay cycle calls `claim_queue` on a hub queue or `pull_remote_queue`. `push` claims micro-m4's own queue only to ship it |
| V7 | A new entry is on the phone's page within about 5 s of the send, with no reload | S7 |
| V8 | A page that slept shows what it missed | S10 |
| V9 | The mini has no new all-interfaces listener | `lsof -nP -iTCP -sTCP:LISTEN` on the mini shows 5556 on `127.0.0.1` only |
| V10 | Nothing is lost or doubled under concurrency | 500 sends on micro-m4 with distinct titles in a tight loop while the relay runs; the hub's history then holds exactly 500 of those titles, none twice |
| V11 | A page that fails during an internet outage on the mini still arrives | On the mini, block `api.pushover.net` (or pull the WAN), send a priority-2, restore within the hour; the phone buzzes after the restore |

## 8. Tests (TDD — written first)

Cycle tests call `relay_cycle(state, now)` with an explicit `now`; nothing sleeps and nothing mocks `time`. Remote commands are built by functions and run under `sh -c` against `tmp_path`, with no ssh and no mocks; ssh itself is mocked only for return codes and timeouts.

| ID | Test | Pins |
|----|------|------|
| T1 | `test_relay_cycle_consumes_and_forwards` | One cycle: local queue and a spool file → history; forward called only for entries ≥ threshold and ≠ −2; `.processing` and the spool file gone only after history is written. `--once` prints the summary for an empty cycle, writes state, spawns no ping |
| T2 | `test_relay_remote_cadence` | Remotes pulled on the first cycle and again only after `REMOTE_POLL_INTERVAL_S`; `remote_machines = []` pulls nothing; the ack runs after the history write and only for files that were returned; an unreached remote leaves its state entry unchanged |
| T3 | `test_push_recovery` | (a) ssh fails → `.processing` holds the batch. (b) `.processing` exists, new sends land in the queue, ssh fails again → every entry is still on disk. (c) A `.processing` older than `STALE_PROCESSING_AGE` is still shipped. (d) Success after two failures ships each entry and leaves no file. (e) An ssh that exceeds `PUSH_SSH_TIMEOUT_S` counts as a failure |
| T4 | `test_push_remote_command` | Run under `sh -c`: input lands as one `queue.jsonl.in.*` file with exactly the bytes sent, no temp file, `queue.jsonl` untouched. A loop doing claim-and-commit concurrently with 1,000 runs loses no line |
| T5 | `test_push_lock` | An entry appended while a push is mid-ship is shipped by the same push's next loop iteration; a push started while the lock is held exits 0 having shipped nothing |
| T6 | `test_send_spawns_push_only_with_hub` | `Popen` called iff `config.get("hub")`, with `start_new_session=True` and `[sys.executable, <newsdesk.py>, "push"]`; a `Popen` that raises leaves the entry queued and `send` returns 0; every entry carries an `id`; rotation is skipped iff `hub` is set |
| T7 | `test_feed_limit_and_order` | W2 returns the newest `limit` entries in file order with the state alongside; an entry with an old `ts` accepted late is last |
| T8 | `test_web_routes` | Real `ThreadingHTTPServer` on an ephemeral port: `/` serves the file with `no-store` and contains the element ids the page script depends on; `/api/feed` is JSON; unknown path is 404; a bind on a taken port retries without raising into the loop |
| T9 | `test_forward_retry` | A failed forward is retried only after `FORWARD_RETRY_INTERVAL_S`, stops on success, and is dropped and logged after `FORWARD_RETRY_EXPIRE_S`; `retrying` in the state tracks the list |
| T10 | `test_heartbeat_ping` | curl spawned iff URL; none before `HEARTBEAT_INTERVAL_S` of uptime; `/fail` suffix when tokens missing, never under `--no-pushover`; spawned detached and not waited on; no ping from a cycle that raised; missing tokens are re-read on the next heartbeat |
| T11 | Existing tests | All 72 stay green through S1–S11. At S12, 27 TUI-only tests go and 45 remain (D8) |
| T12 | `test_relay_dedupes_by_id` | An entry whose `id` is in history is not written or forwarded again; **two spool files carrying the same batch in one cycle yield each entry once**; a truncated spool file followed by the full resend yields each entry once; an entry with no `id` passes through; a batch replayed after a crash between history write and commit adds nothing |
| T13 | `test_history_write_is_atomic` | `append_to_history` goes through the atomic-write helper; the cap is `HISTORY_MAX_ENTRIES` |
| T14 | `test_relay_survives_bad_entries` | A forward that raises and an entry with a non-integer priority each leave the cycle running and the other entries handled |
| T15 | `test_relay_single_instance` | A second relay started while `relay.lock` is held exits 1 |
| T16 | `test_remote_pull_and_ack` | Under `sh -c`: the pull renames the queue to an `.out.*` file and prints every `.out.*` file with its name, deleting nothing; a second pull without an ack returns the same entries again; the ack removes exactly the named files and leaves a newer one; no queue file → `reached` true, no entries. With ssh mocked: failure and timeout → `reached` false |
| T17 | `test_read_keychain_token_account` | The `account` argument reaches the `security` command; the default is `pushover` |

## 9. Decisions

| ID | Question | Recommendation | Status |
|----|----------|----------------|--------|
| D1 | Does any consume path survive outside `relay`? | **No, once S12 removes `watch`.** | **Decided 2026-10-02 (Dave)** |
| D2 | ~~Viewer on micro-m4: `ssh -t mini` or `--hub`?~~ | Superseded by the web viewer. | Moot |
| D3 | Push trigger: spawn-on-send, launchd backstop, or both? | **Spawn only.** No launchd on micro-m4. | **Decided 2026-09-19 (Dave)** |
| D4 | Relay remote cadence | 30 s. Box messages are boot/update/throttle reports. | **Decided 2026-10-02 (Dave)** |
| D5 | Relay pings Healthchecks `/fail` when Keychain tokens are missing? | **Yes.** It turns F5 from silent into paged. | **Decided 2026-10-02 (Dave)** |
| D6 | Keep `--no-pushover` on `relay`? | Yes — S3 depends on it. | **Decided 2026-10-02 (Dave)** |
| D7 | Branch | `relay-hub`; the mini runs the branch from S2; merged to main at S13. | **Decided 2026-10-02 (Dave)** |
| D8 | Remove the curses TUI, and when? | **Yes, at S12 — after the relay has passed S11.** Keeping it as a viewer of the hub would mean rewriting it as a read-only client of the page API, about 250 lines of curses kept in step with the page. `newsdesk tail` (X6) covers a terminal glance. | **Decided 2026-10-02 (Dave)** |
| D9 | Exposure: `tailscale serve` or bind the tailnet IP on 5556 (plain http)? | **`tailscale serve` first.** No new all-interfaces listener, a real URL, HTTPS. Fallback is one config key. | **Decided 2026-10-02 (Dave)**; SP1 confirms it or triggers the fallback |
| D10 | Row order on the page | **Newest first, in the order the hub accepted them.** | **Decided 2026-10-02 (Dave)** |
| D11 | Web server inside the relay process, or a separate `newsdesk web`? | **Inside.** One plist, one log, one process; the state is served from memory. | **Decided 2026-10-02 (Dave)** |
| D12 | `pushover_min_priority` on the hub once it relays 24/7: keep 2, or lower to 1? | **Decide at S4/S5.** At 1 the freeze watch's first stages page hours sooner, but every Claude Code permission prompt pages too, because the threshold is global. The facts are in Appendix B. | **Open — Dave, at the cutover** |
| D13 | Healthchecks on any machine other than the mini? | **No — one check, `newsdesk-relay`, on the hub.** micro-m4 sleeps; the boxes carry no outbound path, and a dark box is hvac W6's alert. A dead mini pages twice if the backup-monitor check is live — accepted. | **Decided 2026-10-02 (Dave)** |
| D14 | A delivery whose acknowledgement is lost repeats. Dedupe, or accept duplicates? | **Dedupe**, by an `id` on each entry. | **Decided 2026-10-02 (Dave)** |
| D15 | During a long mini outage, cap micro-m4's backlog or keep everything? | **Keep everything: no rotation on a machine with a `hub`.** The queue grows without bound while the mini is unreachable. | **Decided 2026-10-02 (Dave)** |
| D16 | `HISTORY_MAX_ENTRIES` on the hub | **5000, up from 1000.** | **Decided 2026-10-02 (Dave)** |
| D17 | Live updates: a stream (SSE) or a page that polls? | **Poll every 3 s.** Removes the stream, its subscriber bookkeeping and a sequence counter, for a few seconds of page latency. | **Decided 2026-10-02 (Dave)** |
| D18 | A Pushover forward that fails: log it, or retry? | **Retry in memory for up to an hour.** This is the path the plan exists for. | **Decided 2026-10-02 (Dave)** |
| D19 | Box-pull code: Phase 1 or Phase 2? | **Phase 1, tested, inert.** Phase 2 stays config-only. | **Decided 2026-10-02 (Dave)** |

## 10. Deferred / out of scope

| ID | Item | Why not now |
|----|------|-------------|
| X1 | Phase 2 box pull | Dave's call — Appendix A, no Pi-side work yet |
| X2 | SSH backoff for unreachable remotes (next-steps R2) | A dark box fails at `ConnectTimeout` = 2 s; three of them cost the loop 6 s every 30 s. Tolerable |
| X3 | Persisting the forward retry list across a relay restart | The in-memory list covers a blip; a restart during an outage is the remaining gap (F7) |
| X4 | Replacing SSH+JSONL with an HTTP ingest on the mini (next-steps I1) | #821 already rejected a push API on the mini for hvac; the web server here is read-only |
| X5 | ntfy (#206) | Deferred there; this closes the "unified web view" gap that was ntfy's main draw |
| X6 | `newsdesk tail` — print recent history to a terminal | Only if the TUI is missed after S12 |
| X7 | Web Notifications / PWA install | Pushover already does phone push |
| X8 | Auth on the web viewer | Tailnet reachability is the auth, as for every other mini service |
| X9 | A live stream to the page (SSE) | D17. Add it only if a 3 s poll proves too slow or too chatty |

## 11. Cross-project follow-ups

| ID | Where | Change | Phase |
|----|-------|--------|-------|
| O1 | `hvac_monitor/docs/lewiston-collection-agents-plan.md` W7 | "micro-m4's `newsdesk watch` polls that queue" → the mini's relay polls it; the ssh-config/known_hosts prerequisite moves to the mini | 2 |
| O2 | OpenBrain #42 | The "no headless relay mode" correction becomes history once S9 passes | 1 |
| O3 | OpenBrain #211 | "restart `watch` to apply a threshold change" → `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` | 1 |
| O4 | `README.md`, `CLAUDE.md`, `docs/newsdesk-plan.md` | New subcommands, the relay/viewer split, launchd install, the page URL; drop `watch`. `CLAUDE.md`'s "single-file CLI", "no server" and "curses for watch UI" statements and `newsdesk.py`'s ABOUTME lines become false. The config path in `README.md` and `CLAUDE.md` is already wrong (the code uses `~/.config/newsdesk/config.json`), and the test command needs `.venv/bin/python`. `web/index.html`, the plist and the install script get ABOUTME headers | 1 |
| O5 | OpenBrain #101 port registry | 5556 newsdesk viewer, loopback-bound behind `tailscale serve` | 1 |
| O6 | `docs/alternate-architectures.md` | Note that A1–A3 were built (this plan, with polling in place of SSE) and why the ntfy deferral held | 1 |

## 12. References

| ID | Ref | What it establishes |
|----|-----|---------------------|
| R1 | OpenBrain #42 | Relay = open TUI window; the two real fixes; boxes can be senders |
| R2 | OpenBrain #821 | Pull from the boxes; why a pulled box is the safer box |
| R3 | OpenBrain #30 | micro-m4 ↔ mini SSH is one-directional, by choice |
| R4 | OpenBrain #211 | `pushover_min_priority` = 2; read once at startup |
| R5 | OpenBrain #547 / `backup-migration/docs/monitoring-design.md` | Healthchecks.io as the mini's dead-man; Keychain URL pattern |
| R6 | OpenBrain #698 | Local Network Privacy blocks launchd processes from the *LAN*; Tailscale 100.x is not LAN — verify at Q0 |
| R7 | hvac plan §5.1, W7 | ACL grant `autogroup:member → tag:agent`, Tailscale SSH `accept`; the box-originated messages |
| R8 | `docs/alternate-architectures.md` §2.1 | A1 headless poller, A2 stdlib http.server + static page, A3 Tailscale-only |
| R9 | OpenBrain #101 | Mini port registry; deliberate all-interfaces listeners are 5555/8766/8767 only |
| R10 | `docs/mini-relay-plan-review.md`, `docs/mini-relay-plan-assessment.md` | The 2026-10-02 review (48 findings) and assessment (7 findings) |

## Appendix A — Phase 2 deployment (deferred)

No code. The pull, its acknowledgement and the remote cadence were built and tested in Phase 1 (C1, C11, T2, T16). What Phase 1 cannot test is the real boxes: ssh from the mini's launchd job to each one. Q0 and Q2 are where that is proven.

Prerequisites: hvac plan W3/W7 landed, so `newsdesk` is installed on each box from a version that stamps an `id`, and its units call `newsdesk send`.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| Q0 | mini | From a LaunchAgent, time a cold `ssh -o BatchMode=yes <box> true` to each box. This is also where Local Network Privacy would show up (R6) | Each connects, in well under `REMOTE_PULL_TIMEOUT_S`. If a cold connect often exceeds the 2 s `ConnectTimeout`, the box reads as unreached on that poll and is pulled 30 s later — nothing is lost either way |
| Q1 | boxes | Confirm which user the box's systemd units send as — the queue path in K2 must be that user's `~/.local/share/newsdesk/queue.jsonl` (root's home is `/root`) | `ls -la` the queue after a unit has sent |
| Q2 | mini | `~/.ssh/config` alias per box with matching `User`; one manual `ssh <box> true` each to populate `known_hosts` — BatchMode refuses unknown hosts silently | `ssh -o BatchMode=yes <box> true` exits 0 |
| Q3 | mini | Add the three `remote_machines` entries; `launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay` (config is read at startup) | `relay.log` shows `remotes=3/3`; page header shows `remotes 3/3` |
| Q4 | a box | `newsdesk send "Pull test" "from lake-agent-1" --priority 0` | Row on the page within about 35 s with the box's machine name; the box's `queue.jsonl.out.*` file is gone after the ack |
| Q5 | hvac_monitor | Amend hvac W7 (O1) | — |

## Appendix B — Facts for D12 (hub threshold 2 or 1)

Added 2026-10-02 from hvac_monitor's Lake House freeze watch (`docs/freeze-watch-plan.md` §9 N6).

- **At 2 (today)** only emergencies page. The freeze watch's first stages — Lake house dark at 90 min, Nest offline at 30 min, can't see the Nest, collector down, Longmont offline, Longmont on battery with > 2 h of runway — stay on the page and in the hvac check-in. The phone hears a Lake power cut at 4 h (if Gaylord < 40 °F) and a furnace that lost 24 V at 2 h 15 m.
- **At 1** those first stages page too, about 2½ h and 1½ h sooner.
- **The threshold is global.** `~/.claude/hooks/newsdesk-notify.sh` sends every Claude Code permission prompt at priority 1, so at 1 each one from every session pages the phone. backup-migration, Memex and PostCardMaker choose per message. A per-project threshold, or the hook at 0, would remove the trade-off.
- **Forwarding during an internet blip on the mini** is now retried for up to an hour (D18, F7). The freeze watch also carries a second path for its emergencies through healthchecks.io (`HEALTHCHECKS_FREEZE_ALARM_URL`).

## Appendix C — What the review and assessment changed

Both ran on 2026-10-02; the full findings are in R10.

**Review (48 findings, 4 critical — all in the push path).**
- Push no longer appends to the hub's queue; it renames a staged file into a spool the relay claims. Appending could lose a batch to the relay's rename.
- Push never deletes an unshipped backlog: `.processing` is shipped first, has no age limit, and is never overwritten.
- Entries carry an `id` and the relay dedupes (D14); the relay commits a claim only after the history write.
- The TUI removal moved from the build to S12 (D8), which gives every earlier step a rollback.
- The mini gets its own Pushover tokens and an explicit threshold (S3); the plist sets `PATH` and unbuffered output; the relay is single-instance; the first Healthchecks ping is delayed.
- Test counts corrected: 27 tests go with the TUI, 45 stay.

**Assessment (7 findings; six applied, one declined).**
- X1: the SSE stream and its hub-side sequence number were replaced by a polling page (D17). SP1 shrank to a reachability check.
- X2: failed Pushover forwards are retried (D18).
- X3: the remote byte-count check was dropped; the dedupe already covers a cut transfer. SP5 went with it.
- X5: the dedupe uses a seen set updated as entries are accepted, so a repeat inside one cycle is caught.
- X7: push exits if the lock is held, with no wait.
- X6: Phase 2 steps, D12's facts and this history moved to appendices.
- X4 (move the box-pull code to Phase 2) was **declined**: Dave wants Phase 2 config-only (D19). The pull was instead made loss-safe in Phase 1 (pull, then acknowledge), which removes the timeout risk the review raised for the boxes.
