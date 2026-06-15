#!/usr/bin/env bash
# hub-night.sh — adaptive night dispatcher for the planning hub (issue #41).
#
# Phase 1 of night mode (epic #40): drain a queue of pre-scoped `night`-labelled
# issues overnight with a self-tuning concurrency cap, so the subscription pool
# isn't blown yet the queue still finishes by wake time. Run it on the hub (main
# checkout) before bed; it loops until the queue is drained or the launch cutoff
# is reached. Re-running mid-night is safe (idempotent — see inflight_issues).
#
# Scope is the dispatcher mechanics ONLY. The night kickoff + agent gate-review
# (Phase 2), the pre-flight batching scout (Phase 3) and the morning report
# (Phase 4) are separate follow-ups on #40 and are not built here.
#
# Adaptive concurrency, recomputed each supervisor tick:
#   target = clamp(ceil(tasks_left * T_task / time_left), 1, NIGHT_MAX_CONCURRENCY)
# Sequential when the queue fits the remaining night, parallel only when it does
# not, ramping up to the cap as the night burns down. Worked cases (T_task=90,
# cap=3):
#   5 tasks  / 480 min -> 1   (queue fits the night -> sequential)
#   20 tasks / 480 min -> 3   (ceil 3.75 = 4, clamped to the cap)
#   5 tasks  / 150 min -> 3   (night burning down -> ramps up to the cap)
#
# Knobs (env, with defaults):
#   NIGHT_END=07:00              wake time; next occurrence of HH:MM at/after now
#   NIGHT_MAX_CONCURRENCY=3      concurrency cap
#   NIGHT_TASK_MINUTES=90        T_task — assumed minutes per task
#   NIGHT_IDLE_MINUTES=15        a spoke idle longer than this frees its slot
#   NIGHT_TICK_SECONDS=300       supervisor poll interval
#   NIGHT_NOW                    override "now" (epoch seconds) — testing/cron
#   WT_NEW                       path to worktree-new.sh (default: sibling)
#   CLAUDE_PROJECTS_DIR          transcript root (default: $HOME/.claude/projects)
#
# Usage:
#   hub-night.sh            # supervise until morning
#   hub-night.sh --once     # run a single tick and exit (tests / external cron)
#
# Read-only against the work except for dispatching spokes via worktree-new.sh.
# It never merges, never lands — the hub lands finished spokes on /land.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${NIGHT_END:=07:00}"
: "${NIGHT_MAX_CONCURRENCY:=3}"
: "${NIGHT_TASK_MINUTES:=90}"
: "${NIGHT_IDLE_MINUTES:=15}"
: "${NIGHT_TICK_SECONDS:=300}"

log() { printf '%s\n' "$*" >&2; }

# --- portable date helpers ----------------------------------------------------
# BSD (macOS) and GNU date differ; try BSD form first, fall back to GNU.

# _date_ymd <epoch> -> YYYY-MM-DD (local time)
_date_ymd() {
  date -r "$1" +%Y-%m-%d 2>/dev/null || date -d "@$1" +%Y-%m-%d
}

# _epoch_at <yyyy-mm-dd> <hh:mm> -> epoch seconds (local time)
# Seconds are pinned to :00 explicitly: BSD `date -j -f` fills a missing %S
# field from the current wall clock, which would make minutes_until leak the
# invocation second and flip a cutoff/concurrency decision by a minute.
_epoch_at() {
  date -j -f "%Y-%m-%d %H:%M:%S" "$1 $2:00" +%s 2>/dev/null || date -d "$1 $2" +%s
}

# --- pure decision layer ------------------------------------------------------

# night_target <tasks_left> <time_left_min> -> concurrency target.
# clamp(ceil(tasks_left * T_task / time_left), 1, NIGHT_MAX_CONCURRENCY).
# A non-positive time_left (night already over) pins to the cap rather than
# dividing by zero; the launch cutoff stops new spokes in that case anyway.
night_target() {
  local tasks_left="$1" time_left="$2" target
  if [ "$time_left" -le 0 ]; then
    target="$NIGHT_MAX_CONCURRENCY"
  else
    target=$(( (tasks_left * NIGHT_TASK_MINUTES + time_left - 1) / time_left ))
  fi
  [ "$target" -lt 1 ] && target=1
  [ "$target" -gt "$NIGHT_MAX_CONCURRENCY" ] && target="$NIGHT_MAX_CONCURRENCY"
  printf '%s\n' "$target"
}

# minutes_until <hh:mm> <now_epoch> -> whole minutes until the next HH:MM.
# Today's HH:MM if it is still ahead, otherwise tomorrow's.
minutes_until() {
  local hhmm="$1" now="$2" target
  target="$(_epoch_at "$(_date_ymd "$now")" "$hhmm")"
  [ "$target" -le "$now" ] && target=$(( target + 86400 ))
  printf '%s\n' "$(( (target - now) / 60 ))"
}

# launch_cutoff_reached <time_left_min> — true (exit 0) when too little of the
# night remains to start another task: no abandoned half-builds. In-flight
# spokes are untouched; this only gates new launches.
launch_cutoff_reached() {
  [ "$1" -lt "$NIGHT_TASK_MINUTES" ]
}

# now_epoch -> current time, overridable via NIGHT_NOW for tests/cron.
now_epoch() {
  printf '%s\n' "${NIGHT_NOW:-$(date +%s)}"
}

main() {
  # Dispatch + supervisor loop are wired in the following subtasks (ST2/ST3).
  log "hub-night configured: NIGHT_END=$NIGHT_END NIGHT_MAX_CONCURRENCY=$NIGHT_MAX_CONCURRENCY NIGHT_TASK_MINUTES=$NIGHT_TASK_MINUTES"
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
