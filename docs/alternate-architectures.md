<!-- ABOUTME: Architecture alternatives explored for newsdesk and not adopted. -->
<!-- ABOUTME: Reference for future revisits — what we considered, why we deferred. -->

# Alternate Architectures Considered

*Last updated: 2026-04-29 —:—*

## 1. Context

In April 2026 we explored several alternatives to the current newsdesk architecture (local JSONL queue + curses watcher + Pushover relay), driven by a converging set of needs:

- Move the always-on hub from Micro-M4 to Micro-Mac-Mini
- A web UI so the queue is viewable from anywhere
- Possibly phase out Pushover
- Reading notifications from agents (MCP)
- Honest gut check: is this an unmet need, or are we reinventing wheels?

**Outcome:** stay on current newsdesk for now. Revisit ntfy if the roadmap items above become pressing. This document captures the reasoning so a future session can pick up where we left off.

## 2. Alternatives Considered

### 2.1 Build a web UI on top of newsdesk

| ID | Item | Notes |
|----|------|-------|
| A1 | Headless poller daemon | Extract polling out of curses watcher so it can run as a service |
| A2 | stdlib `http.server` + SSE + static page | No new deps, fits "stdlib only" ethos |
| A3 | Tailscale-only deploy | No auth/TLS complexity |

Effort: half-day (read-only) to 2 days (parity with curses + SSE).

Why deferred: ntfy provides this for free, plus phone push.

### 2.2 Apple notifications as the UI

| ID | Item | Notes |
|----|------|-------|
| B1 | `osascript` output sink | One-line `display notification` per arriving entry |
| B2 | Toggle by priority/project | Same gating model as Pushover |

Effort: 1–2 hours.

Pros: Native Mac integration, free Focus/DND/Notification Center.

Cons:
- Doesn't replace Pushover (no phone push without a code-signed iOS app — much bigger project than a web UI)
- Loses queue browsability (Notification Center isn't searchable or exportable)
- `osascript display notification` has been getting flakier across recent macOS versions
- Modern UNUserNotificationCenter requires a signed app bundle

Verdict: useful as an *additional* output channel, not a replacement. Skipped for now.

### 2.3 Pivot to a standard logging system

Considered: Loki/Vector/Grafana, syslog, journald, macOS unified logging.

Verdict: rejected. Notifications and logs are different beasts:
- Logs: high volume, queried after the fact, retained for analytics
- Notifications: low volume, demand attention now, route to humans

Logging stacks don't naturally express priority + per-project Pushover toggles + relay semantics. Cost of running Loki+Grafana on a Mac mini is enormous for the volume we have.

If storage/query becomes a bottleneck later, the right upgrade is JSONL → SQLite *inside* newsdesk, not adopting an external logging stack.

A useful adjacent idea: a `newsdesk ingest` mode that tails a logfile and forwards matching lines to the queue. That lets newsdesk *consume* from logging sources without becoming one.

### 2.4 Switch to ntfy.sh (the recommended migration if/when revisited)

[ntfy.sh](https://ntfy.sh) — open-source pub/sub notification server.
GitHub: https://github.com/binwiederhier/ntfy
Docs: https://docs.ntfy.sh

Honest verdict: ntfy directly delivers ~80% of newsdesk's purpose **plus** the next several roadmap items (web UI, mobile push, multi-device, phase out Pushover) **for free**.

The only meaningful structural gap: **offline-tolerant send**. ntfy requires the server to be reachable when sending; newsdesk writes to a local file and works fully offline.

Other gaps (all easily mitigated):
- Per-project runtime Pushover toggle — encode via topic structure or small relay script
- MCP integration — none yet, but same effort to add as it would be for newsdesk
- Routing richer than topic = string — encode axes into topic names
- Project auto-detect from git, machine auto-detect — recreate in a 10-line shell wrapper

## 3. ntfy vs newsdesk side-by-side

| Concern | Newsdesk | ntfy |
|---|---|---|
| Send protocol | Local file write (JSONL) | HTTP POST to server |
| Offline send | Works fully offline | Requires reachable server |
| Storage | JSONL on disk (grep-able) | SQLite on server |
| Mac UI | Curses TUI | Web UI + CLI subscribe |
| Remote viewing | Only via SSH or planned web UI | Built-in web + apps |
| Phone push | Via Pushover (paid, third-party) | Native iOS + Android apps, free |
| Routing model | Priority + project + per-project Pushover toggle | Topics (1-axis) |
| Live runtime toggles | Per-project Pushover on/off in TUI | Static via topic structure |
| MCP integration | Could be added | None yet |
| Auth | None (local file) | Built-in users/ACLs/tokens |
| Maintenance | We own all bugs | Active OSS community |
| Deps | Python stdlib only | Go binary (or Docker) |
| Privacy | Stays on local disk | Goes through a server |
| Lines of code to maintain | ~700 | ~0 (plus a thin wrapper) |

## 4. CLI ergonomics — honest comparison

The newsdesk CLI's apparent advantages turn out to be ~10 lines of shell:

```bash
nd() {
    local title="$1" msg="$2" priority="${3:-default}"
    local project="${4:-$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null || echo default)}"
    curl -s -d "$msg" \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -H "Tags: $(hostname -s),$project" \
        "https://ntfy.local/$project" >/dev/null
}
```

That replicates project auto-detect, machine auto-detect, default project, and the positional `title message` ergonomics. Conclusion: don't preserve newsdesk Python "as a thin wrapper" if migrating — it would be nostalgia, not analysis.

## 5. MCP integration — separate evaluation

Considered adding an MCP server for newsdesk so agents can send/read structured.

Verdict: skip for now.

- **Send path:** already trivial via shell. MCP adds nothing because the Claude Code hook is already shelling out, and in-conversation sends would be one extra Bash call. No real ergonomic win.
- **Read path:** marginal value. Letting Claude query "what's been happening today" conversationally is nice, but a single `Bash` grep over `history.jsonl` (or ntfy's JSON export) gets ~90% there.

If MCP becomes desirable later, build it as a **sibling package** that imports from a shared core module. Don't pull MCP into the main CLI; that would force a real dependency on the Anthropic MCP SDK and break the stdlib-only ethos.

## 6. Migration footprint (if/when ntfy is adopted)

### Files referencing `newsdesk` on Micro-M4

| ID | Path | Type |
|----|------|------|
| M1 | `~/.claude/settings.json` | Likely hook reference |
| M2 | `~/.claude/claude.md` | Install instructions for cross-project setup |
| M3 | `~/.claude/hooks/newsdesk-notify.sh` | Global Notification hook script |
| M4 | `~/.zshrc` | PATH addition |
| M5 | `~/bin/newsdesk` | Symlink to repo wrapper |

### Other projects with code call sites

| ID | Project | Files |
|----|---------|-------|
| C1 | backup-migration | `monitor/check-backups.sh` |
| C2 | jon-audio | `notify.py` |
| C3 | PostCardMaker | `scripts/notify.sh`, `scripts/permission-hook.sh` |
| C4 | (global hook M3) | `~/.claude/hooks/newsdesk-notify.sh` |

Plus doc updates in: backup-migration/docs/*, OpenBrain/docs/auto-capture-hook-plan.md, jon-audio/README.md, PostCardMaker/CHANGELOG.md, PostCardMaker/CLAUDE.md, PostCardMaker/.claude/commands/pcm-{implement,scenario}.md, Developer/docs/uv-migration-plan.md.

### Mini was not swept

SSH from the Claude Code sandbox is blocked. Re-run the sweep on Micro-Mac-Mini directly when migrating.

### Migration shape

Roughly 4–6 actual code edits — each call site is `newsdesk send "Title" "msg" --priority N --project foo`. Replace with the `nd()` shell function above, or a direct `curl` to ntfy.

## 7. Recommended migration sequence (if/when revisited)

| ID | Step | Notes |
|----|------|-------|
| S1 | Trial ntfy.sh public server | 15 min — install iOS app, subscribe to a hard-to-guess topic, evaluate UX before any infra |
| S2 | Dual-emit from newsdesk | Add fire-and-forget POST inside `send_notification()`, env-var-gated. Run for a few days. Real volume, no risk, easily reversible |
| S3 | Self-host ntfy on Micro-Mac-Mini | `brew install ntfy`, launchd plist, Tailscale-only bind |
| S4 | Decide access model | Tailscale-only vs. Tailscale Funnel vs. internet+auth — see §8 |
| S5 | Switch dual-emit to local server | Confirm everything still works |
| S6 | Migrate call sites | Replace `newsdesk send` with `nd()` wrapper or direct `ntfy publish` |
| S7 | Drop curses watcher, Pushover relay code, SSH polling | Newsdesk Python repo can be archived |
| S8 | Drop Pushover entirely | Use ntfy iOS/Android apps |
| S9 | Skip MCP unless read-path use case materializes | Revisit later if needed |

## 8. Open architectural questions

| ID | Question | Notes |
|----|----------|-------|
| Q1 | Tailscale always-on on iPhone? | Determines whether Tailscale-only bind is viable for receive |
| Q2 | Ever send from offline machines? | If yes, ntfy's server-required model is a real loss; if no, it's fine |
| Q3 | Anomaly/pattern detection desired? | If yes, MCP read tools become more valuable; if no, skip |
| Q4 | Keep curses TUI as a fallback? | Free if poller is extracted cleanly; cuts more code if dropped |

## 9. Why we deferred

- Newsdesk works today; migration cost > current pain
- The roadmap items (hub on mini, web UI, phase out Pushover) are wants, not needs yet
- Hacking on newsdesk has independent value as a personal project
- ntfy will still be there when we revisit

The cleanest signal that it's time to migrate: any of the roadmap items becomes a real friction point — e.g., wanting to check the queue from the phone repeatedly and finding the SSH tunnel + grep workflow tiresome.
