#!/usr/bin/env bash
# reviewer-sep-warn — shipping-gate hook (git push / gh pr).
# For each HEAD-range commit lacking a `Reviewed-by: code-review` trailer,
# surfaces the absence before shipping.
#
# A local hook inspecting a command string CANNOT verify that a *different*
# agent (the code-review subagent) authored the trailer versus the implementing
# agent simply typing it. The trailer is auditable evidence of intent in
# history, not proof of reviewer separation. This hook surfaces the ABSENCE of
# the trailer; it makes no claim about the honesty of its presence.
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): hard DENY (exit 2) — push/PR blocked until
#     every commit carries the trailer.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on shipping-gate commands: git push, or gh pr create/merge.
echo "$COMMAND" | grep -qE '^\s*(git\s+push\b|gh\s+pr\s+(create|merge)\b)' || exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")

# Commit range to be pushed. Prefer tracked upstream; fall back to a bounded
# window. Any git failure ⇒ exit 0 (advisory hook must never block a push).
if UPSTREAM=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null); then
  RANGE="$UPSTREAM..HEAD"
else
  # No upstream: inspect a recent window, clamped to available history so a
  # repo with <20 commits still gets checked instead of silently skipped.
  COUNT=$(git -C "$PROJECT_ROOT" rev-list --count HEAD 2>/dev/null || echo 0)
  if [ "$COUNT" -le 0 ]; then
    exit 0
  elif [ "$COUNT" -le 20 ]; then
    RANGE="HEAD"
  else
    RANGE="HEAD~20..HEAD"
  fi
fi

COMMITS=$(git -C "$PROJECT_ROOT" rev-list --no-merges "$RANGE" 2>/dev/null || true)
[ -z "$COMMITS" ] && exit 0

OFFENDERS=""
while IFS= read -r sha; do
  [ -z "$sha" ] && continue
  BODY=$(git -C "$PROJECT_ROOT" log -1 --format=%B "$sha" 2>/dev/null || true)
  if ! echo "$BODY" | grep -qiE '^[[:space:]]*Reviewed-by:[[:space:]]*code-review'; then
    SUBJECT=$(git -C "$PROJECT_ROOT" log -1 --format='%h %s' "$sha" 2>/dev/null || true)
    OFFENDERS="$OFFENDERS\n  • $SUBJECT"
  fi
done <<< "$COMMITS"

if [ -n "$OFFENDERS" ]; then
  ship_gate_enforce "$INPUT" "reviewer-separation: these commits carry no 'Reviewed-by: code-review' trailer —
no record that a separate code-review agent inspected them:$(echo -e "$OFFENDERS")

Spawn the code-review agent on the diff; once it approves, add a
'Reviewed-by: code-review' trailer to the commit. On Cursor the push is BLOCKED
until every commit carries the trailer. NOTE: the hook cannot verify a
different agent authored the trailer, only that it exists."
fi

exit 0
