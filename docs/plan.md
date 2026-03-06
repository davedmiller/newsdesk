*Last updated: 2026-03-06 11:50 MST*

# newsdesk — Unified Notification Hub

## Context

Pushover notifications have variable/slow latency (3-30s) due to the Pushover → APNs chain. When Dave is at his desk, notifications should arrive near-instantly. When away, Pushover still delivers to his phone. Currently, notification logic is scattered across shell scripts in PostCardMaker.

**newsdesk** consolidates everything into a single Python CLI with two subcommands:
- `newsdesk send` — writes JSONL locally (replaces `pushover_notify`)
- `newsdesk watch` — polls local + remote JSONL files, displays in terminal, optionally forwards to Pushover

**Repo:** `~/Developer/newsdesk` (pushed to GitHub, project-agnostic)

## Architecture

```
Any script (either machine)         newsdesk watch (microM4 terminal)
───────────────────────────         ─────────────────────────────────
newsdesk send "title" "msg"         Polls every 2s:
  → appends to queue JSONL             ← local queue (direct read)
     (ephemeral, cleared               ← remote queue (ssh mini cat ...)
      after watch reads)                → appends to history.jsonl (1000 entries)
                                        → terminal display + bell
                                        → Pushover forwarding (if ON)
                                        → clears queue after processing
```

## Terminal UI

```
newsdesk — (L)atest (H)istory (C)lear (S)ave (P)ushover (Q)uit
Pushover: ON  ⏳ polling local + mini
────────────────────────────────────────────────────────────

10:42:15  ✅ Tests Passed — 2400 passed, 5 skipped
10:44:02  🔔 Claude: Permission — Bash: git push -u origin HEAD
10:51:30  🚀 Release Pipeline — Step 1/3: Releasing v1.08.00...
10:53:44  🚀 Release Pipeline — ✅ Pipeline complete!
```

- **Latest (default):** auto-scrolling feed, new notifications append
- **History:** scrollable view (j/k or arrows)
- **Clear:** clears display
- **Save:** dumps to `newsdesk-YYYY-MM-DD-HHMM.log`
- **Pushover:** toggle Pushover forwarding on/off
- **Quit:** exit
- Single-keypress commands, light curses for keyboard input
- Bell (`\a`) for priority >= 1

## Config

`~/.config/newsdesk/config.json` (created on first run with defaults):

```json
{
  "notifications_file": "~/.local/share/newsdesk/notifications.jsonl",
  "remote_host": "mini",
  "pushover_enabled": true
}
```

Pushover tokens stay in macOS Keychain (already set up on both machines):
- `security find-generic-password -a pushover -s pcm-app-token -w`
- `security find-generic-password -a pushover -s pcm-user-key -w`

Pushover forwarding happens in `watch`, not `send`. The `send` command has zero network dependencies.

## Files

### newsdesk repo (`~/Developer/newsdesk` → GitHub)

| ID | File | Action |
|----|------|--------|
| F1 | `newsdesk.py` | **Create** — CLI with `send` and `watch` subcommands |
| F2 | `README.md` | **Create** — setup, usage, config, wiring into projects |

### PostCardMaker repo (done in a separate session back in PCM)

| ID | File | Action |
|----|------|--------|
| F3 | `scripts/notify.sh` | **Rewrite** — replace Pushover logic with `newsdesk send` call |
| F4 | `scripts/notify-pushover.sh` | **Delete** — no longer needed |
| F5 | `scripts/scenario-notify.sh` | **Delete** — no longer needed |
| F6 | `.claude/hooks/pushover-notify.sh` | **Rewrite** — call `newsdesk send` directly |

### Global Claude hooks (move notification hook from project to global)

| ID | File | Action |
|----|------|--------|
| F7 | `~/.claude/hooks/newsdesk-notify.sh` | **Create** — global hook that calls `newsdesk send` on permission/idle prompts |

The current `pushover-notify.sh` hook is project-level (PCM only). Moving it to `~/.claude/hooks/` makes it fire for all Claude sessions on both machines. The PCM project hook (F6) gets deleted once the global hook is in place.

### Per-machine setup

Both machines clone newsdesk repo. Add `~/Developer/newsdesk` to PATH (or symlink `newsdesk` into a PATH dir).

| Machine | Role |
|---------|------|
| **microM4** | Runs `newsdesk send` (from any project). Runs `newsdesk watch` in a terminal tab. |
| **microMacMini** | Runs `newsdesk send` only. microM4's watcher pulls its JSONL via SSH. |

### Wiring into any project

Any project's scripts or Claude hooks can call:
```bash
newsdesk send "title" "message" --priority 0
```

No sourcing, no library, just a CLI call. Fails silently if newsdesk isn't installed.

## S1. Create `newsdesk.py`

Single-file Python CLI. No dependencies beyond stdlib + `subprocess` for Pushover curl and SSH.

**Subcommands:**

### `newsdesk send "title" "message" [--priority N]`

1. Read config for notifications file path
2. Append JSONL line: `{"ts":..., "title":"...", "message":"...", "priority":N, "source":"newsdesk"}`
3. Best-effort — any failure exits 0
4. Rotate queue if >200 lines (keep last 100) — safety cap for when watch isn't running
5. No network calls, no Keychain access — just a file append + rotation check
6. The queue file is ephemeral; `watch` clears it after reading during normal operation

### `newsdesk watch [--pushover | --no-pushover]`

1. Read config for notifications file path, remote host, and pushover_enabled default
2. `--pushover` / `--no-pushover` flags override config default
3. If Pushover enabled, read tokens from Keychain at startup (warn if missing)
4. Enter curses mode, show header with commands and Pushover status
5. Poll loop every 2s:
   - Read local queue JSONL, collect new entries
   - Run `ssh -o ConnectTimeout=2 -o BatchMode=yes <remote> cat <path>` via subprocess with 3s timeout, collect new entries
   - Append new entries to `history.jsonl` (master log, kept to 1000 entries)
   - Clear local queue file after reading (truncate to 0)
   - Remote queue: track last-seen timestamp (can't clear remote file from watcher)
   - Display new entries in terminal
   - If Pushover ON: forward new entries to Pushover API (curl subprocess, best-effort)
6. Handle single-keypress commands (L/H/C/S/P/Q)
7. Ring `\a` for priority >= 1
8. `(H)istory` reads from `history.jsonl`, not the queue files
9. `(C)lear` clears the display only, not the history file

**Priority display:**

| Priority | Icon | Sound |
|----------|------|-------|
| -2 silent | (skipped entirely) | — |
| -1 quiet | (no icon) | — |
| 0 normal | ✅ | — |
| 1 high | 🔔 | bell |
| 2 emergency | 🔔 | bell |

### `newsdesk init`

Creates config file with defaults if it doesn't exist. Checks Keychain for Pushover tokens and reports status.

## S2. Create `README.md`

Setup, usage, config file format, Keychain setup for Pushover, wiring into projects.

## S3. Rewrite `scripts/notify.sh` (PostCardMaker)

Replace the Pushover logic with a `newsdesk send` call:

```bash
pushover_notify() {
    local title="$1"
    local message="$2"
    local priority="${3:-0}"
    newsdesk send "$title" "$message" --priority "$priority" 2>/dev/null || true
}
```

The `pushover_check()` function either removed or rewritten to check `which newsdesk`.

Keep the file as the PCM-specific wrapper so existing callers don't change.

## S4. Delete `scripts/notify-pushover.sh` and `scripts/scenario-notify.sh`

These were thin wrappers around `pushover_notify`. Callers that used them should call `newsdesk send` directly or continue through `notify.sh`.

**Check callers before deleting:**
- `notify-pushover.sh` → called by `pcm-implement.md` skill and `pushover-notify.sh` hook
- `scenario-notify.sh` → called by `pcm-scenario.md` skill

Update these callers to use `newsdesk send` directly.

## Edge Cases

| Concern | Mitigation |
|---------|-----------|
| newsdesk not in PATH | `notify.sh` wraps in `|| true`, scripts don't break |
| SSH hangs | `ConnectTimeout=2` + `subprocess timeout=3` + skip if previous poll pending |
| File doesn't exist yet | Return empty, no error |
| Malformed JSON | `try/except json.loads`, skip line |
| Queue rotation while watch not running | Sender caps at 200 lines; oldest drop but most recent 100 survive |
| Watch restarts | Reads full queue + history on startup; may re-display recent entries (acceptable) |
| Keychain missing tokens | Skip Pushover, warn in status line, local display still works |
| Terminal resize | curses SIGWINCH handling |
| Python not in PATH | Both machines have python3 available |

## Verification

| ID | Test | How |
|----|------|-----|
| T1 | send writes JSONL | `newsdesk send "Test" "Hello" --priority 0` then check JSONL file |
| T2 | send has no network calls | Disconnect network, send still works (just writes file) |
| T3 | watch shows local | Run watch, send a notification, verify it appears within 2s |
| T4 | watch shows remote | Send on microMacMini, verify it appears on microM4 within 4s |
| T5 | queue rotation | Send 250 notifications without watch running, verify queue capped at 100 |
| T6 | SSH failure silent | Disconnect Tailscale, verify watch still shows local, no errors |
| T7 | commands work | Test L/H/C/S/Q in watch mode |
| T8 | history retention | Verify history.jsonl caps at 1000 entries |
| T9 | Pushover toggle | Press P in watch, verify status changes and forwarding stops/starts |
| T10 | Pushover forwarding | With Pushover ON in watch, send a notification, verify phone receives it |
| T11 | PCM integration | Run PCM tests, verify newsdesk receives test result notification |
| T12 | hook integration | Trigger Claude permission prompt, verify newsdesk receives it |
| T13 | init | Run `newsdesk init`, verify config created and Keychain status shown |

## Implementation Order

1. Create `~/Developer/newsdesk` repo, `git init`
2. **F1** — `newsdesk.py` (send subcommand first, then watch)
3. **T1, T2, T5** — verify send + Pushover + rotation
4. **T3** — verify watch shows local
5. **F2** — README.md
6. Push to GitHub
7. **F3** — rewrite PCM `notify.sh`
8. **F4, F5** — delete old wrappers, update callers (skills + hook)
9. **T4, T6-T9** — end-to-end verification
10. Commit PCM changes
