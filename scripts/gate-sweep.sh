#!/usr/bin/env bash
#
# gate-sweep.sh — conditional post-land background full-suite sweep (issue #124).
#
# Once the pre-push gate runs pruned sets (testmon / reverse-index "selected"),
# a selection miss could rot the default branch silently: nothing would ever run
# the tests the selector didn't pick. This script is the safety net, launched
# best-effort by worktree-land.sh's tail — never on the land's critical path.
#
# Usage:
#   gate-sweep.sh --spawn <merged-sha> [--branch <b>] [--issue <n>]
#   gate-sweep.sh --run   <merged-sha> [--branch <b>] [--issue <n>]
#
#   --spawn  synchronous decision (milliseconds): read the green-tree stamp
#            (issue #122) for <merged-sha>^{tree}. A PRUNED tier (testmon or
#            selected) detaches a --run worker; a `full` stamp or no stamp at
#            all (docs-only skip, --skip-tests — the gate certified nothing
#            pruned) launches nothing. Always exits 0.
#   --run    the detached worker. One sweep at a time per checkout: a pidfile
#            lock under <git-common-dir>/.gate-sweep/; a held lock queues at
#            most ONE newest-wins follow-up in `queue`. The worker re-keys on
#            the checkout's CURRENT clean tree (a land may have superseded the
#            spawn), skips a tree already stamped `full`, and runs the full
#            suite. Green upgrades the tree's stamp to `full` (so back-to-back
#            lands of the same content sweep once); red files a GitHub issue
#            carrying the failing test ids + the landed commit/branch so the
#            failure drops into the normal backlog → spoke flow. A gh failure
#            is written to sweep.log — logged, never swallowed. Exits 0.
#
# ENV:
#   GATE_SWEEP_CMD  run this command as the suite instead of the detected
#                   pytest (the stubbed runner used by tests; mirrors
#                   TEST_SELECT_CMD's role for the gate).
#
# Worker output (notes + suite log) lands in <git-common-dir>/.gate-sweep/
# sweep.log, so the detached run stays observable after the land returned.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

warn() { echo "gate-sweep: $*" >&2; }

# --- args -------------------------------------------------------------------------
MODE=""
SHA=""
BRANCH=""
ISSUE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --spawn)  MODE="spawn"; shift ;;
    --run)    MODE="run"; shift ;;
    --branch) [ "$#" -ge 2 ] || { warn "--branch needs a value"; exit 0; }; BRANCH="$2"; shift 2 ;;
    --issue)  [ "$#" -ge 2 ] || { warn "--issue needs a value"; exit 0; }; ISSUE="$2"; shift 2 ;;
    -*)       warn "unknown option: $1"; exit 0 ;;
    *)        SHA="$1"; shift ;;
  esac
done
if [ -z "$MODE" ] || [ -z "$SHA" ]; then
  warn "usage: gate-sweep.sh --spawn|--run <merged-sha> [--branch <b>] [--issue <n>]"
  exit 0
fi

# --- the green-tree stamp lib (issue #122) is the tier source of truth -------------
# In-repo checkout first; fall back to the copy install-git-hooks.sh places next
# to the native hooks. Best-effort like everything here: no lib → no sweep.
GS_LIB=""
for cand in "$SCRIPT_DIR/../shared/hooks/lib/gate-stamp.sh" \
            "$(git rev-parse --git-path hooks/ai-toolkit-scripts/lib/gate-stamp.sh 2>/dev/null || true)"; do
  if [ -n "$cand" ] && [ -f "$cand" ]; then GS_LIB="$cand"; break; fi
done
if [ -z "$GS_LIB" ]; then
  warn "gate-stamp lib not found — cannot read gate tiers; no sweep"
  exit 0
fi
# shellcheck source=../shared/hooks/lib/gate-stamp.sh
source "$GS_LIB"

# Print the stamped tier for a tree ("" when unstamped). Read-only sibling of
# gate_stamp_check: the sweep needs the tier itself, not a covers-demand verdict
# — and deliberately ignores the env fingerprint (a full pass under ANY runner
# means the tree needs no safety-net re-run).
stamped_tier() {
  local dir stamp
  dir="$(gate_stamp_dir)" || return 0
  stamp="$dir/$1"
  [ -f "$stamp" ] || return 0
  sed -n 's/^tier=//p' "$stamp" | head -1
}

# <git-common-dir>/.gate-sweep, absolutized the same way gate_stamp_dir is.
sweep_dir() {
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$common" in
    /*) ;;
    *)  common="$PWD/$common" ;;
  esac
  printf '%s/.gate-sweep' "$common"
}

SWEEP_DIR="$(sweep_dir)" || { warn "not inside a git repo — no sweep"; exit 0; }
LOG="$SWEEP_DIR/sweep.log"

# Worker notes go to the log file (the worker is detached — stderr leads
# nowhere); --spawn talks on stdout as part of the land's own output.
log() {
  printf '%s gate-sweep: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true
}

# --- --spawn: decide from the landed tree's stamp, detach, return ------------------
do_spawn() {
  local tree tier
  if ! tree="$(git rev-parse --verify --quiet "${SHA}^{tree}" 2>/dev/null)"; then
    warn "cannot resolve ${SHA}^{tree} — no sweep"
    return 0
  fi
  tier="$(stamped_tier "$tree")"
  case "$tier" in
    testmon|selected)
      mkdir -p "$SWEEP_DIR"
      echo "→ post-land sweep: landed tree was gated '$tier' (pruned) — launching background full-suite sweep (log: $LOG)"
      nohup bash "${BASH_SOURCE[0]}" --run "$SHA" \
        ${BRANCH:+--branch} ${BRANCH:+"$BRANCH"} ${ISSUE:+--issue} ${ISSUE:+"$ISSUE"} \
        >> "$LOG" 2>&1 < /dev/null &
      ;;
    full)
      echo "→ post-land sweep: landed tree already gated 'full' — no sweep needed"
      ;;
    "")
      echo "→ post-land sweep: no gate stamp for the landed tree (docs-only or skipped gate) — no sweep"
      ;;
    *)
      echo "→ post-land sweep: unknown stamped tier '$tier' — no sweep"
      ;;
  esac
}

# --- --run: lock / queue ------------------------------------------------------------
PIDFILE=""
LOCK_OWNED=0
release_lock() { [ "$LOCK_OWNED" = "1" ] && rm -f "$PIDFILE" 2>/dev/null || true; }

# Overwrite-in-place queueing: a tmp + mv per request keeps writes atomic and
# makes the NEWEST blocked request the only one kept.
queue_request() {
  local tmp
  tmp="$(mktemp "$SWEEP_DIR/.queue.XXXXXX")" || return 1
  printf '%s\t%s\t%s\n' "$SHA" "$BRANCH" "$ISSUE" > "$tmp"
  mv -f "$tmp" "$SWEEP_DIR/queue"
}

# 0 = acquired; 1 = held by a live sweep (queue instead). A pidfile whose
# process is gone is stale (a crashed sweep must not wedge the safety net) —
# remove it and retry; noclobber arbitrates racing takers.
acquire_lock() {
  local pid
  while :; do
    if ( set -C; echo "$$" > "$PIDFILE" ) 2>/dev/null; then
      LOCK_OWNED=1
      return 0
    fi
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 1
    fi
    rm -f "$PIDFILE"
  done
}

# --- --run: one sweep --------------------------------------------------------------
# Runs the suite for the checkout's CURRENT clean tree; output captured to $1.
run_suite() {
  local cap="$1"
  if [ -n "${GATE_SWEEP_CMD:-}" ]; then
    bash -c "$GATE_SWEEP_CMD" > "$cap" 2>&1
    return
  fi
  # Real runner: reuse the gate's own resolution + repo-integrity tripwire.
  # utils.sh lives beside gate-stamp.sh in both source and installed layouts.
  local utils
  utils="$(dirname "$GS_LIB")/utils.sh"
  # shellcheck disable=SC1090
  source "$utils" 2>/dev/null || true
  if ! command -v detect_pytest >/dev/null 2>&1; then
    log "no runner resolvable (hooks utils.sh unavailable) — sweep aborted"
    return 0
  fi
  local runner
  runner="$(detect_pytest "." || true)"
  if [ -z "$runner" ]; then
    log "no pytest available — sweep aborted"
    return 0
  fi
  read -r -a RUNNER_ARR <<< "$runner"
  if command -v run_under_tripwire >/dev/null 2>&1; then
    run_under_tripwire "${RUNNER_ARR[@]}" > "$cap" 2>&1
  else
    "${RUNNER_ARR[@]}" > "$cap" 2>&1
  fi
}

sweep_fingerprint() {
  if [ -n "${GATE_SWEEP_CMD:-}" ]; then
    printf '%s' "GATE_SWEEP_CMD:${GATE_SWEEP_CMD}"
  elif [ -n "${RUNNER_ARR+x}" ]; then
    "${RUNNER_ARR[@]}" --version 2>/dev/null | head -n 1 || true
  else
    printf 'gate-sweep'
  fi
}

# Red → GitHub issue: failing ids + the landing commit/branch/source issue, so
# the failure enters the normal backlog → spoke flow. gh missing or failing is
# an ERROR in the log, never a silent swallow (and never a non-zero exit).
file_red_issue() {
  local cap="$1" fails title body
  fails="$(grep -E '^(FAILED|ERROR) ' "$cap" | head -50 || true)"
  [ -n "$fails" ] || fails="$(tail -n 30 "$cap" || true)"
  title="Post-land sweep red: ${BRANCH:-unknown-branch} @ ${SHA:0:9}"
  body="The conditional post-land background sweep (issue #124) ran the full suite
after this land and it FAILED — the pruned gate that certified the land missed these.

- landed commit: ${SHA}
- branch: ${BRANCH:-unknown}${ISSUE:+
- source issue: #${ISSUE}}

Failing tests:
\`\`\`
${fails}
\`\`\`"
  if ! command -v gh >/dev/null 2>&1; then
    log "ERROR: sweep red but gh not found — issue NOT filed; failures:"
    printf '%s\n' "$fails" >> "$LOG"
    return 0
  fi
  if gh issue create --title "$title" --body "$body" >> "$LOG" 2>&1; then
    log "sweep red — filed issue: $title"
  else
    log "ERROR: sweep red but gh issue creation failed — file it by hand; failures:"
    printf '%s\n' "$fails" >> "$LOG"
  fi
}

process_request() {
  local tree tier cap rc
  if ! tree="$(gate_stamp_tree)"; then
    log "sweep skipped: working tree dirty — cannot key a proof on HEAD^{tree}"
    return 0
  fi
  tier="$(stamped_tier "$tree")"
  if [ "$tier" = "full" ]; then
    log "sweep skipped: tree $tree already carries a full-tier stamp"
    return 0
  fi
  cap="$(mktemp "${TMPDIR:-/tmp}/gate-sweep-out.XXXXXX")" || return 0
  log "sweep start: tree $tree (stamped: ${tier:-none}) for landed ${SHA:0:9}${BRANCH:+ ($BRANCH)}"
  rc=0
  run_suite "$cap" || rc=$?
  if [ "$rc" -eq 0 ]; then
    gate_stamp_mint "$tree" full "$(sweep_fingerprint)" \
      || log "stamp upgrade failed (non-fatal — a later gate re-proves the tree)"
    log "sweep green: stamp upgraded to full for tree $tree"
  else
    log "sweep RED (suite exit $rc) for landed ${SHA:0:9} — filing issue"
    file_red_issue "$cap"
  fi
  rm -f "$cap"
}

do_run() {
  mkdir -p "$SWEEP_DIR"
  PIDFILE="$SWEEP_DIR/lock.pid"
  if ! acquire_lock; then
    queue_request || log "could not queue follow-up sweep for ${SHA:0:9}"
    log "sweep already running — queued follow-up for ${SHA:0:9}"
    return 0
  fi
  trap release_lock EXIT
  while :; do
    process_request
    [ -f "$SWEEP_DIR/queue" ] || break
    IFS=$'\t' read -r SHA BRANCH ISSUE < "$SWEEP_DIR/queue" || true
    rm -f "$SWEEP_DIR/queue"
  done
}

case "$MODE" in
  spawn) do_spawn ;;
  run)   do_run ;;
esac
exit 0
