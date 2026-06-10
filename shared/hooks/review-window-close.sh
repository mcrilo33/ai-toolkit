#!/usr/bin/env bash
# review-window-close — subagentStop hook (Cursor).
#
# Closes the review window (`.review/.window`) when a code-review subagent
# stops, so approve_review cannot be called after the review session ends.
# Counterpart of review-window-open.sh. Always exits 0 — a lifecycle hook
# must never block.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)

# Only a code-review subagent closes the window.
printf '%s' "$INPUT" | grep -q "code-review" || exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")
rm -f "$PROJECT_ROOT/.review/.window"

exit 0
