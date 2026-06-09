#!/usr/bin/env bash
# red-proof-verify — PreToolUse hook (git commit).
# The EXECUTING half of red-before-green proof, run at the RED commit boundary.
#
# When a commit carries a `Tested-RED:` trailer, this hook does not merely trust
# the trailer — it RUNS the named pytest node against the about-to-be-committed
# tree and requires it to FAIL. At the RED commit the implementation does not
# exist yet, so a genuine red-before-green test MUST be failing here. If the
# node PASSES, the test demands no new implementation and cannot be driving the
# code — that is not TDD, so the commit is blocked.
#
# This converts the `Tested-RED:` trailer from an unverified claim into a
# locally PROVEN fact (no CI required): a test that this hook observed failing
# before its implementation commit exists.
#
# Honest limits (do not overclaim):
#   • It proves the named node fails NOW, at this commit — it cannot prevent a
#     later history rewrite. That ceiling is inherent to any client-side hook.
#   • If pytest cannot RUN (no runner, collection/usage/internal error, missing
#     deps) the outcome is BOOTSTRAP, which DEGRADES to allow — never a false
#     block. A genuine RED test commonly fails via ImportError/collection, which
#     pytest still reports as exit 1 (FAIL), so the common case is provable.
#   • pytest-only. Non-pytest stacks carry no `Tested-RED:` trailer in this
#     toolkit, so there is nothing to verify and the hook no-ops.
#
# Exit 2 = block (a Tested-RED node PASSES — not a real RED test)
# Exit 0 = allow (node correctly FAILS, no trailer, bootstrap, or non-commit)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands.
echo "$COMMAND" | grep -qE '^\s*git\s+commit\b' || exit 0

# Pull the Tested-RED node IDs from the commit message. No trailer ⇒ this is
# not a declared RED commit; nothing to verify here (commit-gauntlet and the
# push-time backstop cover the rest).
NODES=$(extract_tested_red_nodes "$COMMAND")
[ -z "$NODES" ] && exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")

PASSERS=""
while IFS= read -r node; do
  [ -z "$node" ] && continue
  case "$(run_pytest_node "$PROJECT_ROOT" "$node")" in
    FAIL)
      # Correct RED state — the test fails before implementation. Proven.
      ;;
    PASS)
      PASSERS="$PASSERS\n  • $node"
      ;;
    BOOTSTRAP)
      warn "red-proof-verify: could not run '$node' (no pytest runner or it failed to start) — RED proof SKIPPED for this node, falling back to trailer-presence only."
      ;;
  esac
done <<< "$NODES"

if [ -n "$PASSERS" ]; then
  deny "red-proof-verify blocked the commit — these Tested-RED nodes PASS at the RED commit:$(echo -e "$PASSERS")

A red-before-green test must FAIL before its implementation exists. A passing
'Tested-RED' node proves the opposite: it needs no new code, so it is not
driving the implementation. Either the test is asserting already-existing
behavior (write a test for the NEW behavior), or the implementation was written
first (delete it and restart from RED). Fix the test or remove the trailer."
fi

exit 0
