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
#     deps) the outcome is BOOTSTRAP, which DENIES: declaring a `Tested-RED:`
#     pytest node claims pytest works, so a broken runner must be FIXED, not
#     skipped — a trailer the hook cannot execute proves nothing. A genuine RED
#     test commonly fails via ImportError/collection, which pytest still
#     reports as exit 1 (FAIL), so the common case is provable.
#   • pytest-only. Non-pytest stacks carry no `Tested-RED:` trailer in this
#     toolkit, so there is nothing to verify and the hook no-ops.
#
# Exit 2 = block (a Tested-RED node PASSES, or it cannot run — BOOTSTRAP)
# Exit 0 = allow (node correctly FAILS, no trailer, or non-commit)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands.
# Boundary-aware: chained/prefixed forms (`cd x && git commit`) must not bypass.
is_git_commit "$COMMAND" || exit 0

# Pull the Tested-RED node IDs from the commit message. No trailer ⇒ this is
# not a declared RED commit; nothing to verify here (commit-gauntlet and the
# push-time backstop cover the rest).
#
# The native commit-msg backstop (install-git-hooks.sh, issue #210) synthesizes
# this command via jq @json, which encodes the message's newlines as a literal
# backslash-n. extract_tested_red_nodes terminates a node ID on whitespace only,
# so a collapsed "\n" would otherwise be absorbed into the ID (yielding a bogus,
# unrunnable node) and a second Tested-RED: on that collapsed line would never
# match. Normalize literal "\n" back to whitespace so a Tested-RED: node that is
# not the final token — and multiple nodes across separate -m lines — extract
# identically to the agent (CC) path. Valid pytest node IDs never contain a
# backslash, so the agent path (no literal "\n") is unaffected.
NODES=$(extract_tested_red_nodes "${COMMAND//\\n/ }")
[ -z "$NODES" ] && exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")

PASSERS=""
BOOTSTRAPS=""
BREACHES=""
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
      BOOTSTRAPS="$BOOTSTRAPS\n  • $node"
      ;;
    BREACH)
      # Issue #31: the node mutated THIS repo (escaped isolation). The tripwire
      # already restored the snapshot; block so a corrupting test can't commit.
      BREACHES="$BREACHES\n  • $node"
      ;;
  esac
done <<< "$NODES"

if [ -n "$BREACHES" ]; then
  deny "red-proof-verify blocked the commit — these Tested-RED nodes mutated the
real repo when run (a test escaped isolation — issue #31):$(echo -e "$BREACHES")

The tripwire restored the repo, but a test that moves a ref or flips git config
is corrupting your checkout, not driving an implementation. Fix the test's
isolation (it must operate on its own tmpdir, never the real repo), then retry."
fi

if [ -n "$BOOTSTRAPS" ]; then
  deny "red-proof-verify blocked the commit — these Tested-RED nodes could not be RUN (no pytest runner, or it failed to start):$(echo -e "$BOOTSTRAPS")

Declaring a 'Tested-RED:' pytest node claims pytest works in this environment.
A node the hook cannot execute proves nothing — fix the environment (install
pytest / repair the runner) so the RED state can be verified, then retry."
fi

if [ -n "$PASSERS" ]; then
  deny "red-proof-verify blocked the commit — these Tested-RED nodes PASS at the RED commit:$(echo -e "$PASSERS")

A red-before-green test must FAIL before its implementation exists. A passing
'Tested-RED' node proves the opposite: it needs no new code, so it is not
driving the implementation. Either the test is asserting already-existing
behavior (write a test for the NEW behavior), or the implementation was written
first (delete it and restart from RED). Fix the test or remove the trailer."
fi

exit 0
