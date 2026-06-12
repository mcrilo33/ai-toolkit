#!/usr/bin/env bash
# todo-ledger-warn — shipping-gate hook (git push / gh pr).
#
# Enforces that the spoke seeded a TodoWrite ledger this session, so the cycle
# is visible (Ctrl+T) and resumable after a dead worktree. A hook cannot call
# TodoWrite for the agent; it can only gate the ship action on EVIDENCE that a
# ledger exists. That evidence is a `TodoWrite` tool call in the session
# transcript:
#
#   • Read the session `transcript_path` from the hook stdin JSON.
#   • Scan it for a `TodoWrite` tool_use this session.
#   • If none is found → ship_gate_enforce (warn on Claude, hard-deny on Cursor).
#
# SINGLE-STEP ESCAPE HATCH: a `No-Ledger: <reason>` trailer on any commit in the
# pushed range bypasses the gate (mirrors how red-proof-warn scopes to the
# range's commits), so trivial/one-liner work is not punished for skipping the
# ledger the solo-cycle calls optional for single-step subtasks.
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): hard DENY (exit 2) — push/PR blocked until a
#     TodoWrite call exists in the transcript or a No-Ledger: trailer is added.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
#
# Degrades to allow (never a false block) on any unadjudicable state: no
# transcript path in the payload, an unreadable transcript, an empty pushed
# range, or no resolvable base ref for the escape-hatch range. An advisory hook
# must never block a push it cannot adjudicate.
#
# CURSOR CAVEAT: the hard-deny is contingent on Cursor's beforeShellExecution
# payload carrying a `transcript_path`. The live probe behind
# docs/cursor-hooks-migration-plan.md recorded {command, cwd, sandbox,
# workspace_roots} for that event and did NOT confirm transcript_path (a Claude
# Code field). If Cursor does not supply it, this hook degrades to allow there
# (fail-open) and the gate is effectively Claude-advisory-only. The deny path is
# kept so the gate activates if/when the field is present.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on shipping-gate commands: git push, or gh pr create/merge.
# Boundary-aware: chained/prefixed forms (`cd x && git push`) must not bypass.
is_git_push_or_pr "$COMMAND" || exit 0

# The session transcript is the only evidence a hook has of a TodoWrite call.
# No transcript path, or one that does not resolve to a readable file ⇒ exit 0:
# an advisory hook must never block a push it cannot adjudicate.
TRANSCRIPT=$(json_field "$INPUT" "transcript_path")
[ -z "$TRANSCRIPT" ] && exit 0
[ -f "$TRANSCRIPT" ] && [ -r "$TRANSCRIPT" ] || exit 0

# A TodoWrite tool_use anywhere in the transcript proves a ledger was seeded.
if grep -qE '"name"[[:space:]]*:[[:space:]]*"TodoWrite"' "$TRANSCRIPT"; then
  exit 0
fi

# No ledger in the transcript. Honor the single-step escape hatch: a
# `No-Ledger:` trailer on any commit in the pushed range bypasses the gate.
# Resolve the base the same way as reviewer-sep so the range is the pushed
# work; an unresolvable base ⇒ exit 0 (cannot adjudicate the range).
PROJECT_ROOT=$(project_root_from_payload "$INPUT")
BASE=$(review_base_ref "$PROJECT_ROOT")
[ -z "$BASE" ] && exit 0

COMMITS=$(git -C "$PROJECT_ROOT" rev-list --no-merges "$BASE..HEAD" 2>/dev/null || true)
# Empty range (HEAD == base, e.g. `gh pr create` after the push): nothing is
# being shipped, so there is nothing to gate — and the No-Ledger: escape hatch
# would be impossible to apply. Mirror red-proof-warn and allow.
[ -z "$COMMITS" ] && exit 0
while IFS= read -r sha; do
  [ -z "$sha" ] && continue
  BODY=$(git -C "$PROJECT_ROOT" log -1 --format=%B "$sha" 2>/dev/null || true)
  if echo "$BODY" | grep -qiE '^[[:space:]]*No-Ledger:[[:space:]]*\S'; then
    exit 0
  fi
done <<< "$COMMITS"

ship_gate_enforce "$INPUT" "todo-ledger: no TodoWrite ledger found in this session's transcript.
The solo-cycle seeds a TodoWrite ledger (one todo per subtask x cycle step) so
the cycle is visible (Ctrl+T) and resumable after a dead worktree.

Seed the ledger now, or — for genuinely single-step work (one tiny subtask, a
docs/config one-liner) — add a 'No-Ledger: <reason>' trailer to a commit in the
pushed range to bypass this gate. On Cursor the push is BLOCKED until one of
those is present."

exit 0
