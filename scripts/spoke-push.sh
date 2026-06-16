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

# --- telemetry (opt-in, optional) ---------------------------------------------
# Source the shared span emit layer so this control script appears as a kind=script
# trace node (Issue #54). Self-contained and gated by AI_TOOLKIT_TELEMETRY=1, so
# sourcing is a no-op when telemetry is off. Located relative to THIS script: in the
# ai-toolkit checkout under shared/hooks/lib/; in a synced target co-located in
# .ai-toolkit/scripts/. The start clock is read up front so the span's window covers
# the whole push. This runs IN the spoke, so the span resolves the spoke's own
# spoke_run_id / branch / repo from CWD.
_SP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _c in "$_SP_DIR/telemetry.sh" "$_SP_DIR/../shared/hooks/lib/telemetry.sh"; do
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
unset _c
_SP_T0="$(command -v _telemetry_now_ms >/dev/null 2>&1 && _telemetry_now_ms || true)"

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

# Final subtask: emit the ready/<issue> completion marker via the canonical
# marker emitter (issue #45) — one annotated, force-moved (idempotent) tag pushed
# as a tag-only push the pre-push gate short-circuits. spoke-ready.sh is
# co-located with this script (both in the toolkit scripts/ and a synced
# .ai-toolkit/scripts/), so resolve it as a sibling. The hub consumes the marker
# on /land; a mid-cycle push passes no --ready and so emits nothing.
if [ -n "$READY" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  bash "$SCRIPT_DIR/spoke-ready.sh" "$READY"
fi

echo "✓ spoke-push complete: pushed $BRANCH${READY:+ + marker ready/$READY}"

# Trace node: this push run as a kind=script span. Emitted only on the success
# path (a rejected push exits earlier under set -e). emits stays null on push.
# An `if` (not `&&`) so a missing emit layer leaves the script's exit status 0.
# UPGRADE: emit a status=failure span on a gate-rejected push — wrap the git push
#   above — once failed-push visibility is wanted in the trace.
if command -v telemetry_emit_span >/dev/null 2>&1; then
  telemetry_emit_span --kind script --name spoke-push \
    --status success --start-ms "$_SP_T0"
fi
