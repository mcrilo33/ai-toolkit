#!/usr/bin/env bash
#
# spoke-ready.sh — emit a workflow marker tag as ONE allowlistable process (#45).
#
# Marker emission is a workflow control signal: ``ready/<N>`` means the whole
# issue is done (the hub consumes it on /land), ``gate/<N>`` means the spoke is
# parked at its PLAN gate awaiting review. Having the LLM hand-run
# ``git tag -f -a … && git push -f …`` is unreliable (#43: narrated but never
# executed), re-prompts (Claude Code's Bash matcher decomposes a compound command
# and requires every segment to be allowed, so the chain never matches a bare
# allow rule — see scripts/spoke-push.sh / #37), and wastefully fires the full
# pre-push suite to push a tag that carries no code.
#
# Collapsing emission into this single script means the model runs ONE
# allowlistable command — ``bash .ai-toolkit/scripts/spoke-ready.sh <N>`` — with
# nothing to mis-assemble or chain. worktree-new.sh seeds the matching allow rule
# ``Bash(bash .ai-toolkit/scripts/spoke-ready.sh:*)``.
#
# The tag is ANNOTATED and force-moved, so a re-run is IDEMPOTENT (it re-points
# the marker at the current tip and re-pushes). The push fires the pre-push hook
# exactly as a hand-typed push would (no --no-verify): the gate itself
# (test-select.sh) short-circuits a tag-only push, so emitting a marker never
# runs the suite.
#
# Usage:
#   spoke-ready.sh <issue>          # emit ready/<issue> at HEAD and push it
#   spoke-ready.sh --gate <issue>   # emit gate/<issue> (the PLAN-gate park marker)
#
set -euo pipefail

usage() {
  echo "usage: spoke-ready.sh [--gate] <issue>" >&2
  exit 2
}

# KIND selects the marker namespace; MSG is the annotated tag's message. ready/<N>
# carries a plain "ready"; gate/<N> carries the park state ("plan" for the PLAN
# gate). The red/draft park states named in solo-cycle are reserved for their
# follow-up issues.
# UPGRADE: add a --state flag for gate/<N> when the red/draft gates go live.
KIND="ready"
MSG="ready"
ISSUE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --gate)    KIND="gate"; MSG="plan"; shift ;;
    -h|--help) usage ;;
    -*)        echo "spoke-ready: unknown option: $1" >&2; usage ;;
    *)         [ -z "$ISSUE" ] || { echo "spoke-ready: unexpected argument: $1" >&2; usage; }
               ISSUE="$1"; shift ;;
  esac
done

[ -n "$ISSUE" ] || { echo "spoke-ready: an issue number is required" >&2; usage; }

# Resolve HEAD up front so an empty/detached repo fails clearly rather than
# emitting a dangling marker.
if ! git rev-parse --verify -q HEAD >/dev/null; then
  echo "spoke-ready: cannot resolve HEAD — nothing to mark" >&2
  exit 1
fi

TAG="$KIND/$ISSUE"

echo "→ git tag -f -a $TAG -m $MSG"
git tag -f -a "$TAG" -m "$MSG"
echo "→ git push -f origin $TAG"
git push -f origin "$TAG"

echo "✓ spoke-ready: emitted $TAG at $(git rev-parse --short HEAD)"
