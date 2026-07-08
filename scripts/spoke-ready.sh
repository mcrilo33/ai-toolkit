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
# The SSH-keepalive push wrapper wt_git_push and its transport-death predicate
# wt_push_transport_died (issue #119) live in worktree-lib.sh, co-located with
# this script in both layouts (toolkit scripts/, synced .ai-toolkit/scripts/).
# A missing sibling is a broken sync — fail loudly rather than fall back to a
# keepalive-less push (same stance as spoke-push.sh).
. "$_SR_DIR/worktree-lib.sh"
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
PLAN_FILE=""

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
    --plan-file)   [ "$#" -ge 2 ] || { echo "spoke-ready: --plan-file needs a value" >&2; usage; }
                   PLAN_FILE="$2"; shift 2 ;;
    --plan-file=*) PLAN_FILE="${1#--plan-file=}"; shift ;;
    -h|--help)     usage ;;
    -*)            echo "spoke-ready: unknown option: $1" >&2; usage ;;
    *)             [ -z "$ISSUE" ] || { echo "spoke-ready: unexpected argument: $1" >&2; usage; }
                   ISSUE="$1"; shift ;;
  esac
done

[ -n "$ISSUE" ] || { echo "spoke-ready: an issue number is required" >&2; usage; }

# The plan text (a PLAN-gate park's handoff, issue #175) comes either inline (-m) or
# from a file (--plan-file); both feed BODY, so they are mutually exclusive. --plan-file
# reads the whole file, letting a spoke hand over a multi-line plan without argv quoting.
if [ -n "$PLAN_FILE" ]; then
  if [ -n "$BODY" ]; then
    echo "spoke-ready: -m and --plan-file conflict (pick one)" >&2
    usage
  fi
  [ -f "$PLAN_FILE" ] || { echo "spoke-ready: --plan-file not found: $PLAN_FILE" >&2; exit 1; }
  BODY="$(cat "$PLAN_FILE")"
fi

# Resolve HEAD up front so an empty/detached repo fails clearly rather than
# emitting a dangling marker.
if ! git rev-parse --verify -q HEAD >/dev/null; then
  echo "spoke-ready: cannot resolve HEAD — nothing to mark" >&2
  exit 1
fi

# Durability: accept/<N> CLAIMS the work is reviewable/landable, so the hub frees
# the slot and the morning report shows an EYEBALL row. If the branch commits never
# reached origin (the #43 narrated-push failure), that claim is over un-pushed work
# the hub can't see — refuse unless HEAD is contained in the branch's pushed
# upstream. gate/<N> (PLAN park) and blocked/<N> (stuck) are STOP signals over
# incomplete work that make no landable claim — and the hub itself emits blocked/<N>
# when it reaps a hung spoke whose work never landed — so they are EXEMPT. ready/<N>
# is NOT here: it has the stricter #172 precondition gate below.
case "$KIND" in
  accept)
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

# ── ready/<N> precondition gate (issue #172) ─────────────────────────────────
# ready/<N> is auto_land's ENTIRE trust basis — the drain lands it with
# --skip-tests — so "ready" must be a MECHANICALLY verified contract, not an LLM
# judgment. Refuse ready/<N> unless all three hold, naming the first unmet one and
# the command that fixes it:
#   1. clean working tree — no staged or unstaged changes (a dirty tree means the
#      pushed tip is not what's on disk);
#   2. HEAD == @{upstream} — everything is pushed, so the pre-push gates ran on
#      THIS exact commit (stricter than accept's "ancestor of upstream": a HEAD
#      behind the pushed tip is refused too);
#   3. a review artifact (.review/*.json) covers the tip.
# Escape hatch AI_TOOLKIT_READY_FORCE=1 skips the gate, logged loudly.

# _ready_artifact_verdict <file> — print a review artifact's verdict (APPROVE |
# REQUEST_CHANGES). jq when present, else a grep fallback — the same self-contained
# parse review_artifact_verdict uses in utils.sh, inlined here to avoid sourcing.
_ready_artifact_verdict() {
  local file="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r '.verdict // empty' "$file" 2>/dev/null
  else
    grep -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]*"' "$file" 2>/dev/null \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# _ready_approved_review_covers_tip — true when a .review/*.json with an APPROVE
# verdict is at least as new as the tip commit (an approving review was recorded at
# or after HEAD was committed). Verdict matters: ready/<N> is auto_land's basis and
# lands with --skip-tests, so a fresh REQUEST_CHANGES must not satisfy the gate.
#
# We deliberately do NOT source utils.sh to reuse review_diff_hash: (a) sourcing it
# has source-time side effects (it arms a per-hook telemetry span and exits when the
# toolkit is globally disabled), and (b) at ready time HEAD == @{upstream}, so its
# hash base (merge-base @{upstream} HEAD) collapses to HEAD and the range diff is
# empty — the hash can't bind a whole-branch review here anyway. So use the issue
# #172 timestamp fallback. Portable stat is GNU-first (`-c %Y`) then BSD (`-f %m`);
# the order is load-bearing (see wt_bridge_source_mtime in worktree-lib.sh, #132).
_ready_approved_review_covers_tip() {
  local root tip m f
  root="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1
  [ -d "$root/.review" ] || return 1
  tip="$(git log -1 --format=%ct HEAD 2>/dev/null)" || return 1
  [ -n "$tip" ] || return 1
  for f in "$root"/.review/*.json; do
    [ -f "$f" ] || continue
    m="$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null)" || continue
    [ "$m" -ge "$tip" ] || continue
    [ "$(_ready_artifact_verdict "$f")" = "APPROVE" ] && return 0
  done
  return 1
}

# verify_ready_preconditions <issue> — exit 1 on the first unmet precondition,
# printing what failed and the fix; return 0 when all three hold.
verify_ready_preconditions() {
  local issue="$1" upstream_sha head_sha
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "spoke-ready: refusing ready/$issue — the working tree is not clean (uncommitted changes)." >&2
    echo "  Commit or discard them (git status), then re-run." >&2
    exit 1
  fi
  if ! upstream_sha="$(git rev-parse --verify -q '@{upstream}' 2>/dev/null)"; then
    echo "spoke-ready: refusing ready/$issue — the branch has no pushed upstream." >&2
    echo "  Push it first (bash .ai-toolkit/scripts/spoke-push.sh), then re-run." >&2
    exit 1
  fi
  head_sha="$(git rev-parse --verify HEAD 2>/dev/null)"  # HEAD verified at start
  if [ "$head_sha" != "$upstream_sha" ]; then
    echo "spoke-ready: refusing ready/$issue — HEAD is not the pushed tip (@{upstream})." >&2
    echo "  Push it first (bash .ai-toolkit/scripts/spoke-push.sh), then re-run." >&2
    exit 1
  fi
  if ! _ready_approved_review_covers_tip; then
    echo "spoke-ready: refusing ready/$issue — no APPROVED code-review artifact covers the current tip." >&2
    echo "  Review this diff (the code-review agent writes an APPROVE .review/<hash>.json), then re-run." >&2
    exit 1
  fi
}

if [ "$KIND" = "ready" ]; then
  if [ "${AI_TOOLKIT_READY_FORCE:-}" = "1" ]; then
    echo "spoke-ready: ⚠ AI_TOOLKIT_READY_FORCE=1 — emitting ready/$ISSUE WITHOUT verifying its preconditions (clean tree / pushed tip / review artifact). This bypasses auto_land's trust gate." >&2
  else
    verify_ready_preconditions "$ISSUE"
  fi
fi

TAG="$KIND/$ISSUE"

# The annotated tag carries SUBJECT (the state word) and, when a reason was
# given, BODY as a second message paragraph — read back by consumers via
# %(contents:subject) / %(contents:body). Force-move + force-push keep emission
# idempotent (a re-run re-points the marker at the current tip and re-pushes).
MSG_ARGS=(-m "$SUBJECT")
if [ -n "$BODY" ]; then
  MSG_ARGS+=(-m "$BODY")
fi

# A PLAN-gate park writes its plan to a SCRIPTED handoff artifact (issue #175) the
# gate-broker reads directly, replacing the transcript heuristic (extract_pending_question):
# a script reads what a script wrote. The plan still rides the tag annotation (BODY above);
# the artifact is the primary channel. Written before the tag push, under the gitignored
# .ai-toolkit/ dir so it never dirties the working tree.
#
# The write site OWNS the artifact's freshness (#175 review): every --gate emission either
# writes the current plan OR, when none was handed over (a bare --gate re-park after an
# escalation or an in-pane human answer), CLEARS any stale artifact from a prior park — so
# the broker never prefers an outdated plan over the current transcript.
if [ "$KIND" = "gate" ]; then
  GATE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$GATE_ROOT" ]; then
    GATE_ARTIFACT="$GATE_ROOT/.ai-toolkit/gate-$ISSUE.md"
    if [ -n "$BODY" ]; then
      mkdir -p "$GATE_ROOT/.ai-toolkit"
      printf '%s\n' "$BODY" > "$GATE_ARTIFACT"
      echo "→ wrote plan artifact .ai-toolkit/gate-$ISSUE.md"
    elif [ -f "$GATE_ARTIFACT" ]; then
      rm -f "$GATE_ARTIFACT"
      echo "→ cleared stale plan artifact .ai-toolkit/gate-$ISSUE.md (bare --gate hands over no plan)"
    fi
  fi
fi

echo "→ git tag -f -a $TAG ${MSG_ARGS[*]}"
git tag -f -a "$TAG" "${MSG_ARGS[@]}"

# The tag push routes through wt_git_push (issue #119, the #184 residual): on the
# --ready path it runs seconds after the branch push's ~6-minute in-push gate
# staled the SSH connection, and a bare push died in the transfer phase (exit
# 141 / SIGPIPE) — the branch landed but the marker never reached origin, so the
# spoke finished without announcing itself and the drain stalled. Second line of
# defense, mirroring worktree-land: when a failed attempt is demonstrably a
# TRANSPORT death, retry exactly once. Unlike the land path there is no
# TEST_SELECT_SKIP on the retry and no pytest-shape filter on the capture — the
# gate short-circuits a tag-only push, so no suite runs inside it and a transport
# signature in the output can only be the transport itself. The capture file is
# complete when the pipeline returns (tee exits 0, pipefail keeps git's own code).
echo "→ git push -f origin $TAG"
PUSH_LOG="$(mktemp "${TMPDIR:-/tmp}/spoke-ready-push.XXXXXX")"
PUSH_RC=0
wt_git_push -f origin "$TAG" 2>&1 | tee "$PUSH_LOG" || PUSH_RC=$?
if [ "$PUSH_RC" -ne 0 ]; then
  if wt_push_transport_died "$PUSH_RC" "$PUSH_LOG"; then
    rm -f "$PUSH_LOG"
    echo "spoke-ready: push transport died (SSH staleness, issue #119) — retrying ONCE" >&2
    wt_git_push -f origin "$TAG"
  else
    rm -f "$PUSH_LOG"
    echo "spoke-ready: push of $TAG rejected — the marker did not reach origin" >&2
    exit "$PUSH_RC"
  fi
fi
rm -f "$PUSH_LOG"

echo "✓ spoke-ready: emitted $TAG at $(git rev-parse --short HEAD)"

# --- event-driven wake (issue #176) -------------------------------------------
# Now that the marker reached origin, ANNOUNCE it to a live /afk supervisor: drop a
# content-free <epoch>-<issue>-<kind> file in the event spool and SIGUSR1 the heartbeat
# pid, so a parked answer/land fires in seconds instead of waiting the backstop tick.
# Gated on a LIVE supervisor (the heartbeat pid is a running process) so an attended run
# leaves no spool artifact and signals nothing. Events are WAKE-UPS, not state — the
# supervisor re-derives via slot_state — so this is best-effort: a missed signal is caught
# by the next sweep, and the writer never fails the emission. The spool path + filename
# mirror the reader (afk_event_dir / afk_drain_event_issues in gate-broker.sh) and honor
# the same AFK_HEARTBEAT / AFK_STATE_DIR overrides; the emit is inlined here (not shared)
# because a spoke deploys spoke-ready.sh to a different dir than the hub reader.
_afk_emit_wake_event() {
  local issue="$1" kind="$2" common hb pid dir
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 0
  hb="${AFK_HEARTBEAT:-$common/.afk-heartbeat}"
  [ -f "$hb" ] || return 0
  pid="$(head -n1 "$hb" 2>/dev/null | awk '{print $1}')"
  case "$pid" in '' | *[!0-9]*) return 0 ;; esac
  kill -0 "$pid" 2>/dev/null || return 0   # no live supervisor -> nothing to wake
  dir="${AFK_STATE_DIR:-$common/ai-toolkit-afk}/events"
  mkdir -p "$dir" 2>/dev/null || return 0
  : > "$dir/$(date +%s)-$issue-$kind" 2>/dev/null || return 0
  kill -USR1 "$pid" 2>/dev/null || true
}
_afk_emit_wake_event "$ISSUE" "$KIND"

# Trace node: this run as a kind=script span, tagged with the marker namespace it
# emitted (phase = ready|gate|accept|blocked) so the trace tells a completion
# marker apart from a PLAN-gate park. emits stays null on push (parser-filled).
# An `if` (not `&&`) so a missing emit layer leaves the script's exit status 0.
if command -v telemetry_emit_span >/dev/null 2>&1; then
  telemetry_emit_span --kind script --name spoke-ready \
    --phase "$KIND" --status success --start-ms "$_SR_T0"
fi
