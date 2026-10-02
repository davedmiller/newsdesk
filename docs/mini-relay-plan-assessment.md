*Assessment of `docs/mini-relay-plan.md` — 2026-10-02. Produced by a background agent (`/ddm-assess-plan`) against the plan at commit `3199664`; its output is reproduced verbatim below.*

# Assessment: `docs/mini-relay-plan.md`

*2026-10-02. Holistic read against `newsdesk.py` and the test suite; nothing run, no files changed.*

## Verdict

The architecture is right and the core is appropriately sized; trim before building, don't split. The review's fixes hardened the laptop push path, which by the plan's own account (C2) carries nothing that pages. Meanwhile the path that does page (mini queue → Pushover) still drops a failed forward. The viewer's live-update machinery is the other excess. With X1–X3 applied, build as written.

**Churn** is low until S12: S1 is additive plus small signature changes, then one clean deletion of about half the file and 27 tests. **Reuse** is good: `parse_jsonl`, `append_to_history`, `should_forward_pushover`, `forward_to_pushover` and `consume_remote_queue` all carry over.

**Worth their weight:** loop-issued Healthchecks ping with delayed first ping, `relay.lock`, claim/commit, `.processing` kept without age limit, the push lock, temp-file-then-`mv` spool, TUI removal deferred to S12, and spikes SP2 and SP3.

## Findings

**X1 — Material — SSE plus `seq` is the largest mechanism not paying for itself.**
It brings the subscriber list, lock, bounded queues, keepalives, seq stamping and restart scan, `since`, SP1's 30-minute soak, T7, T9 and three constants.
Recommend: the page polls `/api/history?limit=200` every few seconds and re-renders; history file order already is arrival order.
Given up: page latency goes from about 2 s to the poll interval (V7 changes). Nothing on the paging path.

**X2 — Material — the pager path is still at-most-once (F7/X3).**
A priority-2 forwarded during an internet blip shorter than the Healthchecks grace is lost from the phone, with only a log counter.
Recommend: pull a minimal retry into scope — failed forwards kept in memory and retried each cycle until an expiry. About 15 lines and one test.

**X3 — Minor — the remote byte-count check duplicates D14.**
A truncated batch leaves a torn last line that `parse_jsonl` already skips; the sender resends and dedupe drops the repeats.
Recommend: keep temp-file-then-`mv`; drop the `wc -c` test, SP5 and half of T4.
Given up: an entry without an `id` could duplicate after a cut transfer. Only a pusher on old code produces those, and micro-m4 is the only pusher.

**X4 — Minor — Phase 2 machinery ships in Phase 1.**
The `(entries, reached)` return, `remotes` state, remote cadence, T2 and T16 serve a deferred phase.
Recommend: move them to Phase 2.
Given up: nothing now; Phase 2 becomes small code plus config instead of config-only.

**X5 — Minor — dedupe as specified misses duplicates within one cycle.**
Two spool files carrying the same batch, both claimed after a relay restart, each pass an "already in history" check.
Recommend: seed the seen-set from history, add ids as entries are accepted, and add the case to T12.

**X6 — Minor — the document is about 9,300 words for roughly 500 lines of code.**
It is still implementable through its IDs, but rationale and review history sit inside spec cells (C1's heartbeat row, D12's freeze-watch brief).
Recommend: move §6, D12's detail and the review cross-references to an appendix, so §3, §5 and §8 read as the build spec.

**X7 — Nit — the push lock's 30 s wait loop is unnecessary.**
The lock holder already loops to drain later sends.
Recommend: `LOCK_NB`, exit if held; drop `PUSH_LOCK_TIMEOUT_S`.
Given up: an entry landing in the instant between the holder's final empty check and its unlock waits for the next send, which D3 already accepts.

## Severity count

Critical 0 · Material 2 · Minor 4 · Nit 1
