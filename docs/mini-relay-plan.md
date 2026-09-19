*Last updated: 2026-09-19 07:28 EDT*

# Newsdesk — Hub-on-the-Mini Relay

**Status: DRAFT for study. Nothing built. Decisions in §8 are open unless marked otherwise.**

## 1. Problem

Today the Pushover relay is the interactive `newsdesk watch` TUI on micro-m4 — `forward_to_pushover` has one call site, inside `cmd_watch_curses`. Nothing reaches a phone unless that iTerm window is open. During the Sept 2026 Lewiston absence the alerting path for two Michigan houses was one terminal window for fifteen days (OpenBrain #42). The window survives neither a reboot nor a closed lid, and micro-m4 is the machine that reboots, sleeps, and travels.

Three things need to change together:

1. The relay must live on the always-on machine (micro-mac-mini) and be launchd-managed, not a TUI.
2. Senders must get their entries to the mini in whichever direction the existing trust already runs.
3. The relay must be observed by something that does not depend on the relay working.

## 2. Design

### 2.1 Roles per machine

| ID | Machine | Role | Runs |
|----|---------|------|------|
| M1 | micro-mac-mini (`davidmiller`) | **Hub.** Sole consumer of every queue; sole Pushover forwarder; owns the unified history | `newsdesk relay` under launchd (KeepAlive) |
| M2 | micro-m4 (`dave`) | **Pushing sender.** Writes locally, ships its queue to the hub | `newsdesk send` (unchanged callers) → spawns `newsdesk push`; launchd backstop `push` every 60 s |
| M3 | Lewiston agent boxes ×3 (`tag:agent`) | **Pulled sender.** Writes locally, nothing else | `newsdesk send` only (hvac plan W7). No daemon, no key, no outbound path |
| M4 | Any machine Dave is sitting at | **Viewer.** Reads the hub's history | `newsdesk watch` on the mini, reached via `ssh -t mini` inside tmux |

### 2.2 Data flow

```
micro-m4                        micro-mac-mini                       phone
────────                        ──────────────                       ─────
send ──▶ queue.jsonl                                                  
           │ push (ssh mini, stage+append)                            
           └────────────────▶ queue.jsonl ──┐                         
                                            │                         
agent box ×3                                ▼                         
──────────                            relay (launchd)                 
send ──▶ queue.jsonl ◀── ssh pull ── every 30 s                       
                                            │                         
                                            ├─▶ history.jsonl ──▶ watch (viewer)
                                            ├─▶ Pushover (≥ threshold) ─────▶ 📱
                                            ├─▶ relay.state (liveness for the viewer)
                                            └─▶ Healthchecks.io ping (every 60 s)
                                                        │
                                             dead-man fires → Pushover ──▶ 📱
```

### 2.3 Direction per sender — the decision and why

| ID | Sender | Direction | Reason |
|----|--------|-----------|--------|
| P1 | Agent boxes → mini | **Mini pulls** | Matches hvac #821: a pulled box has no outbound path into the tailnet, holds no key to the mini, keeps no delivery state, runs no push daemon. The grant `autogroup:member → tag:agent` (all ports) plus the Tailscale SSH `accept` rule already lets the mini in. Cost: one `remote_machines` entry per box. |
| P2 | micro-m4 → mini | **micro-m4 pushes** | The mini cannot reach micro-m4 (no Remote Login — deliberate, #30), and micro-m4 is the intermittent machine, so pulling from it would mostly hit a sleeping laptop. micro-m4 → mini SSH already exists. |
| P3 | Mini's own jobs → mini | Local | Already the case. |

Dave's original framing — "nobody's being reached into" — is not a property worth buying for the boxes; #821 found the opposite is *better* for them. It *is* the right property for micro-m4, for the reason in P2. So direction follows existing trust, per sender, rather than being uniform.

### 2.4 Invariants preserved

| ID | Invariant | How |
|----|-----------|-----|
| N1 | `send` has zero network dependency (#39) | Local append first, always. The push is a *detached* child; `send` returns before it connects. |
| N2 | No Pushover secret leaves the hub | Keychain read only in `relay`, only on the mini. `push` carries entries, never tokens. |
| N3 | Queues are durable through outages | Unchanged rename-read-delete; push retains `.processing` on failure and retries. |
| N4 | Each entry is consumed exactly once | There is exactly one consumer (the relay). `watch` never consumes. |
| N5 | stdlib only | `subprocess` for ssh and curl, as today. |

## 3. Components

### 3.1 C1 — `newsdesk relay`

Headless loop, intended for launchd. Replaces the consume/forward half of today's `watch`.

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk relay [--once] [--no-pushover]` |
| Each cycle (every `POLL_INTERVAL_S` = 2 s) | `consume_local_queue` → `append_to_history` → `forward_to_pushover` for entries passing `should_forward_pushover(entry, pushover_min_priority)` |
| Every `REMOTE_POLL_INTERVAL_S` = 30 s | `consume_remote_queue` for each `remote_machines` entry (three boxes in Michigan; 2 s cadence would be 90 ssh/min for messages that are boot reports) |
| Every `HEARTBEAT_INTERVAL_S` = 60 s | Write `relay.state` (C5); `curl -fsS -m 10 <healthchecks_url>` if configured |
| Tokens | Read from Keychain once at startup, as `watch` does now. Missing tokens → log once, keep relaying (history still accrues). |
| Logging | One line per cycle that did anything: `2026-09-19T07:28:00 consumed=3 forwarded=1 remotes=2/3` to stdout (launchd routes to `relay.log`). Silent cycles log nothing. |
| `--once` | One full cycle including remotes, then exit 0. For manual verification and tests. |
| Robustness | Per-entry try/except around forward; per-remote try/except already inside `consume_remote_queue`. The loop must not die on a bad line, a bad host, or a Pushover 4xx. |

### 3.2 C2 — `newsdesk push`

Ships the local queue to the hub. Mirror image of `consume_remote_queue`.

| Aspect | Spec |
|--------|------|
| Invocation | `newsdesk push` — reads `config["hub"]`; exits 0 silently if no hub configured |
| Steps | 1. Local rename-read-delete *without* the delete: `queue.jsonl` → `queue.jsonl.processing`, read entries (plus any fresh `.processing` left by a failed prior push). 2. If nothing to ship, exit 0. 3. `ssh -o BatchMode=yes -o ConnectTimeout=2 <hub> 'f=...; t="$f.incoming.$$"; cat > "$t" && cat "$t" >> "$f" && rm "$t"'` with the entries on stdin. 4. On exit 0: delete `.processing`. On any failure: leave `.processing` in place — the next push recovers it (it is fresh, < `STALE_PROCESSING_AGE`). |
| Why stage-then-append | A dropped connection mid-stream must not leave a half line in the hub's queue. The remote `cat "$t" >> "$f"` is a local operation on the mini and completes or doesn't. `parse_jsonl` would skip a torn line silently — that is a lost notification, and staging makes it impossible. |
| Concurrency with the relay | The relay renames the hub's queue; an append that races it either lands before the rename (consumed now) or opens a fresh file after (consumed next cycle). No loss either way. |
| Concurrency with itself | Two pushes at once (spawned + launchd backstop): the second finds no queue or a fresh `.processing` owned by the first. `.processing` recovery must only pick up files older than a few seconds — add `PROCESSING_MIN_AGE_S` = 5 so a push in flight is not double-shipped. |

### 3.3 C3 — `send` spawns a push

After the local append, if `config["hub"]` is set: `subprocess.Popen([newsdesk, "push"], start_new_session=True, stdin/stdout/stderr=DEVNULL)`. Fire-and-forget; `send` still returns in milliseconds; a Claude Code hook that spawned it is not blocked and cannot kill it. On the boxes there is no `hub`, so nothing changes there.

### 3.4 C4 — `watch` becomes a viewer

| Aspect | Spec |
|--------|------|
| Removed | Queue consumption, remote polling, Pushover forwarding, `--no-pushover`, the Keychain status line on help page 2 |
| LATEST mode | Each poll re-reads `history.jsonl`; shows entries with `ts` > last-seen `ts` (initial last-seen = watch start time). Bell logic unchanged. |
| HISTORY mode | Unchanged |
| Header status | Replaces `Pushover: ≥ N` with relay liveness from C5: `relay: 3s ago · pushover ≥ 2` or `relay: STALE 6m` (stale = last cycle older than 3 × `HEARTBEAT_INTERVAL_S`). A viewer that cannot tell the relay is dead is the same hole as today. |
| Where it runs | On the mini, inside tmux, reached from micro-m4 with `ssh -t mini`. Bell reaches iTerm through the ssh tty. tmux is the right tool this time — it is an ssh session, and the exposure it covers is ssh disconnect. |

### 3.5 C5 — Relay state file

`~/.local/share/newsdesk/relay.state`, JSON, rewritten atomically (write temp, rename) on every heartbeat:

```json
{"last_cycle_ts": 1758281280.1, "started_ts": 1758200000.0, "pushover": "≥ 2", "remotes": {"lake-agent-1": 1758281260.3, "creek-agent-1": null}, "forwarded_total": 14}
```

`remotes` holds the last successful pull time per host (`null` = never reached since start). The viewer shows it; nothing else reads it.

### 3.6 C6 — Config

| ID | Key | micro-m4 | mini | box | Notes |
|----|-----|----------|------|-----|-------|
| K1 | `hub` | `{"host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}` | absent | absent | Presence turns on C3. `~` is remote — same rule as `remote_machines` |
| K2 | `remote_machines` | `[]` (**remove** the `mini` entry) | one per box | `[]` | `host` is an `~/.ssh/config` alias on the mini |
| K3 | `pushover_min_priority` | ignored | `2` (current) | ignored | Only the relay reads it now |
| K4 | Healthchecks URL | — | Keychain `newsdesk-hc-url`, account `dave` | — | Same pattern as `backup-monitor-hc-url`. Absent → relay logs once, no ping |

### 3.7 C7 — launchd

| ID | File | Machine | Key settings |
|----|------|---------|--------------|
| L1 | `launchd/com.dave.newsdesk-relay.plist` | mini | `KeepAlive true`, `RunAtLoad true`, `ThrottleInterval 10`, `StandardOutPath`/`StandardErrorPath` → `~/.local/share/newsdesk/relay.log`, `ProgramArguments` = absolute path to `~/bin/newsdesk relay` |
| L2 | `launchd/com.dave.newsdesk-push.plist` | micro-m4 | `StartInterval 60`, `ProgramArguments` = `~/bin/newsdesk push`. Backstop only — the spawn in C3 is the primary trigger |
| L3 | `scripts/install-launchd.sh relay\|push` | both | Substitutes `__HOME__`, copies to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u)`. Idempotent (bootout first if loaded) |

Plists are user agents (`gui/` domain), not system daemons — they need the login Keychain, which is what the backup monitor already relies on.

### 3.8 C8 — New constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `REMOTE_POLL_INTERVAL_S` | 30 | Relay pull cadence for `remote_machines` |
| `HEARTBEAT_INTERVAL_S` | 60 | State file + Healthchecks ping cadence |
| `RELAY_STALE_AFTER_S` | 180 | Viewer shows STALE past this |
| `PROCESSING_MIN_AGE_S` | 5 | Push ignores a `.processing` younger than this (another push owns it) |

## 4. Failure modes

| ID | Failure | Effect | Detected by | Recovery |
|----|---------|--------|-------------|----------|
| F1 | Relay process dies | Nothing forwarded | launchd restarts within 10 s; Healthchecks fires if it keeps dying | Automatic |
| F2 | Mini off / Tailscale down | Nothing forwarded, nothing pulled | **Healthchecks dead-man → Pushover directly** (HC has a native Pushover integration; it must not route through newsdesk) | Manual — but *known* |
| F3 | Box unreachable | Its entries wait in its queue | hvac staleness alert (W6) covers the box being dark; the queue drains when it returns, timestamped | Automatic |
| F4 | micro-m4 can't reach mini | Entries wait in micro-m4's `.processing` / queue | Next push (spawned or backstop) ships the backlog | Automatic |
| F5 | Keychain locked / tokens missing on mini | History accrues, no Pushover | Relay logs once; viewer header shows `no keychain tokens`; nothing else — same silent hole as #24 | ⚠️ Open: should the relay ping HC with `/fail` in this state so it pages? Recommend yes (D5) |
| F6 | Old `watch` on micro-m4 still polling the mini | Races the relay for the mini's queue; items it wins never hit the hub's history or Pushover | Nothing | **Deploy order S4 before S5** |
| F7 | Pushover rejects a message | Entry is in history, not on phone | Not detected (fire-and-forget, #24) | Out of scope here; next-steps R4 |

## 5. Deployment sequence

Order matters because of F6. Each step is complete before the next starts.

| ID | Where | Step | Verifies |
|----|-------|------|----------|
| S1 | micro-m4 | Implement C1–C8 with tests (§7); commit on branch `relay-hub`; push | `python -m pytest tests/ -q` green |
| S2 | mini | `git pull` in `~/Developer/newsdesk` (also retires the stale clone that lacks `--url`, #42) | `newsdesk send --help` shows `--url` |
| S3 | mini | Config: `remote_machines` = the three boxes; `~/.ssh/config` aliases with `User` matching whoever the box's units send as; one manual `ssh <box> true` per box to populate `known_hosts` (BatchMode refuses unknown hosts *silently*). Keychain: `security add-generic-password -a dave -s newsdesk-hc-url -w <url>`. Create the Healthchecks check `newsdesk-relay` (period 5 min, grace 5 min, Pushover integration) | `newsdesk relay --once` prints `remotes=3/3` |
| S4 | micro-m4 | Config: delete `mini` from `remote_machines`; add `hub`. **Quit the running `watch`.** From here until S5 completes, the mini's queue is unconsumed — minutes, and it is durable | `cat ~/.config/newsdesk/config.json` |
| S5 | mini | `scripts/install-launchd.sh relay` | `launchctl list \| grep newsdesk`; `relay.log` shows cycles; HC dashboard shows the check green |
| S6 | mini | Real end-to-end: `newsdesk send "Relay test" "priority 2 via hub" --priority 2` on the mini | Phone buzzes (emergency; acknowledge it). `history.jsonl` has the entry |
| S7 | micro-m4 | `scripts/install-launchd.sh push`; then `newsdesk send "Push test" "from micro-m4" --priority 0` | Entry appears in the mini's `history.jsonl` within ~5 s with `"machine": "micro-m4"` |
| S8 | a box | `newsdesk send "Pull test" "from lake-agent-1" --priority 0` | Entry appears in the mini's history within 30 s |
| S9 | mini | `sudo reboot` (⚠️ disrupts OpenBrain / hvac services for ~2 min — Dave's call on timing) | Relay is running after login without a hand; HC never went red |
| S10 | micro-m4 | Open the viewer: `ssh -t mini 'tmux new -As newsdesk newsdesk watch'` | Header shows `relay: Ns ago`; bell rings in iTerm on a priority-1 send |
| S11 | — | Kill the relay (`launchctl kickstart -k` won't do it — `kill -9` the pid and `launchctl bootout`) and wait 10 min | Phone gets the Healthchecks alert. Reinstall |

## 6. Verification criteria (falsifiable)

| ID | Claim | Test |
|----|-------|------|
| V1 | The relay survives a reboot of the mini | S9 |
| V2 | A relay that stops is reported to the phone without newsdesk's help | S11 |
| V3 | A priority-2 from a box reaches the phone with no window open anywhere | S6 variant from a box, after S10's tmux is detached |
| V4 | `send` on micro-m4 still returns in < 50 ms with the hub unreachable | `time newsdesk send x y` with Tailscale off |
| V5 | No entry is lost when a push is interrupted mid-transfer | T3 (kill ssh mid-stream in a test harness; entry is in `.processing`, not in the hub's queue as a torn line) |
| V6 | Two consumers can no longer exist | `grep -n consume_ newsdesk.py` shows call sites only in `relay` and `push` |

## 7. Tests (TDD — written first)

| ID | Test | Pins |
|----|------|------|
| T1 | `test_relay_once_consumes_and_forwards` | One cycle: local queue → history; forward called only for entries ≥ threshold and ≠ −2; state file written |
| T2 | `test_relay_remote_cadence` | Remotes pulled on the first cycle and again only after `REMOTE_POLL_INTERVAL_S` (mock clock) |
| T3 | `test_push_retains_processing_on_failure` | ssh returncode ≠ 0 → `.processing` still exists with all entries; returncode 0 → deleted |
| T4 | `test_push_command_stages_before_append` | The remote command string writes to `$f.incoming.$$` before `>> "$f"` |
| T5 | `test_push_skips_fresh_processing` | `.processing` younger than `PROCESSING_MIN_AGE_S` is left alone |
| T6 | `test_send_spawns_push_only_with_hub` | `Popen` called iff `config["hub"]`; called with `start_new_session=True` |
| T7 | `test_watch_new_since` | Pure helper: entries with `ts` > last-seen, in order |
| T8 | `test_relay_state_label` | `3s ago` / `STALE 6m` / `no state file` from a state dict and a now |
| T9 | `test_heartbeat_pings_when_url_present` | curl invoked iff URL; `/fail` suffix when tokens missing (if D5 = yes) |
| T10 | Existing 55 tests | Still green; tests that asserted `watch` consumes are rewritten for `relay` |

## 8. Decisions

| ID | Question | Recommendation | Status |
|----|----------|----------------|--------|
| D1 | Does `watch` keep a standalone consume mode for a machine with no relay? | **No.** One consumer, one code path. A single-machine user runs `relay` + `watch`. The compat cost is Dave's micro-m4 habit, which is the thing being changed. | Open |
| D2 | Viewer on micro-m4: `ssh -t mini` or a `watch --hub mini` that reads remote history? | **`ssh -t mini` in tmux.** Zero viewer code; `--hub` is deferred (X1). | Open |
| D3 | Push trigger: spawn-on-send, launchd backstop, or both? | **Both.** Spawn gives ~3–5 s at-desk latency; the 60 s backstop covers "mini was unreachable, then micro-m4 went quiet". One plist. | Open |
| D4 | Relay remote cadence | 30 s. Box messages are boot/update/throttle reports; nothing there needs 2 s. | Open |
| D5 | Relay pings Healthchecks `/fail` when Keychain tokens are missing? | **Yes.** It turns F5 from silent into paged, at the cost of three lines. | Open |
| D6 | Keep `--no-pushover` on `relay`? | Yes — it is the only session-level escape and costs nothing. | Open |
| D7 | Branch | `relay-hub`, merged to main after S8 passes, before S9. | Open |

## 9. Deferred / out of scope

| ID | Item | Why not now |
|----|------|-------------|
| X1 | `watch --hub <host>` — viewer reads the hub's history over ssh from any machine | D2 covers the need with no code |
| X2 | SSH backoff for unreachable remotes (next-steps R2) | 30 s cadence × 2 s ConnectTimeout × 3 boxes is bounded at 6 s per cycle; tolerable |
| X3 | Checking the Pushover response (#24, next-steps R4) | Separate fix, separate commit |
| X4 | Replacing SSH+JSONL with an HTTP ingest on the mini (next-steps I1) | #821 already rejected a push API on the mini for hvac for reimplementing concerns the pull path gets free; same argument here |
| X5 | ntfy (#206) | Deferred there; unchanged |

## 10. Cross-project follow-ups

| ID | Where | Change |
|----|-------|--------|
| O1 | `hvac_monitor/docs/lewiston-collection-agents-plan.md` W7 | "micro-m4's `newsdesk watch` polls that queue" → the mini's relay polls it; the ssh-config/known_hosts prerequisite moves to the mini |
| O2 | OpenBrain #42 | The "no headless relay mode" correction becomes history once S9 passes; amend rather than leave a future session designing around a hole that closed |
| O3 | OpenBrain #211 | "restart `watch` to apply a threshold change" → restart the *relay* (`launchctl kickstart -k gui/$(id -u)/com.dave.newsdesk-relay`) |
| O4 | `README.md`, `CLAUDE.md`, `docs/newsdesk-plan.md` | New subcommands, the viewer/relay split, launchd install |

## 11. References

| ID | Ref | What it establishes |
|----|-----|---------------------|
| R1 | OpenBrain #42 | Relay = open TUI window; the two real fixes; boxes can be senders |
| R2 | OpenBrain #821 | Pull from the boxes; why a pulled box is the safer box; repair channel ≠ data channel |
| R3 | OpenBrain #30 | micro-m4 ↔ mini SSH is one-directional, by choice |
| R4 | OpenBrain #211 | `pushover_min_priority` = 2; read once at startup |
| R5 | OpenBrain #547 / `backup-migration/docs/monitoring-design.md` | Healthchecks.io as the mini's dead-man; Keychain URL pattern |
| R6 | OpenBrain #698 | Local Network Privacy blocks launchd processes from the *LAN*; Tailscale 100.x is not LAN — verify at S5, don't assume |
| R7 | hvac plan §5.1, W7 | ACL grant `autogroup:member → tag:agent`, Tailscale SSH `accept`; the box-originated messages |
| R8 | `docs/alternate-architectures.md` A1 | The headless poller was scoped in April and deferred behind ntfy |
