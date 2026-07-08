#!/usr/bin/env bash
# review-window-close — subagentStop hook (Cursor).
#
# Closes the review window (`.review/.window`) when a code-review subagent
# stops, so approve_review cannot be called after the review session ends.
# Counterpart of review-window-open.sh. Always exits 0 — a lifecycle hook must
# never block.
#
# Two safeguards keep an unrelated subagent stop from deleting a live review
# window (issue #197):
#
#   1. EXACT identity — the same check open uses. A stop payload that merely
#      *mentions* "code-review" (a planner describing next steps, a verify
#      agent quoting the skill name) must NOT close the window. Only a stop
#      whose identity field IS "code-review" closes it; the historical raw
#      substring grep survives only as the no-identity-field fallback, so
#      unknown payload shapes keep working.
#   2. OWNERSHIP — the window is keyed to its opener. When the open payload
#      carried a session id, open records it in `.review/.window.owner`.
#      Ownership is purely additive: this close is skipped ONLY when the stop
#      carries a session id that DIFFERS from the recorded one (a different,
#      concurrent code-review session must not delete a window it did not
#      open). A stop with no resolvable session id, a matching session id, or
#      an unkeyed window (no sidecar — the legacy single-window shape) all
#      close normally, so the reviewer's own stop can never leak the window to
#      the guard's TTL even when the stop payload omits session_id.
#
# The session id field is not pinned by Cursor docs, and there is a single
# global `.review/.window` per project root, so this does not fully de-race two
# simultaneous reviewers (per-session windows would need a matching change in
# review-stamp-guard.sh, which reads existence + mtime only — out of scope).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)

# Only a code-review subagent closes the window — EXACT identity, mirroring
# review-window-open.sh. Parse the known candidate identity fields and require
# equality; fall back to the raw-payload substring grep only when none resolves
# (or jq is unavailable).
IDENTITY=""
if command -v jq &>/dev/null; then
  IDENTITY=$(printf '%s' "$INPUT" \
    | jq -r '.subagent_type // .agent // .name // .agent_type // empty' 2>/dev/null \
    || true)
fi
if [ -n "$IDENTITY" ]; then
  [ "$IDENTITY" = "code-review" ] || exit 0
else
  printf '%s' "$INPUT" | grep -q "code-review" || exit 0
fi

PROJECT_ROOT=$(project_root_from_payload "$INPUT")
WINDOW="$PROJECT_ROOT/.review/.window"
OWNER_FILE="$WINDOW.owner"

# Ownership (additive): when open recorded a session id, leave the window
# alone ONLY if this stop carries a DIFFERENT session id — another reviewer
# owns it. A sessionless stop, a matching session, or an unkeyed window all
# fall through to the close below.
if [ -f "$OWNER_FILE" ]; then
  SESSION_ID=""
  if command -v jq &>/dev/null; then
    SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
  fi
  RECORDED=$(cat "$OWNER_FILE" 2>/dev/null || true)
  if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "$RECORDED" ]; then
    exit 0
  fi
fi

rm -f "$WINDOW" "$OWNER_FILE"

exit 0
