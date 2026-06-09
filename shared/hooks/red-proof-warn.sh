#!/usr/bin/env bash
# red-proof-warn — shipping-gate hook (git push / gh pr).
# For each HEAD-range commit that ADDS source files but carries no
# `Tested-RED:` trailer — production code with no record of a failing test
# driving it (TDD red-before-green) — this surfaces the gap before shipping.
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): hard DENY (exit 2). The push/PR is blocked
#     until every offending commit carries a `Tested-RED:` trailer.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
#
# The trailer is written by the tdd-red agent into the failing-test commit.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on shipping-gate commands: git push, or gh pr create/merge.
echo "$COMMAND" | grep -qE '^\s*(git\s+push\b|gh\s+pr\s+(create|merge)\b)' || exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")

# Determine the commit range about to be pushed. Prefer the tracked upstream;
# fall back to a bounded window if there is none. Any git failure ⇒ exit 0
# (an advisory hook must never block a push).
RANGE=""
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

COMMITS=$(git -C "$PROJECT_ROOT" rev-list "$RANGE" 2>/dev/null || true)
[ -z "$COMMITS" ] && exit 0

SOURCE_RE='\.(py|pyi|ts|tsx|js|jsx|go|rs|java)$'
OFFENDERS=""

while IFS= read -r sha; do
  [ -z "$sha" ] && continue

  # Does this commit add (A) a source file?
  ADDED=$(git -C "$PROJECT_ROOT" diff-tree --no-commit-id --name-only --diff-filter=A -r "$sha" 2>/dev/null || true)
  echo "$ADDED" | grep -qE "$SOURCE_RE" || continue

  # Does its message carry a Tested-RED: trailer?
  BODY=$(git -C "$PROJECT_ROOT" log -1 --format=%B "$sha" 2>/dev/null || true)
  if ! echo "$BODY" | grep -qiE '^[[:space:]]*Tested-RED:[[:space:]]*\S'; then
    SUBJECT=$(git -C "$PROJECT_ROOT" log -1 --format='%h %s' "$sha" 2>/dev/null || true)
    OFFENDERS="$OFFENDERS\n  • $SUBJECT"
  fi
done <<< "$COMMITS"

if [ -n "$OFFENDERS" ]; then
  ship_gate_enforce "$INPUT" "red-proof: these commits add source files with no 'Tested-RED:' trailer —
no record that a failing test drove the code (TDD red-before-green):$(echo -e "$OFFENDERS")

Add a 'Tested-RED: <pytest-node-id>' trailer on the commit that introduced the
failing test (the tdd-red agent writes this). On Cursor the push is BLOCKED
until every offending commit carries the trailer."
fi

exit 0
