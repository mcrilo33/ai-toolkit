#!/usr/bin/env bash
# review-stamp-guard — beforeMCPExecution hook (Cursor, failClosed) and
# PreToolUse hook (Claude Code, telemetry-only).
#
# Cursor: gates the review-stamp MCP tool `approve_review` behind a review
# window: the call is allowed ONLY while `.review/.window` exists and is
# younger than the TTL. The window is opened by review-window-open.sh when a
# code-review subagent starts and closed by review-window-close.sh when it
# stops, so the implementing agent cannot mint its own approval outside a live
# review.
#
# beforeMCPExecution carries tool identity and workspace roots but NO agent
# identity — the decision is tool_name + window state only.
#
# Claude Code (#139): fires as PreToolUse on mcp__review-stamp__approve_review.
# There is NO window check on this path — nothing opens a window in Claude
# Code; enforcement there is (and remains) the positive per-agent MCP wiring
# in the code-review agent frontmatter. The hook only contributes the
# step:review cycle marker, so the REVIEW step auto-populates the spokecycle-
# trace. Only an explicit hook_event_name=PreToolUse takes this path — an
# absent/unknown event keeps the fail-closed Cursor behavior.
#
# Both allow paths emit the marker (idempotent on HEAD via
# telemetry_mark_cycle_step); a GUARD-denied call (no/stale window) emits
# nothing. The reviewer's verdict is not inspected: a REQUEST_CHANGES call on
# an allowed path still emits — the review gate did fire on that HEAD, and the
# fix round lands on a new HEAD and earns its own marker.
#
# Exit 2 = deny (Cursor: no window / stale window)
# Exit 0 = allow (other tools, a fresh window, or the Claude Code path)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

WINDOW_TTL_SECONDS=1800

INPUT=$(read_stdin)
TOOL_NAME=$(get_tool_name "$INPUT")

# Only the approve_review tool is gated — bare (Cursor) or MCP-prefixed
# (Claude Code names the tool mcp__<server>__approve_review).
case "$TOOL_NAME" in
  approve_review | mcp__*__approve_review) ;;
  *) exit 0 ;;
esac

# The step:review cycle marker (#139), shared by both allow paths below.
# Idempotent on the project root's HEAD (the helper's default key): an
# approve retried on the same diff emits nothing; a re-review after fix
# commits sits on a new HEAD and earns a fresh marker.
mark_step_review() {
  if command -v telemetry_mark_cycle_step >/dev/null 2>&1; then
    telemetry_mark_cycle_step review
  fi
}

# Claude Code path: allow + marker, no window semantics (see header).
if [ "$(get_hook_event "$INPUT")" = "PreToolUse" ]; then
  mark_step_review
  exit 0
fi

PROJECT_ROOT=$(project_root_from_payload "$INPUT")
WINDOW="$PROJECT_ROOT/.review/.window"

if [ ! -f "$WINDOW" ]; then
  deny "review-stamp-guard: approve_review denied — no review window is open.
The window (.review/.window) is created when a code-review subagent starts.
Spawn the code-review agent; only a live review session may record approvals."
fi

# Freshness: the window file's mtime must be within the TTL.
# Detect the stat flavor by capability, not by trial: on a Mac with GNU
# coreutils first in PATH, BSD-style `stat -f %m` SUCCEEDS but prints the
# filesystem ID instead of the mtime, silently corrupting the freshness
# check. GNU stat answers `--version`; BSD stat does not.
NOW=$(date +%s)
if stat --version >/dev/null 2>&1; then
  MTIME=$(stat -c %Y "$WINDOW" 2>/dev/null) || MTIME=""
else
  MTIME=$(stat -f %m "$WINDOW" 2>/dev/null) || MTIME=""
fi
if [ -n "$MTIME" ]; then
  AGE=$((NOW - MTIME))
  if [ "$AGE" -gt "$WINDOW_TTL_SECONDS" ]; then
    deny "review-stamp-guard: approve_review denied — the review window is stale
(age ${AGE}s > TTL ${WINDOW_TTL_SECONDS}s). Start a fresh code-review session."
  fi
else
  deny "review-stamp-guard: approve_review denied — cannot stat the review window."
fi

mark_step_review
exit 0
