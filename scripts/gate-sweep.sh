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
#   gate-sweep.sh --run [--refresh-only] <merged-sha> [--branch <b>] [--issue <n>]
#
#   --spawn  synchronous decision (milliseconds): read the green-tree stamp
#            (issue #122) for <merged-sha>^{tree}. A PRUNED tier (testmon or
#            selected) detaches a --run worker; a `full` stamp detaches a
#            --run --refresh-only worker that rebuilds the pre-warmed baseline
#            off the already-proven tree (no suite, no stamp change — issue
#            #327); no stamp at all (docs-only skip, --skip-tests — the gate
#            certified nothing pruned) launches nothing. Always exits 0.
#   --refresh-only  (with --run) skip the safety-net suite entirely and only
#            rebuild <git-common-dir>/.testmondata-baseline (issue #327). The
#            full-tier land already proved the tree green at push time, so this
#            is a baseline rebuild, never a correctness re-check. Shares the
#            one-at-a-time lock + newest-wins queue with the sweep path (the
#            queue carries the mode), since both write the baseline.
#   --run    the detached worker. One sweep at a time per checkout: a pidfile
#            lock under <git-common-dir>/.gate-sweep/; a held lock queues at
#            most ONE newest-wins follow-up in `queue`. The worker re-keys on
#            the checkout's CURRENT clean tree (a land may have superseded the
#            spawn), skips a tree already stamped `full`, and runs the full
#            suite. The suite runs under an OBSERVE-only repo-integrity tripwire
#            (issue #267): the sweep's red/green verdict is the suite's OWN
#            pytest exit, never a tripwire breach, and the tripwire never
#            restores a ref. This is a live hub/main checkout, so a concurrent
#            /afk drain FF-advancing main/origin/*/sibling refs, stamping
#            needs-human-land/* tags, and moving HEAD is legitimate, not a
#            breach; only a genuine escape (config flip / non-FF ref move) is
#            logged (a note in sweep.log) — never a red verdict, never a rewind.
#            Green upgrades the tree's stamp to `full` (so back-to-back
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
# Accepted asymmetry: only a GREEN sweep mints the `full` stamp, so a red tree
# keeps its pruned stamp and a re-land of the same red content sweeps (and
# files) again. Reds are rare and the duplicate issue is a louder signal, not
# data loss — revisit with a per-tree red marker only if it becomes noisy.
#
# Worker output (notes + suite log) lands in <git-common-dir>/.gate-sweep/
# sweep.log, so the detached run stays observable after the land returned.
set -euo pipefail

# Best-effort contract, self-enforced: this runs on the land's tail, so NO
# internal failure (disk full, read-only .git, EPIPE on a closed pipe) may
# ever surface as a non-zero exit to the caller. The EXIT trap makes every
# set -e abort exit 0; do_run swaps in its own trap that adds lock release.
trap 'exit 0' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

warn() { echo "gate-sweep: $*" >&2; }

# --- args -------------------------------------------------------------------------
MODE=""
SHA=""
BRANCH=""
ISSUE=""
REFRESH_ONLY=""   # --run --refresh-only (issue #327): rebuild the baseline only, no suite
while [ "$#" -gt 0 ]; do
  case "$1" in
    --spawn)        MODE="spawn"; shift ;;
    --run)          MODE="run"; shift ;;
    --refresh-only) REFRESH_ONLY=1; shift ;;
    --branch)       [ "$#" -ge 2 ] || { warn "--branch needs a value"; exit 0; }; BRANCH="$2"; shift 2 ;;
    --issue)        [ "$#" -ge 2 ] || { warn "--issue needs a value"; exit 0; }; ISSUE="$2"; shift 2 ;;
    -*)             warn "unknown option: $1"; exit 0 ;;
    *)              SHA="$1"; shift ;;
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

# <git-common-dir>/.testmondata-baseline — the maintained testmon DB this sweep
# refreshes on green (issue #276), copied into every fresh worktree by worktree-new.sh
# so its first push runs a testmon incremental instead of the full-suite seed.
baseline_path() {
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  case "$common" in
    /*) ;;
    *)  common="$PWD/$common" ;;
  esac
  printf '%s/.testmondata-baseline' "$common"
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
      # A FULL-tier land needs no safety-net sweep (the gate already proved the whole
      # suite green at push time), but it must still refresh the pre-warmed baseline —
      # which previously only pruned tiers did, so the baseline drifted stale exactly
      # when the most tests changed (issue #327). Detach a --refresh-only worker that
      # rebuilds the baseline off this already-proven tree: no suite re-run, no stamp
      # change. Same off-critical-path discipline (nohup / trap 'exit 0' / one-at-a-time
      # lock) and same refresh_testmon_baseline mechanism as the pruned green path.
      mkdir -p "$SWEEP_DIR"
      echo "→ post-land baseline refresh: landed tree already gated 'full' — launching background testmon baseline refresh (log: $LOG)"
      nohup bash "${BASH_SOURCE[0]}" --run --refresh-only "$SHA" \
        ${BRANCH:+--branch} ${BRANCH:+"$BRANCH"} ${ISSUE:+--issue} ${ISSUE:+"$ISSUE"} \
        >> "$LOG" 2>&1 < /dev/null &
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
  # Carry REFRESH_ONLY (issue #327) as the 4th field so a newest-wins queued follow-up
  # runs in its OWN mode — a queued refresh is never run as a full sweep, nor a queued
  # sweep downgraded to a refresh, when the two modes share this lock.
  printf '%s\t%s\t%s\t%s\n' "$SHA" "$BRANCH" "$ISSUE" "$REFRESH_ONLY" > "$tmp"
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
  # Real runner: reuse the gate's own runner resolution, then run under an
  # OBSERVE-only tripwire (issue #267 — see the tripwire block below).
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
  # pytest-xdist on the full sweep (issue #276): the post-land sweep re-runs the
  # whole suite, which is I/O-bound and embarrassingly parallel — run it under
  # `-n auto`. Guarded on the plugin being present (an install without pytest-xdist
  # degrades to single-process rather than erroring the sweep). Capture --help then
  # case-match rather than piping to grep -q: an early -q match would SIGPIPE the
  # runner under pipefail and falsely report xdist absent.
  local xdist=() help=""
  help="$("${RUNNER_ARR[@]}" --help 2>/dev/null || true)"
  case "$help" in
    *"-n numprocesses"*|*"--numprocesses"*) xdist=(-n auto) ;;
  esac
  # Observe-only on this surface (issue #267): the sweep re-runs the full suite on the
  # live hub/main checkout, where a concurrent /afk drain legitimately FF-advances
  # main/origin/*/sibling refs, stamps needs-human-land/* tags, and moves HEAD.
  # run_under_tripwire_observe returns the SUITE's OWN exit code — never the tripwire's
  # 97, so a drain ref move can never masquerade as a red — and never restores; it only
  # logs a note (to $LOG) if a genuine escape (config flip / non-FF ref move) is seen.
  # It captures the suite's stdout+stderr into $cap itself. The bare fallback
  # is for an older installed utils.sh that predates the observe helper.
  # Two-phase sweep (issue #328): the parallel-safe bulk under `-n auto -m "not serial"`,
  # then the ref-mutating serial tail single-process under `-m serial` — the same split the
  # push gate runs, so the sweep's floor drops with it. The serial leg's exit 5 ("no tests
  # collected", nothing marked serial) is a GREEN outcome. Each phase captures to its own
  # temp file; both are concatenated into $cap so file_red_issue reads failing ids from
  # either. The green/red verdict is the suite's own (the observe tripwire never returns a
  # breach code here).
  local rc=0 rc2=0
  if command -v run_under_tripwire_observe >/dev/null 2>&1; then
    run_under_tripwire_observe "${cap}.par" "${RUNNER_ARR[@]}" ${xdist[@]+"${xdist[@]}"} \
      -m "not serial" 2>>"$LOG" || rc=$?
    run_under_tripwire_observe "${cap}.ser" "${RUNNER_ARR[@]}" -m serial 2>>"$LOG" || rc2=$?
  else
    "${RUNNER_ARR[@]}" ${xdist[@]+"${xdist[@]}"} -m "not serial" > "${cap}.par" 2>&1 || rc=$?
    "${RUNNER_ARR[@]}" -m serial > "${cap}.ser" 2>&1 || rc2=$?
  fi
  # Exit 5 ("no tests collected") is green on EITHER leg: the serial leg when nothing is
  # marked serial, the parallel leg for a serial-only suite. Normalize both.
  [ "$rc" = "5" ] && rc=0
  [ "$rc2" = "5" ] && rc2=0
  cat "${cap}.par" "${cap}.ser" > "$cap" 2>/dev/null || true
  rm -f "${cap}.par" "${cap}.ser" 2>/dev/null || true
  [ "$rc" -ne 0 ] || rc=$rc2
  return "$rc"
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

# Refresh the pre-warmed .testmondata baseline (issue #276). After a GREEN full sweep,
# rebuild the maintained baseline at <git-common-dir>/.testmondata-baseline via
# `pytest --testmon` (relocated with TESTMON_DATAFILE so the hub's own .testmondata is
# untouched) so the next fresh worktree copies it and runs a first-push testmon
# INCREMENTAL instead of the full-suite seed. SERIAL — testmon serializes a single-writer
# DB and does not compose with xdist, so this leg never gets `-n auto`.
#
# ATOMIC PUBLISH (#276 review): build into a sibling temp seeded from the current baseline
# (keeps the rebuild incremental), then `mv` it over the baseline. worktree-new.sh copies
# the baseline with a bare `cp` and can run concurrently under an /afk drain; an in-place
# rewrite could hand that reader a torn DB mid-checkpoint. The temp+mv means a reader sees
# only the complete old inode or the complete new one. testmon checkpoints its WAL into the
# main file on connection close, so the published file needs no -wal/-shm sidecar.
#
# The refresh runs the suite WITHOUT the #267 observe-only tripwire that wraps the verdict
# run: the verdict pass already observed this exact (green, clean) tree moments earlier, so
# re-wrapping would only re-log a ref escape — not prevent one — for a best-effort DB build.
# It runs inline in the worker, so the first-ever (full) build doubles the worker's runtime;
# subsequent refreshes are incremental (fast). Best-effort: any failure is logged and leaves
# the previous baseline untouched (a stale/missing baseline degrades to a full seed at spawn,
# never a wrong-green — testmon's environment row re-runs full on a dep mismatch).
# GATE_SWEEP_TESTMON_CMD stubs the build for tests (mirrors GATE_SWEEP_CMD).
refresh_testmon_baseline() {
  local tree="$1" baseline work out rc=0
  baseline="$(baseline_path)" || { log "baseline refresh: cannot resolve git-common-dir — skipped"; return 0; }
  out="$(mktemp "${TMPDIR:-/tmp}/gate-sweep-tm.XXXXXX")" || return 0
  # The build temp is a SIBLING of the baseline (same .git dir → same filesystem → atomic mv).
  work="$(mktemp "${baseline}.XXXXXX")" || { rm -f "$out"; return 0; }
  [ -r "$baseline" ] && cp "$baseline" "$work" 2>/dev/null || true   # seed → incremental rebuild
  if [ -n "${GATE_SWEEP_TESTMON_CMD:-}" ]; then
    TESTMON_DATAFILE="$work" bash -c "$GATE_SWEEP_TESTMON_CMD" > "$out" 2>&1 || rc=$?
  else
    # Resolve the runner if run_suite did not already (e.g. the GATE_SWEEP_CMD path).
    if [ -z "${RUNNER_ARR+x}" ]; then
      local utils runner
      utils="$(dirname "$GS_LIB")/utils.sh"
      # shellcheck disable=SC1090
      source "$utils" 2>/dev/null || true
      if ! command -v detect_pytest >/dev/null 2>&1; then
        log "baseline refresh: no runner resolvable — skipped"; rm -f "$out" "$work"; return 0
      fi
      runner="$(detect_pytest "." || true)"
      [ -n "$runner" ] || { log "baseline refresh: no pytest — skipped"; rm -f "$out" "$work"; return 0; }
      read -r -a RUNNER_ARR <<< "$runner"
    fi
    # testmon must be present; capture --help then case-match (a piped `grep -q` would
    # SIGPIPE the runner under pipefail and falsely report it absent).
    local help
    help="$("${RUNNER_ARR[@]}" --help 2>/dev/null || true)"
    case "$help" in
      *--testmon*) ;;
      *) log "baseline refresh: runner lacks testmon — skipped"; rm -f "$out" "$work"; return 0 ;;
    esac
    TESTMON_DATAFILE="$work" "${RUNNER_ARR[@]}" --testmon > "$out" 2>&1 || rc=$?
  fi
  if [ "$rc" -eq 0 ] && mv -f "$work" "$baseline" 2>/dev/null; then
    log "baseline refresh: .testmondata-baseline updated for tree $tree"
  else
    log "baseline refresh FAILED (exit $rc) for tree $tree — previous baseline kept:"
    tail -n 20 "$out" >> "$LOG" 2>/dev/null || true
    rm -f "$work"
  fi
  rm -f "$out"
}

process_request() {
  local tree tier cap rc
  if ! tree="$(gate_stamp_tree)"; then
    log "sweep skipped: working tree dirty — cannot key a proof on HEAD^{tree}"
    return 0
  fi
  # --refresh-only (issue #327): a FULL-tier land already proved this tree green at push
  # time, so rebuild the pre-warmed baseline off it and stop — no suite re-run (not a
  # correctness re-check) and no stamp change (the tree is already stamped full). Runs
  # here so it shares the one-at-a-time lock and newest-wins queue with the sweep path
  # (both write .testmondata-baseline, so serializing them is correct).
  if [ -n "$REFRESH_ONLY" ]; then
    log "baseline refresh (full-tier land): tree $tree for landed ${SHA:0:9}${BRANCH:+ ($BRANCH)}"
    refresh_testmon_baseline "$tree"
    return 0
  fi
  tier="$(stamped_tier "$tree")"
  if [ "$tier" = "full" ]; then
    log "sweep skipped: tree $tree already carries a full-tier stamp"
    return 0
  fi
  cap="$(mktemp "${TMPDIR:-/tmp}/gate-sweep-out.XXXXXX")" || return 0
  log "sweep start: tree $tree (stamped: ${tier:-none}) for landed ${SHA:0:9}${BRANCH:+ ($BRANCH)}"
  # $rc is the suite's OWN exit code (issue #267): run_suite runs the real suite under
  # the observe-only tripwire, which never returns TRIPWIRE_BREACH_RC, so a concurrent
  # /afk drain moving refs mid-sweep can no longer be conflated with a test failure and
  # file a spurious red issue. Only a genuine pytest red reaches file_red_issue below.
  rc=0
  run_suite "$cap" || rc=$?
  if [ "$rc" -eq 0 ]; then
    gate_stamp_mint "$tree" full "$(sweep_fingerprint)" \
      || log "stamp upgrade failed (non-fatal — a later gate re-proves the tree)"
    log "sweep green: stamp upgraded to full for tree $tree"
    # Refresh the pre-warmed testmon baseline off the just-proven-green tree (issue #276).
    refresh_testmon_baseline "$tree"
  else
    log "sweep RED (suite exit $rc) for landed ${SHA:0:9} — filing issue"
    file_red_issue "$cap"
  fi
  rm -f "$cap"
}

do_run() {
  mkdir -p "$SWEEP_DIR"
  PIDFILE="$SWEEP_DIR/lock.pid"
  # Install the release trap BEFORE acquiring the lock, so a signal any time
  # after the pidfile appears releases it instead of leaving a stale lock that
  # wedges the safety net until the kill-0 self-heal notices. The LOCK_OWNED
  # guard in release_lock makes it a no-op until we actually own the lock, so
  # the queue-blocked path below never deletes the live holder's pidfile.
  trap 'release_lock; exit 0' EXIT INT TERM
  if ! acquire_lock; then
    queue_request || log "could not queue follow-up sweep for ${SHA:0:9}"
    log "sweep already running — queued follow-up for ${SHA:0:9}"
    return 0
  fi
  while :; do
    process_request
    # Atomic claim: rename the queue to a private copy before reading it. A
    # request mv'd over `queue` after this point lands as a fresh queue and is
    # picked up on the next iteration — a read-then-rm would instead delete a
    # newer request that arrived in the read→rm window, unprocessed.
    mv "$SWEEP_DIR/queue" "$SWEEP_DIR/queue.$$" 2>/dev/null || break
    IFS=$'\t' read -r SHA BRANCH ISSUE REFRESH_ONLY < "$SWEEP_DIR/queue.$$" || true
    rm -f "$SWEEP_DIR/queue.$$"
  done
}

case "$MODE" in
  spawn) do_spawn ;;
  run)   do_run ;;
esac
exit 0
