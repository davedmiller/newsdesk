*Review of `docs/mini-relay-plan.md` — 2026-10-02. Produced by a background review agent (`/ddm-review-plan`); its output is reproduced verbatim below.*

# Review: `docs/mini-relay-plan.md` (Hub-on-the-Mini Relay + Web Viewer)

*Reviewed 2026-10-02 against `newsdesk.py` (731 lines) and `tests/test_newsdesk.py` (681 lines) at `ee55d81` on `relay-hub`. Read-only: no files changed.*

## Verdict

**48 findings: 4 Critical, 19 Material, 20 Minor, 5 Nit.** The architecture holds up; the defects are concentrated in the push path (C2).

1. **C2 can lose and tear entries on the hub** (A1, A2). A dropped connection still appends a truncated batch, and the remote `>>` races the relay's rename. One fix covers both: verify a byte count, then `mv` the batch into a spool file instead of appending.
2. **C2 can lose the backlog on micro-m4** (A3, A4, A5). "Recover `.processing`" is undefined and, done the way `consume_local_queue` does it, drops the backlog on the second failed push. The 1-day stale rule and `_maybe_rotate` drop it on long outages.
3. **Removing the TUI in S1 removes the only relay before its replacement is proven** (OR1). `~/bin/newsdesk` on micro-m4 runs the working tree, and the plan has no rollback.

The C10/T11 counts are also wrong: 30 tests go, not ~9, and 42 survive, not ~63 (I1).

Verified means I read the cited lines or ran the command. Inferred means I reasoned from the code plus ssh/POSIX semantics without executing it. The suite was run with `.venv/bin/python -m pytest tests/ -q`: **72 passed**.

---

## 1. Inconsistencies

**I1 — Material `[mechanical]` — C10, T11 and D8 test counts are wrong.**
Verified by collection: 72 tests today. Thirty tests in ten classes exercise only helpers that C10 deletes:

| Class | Tests |
|---|---|
| `TestPriorityIconMapping` | 2 |
| `TestPriorityBell` | 6 |
| `TestCycleBellThreshold` | 4 |
| `TestPrioritySilentSkipped` | 2 |
| `TestSilentVisibleInHistory` | 3 |
| `TestPushoverStatusLabel` | 3 |
| `TestProjectFieldInDisplay` | 1 |
| `TestMachineNameInDisplay` | 2 |
| `TestFieldPadding` | 5 |
| `TestLinkMarkerInDisplay` | 2 |

So 30 go and 42 survive (27 and 45 if I3 is accepted). "~9" is roughly the class count.
Fix: correct C10, T11 and D8. The number is mechanical, but D8's warning exists so the deletion is an explicit call, and it should say 30.

**I2 — Minor `[mechanical]` — C10 "about 300 lines" is about 355 of 731 (49%).**
Verified by line ranges:
- `cmd_watch_curses` and its section header, L400–645: 246 lines.
- Display formatting, L180–207: 28.
- Bell, icon, display and status helpers, L109–157 and L171–177 (minus `should_forward_pushover`): about 55.
- `PRIORITY_ICONS`, L37–43.
- Constants L24–25, `import curses` L5, `cmd_watch` L669–674, subparser and dispatch L712–714 and L723–724.

**I3 — Material — C10 deletes `pushover_status_label`, which the surviving design needs.**
C6's state carries `"pushover": "≥ 2"`. Page item V7 shows `pushover ≥ 2`. F5 says the header shows `no keychain tokens`. D6 keeps `--no-pushover`. Those are exactly the three outputs of `pushover_status_label` (L171–177, verified).
No other removed helper is used by code that stays: `should_forward_pushover` (L160) has no dependency on the removed set.
Fix: keep `pushover_status_label` and its 3 tests, and have the relay fill `state["pushover"]` from it.

**I4 — Minor — V6 contradicts D1 and cannot be met as written.**
V6 expects `consume_` call sites "only in `relay` and `push`". D1 says no consume path survives outside `relay`. `push` also cannot call `consume_local_queue`, because that function unlinks `.processing` before returning (L223, L233), while C2 step 5 must retain it on failure. The grep also matches the two `def` lines.
Fix: V6 becomes "`consume_local_queue` and `consume_remote_queue` are each called from exactly one function, the relay cycle". See SH1 for what push uses instead.

**I5 — Minor — V5 cites T3 for something T3 does not do.**
V5 says T3 kills ssh mid-stream and shows no torn line on the hub. T3 as specified mocks an ssh return code and checks the local `.processing`. Nothing in T1–T11 exercises the remote side, and per A1 the claim is false for the current design.
Fix: see TG2.

**I6 — Minor `[mechanical]` — ID collisions inside the plan.**
- §3.5 uses V1–V10 for page features and §7 uses V1–V9 for verification criteria. "Keychain status moves to V7" and "V7" in §7 are different things.
- §3.4 uses W1–W5 for routes while M3, F3 and O1 cite hvac's W3/W6/W7 unqualified.
Fix: rename the page features (for example U1–U10) and write hvac references as "hvac W6".

**I7 — Minor `[mechanical]` — M4 offers `http://100.70.51.21:5556` as an alternative URL.**
Under the default loopback bind (K4, N6, V9) that URL does not answer. F10 says so correctly.
Fix: qualify M4 the way F10 does.

**I8 — Nit `[mechanical]` — X2's bound is 9 s, not 6 s.**
`consume_remote_queue` is bounded by `SSH_COMMAND_TIMEOUT = 3` (L21, L262), not by `ConnectTimeout`. Three dark boxes block the relay loop for up to 9 s every 30 s.

**I9 — Minor — D13's rationale cites a capability no component provides.**
D13 says "a dark box is an alert the hub raises". C6 only records last-successful-pull, and F3 assigns the dark-box alert to hvac W6. D13 itself is decided and not in question.
Fix: reword the rationale to point at hvac W6. See also A7: the relay cannot currently tell a successful pull from a failed one.

**I10 — Minor — "one ssh connection per burst" (C2) and "one batch, not five" (S8) do not follow from the drain loop.**
The first push renames a queue holding one entry, ships it, then loops and ships the rest. A burst normally means two connections and can straddle a 2 s relay poll.
Fix: say "at most two connections" in C2. See VC3 for S8.

**I11 — Minor — `--once` semantics disagree across C1, C6, T1 and S3.**
- C1 says silent cycles log nothing, but S3 expects `consumed=… remotes=0/0` from a possibly empty queue.
- C6 ties the state file to the heartbeat, but T1 expects `--once` to write it.
- Whether `--once` pings Healthchecks is unstated.
Fix: specify that `--once` always prints its summary line, always writes state, and never pings.

**I12 — Nit — N4 says "exactly once".**
The design is at-least-once on the push leg (A6) and at-most-once inside the relay (A11).
Fix: state it that way, or adopt the entry id in A6.

## 2. Missing elements

**G1 — Material — S3 never puts Pushover credentials or the threshold on the mini.**
S3 adds only the Healthchecks URL.
- The project memory records the Pushover tokens as set up on Micro-M4. Without them on the mini, the relay starts in F5 and pings `/fail`, which pages as an Emergency.
- K3 says the mini's `pushover_min_priority` is "2 (current)". Verified: 2 is micro-m4's value in `~/.config/newsdesk/config.json`. The code default is −1 (L34), which would forward every priority-0 "Turn complete" ping to the phone.
- Not verified: the mini's actual Keychain and config. I could not inspect that machine.
Fix: S3 adds both `security add-generic-password` lines and sets the config key. Verify with `newsdesk init` showing both ticks and `relay --once --no-pushover` reporting `pushover` state.

**G2 — Material — the push ssh has no overall timeout.**
C2 sets `ConnectTimeout=2` only. A connection that stalls after connect (lid closed, mini drops off) holds the flock until TCP gives up. `PUSH_LOCK_TIMEOUT_S` bounds the waiters, not the holder. `consume_remote_queue` bounds itself with `timeout=SSH_COMMAND_TIMEOUT` (L262); push needs the same.
Fix: `subprocess.run(..., timeout=PUSH_SSH_TIMEOUT_S)` plus `-o ServerAliveInterval=5 -o ServerAliveCountMax=2`, and add the constant to C9.

**G3 — Material — L1 does not specify the launchd environment.**
- The wrapper does `exec python3` (verified in `newsdesk`). launchd's default PATH resolves that to `/usr/bin/python3`, not the Homebrew Python used interactively. S3's interactive `--once` will therefore not catch a version problem.
- stdout to a file is block-buffered, so `relay.log` stays empty for a long time and S5's "relay.log shows cycles" misleads.
Fix: the plist sets `EnvironmentVariables` (`PATH`, `PYTHONUNBUFFERED=1`), or the relay prints with `flush=True`.

**G4 — Minor — C3 does not say how the child finds `newsdesk`.**
`Popen([newsdesk, "push"])` is under-specified. Hooks do not source `.zshrc` (project memory).
Fix: `[sys.executable, os.path.abspath(__file__), "push"]`. The same applies to locating `web/index.html`: resolve it relative to `__file__`.

**G5 — Minor — no single-instance guard on the relay.**
A manual `newsdesk relay` or `--once` beside the launchd one gives two processes doing read-modify-write on `history.jsonl` (L97–106) and two writers of `relay.state`.
Fix: a non-blocking flock on `relay.lock` at startup; exit with a message if held.

**G6 — Minor — config plumbing is unstated.**
`DEFAULT_CONFIG` (L30–35) needs `web_bind` and `web_port`. C3 must use `config.get("hub")`, because the existing `test_cmd_send_passes_url_through_to_queue` monkeypatches `load_config` to a dict with no `hub` key (verified). The spawn belongs in `cmd_send` (L650) inside its own try/except, not in `send_notification`, which has no config.

**G7 — Minor — the page's rendering rules are unstated.**
Titles and messages come from arbitrary senders, including hook text.
Fix: specify `textContent` only, and that V8 links are rendered only for `http:`/`https:` URLs.

**G8 — Minor — SSE subscriber queues are unbounded.**
C1 promises the loop survives "a slow browser" without a mechanism.
Fix: `Queue(maxsize=…)`, `put_nowait`, drop the subscriber on `Full`. The page's reconnect-and-refetch already covers the drop.

**G9 — Minor — history is capped at 1000 entries (L18, L100).**
The hub's history becomes the unified feed, including a priority-0 entry for every Claude Code turn on micro-m4. The viewer's whole reachable past is 1000 rows.
Fix: decide whether to raise `HISTORY_MAX_ENTRIES` on the hub or accept it, and say so.

## 3. Simplification opportunities

**SI1 — Material — replace "stage then append" with "stage, verify, rename into a spool".**
This removes the reasoning in C2's "Why stage-then-append" and "Concurrency with the relay" rows, both of which are wrong (A1, A2). The relay consumes `queue.jsonl` plus any completed `queue.jsonl.in.*` files. Details are under A2.

**SI2 — Minor — the blocking flock plus `SIGALRM` is fiddly.**
Python retries an interrupted `flock` unless the handler raises (PEP 475), so a handler that only sets a flag never times out.
Options: (Recommended) a `LOCK_NB` loop with a short sleep up to `PUSH_LOCK_TIMEOUT_S`; or keep the alarm and specify that the handler raises.

**SI3 — Nit — `--no-web` has no stated user.**
`--once` already implies no web server. Drop the flag unless a test needs it.

**SI4 — Nit — F8's "restart the server" is mostly unnecessary.**
`socketserver` already isolates per-request exceptions. The only realistic thread death is a failed bind, so a retry-bind loop with a sleep is the whole feature.

## 4. Code and UX sharing opportunities

**SH1 — Minor — split `consume_local_queue` into claim and commit.**
`claim_queue(path)` merges into `.processing` and returns entries without deleting. `commit_queue(path)` unlinks. `consume_local_queue` becomes claim plus commit, and push uses claim, ships, then commits. This fixes A3 and makes I4's V6 true. The existing queue tests (L496–571) keep pinning the consume behaviour.

**SH2 — Minor — one helper for the remote path.**
`consume_remote_queue` does the `~` to `$HOME` substitution inline (L243) and K1 says push follows the same rule.
Fix: extract it and use it in both.

**SH3 — Minor — one atomic-write helper.**
C6 already specifies temp-write plus rename for `relay.state`. `append_to_history` needs the same (A9).

**SH4 — Nit — the page re-implements the icon map and bell rule in JS while their Python tests are deleted.**
My read: accept the duplication. It is five icons and one comparison, and serving them from `/api/state` is more machinery than it saves.

## 5. Phase dependencies and ordering

**OR1 — Material — C10 inside S1 removes the only relay before the new one is proven.**
Verified: `~/bin/newsdesk` on micro-m4 is a symlink into `/Users/dave/Developer/newsdesk`, so micro-m4 runs whatever is in the working tree. Once the TUI is deleted on the branch, a `watch` window that closes for any reason cannot be reopened. That holds for however long S1 takes, and the plan has no rollback if S5–S11 fail.
Fix (Recommended): split S1. S1a builds C1–C9 with `watch` intact. A new step after S11 removes the TUI (C10) and then merges. S2's "`newsdesk watch` no longer exists" check moves with it. Until then, rollback is "restore `remote_machines`, remove `hub`, reopen `watch`".

**OR2 — Material — S2's `git pull` will not fetch the work.**
S1 pushes branch `relay-hub`; the merge to main is S12. The mini's clone needs `git fetch && git checkout relay-hub`. After S12 it needs to return to main, pull, and `launchctl kickstart -k` the relay. Neither is listed.

**OR3 — Minor — S3's `relay --once` is a live consumer.**
It consumes the mini's queue and forwards while micro-m4's `watch` is still polling that queue (F6 in miniature), at whatever threshold the mini's config has (G1).
Fix: S3 runs `relay --once --no-pushover`.

**OR4 — Minor — S4 is verified by `cat` of the config.**
A broken push (host alias, ssh auth from the detached child, python path) stays invisible until S8.
Fix: end S4 with one `newsdesk send` and `ssh mini 'wc -l ~/.local/share/newsdesk/queue.jsonl'`.

**OR5 — Minor — S6 has unlisted prerequisites.**
L3 mentions MagicDNS and HTTPS certificates but no step enables them. Nothing checks whether `tailscale serve` already has a handler on 443 that this command would replace.
Fix: add a pre-check step (`tailscale serve status`, admin-console settings) before S6.

## 6. Testing strategy gaps

**TG1 — Material — nothing pins push recovery.**
T3 covers one failure. There is no test for the cases in A3 and A4:
- an existing `.processing` plus a new queue, then a second failure: all entries still on disk;
- a `.processing` older than `STALE_PROCESSING_AGE`: still shipped;
- a success after two failures: each entry shipped once.

**TG2 — Material — T4 asserts the command's text, not its behaviour.**
Fix: build the remote command in a function and run it with `sh -c` against a `tmp_path` queue, with no ssh and no mocks. Cases: full input lands; truncated input (byte count mismatch) leaves the queue untouched and exits non-zero; a concurrent consumer loop loses nothing. That makes V5 real.

**TG3 — Minor — "mock clock" (T2, T9, T10) needs a design the plan does not state.**
A `q.get(timeout=…)` keepalive ignores a mocked clock.
Fix: specify the relay as a `relay_cycle(state, now)` step function driven by the loop, and have T9 monkeypatch `SSE_KEEPALIVE_S` to a small value.

**TG4 — Minor — failure modes and robustness claims with no test.**
- F8: web thread or bind failure.
- C1 robustness: a forward that raises, or an entry with a non-integer priority, does not stop the cycle.
- The SSE `state` event.
- W2 during a history rewrite (A9).
- W2 with a late-arriving old `ts` (A10).
- `/fail` not sent under `--no-pushover`.
- C3: a `Popen` that raises still leaves the entry queued and `send` returning 0.

**TG5 — Minor — C5 has no automated check.**
The global CLAUDE.md carve-out asks for assertions against rendered output.
Fix: T8 also asserts that the served HTML contains the element ids the page script depends on.

**TG6 — Nit `[mechanical]` — S1's test command does not run on micro-m4 as written.**
Verified: there is no `python` on PATH, and the Homebrew and system `python3` have no pytest. `.venv/bin/python -m pytest tests/ -q` works. The same line in `CLAUDE.md` and `README.md` is stale.

## 7. Risks with mitigations

**RK1 — Material — a crash-looping relay can stay green.**
C1 pings "at the end of a cycle that completed" every 60 s but does not say when the first ping is. If it is on the first cycle, a relay that dies after 20 s and is restarted by launchd pings every restart and never goes red.
Fix: the first ping fires only after `HEARTBEAT_INTERVAL_S` of uptime.

**RK2 — Material — forwarding is still synchronous and unlogged on what is now the 24/7 alert path.**
`forward_to_pushover` blocks up to 10 s per entry (L310) and swallows every failure (L311). The plan made the Healthchecks curl non-blocking for this reason but left forwarding blocking. The log line `forwarded=1` would also be untrue after a Pushover rejection.
Fix: without pulling X3 fully in, have the function return success and log `forward_failed=N`.

**RK3 — Material (Phase 2) — `SSH_COMMAND_TIMEOUT = 3` can lose box entries.**
The remote command ends `cat "$p" && rm "$p"` (L250). If the 3 s timeout fires after the remote `rm` but before the output arrives, `TimeoutExpired` returns `[]` (L266) and the entries are gone. Cold connections to the Michigan boxes over a relayed path plausibly exceed 3 s. This is inferred, not run.
Fix: before Q3, measure cold `ssh <box> true` and raise the timeout for the relay's cadence. A two-step read then acknowledge-delete is better.

**RK4 — Minor — tokens are read once at startup.**
A transient Keychain miss at login leaves the relay pinging `/fail` until someone restarts it.
Fix: retry the read on each heartbeat while tokens are missing.

**RK5 — Minor — an absent Healthchecks URL is silent un-monitoring.**
K5 says "absent: logs once, no ping". A Healthchecks check that has never been pinged does not alert. S5's "check green" catches it at deploy, not later. See A8 for why this is likely on first run.

**RK6 — Minor — `web_bind` set to the tailnet IP can fail at boot.**
Tailscale may not be up when the relay starts.
Fix: SI4's retry-bind loop covers it.

## 8. Verification criteria specificity

**VC1 — Material — V4's "< 50 ms" is not anchored to a baseline.**
Measured once here: `./newsdesk` with no arguments took 55 ms wall. `send` today adds a config load, an append and a full re-read in `_maybe_rotate` (L387–397). V4 may fail before any hub code exists.
Fix: "with the hub unreachable, `send` takes no more than 20 ms longer than with no `hub` configured, median of 10".

**VC2 — Minor — no criterion covers loss or duplication under concurrency.**
That is the property C2 exists for.
Fix: add one. For example, 500 sends on micro-m4 with distinct titles while the relay runs, then exactly 500 distinct titles in the hub's history, none twice.

**VC3 — Minor — several step checks are not falsifiable as written.**
- S8 "one batch, not five" (I10). Use "all five rows, each once, at most two ssh connections".
- S3's expected output (I11).
- S11 "wait 10 min" equals period plus grace exactly. Use "alert within 12 min".
- S9 "without a hand" should say whether a login at the console counts as a hand.

**VC4 — Nit — V3 says "every terminal closed" but S7 runs the send in a terminal.**
Use a delayed send (`sleep 60 && newsdesk send …`) from a session that is then closed.

## 9. Actionability

**AC1 — Material — C2 step 1 "recover any `.processing`" is not a specification.**
Read as "do what `consume_local_queue` does", it loses data (A3). The step must say what is on disk after each failure.

**AC2 — Minor — the relay-to-web hand-off is unspecified.**
Missing: the subscriber list and its lock; SSE response headers (`Content-Type: text/event-stream`, `Cache-Control: no-cache`) and a flush after every write; `daemon_threads = True`; how the page orders rows (by `ts` or by arrival).

**AC3 — Minor — heartbeat edge cases are unspecified.**
The first tick (RK1). Whether `/fail` applies under `--no-pushover`: it should not, since tokens are deliberately unread.

**AC4 — Minor — L2 says "substitutes `__HOME__`" but L1 does not show the template.**
The interpreter path is not addressed either (G3).

## 10. Code churn

**CH1 — Minor — churn is larger than the plan implies, and four "reused" functions need changes.**
- About 355 of 731 lines removed (I2) and 30 of 72 tests (I1).
- Functions the plan treats as reused unchanged that need edits: `consume_local_queue` (SH1), `consume_remote_queue` (A7), `append_to_history` (A9), `read_keychain_token` (A8).
- §2.5's "Phase 2 code: None" holds only if A7 and RK3 are fixed in Phase 1.

**CH2 — Minor — `CLAUDE.md`'s architecture statements become false and O4 does not list them.**
"Single-file CLI", "no server", "curses for watch UI", and the two ABOUTME lines of `newsdesk.py` (L1–2) all change. `web/index.html`, the plist and the install script each need ABOUTME headers.

## 11. Assumption accuracy

**Verified as the plan states:**
- `consume_local_queue(queue_path)` L213, `consume_remote_queue(host, queue_file)` L240, `append_to_history(history_path, new_entries)` L92, `forward_to_pushover(entry, app_token, user_key)` L284, `should_forward_pushover(entry, min_priority=-1)` L160, `send_notification(...)` L355, `_maybe_rotate(queue_path)` L387 and `load_config(path)` L49 all exist with those signatures.
- `forward_to_pushover` has exactly one call site, L446 inside `cmd_watch_curses`.
- `POLL_INTERVAL_S` is 2; `STALE_PROCESSING_AGE` is 86400.
- The config path is `~/.config/newsdesk/config.json` (L652), as S4 uses.
- Host alias `mini` is in micro-m4's config.
- `send` today: loads config, appends one JSON line (L381–382), re-reads the whole queue to rotate (L384), returns 0 with every exception swallowed (L664–666). No network.

**A1 — Critical — a dropped connection still appends, so staging does not prevent a torn line.**
Evidence (inferred from ssh semantics, not run): with no pty, when the connection dies sshd closes the remote command's stdin. `cat > "$t"` sees EOF and exits 0, and `&& cat "$t" >> "$f"` runs on whatever arrived. The remote cannot tell disconnect-EOF from end-of-data.
- A truncated batch ends without a newline. The next append to the hub queue is glued onto it, fails `json.loads`, and is skipped silently (L82–83). That next entry could be a locally sent priority-2 alarm.
- Small batches usually fit one packet, so this is low probability. It grows with backlog size, which is exactly the post-outage case.
Fix: micro-m4 passes the byte count, and the remote runs `[ "$(wc -c < "$t")" -eq N ]` before committing, else `rm` and exit 1.

**A2 — Critical — the remote `>>` races the relay's rename and can lose a whole batch.**
C2's "no loss either way" assumes open and write are one instant. They are not.
- The remote shell opens `$f` for append, then execs `cat`, which writes a few milliseconds later.
- If the relay's `rename` and read (L228–233, verified) fall in that gap, the bytes land in an inode the relay has already read and unlinked.
- ssh exits 0, micro-m4 deletes `.processing`, and the batch is gone.
- If `$f` did not exist, the shell creates it empty and the same thing happens.
- Rough estimate: gap of 1–3 ms against a 2 s poll gives about 0.05–0.15% per push. At a push per Claude Code turn that is an entry every few days. Inferred, not measured; see spike SP5.
Options:
- (Recommended) After the byte check, `mv "$t"` to a unique `queue.jsonl.in.<id>` and have the relay consume `queue.jsonl` plus every `queue.jsonl.in.*`. Rename is atomic, so there is no gap and no torn line.
- Run `newsdesk ingest` on the mini to validate and append in one `write`. This narrows the gap to the microseconds every local `send` already has (A12) but does not close it.

**A3 — Critical — C2's recovery, done the way the existing code does it, loses the backlog on the second failure.**
Verified: `consume_local_queue` recovers by reading `.processing` into memory and unlinking it (L219–223), then renames the queue over the same name (L228).
A push that mirrors this holds the older entries only in memory. If ssh fails again, "leave `.processing` in place" preserves only the newer batch. A bare `os.rename` onto an existing `.processing` replaces it outright.
Fix (Recommended): if `.processing` exists, ship it as it stands first and delete it on success. Only then rename the queue. On failure, exit without touching the queue. Pin with TG1.

**A4 — Critical — the 1-day stale rule deletes an undelivered backlog unread.**
C2 step 1 recovers only a "fresh, < `STALE_PROCESSING_AGE`" file, inheriting L220–223, where an older file is unlinked without being read.
That rule exists so a consumer does not replay day-old alerts. In push it means a laptop closed for a weekend after one failed push silently discards that batch on Monday. That contradicts N3 and F4's "delayed, never lost".
Fix: push never applies the stale rule.

**A5 — Material — `_maybe_rotate` caps the offline backlog at 200 entries.**
Verified L395–397: past 200 lines the queue is cut to the newest 100. With the mini unreachable and the fix in A3 (backlog waits in `queue.jsonl`), a long outage drops the oldest entries.
Options: (Recommended) state the cap in F4 and N3 as an accepted bound; or skip rotation while `hub` is configured and a `.processing` exists.

**A6 — Material — retries duplicate entries; N4's "exactly once" does not hold for push.**
If the remote commit succeeds but the exit status never arrives (lid closed, ssh killed by G2's timeout, crash before the delete in step 5), the batch is re-shipped. Result: duplicate history rows and, for forwardable priorities, duplicate pages.
Options: (Recommended) add an `id` (uuid) to each entry at L366 and have the relay skip ids already in history; or declare at-least-once in N4 and accept it.

**A7 — Material — `consume_remote_queue` cannot report success.**
Verified L264–267: ssh failure, timeout and an empty queue all return `[]`. The remote command also exits non-zero when there is simply no queue file (`mv` fails, L250).
So C6's "last successful pull", the `remotes=3/3` log field, Q3's check and D13's rationale cannot be implemented on the function as it stands.
Fix: end the remote command with `; true` so exit 0 means "reached", and return `(entries, reached)`.

**A8 — Material — `read_keychain_token` cannot read the Healthchecks URL.**
Verified L319: the account is hardcoded as `-a pushover`. K5 and S3 store the URL under account `dave`. The lookup fails, the relay "logs once, no ping", and the check never starts (RK5).
Fix: add an `account="pushover"` parameter.

**A9 — Material — `append_to_history` rewrites the file in place while web threads read it.**
Verified L104: `open(history_path, "w")` truncates and then writes. Today the only reader is the same thread. With C4, a W2 request during the rewrite sees a prefix, missing exactly the newest entries the page's reconnect refetch is asking for.
Fix: temp-write plus `os.replace` (SH3).

**A10 — Material — `since=<ts>` filters on the sender's clock.**
Verified L367: `ts` is `time.time()` at send on the sending machine. History order is consume order.
An entry that arrives late carries an old `ts`: a micro-m4 backlog after an outage (F4), or a box's boot message before its clock is set. A page that already saw a newer `ts` never fetches it. "Nothing consumed during a disconnect is missed" is false for exactly the delayed entries.
Fix (Recommended): the relay stamps each entry with a hub-side receive counter or time at consume, and `since` filters on that. The plan's "no sequence numbers" does not survive late arrivals.

**A11 — Minor — the relay is at-most-once between consume and history.**
Verified: `consume_local_queue` has deleted the entries from disk (L233) before the caller appends to history or forwards. A `kill -9` in that window (S11 does one) loses them. This is existing behaviour, now on an unattended process.
Fix: accept and state it in N4, or use SH1's claim and commit so the relay commits after the history write.

**A12 — Nit — every local `send` already has a microsecond version of A2.**
Open at L381, write at L382, against rename and read at L228–231. It is unchanged by this plan and mentioned only so the invariant is not overstated.

**A13 — Nit `[mechanical]` — `README.md` and `CLAUDE.md` give the config path as `~/.local/share/newsdesk/config.json`.**
The code uses `~/.config/newsdesk/config.json` (L652, L671, L679). Fold the correction into O4.

## Open decisions

| ID | Recommendation holds? | Note |
|----|----|----|
| D1 | Yes | Needs SH1 so push has its own claim/commit; fix V6 (I4) |
| D4 | Yes | Fix RK3 and I8 before Phase 2 |
| D6 | Yes | It also makes S3 safe (OR3) |
| D7 | Yes | Add the branch checkout and post-merge steps (OR2) |
| D8 | Yes, with two changes | It is 30 tests, not ~9 (I1); do the removal after S11, not in S1 (OR1) |
| D9 | Conditional | Depends on SP1; run it before S1, not at S6 |
| D10 | Yes | State whether "newest" means `ts` or arrival (A10, AC2) |
| D11 | Yes | Web threads are unaffected by a relay loop blocked in ssh or curl |
| D12 | Yes, as a decision for the cutover | G1 must set the value explicitly on the mini either way |

## Spikes

None of these is passed or failed here. Each is small and each can change the design, so all but SP6 belong before S1.

| ID | Assumption | Spike | Where |
|----|----|----|----|
| SP1 | `tailscale serve` carries a long-lived SSE stream without buffering or idle disconnects | A 20-line `http.server` on the mini emitting one event a minute and a comment every 15 s, behind `tailscale serve`. Watch from the laptop and the phone for 30 minutes; confirm events arrive singly and the stream survives idle. | Before S1 (D9, N6 and K4 depend on it) |
| SP2 | A LaunchAgent on the headless mini can read the Keychain | A throwaway plist that runs `security find-generic-password … -w` and logs the exit code, plus `python3 --version` and `echo $PATH`. Reboot the mini with nobody at the console. Also establishes whether a `gui/` agent starts without a login, which is what S9's "without a hand" turns on. | Before S1; needs G1's tokens first; re-confirmed at S9 |
| SP3 | The detached push survives the exit of the Claude Code hook | A temporary Stop hook that `Popen`s `sh -c 'sleep 20; date >> file'` with `start_new_session=True`, then check the file. Repeat with `ssh -o BatchMode=yes mini true` as the child to confirm ssh auth works from that context. | Before S1 (C3 and D3 rest on it) |
| SP4 | A `send` run from Claude Code's sandboxed Bash tool can spawn a working push | Same child as SP3, launched from a sandboxed Bash call. The child likely inherits the sandbox and cannot reach the mini. If so, confirm the failure leaves `.processing` intact for the next hook-spawned push. | With SP3 |
| SP5 | The hub-side commit is safe against the relay's rename and against truncated input (A1, A2) | Locally, with no ssh: run the remote command 10,000 times against a loop doing rename-read-delete and count lost lines. Separately, `(printf partial; sleep 5) \| ssh mini '…'`, kill the client, and inspect the queue. | Before S1; it decides SI1 |
| SP6 | Healthchecks behaves as D5 and RK5 assume | On the new check: confirm a never-pinged check does not alert, and that `/fail` every 60 s produces one Pushover Emergency, not one per ping. | At S3 |
| SP7 | (Phase 2) launchd on the mini can reach 100.x peers, and 3 s is enough for a cold ssh to a box | Already flagged at Q3 for Local Network Privacy. Add a timed cold `ssh <box> true` from a LaunchAgent (RK3). | Before Q3 |
