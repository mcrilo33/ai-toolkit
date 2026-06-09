#!/usr/bin/env bash
# red-proof-warn — shipping-gate hook (git push / gh pr).
#
# Two complementary checks, both scoped to the HEAD commit range being pushed:
#
#   1. TRAILER PRESENCE — every commit that ADDS source files must carry a
#      `Tested-RED:` trailer. A source-adding commit with no trailer has no
#      record that a failing test drove it (TDD red-before-green).
#
#   2. GREEN BACKSTOP (execution) — for every `Tested-RED:` node found in the
#      range, RUN that node against the current tree and require it to PASS.
#      This is the closing half of red-before-green proof: the RED commit hook
#      (red-proof-verify) proved the node FAILED before implementation; here we
#      prove the SAME node now PASSES with the implementation in place. A node
#      that still fails means the shipped code does not satisfy the test it
#      claims to. Bootstrap failures (no runner / pytest cannot start) DEGRADE
#      to a skip — never a false block.
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): hard DENY (exit 2). The push/PR is blocked
#     until every offending commit carries a `Tested-RED:` trailer AND every
#     Tested-RED node passes.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
#
# The trailer is written by the tdd-red agent into the failing-test commit;
# red-proof-verify proves it was RED at commit time.
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
RED_NODES=""

while IFS= read -r sha; do
  [ -z "$sha" ] && continue

  BODY=$(git -C "$PROJECT_ROOT" log -1 --format=%B "$sha" 2>/dev/null || true)

  # Collect any Tested-RED node IDs declared in this commit for the GREEN
  # backstop below (deduplicated after the loop).
  NODES_IN_COMMIT=$(extract_tested_red_nodes "$BODY")
  [ -n "$NODES_IN_COMMIT" ] && RED_NODES="$RED_NODES
$NODES_IN_COMMIT"

  # Check 1 — trailer presence on source-adding commits.
  ADDED=$(git -C "$PROJECT_ROOT" diff-tree --no-commit-id --name-only --diff-filter=A -r "$sha" 2>/dev/null || true)
  echo "$ADDED" | grep -qE "$SOURCE_RE" || continue
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

# ── GREEN backstop: every declared Tested-RED node must PASS now ─────
# Run each unique node against the current tree. A node that still FAILS means
# the shipped implementation does not satisfy the test it claims to drive.
# BOOTSTRAP (cannot run) degrades to skip — never a false block.
FAILING_NODES=""
SEEN=""
while IFS= read -r node; do
  [ -z "$node" ] && continue
  case "$SEEN" in *"|$node|"*) continue ;; esac
  SEEN="$SEEN|$node|"
  case "$(run_pytest_node "$PROJECT_ROOT" "$node")" in
    PASS) ;;
    FAIL) FAILING_NODES="$FAILING_NODES\n  • $node" ;;
    BOOTSTRAP)
      warn "red-proof: could not run '$node' (no pytest runner or it failed to start) — GREEN backstop SKIPPED for this node."
      ;;
  esac
done <<< "$RED_NODES"

if [ -n "$FAILING_NODES" ]; then
  ship_gate_enforce "$INPUT" "red-proof (green backstop): these Tested-RED nodes still FAIL at push —
the shipped code does not satisfy the test that claims to drive it:$(echo -e "$FAILING_NODES")

Run the node locally to reproduce, then make it pass before pushing. On Cursor
the push is BLOCKED until every Tested-RED node passes."
fi

exit 0
