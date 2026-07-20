#!/usr/bin/env bash
#
# test-budget-watch.sh — duration-budget watcher (issue #336).
#
# The pre-push gate's wall-clock silently ROTS when a slow test creeps in: #328's
# one-off profile found a 122.6s test (~6.6% of the whole run) discovered only by
# luck, by hand. This watcher is the standing mechanism. It reads the per-test
# durations the gate-sweep full run ALREADY produces off the critical path
# (gate-sweep.sh run_suite, `-n auto --durations=0`) — no new suite execution —
# compares them against two configured budgets, and on a breach surfaces a deduped
# enhancement.
#
# Usage:
#   test-budget-watch.sh <durations-capture-file> [--branch <b>] [--issue <n>]
#
#   <durations-capture-file>  the combined stdout of the sweep's pytest run(s)
#                             (per-test `--durations=0` lines + the `in <X>s`
#                             summary). Read-only — the watcher never runs a suite.
#
# Budgets (settings/ai-toolkit.yml, the single source of truth):
#   test_budget:
#     slow_test_seconds: 30   # a single test slower than X is flagged
#     suite_seconds: 480      # the whole suite slower than Y is flagged
# A general bound (a per-test ceiling + a suite target), NOT thresholds tuned to
# today's known-slow tests (scientific-integrity rule).
#
# On a breach the watcher:
#   1. Dispatches `followup-scoper` (#331) to file/append a correctly-scoped
#      `enhancement` carrying the durations as evidence. Debounced on a single-writer
#      last-seen set under <git-common-dir>/.test-budget-watch/ (AFK principle #5): a
#      persistent slow test files ONCE per regression, not every sweep.
#   2. Writes a breach record under the AFK state dir so the hub notifier
#      (hub-notify.sh) fires exactly one desktop ping per new breach.
#
# Best-effort contract (AFK #2/#6): this runs on the sweep's tail, so NO internal
# failure ever surfaces as a non-zero exit to the caller (the EXIT trap makes every
# path exit 0). A filer that cannot land is logged LOUDLY (an ERROR line with the
# full evidence), never dropped silently.
#
# DEFERRED — phase 2, documented not built here: AUTO-QUARANTINE — moving an
# over-budget test off the critical-path gate automatically. The gate is a
# correctness surface, so auto-action must be EARNED after this watch loop is
# proven. If ever added it is MOVE-TO-ASYNC-SWEEP, NEVER SKIP (coverage preserved),
# loud, and journaled. v1 surfaces the breach; a human/hub takes the action.
#
# ENV seams (tests / non-standard hosts):
#   TEST_BUDGET_CONFIG          config path (default: <script>/../settings/ai-toolkit.yml)
#   TEST_BUDGET_SLOW_SECONDS    per-test budget override (skips the config read)
#   TEST_BUDGET_SUITE_SECONDS   suite budget override (skips the config read)
#   TEST_BUDGET_SCOPER_CMD      the followup-scoper dispatch seam; invoked as
#                               `<cmd> test-budget-watch <kind> <node> <seconds>`
#   TEST_BUDGET_HUB_AGENT       explicit hub-agent.sh path (else resolved as a sibling)
#   AFK_STATE_DIR               breach-record home (default: <git-common-dir>/ai-toolkit-afk)
set -uo pipefail

# Best-effort, self-enforced: never surface a non-zero exit to the sweep's tail.
trap 'exit 0' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- args -------------------------------------------------------------------------
CAP=""
BRANCH=""
ISSUE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --branch) [ "$#" -ge 2 ] || exit 0; BRANCH="$2"; shift 2 ;;
    --issue)  [ "$#" -ge 2 ] || exit 0; ISSUE="$2"; shift 2 ;;
    -*)       shift ;;   # unknown flag — ignore (best-effort)
    *)        CAP="$1"; shift ;;
  esac
done
[ -n "$CAP" ] && [ -f "$CAP" ] || exit 0   # no capture → nothing to watch

# --- state dirs (single-writer, under the git-common-dir) --------------------------
COMMON="$(git rev-parse --git-common-dir 2>/dev/null || true)"
[ -n "$COMMON" ] || exit 0                  # not in a git repo → no durable state
case "$COMMON" in /*) ;; *) COMMON="$PWD/$COMMON" ;; esac

STATE_DIR="$COMMON/.test-budget-watch"       # our own debounce + log home
SEEN="$STATE_DIR/seen"
LOG="$STATE_DIR/watch.log"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# The breach records hub-notify surfaces live under the SAME dir it already scans
# for warned-*.txt (issue #241): AFK_STATE_DIR wins, else the git-common-dir default.
NOTIFY_DIR="${AFK_STATE_DIR:-$COMMON/ai-toolkit-afk}"

log() {
  printf '%s test-budget-watch: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true
}

# --- budgets ----------------------------------------------------------------------
# Read one integer key from the `test_budget:` block of the config. POSIX awk (BSD
# awk on this macOS host has no gawk gensub/3-arg match): track whether the current
# top-level block is `test_budget:`, then match the indented key.
_read_budget_key() {
  local key="$1" config="${TEST_BUDGET_CONFIG:-$SCRIPT_DIR/../settings/ai-toolkit.yml}"
  [ -f "$config" ] || return 0
  LC_ALL=C awk -v key="$key:" '
    /^[^[:space:]#]/ { in_block = ($1 == "test_budget:") }
    in_block && $1 == key { print $2; exit }
  ' "$config" 2>/dev/null
}

# Fail-safe defaults (AFK #2): a parse miss degrades to the DOCUMENTED budget, never
# silence. Env overrides win (tests); a non-integer config value falls back too.
SLOW="${TEST_BUDGET_SLOW_SECONDS:-$(_read_budget_key slow_test_seconds)}"
SUITE="${TEST_BUDGET_SUITE_SECONDS:-$(_read_budget_key suite_seconds)}"
case "$SLOW" in '' | *[!0-9]*) SLOW=30 ;; esac
case "$SUITE" in '' | *[!0-9]*) SUITE=480 ;; esac

# --- detect breaches from the capture ---------------------------------------------
# Emits one tab-separated breach line per breach:
#   test<TAB><seconds><TAB><nodeid>     a single test whose total > SLOW
#   suite<TAB><seconds><TAB>            the whole suite whose total > SUITE
# Per-test total sums the setup+call+teardown phases (the test's true wall cost);
# the suite total sums every `in <X>s` summary (the sweep runs two legs — parallel +
# serial — each with its own summary). LC_ALL=C keeps %.2f dot-decimal on this host.
_detect_breaches() {
  LC_ALL=C awk -v slow="$SLOW" -v suite="$SUITE" '
    {
      f1 = $1
      if (f1 ~ /^[0-9]+(\.[0-9]+)?s$/ && ($2 == "call" || $2 == "setup" || $2 == "teardown")) {
        sub(/s$/, "", f1)
        node = $3
        for (i = 4; i <= NF; i++) node = node " " $i
        sum[node] += f1 + 0
      }
      # Suite total: sum the wall-clock from each pytest SUMMARY line (the sweep runs two
      # legs — parallel + serial — each with its own summary). ANCHOR on the summary rule
      # (a line starting with the ==== fill pytest draws around it): a bare "in <N>s"
      # inside a failing tests captured stdout — which pytest echoes on a RED sweep — must
      # never inflate the total.
      if ($0 ~ /^[[:space:]]*=+/) {
        for (i = 1; i < NF; i++) {
          if ($i == "in" && $(i + 1) ~ /^[0-9]+(\.[0-9]+)?s/) {
            t = $(i + 1); sub(/s.*$/, "", t); suite_total += t + 0
          }
        }
      }
    }
    END {
      for (n in sum) if (sum[n] > slow) printf "test\t%.2f\t%s\n", sum[n], n
      if (suite_total > suite) printf "suite\t%.2f\t\n", suite_total
    }
  ' "$CAP" 2>/dev/null
}

# --- dispatch followup-scoper (best-effort, loud on miss) -------------------------
# Mirrors hub-watchdog-intervene.sh::_wd_file_defect: a stubbable seam, else a headless
# `claude -p` on the hub-agent trackable surface. A miss is an ERROR in the log with the
# full evidence — never a silent drop (AFK #2/#6).
_dispatch_scoper() {
  local kind="$1" secs="$2" node="$3" desc="$4"
  if [ -n "${TEST_BUDGET_SCOPER_CMD:-}" ]; then
    bash -c "$TEST_BUDGET_SCOPER_CMD" test-budget-watch "$kind" "$node" "$secs" >/dev/null 2>&1 \
      || log "ERROR: scoper seam failed for [$desc] — followup NOT filed; evidence: ${node:-suite} ${secs}s"
    return 0
  fi
  local ha="${TEST_BUDGET_HUB_AGENT:-}"
  if [ -z "$ha" ]; then
    for cand in "$SCRIPT_DIR/hub-agent.sh" \
                "$SCRIPT_DIR/../shared/skills/hub/scripts/hub-agent.sh"; do
      [ -f "$cand" ] && { ha="$cand"; break; }
    done
  fi
  if [ -z "$ha" ] || ! command -v claude >/dev/null 2>&1; then
    log "ERROR: no dispatch path (hub-agent.sh / claude absent) — followup NOT filed for [$desc]; evidence: ${node:-suite} ${secs}s (budgets slow=${SLOW}s suite=${SUITE}s)"
    return 0
  fi
  local prompt
  prompt="A test duration-budget breach was detected by the post-land sweep (issue #336): $desc. \
Act as followup-scoper: file ONE deduped 'enhancement' documenting this over-budget \
${kind}, carrying the measured durations as evidence (${node:-whole suite} = ${secs}s vs \
budget slow=${SLOW}s / suite=${SUITE}s). Derive the Scope: by reading the offending test \
file(s). Fix direction: profile and speed up the test, or (phase-2, not now) move it to the \
async sweep off the critical-path gate. Dedup against open issues first."
  bash "$ha" "test-budget-${kind}" --purpose "test-budget breach: $desc" -- claude -p "$prompt" >/dev/null 2>&1 \
    || log "ERROR: could not dispatch followup-scoper for [$desc]; evidence: ${node:-suite} ${secs}s"
  return 0
}

# --- act: dispatch on a NEW breach, keep the notify records in step ----------------
# Records reflect CURRENT state: clear ours first, rewrite the current breaches. A
# breach identity (a nodeid, or the literal `suite`) not already in SEEN is NEW → file.
[ -f "$SEEN" ] || : > "$SEEN" 2>/dev/null || true
seen="$(cat "$SEEN" 2>/dev/null || true)"
rm -f "$NOTIFY_DIR"/test-budget-breach-*.txt 2>/dev/null || true

current=""
breaches="$(_detect_breaches)"
while IFS=$'\t' read -r kind secs node; do
  [ -n "$kind" ] || continue
  if [ "$kind" = "suite" ]; then
    id="suite"; slug="suite"; desc="suite total ${secs}s > ${SUITE}s budget"
  else
    id="$node"
    slug="$(printf '%s' "$node" | LC_ALL=C tr -c 'A-Za-z0-9._-' '-')"
    desc="test $node took ${secs}s > ${SLOW}s budget"
  fi
  current="$current$id"$'\n'

  # Write the breach record (current state) for hub-notify to surface.
  mkdir -p "$NOTIFY_DIR" 2>/dev/null || true
  printf '#%s test-budget breach — %s\n' "${ISSUE:-?}" "$desc" \
    > "$NOTIFY_DIR/test-budget-breach-$slug.txt" 2>/dev/null || true

  # Dispatch the scoper only for a NEW breach IDENTITY (single-writer debounce): a
  # persistent — or worsening — breach files ONCE per regression, not every sweep. This
  # is deliberately coarser than the hub-notify content-hash dedup (which re-pings a
  # worsening number): the filed enhancement is the durable record, the ping is the
  # attended nudge, so re-filing on every slowdown would spam the tracker.
  if ! printf '%s\n' "$seen" | grep -qxF "$id"; then
    log "NEW breach: $desc${BRANCH:+ (branch $BRANCH)}"
    _dispatch_scoper "$kind" "$secs" "$node" "$desc"
  fi
done <<EOF
$breaches
EOF

# Persist the CURRENT breach identities as last-seen: a breach that cleared drops out
# (an empty set TRUNCATES the file), so a later re-occurrence reads as a fresh
# regression and re-files (AFK #5).
if [ -n "$current" ]; then
  printf '%s' "$current" | sort -u > "$SEEN" 2>/dev/null || true
else
  : > "$SEEN" 2>/dev/null || true
fi

exit 0
