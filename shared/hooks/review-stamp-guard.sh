#!/usr/bin/env bash
# review-stamp-guard — beforeMCPExecution hook (Cursor, failClosed).
#
# Gates the review-stamp MCP tool `approve_review` behind a review window:
# the call is allowed ONLY while `.review/.window` exists and is younger than
# the TTL. The window is opened by review-window-open.sh when a code-review
# subagent starts and closed by review-window-close.sh when it stops, so the
# implementing agent cannot mint its own approval outside a live review.
#
# beforeMCPExecution carries tool identity and workspace roots but NO agent
# identity — the decision is tool_name + window state only.
#
# Exit 2 = deny (no window / stale window)
# Exit 0 = allow (other tools, or a fresh window)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

WINDOW_TTL_SECONDS=1800

INPUT=$(read_stdin)
TOOL_NAME=$(get_tool_name "$INPUT")

# Only the approve_review tool is gated.
[ "$TOOL_NAME" = "approve_review" ] || exit 0

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

exit 0
