*Last updated: 2026-03-06 13:30 MST*

# newsdesk — Unified Notification Hub

## Context

Pushover notifications have variable/slow latency (3-30s) due to the Pushover → APNs chain. When Dave is at his desk, notifications should arrive near-instantly. When away, Pushover still delivers to his phone. Currently, notification logic is scattered across shell scripts in PostCardMaker.

**newsdesk** consolidates everything into a single Python CLI with three subcommands:
- `newsdesk send` — writes JSONL locally (replaces `pushover_notify`)
- `newsdesk watch` — polls local + remote JSONL files, displays in terminal, optionally forwards to Pushover
- `newsdesk init` — creates config with defaults, checks Keychain status

**Repo:** `~/Developer/newsdesk` (pushed to GitHub, project-agnostic)

**Platform:** macOS Tahoe only. No other OS support needed.

## Setup

No build step. `newsdesk.py` contains all logic. A thin `newsdesk` shell wrapper provides the PATH entry point.

```bash
# Per machine — clone and symlink
git clone git@github.com:davedmiller/newsdesk.git ~/Developer/newsdesk
ln -s ~/Developer/newsdesk/newsdesk ~/bin/newsdesk
newsdesk init
```

## Architecture

Each machine has a single queue file. All senders on that machine (across any number of projects/sessions) append to the same queue. The watcher polls its own local queue plus the queue on each configured remote machine.

### JSONL record format

Both queue and history files use the same JSONL format — one JSON object per line:

```json
{"ts": 1709750535.123, "title": "Tests Passed", "message": "2400 passed, 5 skipped", "priority": 0, "project": "pcm"}
```

| Field | Type | Description |
|-------|------|-------------|
| `ts` | float | Unix timestamp (time.time()) when sent |
| `title` | string | Short notification title |
| `message` | string | Notification body |
| `priority` | int | -2 (silent) to 2 (emergency) |
| `project` | string | Project tag (defaults to DEFAULT_PROJECT) |

**Queue files** (`queue.jsonl`) exist on every machine. Senders append, watcher reads and clears. Ephemeral.

**History file** (`history.jsonl`) exists only on the watcher machine. The watcher merges all queues into history. This is what `(H)istory` reads and `(S)ave` exports. Capped at HISTORY_MAX_ENTRIES.

### Queue consumption (rename-read-delete)

To avoid data loss between concurrent senders and the watcher, queues are consumed using an atomic rename pattern:

**Local queue:**
1. If `queue.jsonl.processing` exists and is < 1 day old → read it, then delete (crash recovery)
2. If `queue.jsonl.processing` exists and is >= 1 day old → delete without reading (stale)
3. Rename `queue.jsonl` → `queue.jsonl.processing` (atomic; new sends create a fresh `queue.jsonl`)
4. Read `queue.jsonl.processing`
5. Delete `queue.jsonl.processing`

**Remote queue (single SSH command):**
```bash
ssh <host> 'f=<queue_file>; p="$f.processing"; \
  if [ -f "$p" ]; then \
    if [ "$(find "$p" -mtime -1)" ]; then cat "$p"; fi; \
    rm -f "$p"; \
  fi; \
  mv "$f" "$p" 2>/dev/null && cat "$p" && rm "$p"'
```

All operations happen in one SSH connection. The `mv` is atomic on local filesystems, so a sender either writes to the old file (processed this cycle) or creates a new one (picked up next poll).

```
Machine A (any number of senders)     newsdesk watch (watcher machine)
──────────────────────────────────     ─────────────────────────────────
newsdesk send "title" "msg"            Polls every POLL_INTERVAL_S:
  --project pcm                           ← local queue (rename-read-delete)
  → appends to local queue JSONL          ← remote queues (ssh rename-read-delete)
     (ephemeral, cleared                  → appends to history.jsonl
      after watch reads)                  → terminal display + bell
                                          → Pushover forwarding (per-project toggle)
Machine B (same pattern)
──────────────────────────────────
newsdesk send ...
  → appends to local queue JSONL
```

## Terminal UI

```
newsdesk — (L)atest (H)istory (C)lear (S)ave (P)ushover (Q)uit
Pushover: ALL ✓ | pcm ✓ | newsdesk ✗  ⏳ polling local + 1 remote
────────────────────────────────────────────────────────────

10:42:15  [pcm] ✅ Tests Passed — 2400 passed, 5 skipped
10:44:02  [pcm] 🔔 Claude: Permission — Bash: git push -u origin HEAD
10:51:30  [newsdesk] 🚀 Release Pipeline — Step 1/3: Releasing v1.08.00...
10:53:44  [newsdesk] 🚀 Release Pipeline — ✅ Pipeline complete!
```

- **Latest (default):** auto-scrolling feed, new notifications append
- **History:** scrollable view (j/k or arrows)
- **Clear:** clears display
- **Save:** dumps history to `~/.local/share/newsdesk/newsdesk-YYYY-MM-DD-HHMM.log`
- **Pushover:** per-project Pushover toggle (see below)
- **Quit:** exit
- Single-keypress commands, light curses for keyboard input
- Bell (`\a`) for priority >= 1

### Pushover per-project toggle

Pressing **(P)** enters Pushover mode — shows a numbered list of known projects:

```
Pushover forwarding:
  [A] ALL ✓
  [1] pcm ✓
  [2] newsdesk ✗
  [3] general ✓
Press number to toggle, A for ALL, Esc to close
```

- **ALL** is a master switch: OFF disables forwarding entirely, ON re-enables per-project settings
- Each project appears once its first message arrives
- New projects default to ON (per `pushover_projects_default` config)
- Toggle state is kept in memory (resets on restart); config sets initial defaults

## Config

`~/.config/newsdesk/config.json` (created by `newsdesk init` with defaults):

```json
{
  "queue_file": "~/.local/share/newsdesk/queue.jsonl",
  "history_file": "~/.local/share/newsdesk/history.jsonl",
  "remote_machines": [],
  "pushover_enabled": true,
  "pushover_projects_default": true
}
```

- `queue_file` — local queue path (all senders on this machine write here); expanded via `os.path.expanduser()` at load time
- `history_file` — watcher's merged history log (watcher machine only)
- `remote_machines` — list of machines to poll (empty by default; see README for setup). Each entry has a display `name`, SSH `host`, and remote `queue_file` path. Supports up to ~10 machines.

Example remote machine entry:
```json
{"name": "mini", "host": "mini", "queue_file": "~/.local/share/newsdesk/queue.jsonl"}
```

- `pushover_enabled` — initial state of the ALL toggle
- `pushover_projects_default` — whether new projects start ON or OFF

All config paths are expanded via `os.path.expanduser()` at load time.

Pushover tokens stay in macOS Keychain (already set up on both machines):
- `security find-generic-password -a pushover -s pcm-app-token -w`
- `security find-generic-password -a pushover -s pcm-user-key -w`

Pushover forwarding happens in `watch`, not `send`. The `send` command has zero network dependencies.

## Files

### newsdesk repo (`~/Developer/newsdesk` → GitHub)

| ID | File | Action |
|----|------|--------|
| F1 | `newsdesk.py` | **Create** — all CLI logic: `send`, `watch`, `init` subcommands |
| F2 | `newsdesk` | **Create** — thin shell wrapper (see below) for PATH entry point |
| F3 | `tests/test_newsdesk.py` | **Create** — pytest unit tests (TDD — written before implementation) |
| F4 | `README.md` | **Create** — setup, usage, config, wiring into projects, adding remote machines |

**F2 shell wrapper:**
```bash
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/newsdesk.py" "$@"
```

### PostCardMaker repo (done in a separate session back in PCM)

| ID | File | Action |
|----|------|--------|
| F5 | `scripts/notify.sh` | **Rewrite** — replace Pushover logic with `newsdesk send --project pcm` call |
| F6 | `scripts/notify-pushover.sh` | **Delete** — no longer needed |
| F7 | `scripts/scenario-notify.sh` | **Delete** — no longer needed |
| F8 | `.claude/hooks/pushover-notify.sh` | **Delete** — replaced by global hook |
| F9 | `.claude/settings.json` | **Edit** — remove Notification hook entry |
| F10 | `.claude/settings.local.json` | **Edit** — remove Notification hook entry |

### Global Claude hooks (move notification hook from project to global)

| ID | File | Action |
|----|------|--------|
| F11 | `~/.claude/hooks/newsdesk-notify.sh` | **Create** — global hook that calls `newsdesk send --project claude` on permission/idle prompts |
| F12 | `~/.claude/settings.json` | **Edit** — merge Notification hook entry pointing to F11 into existing settings (preserve all other keys) |

The current `pushover-notify.sh` hook is project-level (PCM only). Moving it to `~/.claude/hooks/` and registering it in global `settings.json` makes it fire for all Claude sessions on both machines.

### Per-machine setup

| Machine | Role |
|---------|------|
| **microM4** | Runs `newsdesk send` (from any project). Runs `newsdesk watch` in a terminal tab. Watcher polls local + all remote machines. |
| **microMacMini** | Runs `newsdesk send` only. Its queue is polled by microM4's watcher via SSH. |

### Wiring into any project

Any project's scripts or Claude hooks can call:
```bash
newsdesk send "title" "message" --priority 0 --project myproject
```

No sourcing, no library, just a CLI call. Fails silently if newsdesk isn't installed.

## S1. Create `newsdesk.py` CLI

Single-file Python CLI. No dependencies beyond stdlib + `subprocess` for Pushover curl and SSH.

**Constants (top of file, easily adjustable):**

```python
QUEUE_MAX_LINES = 200       # sender caps queue at this; rotates to half
QUEUE_ROTATE_TO = 100       # lines kept after rotation
HISTORY_MAX_ENTRIES = 1000  # watcher's history.jsonl cap
POLL_INTERVAL_S = 2         # seconds between poll cycles
SSH_CONNECT_TIMEOUT = 2     # seconds for SSH ConnectTimeout
SSH_COMMAND_TIMEOUT = 3     # seconds for subprocess timeout on SSH
DEFAULT_PROJECT = "general" # project name when --project omitted
STALE_PROCESSING_AGE = 86400  # seconds before .processing file is considered stale (1 day)
```

**Subcommands:**

### `newsdesk send "title" "message" [--priority N] [--project NAME]`

1. Read config for queue file path
2. Append JSONL line with fields: `ts`, `title`, `message`, `priority`, `project`
   - `--project` defaults to `DEFAULT_PROJECT` if omitted
3. Best-effort — any failure exits 0
4. Rotate queue if > `QUEUE_MAX_LINES` (keep `QUEUE_ROTATE_TO`) — safety cap for when watch isn't running
5. No network calls, no Keychain access — just a file append + rotation check
6. The queue file is ephemeral; `watch` clears it after reading during normal operation

### `newsdesk watch [--pushover | --no-pushover]`

1. Read config for queue file path, remote_machines list, pushover_enabled default, and pushover_projects_default
2. `--pushover` / `--no-pushover` flags override config default for ALL toggle
3. If Pushover enabled, read tokens from Keychain at startup (warn if missing)
4. Initialize project toggle state: ALL = pushover_enabled, new projects = pushover_projects_default
5. Enter curses mode, show header with commands and per-project Pushover status
6. Poll loop every `POLL_INTERVAL_S`:
   - Consume local queue via rename-read-delete pattern (see Architecture)
   - For each machine in `remote_machines`: consume remote queue via single SSH rename-read-delete command
   - Append new entries to `history.jsonl` (kept to `HISTORY_MAX_ENTRIES`)
   - Display new entries in terminal (prefixed with `[project]`)
   - For each entry: forward to Pushover if ALL is ON *and* that entry's project is ON
7. Handle single-keypress commands (L/H/C/S/P/Q)
8. Ring `\a` for priority >= 1
9. `(H)istory` reads from `history.jsonl`, not the queue files
10. `(C)lear` clears the display only, not the history file

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

## S2. TDD Test Suite

Write tests **before** implementation. Tests use pytest with tmp_path fixtures — no real filesystem, no network. Tests import directly from `newsdesk.py`.

**Phase 1 — send + config tests (before implementing send):**

| ID | Test | What it validates |
|----|------|-------------------|
| U1 | `test_send_appends_jsonl` | send writes valid JSONL with all fields (ts, title, message, priority, project) |
| U2 | `test_send_default_project` | omitted --project results in DEFAULT_PROJECT |
| U3 | `test_send_rotation` | queue > QUEUE_MAX_LINES gets rotated to QUEUE_ROTATE_TO |
| U4 | `test_send_creates_parent_dirs` | send creates missing directories for queue file |
| U5 | `test_parse_jsonl_valid` | valid JSONL lines are parsed correctly |
| U6 | `test_parse_jsonl_malformed` | malformed lines are skipped without error |
| U7 | `test_parse_jsonl_empty_file` | empty/missing file returns empty list |
| U12 | `test_config_defaults` | missing config file produces correct defaults |
| U13 | `test_config_loads` | valid config file is parsed correctly |
| U17 | `test_multi_machine_config` | remote_machines list parsed correctly, each with name/host/queue_file |

**Phase 2 — watch tests (before implementing watch):**

| ID | Test | What it validates |
|----|------|-------------------|
| U8 | `test_history_cap` | history.jsonl capped at HISTORY_MAX_ENTRIES |
| U9 | `test_priority_icon_mapping` | priority values map to correct icons |
| U10 | `test_priority_bell` | priority >= 1 triggers bell |
| U11 | `test_priority_silent_skipped` | priority -2 entries are not displayed |
| U14 | `test_pushover_project_toggle` | ALL OFF disables all; ALL ON respects per-project |
| U15 | `test_pushover_new_project_default` | new project inherits pushover_projects_default |
| U16 | `test_project_field_in_display` | display output includes [project] prefix |
| U18 | `test_queue_rename_read_delete` | rename-read-delete pattern works; .processing file cleaned up |
| U19 | `test_stale_processing_deleted` | .processing file older than STALE_PROCESSING_AGE is deleted without reading |
| U20 | `test_fresh_processing_recovered` | .processing file younger than STALE_PROCESSING_AGE is read then deleted |

## S3. Create `README.md`

Setup, usage, config file format, Keychain setup for Pushover, wiring into projects, adding remote machines.

## S4. Rewrite `scripts/notify.sh` (PostCardMaker)

Replace the Pushover logic with a `newsdesk send` call:

```bash
pushover_notify() {
    local title="$1"
    local message="$2"
    local priority="${3:-0}"
    newsdesk send "$title" "$message" --priority "$priority" --project pcm 2>/dev/null || true
}
```

The `pushover_check()` function either removed or rewritten to check `which newsdesk`.

Keep the file as the PCM-specific wrapper so existing callers don't change.

## S5. Delete `scripts/notify-pushover.sh` and `scripts/scenario-notify.sh`

These were thin wrappers around `pushover_notify`. Callers that used them should call `newsdesk send` directly or continue through `notify.sh`.

**Check callers before deleting:**
- `notify-pushover.sh` → called by `pcm-implement.md` skill and `pushover-notify.sh` hook
- `scenario-notify.sh` → called by `pcm-scenario.md` skill

Update these callers to use `newsdesk send --project pcm` directly.

## Edge Cases

| Concern | Mitigation |
|---------|-----------|
| newsdesk not in PATH | `notify.sh` wraps in `|| true`, scripts don't break |
| SSH hangs | `ConnectTimeout=SSH_CONNECT_TIMEOUT` + subprocess `timeout=SSH_COMMAND_TIMEOUT` + skip if previous poll pending |
| File doesn't exist yet | Return empty, no error |
| Malformed JSON | `try/except json.loads`, skip line |
| Queue rotation while watch not running | Sender caps at QUEUE_MAX_LINES; oldest drop but most recent QUEUE_ROTATE_TO survive |
| Watcher crash mid-read | Rename-read-delete pattern; .processing file recovered on next poll if < 1 day old, deleted if stale |
| Keychain missing tokens | Skip Pushover, warn in status line, local display still works |
| Terminal resize | curses SIGWINCH handling |
| Unknown project in toggle | New projects auto-appear with default ON/OFF per config |
| Remote machine unreachable | Skip that machine for this poll cycle, continue polling others |
| Multiple senders on same machine | All append to same queue file; file locking not needed (append is atomic for small writes) |

## Verification

### Unit tests (pytest, run in CI and locally)

See S2 above — tests U1–U20.

### Integration tests (manual)

| ID | Test | How |
|----|------|-----|
| T1 | send writes JSONL | `newsdesk send "Test" "Hello" --priority 0 --project test` then check JSONL file |
| T2 | send has no network calls | Disconnect network, send still works (just writes file) |
| T3 | watch shows local | Run watch, send a notification, verify it appears within 2s with [project] prefix |
| T4 | watch shows remote | Send on microMacMini, verify it appears on microM4 within 4s |
| T5 | queue rotation | Send 250+ notifications without watch running, verify queue capped at QUEUE_ROTATE_TO |
| T6 | SSH failure silent | Disconnect Tailscale, verify watch still shows local, no errors |
| T7 | commands work | Test L/H/C/S/Q in watch mode |
| T8 | history retention | Verify history.jsonl caps at HISTORY_MAX_ENTRIES |
| T9 | Pushover project toggle | Press P, toggle a project OFF, send from that project, verify no Pushover delivery |
| T10 | Pushover ALL toggle | Toggle ALL OFF, verify no Pushover for any project |
| T11 | Pushover forwarding | With Pushover ON in watch, send a notification, verify phone receives it |
| T12 | PCM integration | Run PCM tests, verify newsdesk receives test result notification with [pcm] |
| T13 | hook integration | Trigger Claude permission prompt, verify newsdesk receives it with [claude] |
| T14 | init | Run `newsdesk init`, verify config created and Keychain status shown |
| T15 | multi-sender | Run two concurrent `newsdesk send` from different projects, verify both appear in queue |
| T16 | remote queue cleared | After watch polls remote, verify remote queue.jsonl is consumed (renamed away) |
| T17 | no data loss | Send while watch is polling, verify no entries lost across 100 send/poll cycles |

## Implementation Order

1. Create `~/Developer/newsdesk` repo, `git init` *(done)*
2. **F3 phase 1** — write send + config tests (U1–U7, U12–U13, U17)
3. **F1** — `newsdesk.py` send subcommand + config loading, iterate until tests pass
4. **F3 phase 2** — write watch tests (U8–U11, U14–U16, U18–U20)
5. **F1** — `newsdesk.py` watch subcommand + project toggles, iterate until tests pass
6. **F2** — create `newsdesk` shell wrapper
7. **T1, T2, T5** — manual verify send + offline + rotation
8. **T3** — manual verify watch shows local
9. **F4** — README.md
10. Push to GitHub
11. **F5** — rewrite PCM `notify.sh`
12. **F6, F7, F8, F9, F10** — delete old wrappers/hooks, clean PCM settings
13. **F11, F12** — create global hook, merge into global settings
14. **T4, T6–T17** — end-to-end verification
15. Commit PCM changes
