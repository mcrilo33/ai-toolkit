#!/usr/bin/env bash
# afk-notify-wake — Notification hook: announce a parked spoke to the /afk supervisor (#176).
#
# WHAT IT CLOSES
#   The other half of the event-driven wake (#176). spoke-ready.sh announces the SCRIPTED
#   markers (gate/ready/blocked); this hook announces the UN-scripted parks — a permission
#   dialog or an AskUserQuestion — that Claude Code surfaces as a Notification. Without it
#   those parks would still wait up to a full 300s backstop tick for the supervisor's sweep
#   to notice them via transcript/pane analysis.
#
# WHAT IT DOES
#   On a Notification event, resolve the issue from the spoke's branch slug, drop a
#   content-free <epoch>-<issue>-park file in the event spool, and SIGUSR1 the heartbeat
#   pid so the supervisor wakes and services the spoke in seconds. Events are WAKE-UPS, not
#   state: the supervisor re-derives everything via slot_state, so a duplicate/stale/lost
#   signal is safe. Gated on a LIVE supervisor (heartbeat pid running), so an attended
#   session leaves no spool artifact and signals nothing.
#
# DISCIPLINE — best-effort, always exit 0. A Notification hook cannot block a tool call and
#   must never fail a session; every step degrades to a silent no-op (no git repo, a hub
#   checkout whose slug has no issue number, no live supervisor).
#
# The spool path + filename mirror the reader (afk_event_dir / afk_drain_event_issues in
# gate-broker.sh) and honor the same AFK_HEARTBEAT / AFK_STATE_DIR overrides. The emit is
# inlined (not shared) because this hook deploys to the platform hooks dir, a different
# location than the hub reader — the shared contract is the filename format, not code.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT="$(read_stdin)"

# Resolve the spoke worktree: the payload cwd when present, else the hook's own cwd.
WT="$(json_field "$INPUT" "cwd")"
[ -n "$WT" ] || WT="$(pwd)"

# Issue number from the branch slug (feature/176-foo -> 176). A hub checkout sits on the
# default branch (slug `main` -> no leading number), so this is self-limiting to spokes.
BR="$(git -C "$WT" branch --show-current 2>/dev/null || true)"
SLUG="${BR##*/}"
ISSUE="${SLUG%%[!0-9]*}"
case "$ISSUE" in '' | *[!0-9]*) exit 0 ;; esac

# Signal a LIVE supervisor only (its heartbeat pid is a running process); otherwise no-op.
COMMON="$(git -C "$WT" rev-parse --git-common-dir 2>/dev/null || true)"
[ -n "$COMMON" ] || exit 0
case "$COMMON" in /*) ;; *) COMMON="$WT/$COMMON" ;; esac   # rev-parse may print a relative dir
HB="${AFK_HEARTBEAT:-$COMMON/.afk-heartbeat}"
[ -f "$HB" ] || exit 0
PID="$(head -n1 "$HB" 2>/dev/null | awk '{print $1}')"
case "$PID" in '' | *[!0-9]*) exit 0 ;; esac
kill -0 "$PID" 2>/dev/null || exit 0

DIR="${AFK_STATE_DIR:-$COMMON/ai-toolkit-afk}/events"
mkdir -p "$DIR" 2>/dev/null || exit 0
: > "$DIR/$(date +%s)-$ISSUE-park" 2>/dev/null || exit 0
kill -USR1 "$PID" 2>/dev/null || true
exit 0
