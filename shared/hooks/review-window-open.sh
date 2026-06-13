#!/usr/bin/env bash
# review-window-open — subagentStart hook (Cursor).
#
# Opens the review window (`.review/.window`) when a code-review subagent
# starts. review-stamp-guard.sh allows the `approve_review` MCP tool only
# while this window exists and is fresh; review-window-close.sh removes it
# when the subagent stops.
#
# Payload field naming for subagent identity is not pinned by Cursor docs, so
# the check parses the known candidate identity fields (subagent_type, agent,
# name, agent_type) and requires EXACT equality with "code-review" — a planner
# prompt that merely *mentions* "code-review" must not open the window. Only
# when no identity field is present (or jq is unavailable) does it fall back
# to the historical raw-payload substring grep, so unknown payload shapes keep
# working. Always exits 0 — a lifecycle hook must never block.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)

# Only a code-review subagent opens the window.
IDENTITY=""
if command -v jq &>/dev/null; then
  IDENTITY=$(printf '%s' "$INPUT" \
    | jq -r '.subagent_type // .agent // .name // .agent_type // empty' 2>/dev/null \
    || true)
fi
if [ -n "$IDENTITY" ]; then
  [ "$IDENTITY" = "code-review" ] || exit 0
else
  # No identity field resolvable — preserve the substring fallback.
  printf '%s' "$INPUT" | grep -q "code-review" || exit 0
fi

# Cycle step: a code-review subagent starting IS the solo-cycle REVIEW gate.
telemetry_mark_step review

PROJECT_ROOT=$(project_root_from_payload "$INPUT")
mkdir -p "$PROJECT_ROOT/.review"
date +%s > "$PROJECT_ROOT/.review/.window"

exit 0
