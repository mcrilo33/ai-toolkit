#!/usr/bin/env bash
# hub-scout.sh — pre-flight batching scout for night mode (issue #40, Phase 3).
#
# Parallel night spokes are blind to each other; collisions waste night work and
# create morning rework. Before any spoke starts, this scout analyzes the whole
# `night` queue and emits a DOSSIER OF FACTS — per-issue file-scope hints, the raw
# file-overlap between issues, and the critical-path feasibility bounds (does an
# all-parallel / all-serial makespan fit before NIGHT_END?). An Opus scout AGENT
# then reads the dossier and makes the JUDGMENT call — classify each overlap
# PARALLEL / SERIAL / MERGE — and the USER approves the plan before hub-night.sh
# launches. The mechanical facts are scripted (and allowlistable); only the overlap
# classification is the agent's (#43: don't let the LLM narrate mechanical work).
#
# It SOURCES hub-night.sh so the feasibility arithmetic uses ONE clock and ONE
# T_task as the dispatcher — the "scout said it fits" and "the supervisor's math"
# cannot drift.
#
# Read-only (the dossier only reads issues). Run on the hub (main checkout) at setup.
set -uo pipefail
# noglob: scope hints are word-split intentionally, but a `Scope: *.py` glob must
# stay the LITERAL token the author wrote, not expand against the hub's cwd (which
# would silently corrupt the very facts the scout reports). The scout never relies
# on filename globbing, so disabling it script-wide is safe.
set -f

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source hub-night.sh for minutes_until / NIGHT_* / queue_issues / now_epoch.
# Resolution: a sibling (the hub skill's scripts dir, or a synced .ai-toolkit copy);
# override with HUB_NIGHT. hub-night's source-guard means this only defines its
# functions, never runs its supervisor loop.
_hub_night="${HUB_NIGHT:-$SCRIPT_DIR/hub-night.sh}"
if [ -r "$_hub_night" ]; then
  # shellcheck source=hub-night.sh
  . "$_hub_night"
else
  echo "hub-scout: cannot source hub-night.sh (set HUB_NIGHT)" >&2
  exit 1
fi

# scope_hints <body> -> the issue's file-scope hints, one per line, or "UNKNOWN".
# An explicit `Scope:` line wins (its space/comma-separated globs); else backticked
# path-looking tokens in the body; else UNKNOWN — an explicit fact the agent must
# resolve or hold the issue out, never a silent empty/disjoint assumption.
scope_hints() {
  local body="$1" line hints
  line="$(printf '%s\n' "$body" | sed -n 's/^[[:space:]]*Scope:[[:space:]]*//p' | head -1)"
  if [ -n "$line" ]; then
    # shellcheck disable=SC2086  # intentional word-split of the glob list
    printf '%s\n' $line | tr ',' '\n' | sed '/^[[:space:]]*$/d'
    return
  fi
  hints="$(printf '%s\n' "$body" | grep -oE '`[^`]+`' 2>/dev/null \
    | tr -d '`' | grep -E '/|[.](py|sh|md|ya?ml|json|js|ts)$' 2>/dev/null || true)"
  if [ -n "$hints" ]; then
    printf '%s\n' "$hints"
    return
  fi
  printf 'UNKNOWN\n'
}

# overlap <scopeA> <scopeB> -> the shared tokens (set intersection), one per line.
# A pure FACT, not a classification: "both touch shared/foo.sh" is for the agent to
# read, never a SERIAL/PARALLEL verdict this script invents.
overlap() {
  local a b tok
  a="$1"
  b=" $2 "
  for tok in $a; do
    case "$b" in *" $tok "*) printf '%s\n' "$tok" ;; esac
  done
}

# parallel_makespan <n> -> minutes to drain n tasks at the concurrency cap:
# ceil(n / NIGHT_MAX_CONCURRENCY) waves * T_task.
parallel_makespan() {
  local n="$1" waves
  waves=$(( (n + NIGHT_MAX_CONCURRENCY - 1) / NIGHT_MAX_CONCURRENCY ))
  printf '%s\n' "$(( waves * NIGHT_TASK_MINUTES ))"
}

# serial_makespan <n> -> minutes to drain n fully-serialized tasks: n * T_task.
serial_makespan() {
  printf '%s\n' "$(( $1 * NIGHT_TASK_MINUTES ))"
}

# fits_before_wake <makespan_min> <now_epoch> -> true (exit 0) when the makespan
# fits the window to NIGHT_END. The critical-path feasibility check: an
# over-committed night is caught at setup, before it is wasted.
fits_before_wake() {
  local makespan="$1" now="$2" left
  left="$(minutes_until "$NIGHT_END" "$now")"
  [ "$makespan" -le "$left" ]
}

# validate_serial_after <issue> <pred> <queue> -> true (exit 0) when a Serial-after
# directive is well-formed: not self-referential and the predecessor is in the night
# queue. A dangling/self predecessor is refused (the scout reports it; never gets
# stamped). UPGRADE: full cycle detection across a Serial-after chain — the agent
# step won't author a cycle, but a deterministic guard would harden it.
validate_serial_after() {
  local issue="$1" pred="$2" queue="$3" tok
  [ "$issue" = "$pred" ] && { echo "scout: #$issue is Serial-after itself" >&2; return 1; }
  for tok in $queue; do
    [ "$tok" = "$pred" ] && return 0
  done
  echo "scout: #$issue is Serial-after #$pred, which is not in the night queue" >&2
  return 1
}

# --- dossier ------------------------------------------------------------------
# Render the facts (queue, per-issue scope hints, feasibility bounds) plus the
# approval prompt. Classification (PARALLEL/SERIAL/MERGE) is the agent's job.
dossier() {
  local now nums count n body i j hints_i hints_j shared
  now="$(now_epoch)"
  nums="$(queue_issues)"
  count="$(printf '%s\n' "$nums" | grep -c '^[0-9]' || true)"

  echo "Night batching scout — ${count} issue(s) labelled 'night'"
  echo
  echo "Per-issue file-scope hints (UNKNOWN = hold out or assign a Scope: line):"
  # Cache each issue's hints (as a space-joined string) for the overlap matrix.
  local hint_of=""   # newline-joined "<issue> <hint1> <hint2> ..." rows
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    body="$(gh issue view "$n" --json body -q .body 2>/dev/null || true)"
    hints_i="$(scope_hints "$body" | tr '\n' ' ')"
    printf '  #%s: %s\n' "$n" "$hints_i"
    hint_of="${hint_of}${n} ${hints_i}"$'\n'
  done <<EOF
$nums
EOF
  echo
  echo "Pairwise file overlap (a FACT for the agent to classify, not a verdict):"
  local any_overlap=0
  for i in $nums; do
    hints_i="$(printf '%s\n' "$hint_of" | sed -n "s/^${i} //p")"
    case " $hints_i " in *" UNKNOWN "*) continue ;; esac
    for j in $nums; do
      [ "$i" -lt "$j" ] 2>/dev/null || continue   # each unordered pair once
      hints_j="$(printf '%s\n' "$hint_of" | sed -n "s/^${j} //p")"
      case " $hints_j " in *" UNKNOWN "*) continue ;; esac
      shared="$(overlap "$hints_i" "$hints_j" | tr '\n' ' ')"
      if [ -n "$shared" ]; then
        printf '  #%s ∩ #%s: %s\n' "$i" "$j" "$shared"
        any_overlap=1
      fi
    done
  done
  [ "$any_overlap" -eq 0 ] && echo "  (no file overlap among issues with known scope)"
  echo
  echo "Critical-path feasibility (T_task=${NIGHT_TASK_MINUTES}m, cap ${NIGHT_MAX_CONCURRENCY}, wake ${NIGHT_END}):"
  printf '  all-parallel makespan: %sm — %s\n' "$(parallel_makespan "$count")" \
    "$(fits_before_wake "$(parallel_makespan "$count")" "$now" && echo fits || echo "WON'T FIT")"
  printf '  all-serial makespan:   %sm — %s\n' "$(serial_makespan "$count")" \
    "$(fits_before_wake "$(serial_makespan "$count")" "$now" && echo fits || echo "WON'T FIT")"
  echo
  echo "Next: a scout agent classifies each overlapping pair PARALLEL / SERIAL / MERGE"
  echo "and stamps Serial-after:/Merge-into: lines into the issues; you approve the plan"
  echo "before launching hub-night.sh. See the hub skill's pre-flight scout step."
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && dossier "$@"
