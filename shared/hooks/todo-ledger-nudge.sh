#!/usr/bin/env bash
# todo-ledger-nudge — SessionStart advisory.
#
# The companion of todo-ledger-warn: that hook gates the PUSH on evidence a
# TodoWrite ledger was seeded; this one nudges the agent to seed it up front so
# the gate is satisfied by working the right way, not by reacting at ship time.
# It injects a one-line reminder at session start (Claude additionalContext);
# the solo-cycle / start-task seed prompt (#5) carries the same guidance, so
# this is a redundant, never-blocking advisory.
#
# Purely advisory: always exits 0, never affects any tool call. SessionStart
# carries no command to gate.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

# Drain stdin so the producer never blocks on a full pipe; the payload is unused.
read_stdin >/dev/null 2>&1 || true

NUDGE="Solo-cycle: seed a TodoWrite ledger (one todo per subtask x cycle step) before your first commit so the cycle is visible and resumable — unless the work is single-step, where it is pure overhead."

# Claude reads hookSpecificOutput.additionalContext from SessionStart stdout.
# Other platforms ignore an unrecognized JSON object, so this is a safe no-op
# there (the seed prompt still carries the nudge).
if command -v jq &>/dev/null; then
  jq -nc --arg ctx "$NUDGE" \
    '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
else
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$NUDGE"
fi

exit 0
