# Newsdesk — Next Steps

*Last updated: 2026-03-11 —:—*

Ideas and potential improvements for newsdesk, roughly grouped by theme. Nothing here is committed — just a backlog to pick from.

## Reliability & Robustness

| ID | Item | Notes |
|----|------|-------|
| R1 | File locking for queue writes | Current append-is-atomic assumption holds for small writes but could break under heavy concurrent use |
| R2 | Graceful SSH fallback with backoff | Skip unreachable remotes silently, but add exponential backoff instead of retrying every poll |
| R3 | Health check subcommand | `newsdesk status` — show queue sizes, remote reachability, Keychain token status, config validation |
| R4 | Structured logging | Optional `--verbose` flag for watch that logs poll timing, SSH errors, Pushover responses to a debug log |

## Features

| ID | Item | Notes |
|----|------|-------|
| F1 | Notification filtering | Filter watch display by project, priority, or time range |
| F2 | Persistent Pushover toggle state | Save per-project Pushover toggle to disk so it survives restarts |
| F3 | `newsdesk tail` subcommand | Non-curses mode — just tail the history file, useful for scripting or quick checks |
| F4 | Notification deduplication | Suppress repeated identical messages within a configurable window |
| F5 | Notification grouping/batching | Batch rapid-fire notifications (e.g., 50 test results) into a single Pushover message |
| F6 | Priority escalation | Auto-escalate priority if the same project sends N+ notifications within M seconds |
| F7 | Custom sounds per project | Map projects to different Pushover sounds for audio differentiation |
| F8 | `newsdesk clear` subcommand | Clear local queue without waiting for watch to consume it |

## UI Improvements

| ID | Item | Notes |
|----|------|-------|
| U1 | Color themes | Configurable color schemes for the curses UI (or at least project-based coloring) |
| U2 | Timestamp format config | Let users choose between HH:MM:SS, relative ("2m ago"), or ISO format |
| U3 | Notification count badge | Show unread count in status line, reset on view |
| U4 | Search in history | `/` key to search history by keyword |
| U5 | Mouse support | Click to select notifications in history view |

## Integration

| ID | Item | Notes |
|----|------|-------|
| I1 | Webhook endpoint | Optional lightweight HTTP listener as alternative to SSH polling |
| I2 | macOS native notifications | Forward to Notification Center via `osascript` as alternative to Pushover |
| I3 | tmux status bar integration | Output current notification count for tmux status line |
| I4 | Launchd plist for watch | Auto-start watcher on login via launchd instead of manual terminal tab |
| I5 | JSON output mode for send | `newsdesk send --json` to return the queued record, useful for programmatic callers |

## Testing & Quality

| ID | Item | Notes |
|----|------|-------|
| T1 | Integration test harness | Automated end-to-end tests using a mock SSH server and temp queues |
| T2 | Performance benchmarking | Measure poll loop timing under load (many remotes, large queues) |
| T3 | CI pipeline | GitHub Actions running pytest on push |

## Cleanup / Debt

| ID | Item | Notes |
|----|------|-------|
| D1 | Update plan Keychain service names | Plan still references `pcm-app-token`/`pcm-user-key` but actual names are `newsdesk-app-token`/`newsdesk-user-key` |
| D2 | PCM cleanup | Verify old Pushover scripts (F6-F8 from plan) are fully removed from PostCardMaker |
