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
#   spoke-ready.sh <issue>               # emit ready/<issue>  (whole issue done)
#   spoke-ready.sh --gate <issue>        # emit gate/<issue>   (PLAN-gate park)
#   spoke-ready.sh --accept <issue>      # emit accept/<issue> (built+reviewed; human sign-off)
#   spoke-ready.sh --blocked <issue>     # emit blocked/<issue> (stuck; answer + re-queue)
#   spoke-ready.sh --blocked <issue> -m "<reason>"   # stamp a reason into the tag body
#
set -euo pipefail

# --- telemetry (opt-in, optional) ---------------------------------------------
# Source the shared span emit layer so this control script appears as a kind=script
# trace node (Issue #54). Self-contained and gated by AI_TOOLKIT_TELEMETRY=1, so
# sourcing is a no-op when telemetry is off. Located relative to THIS script: in the
# ai-toolkit checkout under shared/hooks/lib/; in a synced target co-located in
# .ai-toolkit/scripts/. The start clock is read up front so the span's window spans
# the whole emission. This script runs IN the spoke, so the span resolves the
# spoke's own spoke_run_id / branch / repo from CWD — no cd needed.
_SR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _c in "$_SR_DIR/telemetry.sh" "$_SR_DIR/../shared/hooks/lib/telemetry.sh"; do
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
unset _c
_SR_T0="$(command -v _telemetry_now_ms >/dev/null 2>&1 && _telemetry_now_ms || true)"

usage() {
  echo "usage: spoke-ready.sh [--gate|--accept|--blocked] <issue> [-m <reason>]" >&2
  exit 2
}

# KIND selects the marker namespace and SUBJECT is the annotated tag's subject
# line (the state word). ready/<N> means the whole issue is done; gate/<N> is the
# non-terminal PLAN park (subject "plan"); accept/<N> and blocked/<N> are the two
# extra TERMINAL markers an unattended drain (`/afk`) emits — accept = built +
# pushed + agent-reviewed, final sign-off inherently human; blocked = stuck. A
# -m <reason> becomes the tag BODY (the trust summary / blocker the dashboard
# renders); omitted, the body is empty and the subject is the only payload.
#
# The state flags are mutually exclusive: passing two is a usage error.
KIND="ready"
SUBJECT="ready"
STATE_FLAG=""
ISSUE=""
BODY=""

# set_state <kind> <subject> — select the marker namespace, rejecting a second
# state flag so e.g. `--gate --accept` can't emit an ambiguous marker.
set_state() {
  if [ -n "$STATE_FLAG" ]; then
    echo "spoke-ready: --$1 conflicts with --$STATE_FLAG (pick one)" >&2
    usage
  fi
  STATE_FLAG="$1"
  KIND="$1"
  SUBJECT="$2"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --gate)        set_state gate plan; shift ;;
    --accept)      set_state accept accept; shift ;;
    --blocked)     set_state blocked blocked; shift ;;
    -m|--message)  [ "$#" -ge 2 ] || { echo "spoke-ready: -m needs a value" >&2; usage; }
                   BODY="$2"; shift 2 ;;
    --message=*)   BODY="${1#--message=}"; shift ;;
    -h|--help)     usage ;;
    -*)            echo "spoke-ready: unknown option: $1" >&2; usage ;;
    *)             [ -z "$ISSUE" ] || { echo "spoke-ready: unexpected argument: $1" >&2; usage; }
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

# Durability: ready/<N> and accept/<N> CLAIM the work is landable/reviewable, so
# the hub frees the slot and the morning report shows a LAND/EYEBALL row. If the
# branch commits never reached origin (the #43 narrated-push failure), that claim
# is over un-pushed work the hub can't see — refuse unless HEAD is contained in the
# branch's pushed upstream. gate/<N> (PLAN park) and blocked/<N> (stuck) are STOP
# signals over incomplete work that make no landable claim — and the hub itself
# emits blocked/<N> when it reaps a hung spoke whose work never landed — so they
# are EXEMPT.
case "$KIND" in
  ready|accept)
    if ! git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
      echo "spoke-ready: refusing $KIND/$ISSUE — the branch has no pushed upstream." >&2
      echo "  Push it first (bash .ai-toolkit/scripts/spoke-push.sh), then re-run." >&2
      exit 1
    fi
    if ! git merge-base --is-ancestor HEAD '@{upstream}' 2>/dev/null; then
      echo "spoke-ready: refusing $KIND/$ISSUE — HEAD is ahead of the pushed branch (un-pushed work)." >&2
      echo "  Push it first (bash .ai-toolkit/scripts/spoke-push.sh), then re-run." >&2
      exit 1
    fi
    ;;
esac

TAG="$KIND/$ISSUE"

# The annotated tag carries SUBJECT (the state word) and, when a reason was
# given, BODY as a second message paragraph — read back by consumers via
# %(contents:subject) / %(contents:body). Force-move + force-push keep emission
# idempotent (a re-run re-points the marker at the current tip and re-pushes).
MSG_ARGS=(-m "$SUBJECT")
if [ -n "$BODY" ]; then
  MSG_ARGS+=(-m "$BODY")
fi
echo "→ git tag -f -a $TAG ${MSG_ARGS[*]}"
git tag -f -a "$TAG" "${MSG_ARGS[@]}"
echo "→ git push -f origin $TAG"
git push -f origin "$TAG"

echo "✓ spoke-ready: emitted $TAG at $(git rev-parse --short HEAD)"

# Trace node: this run as a kind=script span, tagged with the marker namespace it
# emitted (phase = ready|gate|accept|blocked) so the trace tells a completion
# marker apart from a PLAN-gate park. emits stays null on push (parser-filled).
# An `if` (not `&&`) so a missing emit layer leaves the script's exit status 0.
if command -v telemetry_emit_span >/dev/null 2>&1; then
  telemetry_emit_span --kind script --name spoke-ready \
    --phase "$KIND" --status success --start-ms "$_SR_T0"
fi
