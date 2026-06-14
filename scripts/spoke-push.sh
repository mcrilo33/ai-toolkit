#!/usr/bin/env bash
#
# spoke-push.sh — the spoke's PUSH step as ONE allowlistable process (issue #37).
#
# Claude Code's Bash matcher decomposes a compound command and requires EVERY
# segment to be individually allowed. A spoke that decorates its ship push with
# diagnostics never matches a bare exact-push allow rule, so the user is
# re-prompted on every ship:
#
#   git push -u origin <branch> 2>&1 | tail -20      # piped  (#36)
#   git status --short && … && git push origin <br>  # chained (#35)
#   git tag ready/34 && git push origin ready/34     # marker (#34) — intrinsically ≥2 cmds
#
# Collapsing the whole PUSH sequence into this single script means the model
# runs ONE allowlistable command — `bash .ai-toolkit/scripts/spoke-push.sh` —
# and never improvises a chain. worktree-new.sh seeds the matching allow rule
# `Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)`.
#
# It does NOT bypass the pre-push gates: it calls the real `git push`, so
# push-scope-guard, red-proof-warn, reviewer-sep-warn and git-push-review all
# fire exactly as they would for a hand-typed push. There is no --no-verify.
#
# Usage:
#   spoke-push.sh            # normal per-subtask push of the current branch
#   spoke-push.sh --ready N  # final subtask: branch push + emit the ready/N marker
#
set -euo pipefail

usage() {
  echo "usage: spoke-push.sh [--ready <issue>]" >&2
  exit 2
}

READY=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ready)   [ "$#" -ge 2 ] || usage; READY="$2"; shift 2 ;;
    --ready=*) READY="${1#--ready=}"; shift ;;
    -h|--help) usage ;;
    *)         echo "spoke-push: unknown argument: $1" >&2; usage ;;
  esac
done

# Resolve the current branch and refuse on the default branch (defense in depth;
# push-scope-guard still backstops a push whose refspec touches main).
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "spoke-push: cannot resolve current branch (detached HEAD?)" >&2
  exit 1
fi
if [ "$BRANCH" = "main" ]; then
  echo "spoke-push: refusing to push the default branch 'main' — main is published only from the hub" >&2
  exit 1
fi

# Diagnostics, as separate read-only steps — never chained onto the push.
echo "→ branch: $BRANCH"
echo "→ git status --short:"
git status --short || true
if ls .review/*.json >/dev/null 2>&1; then
  echo "→ review artifact present in .review/"
else
  echo "→ no .review/*.json artifact found (the pre-push gate blocks if one is required)"
fi

# The push — pre-push gate hooks fire here. No --no-verify, ever.
echo "→ git push -u origin $BRANCH"
git push -u origin "$BRANCH"

# Final subtask: emit the ready/<issue> completion marker at the branch tip.
# The hub consumes it on /land; a mid-cycle push passes no --ready and so emits
# nothing.
if [ -n "$READY" ]; then
  TAG="ready/$READY"
  echo "→ git tag $TAG"
  git tag "$TAG"
  echo "→ git push origin $TAG"
  git push origin "$TAG"
fi

echo "✓ spoke-push complete: pushed $BRANCH${READY:+ + marker ready/$READY}"
