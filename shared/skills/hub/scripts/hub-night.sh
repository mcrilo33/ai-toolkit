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

# --- queue + in-flight + dispatch --------------------------------------------

# queue_issues -> the night queue: numbers of open issues labelled `night`, one
# per line. The issue stays the contract; missing/unauthed gh degrades to empty.
queue_issues() {
  command -v gh >/dev/null 2>&1 || { log "gh not found — empty night queue"; return 0; }
  gh issue list --label night --state open --json number -q '.[].number' 2>/dev/null \
    | grep -E '^[0-9]+$' || true
}

# inflight_worktrees -> "<path>\t<issue>" for every worktree whose branch slug
# leads with an issue number, via `git worktree list` + the leading-digits slug
# parse hub-status.sh uses. The hub's own `main` checkout (no digits) drops out.
inflight_worktrees() {
  local line path branch slug num
  git worktree list 2>/dev/null | while IFS= read -r line; do
    path="$(awk '{print $1}' <<<"$line")"
    branch="$(sed -n 's/.*\[\(.*\)\].*/\1/p' <<<"$line")"
    [ -n "$branch" ] || continue
    slug="${branch##*/}"
    num="$(printf '%s' "$slug" | sed 's/^\([0-9]*\).*/\1/')"
    [ -n "$num" ] && printf '%s\t%s\n' "$path" "$num"
  done
}

# inflight_issues -> just the issue numbers, one per line. Drives the idempotent
# skip: a branch already in flight is never re-spawned.
inflight_issues() {
  inflight_worktrees | cut -f2
}

# _transcript_idle_seconds <worktree-path> -> seconds since the spoke's newest
# Claude transcript was touched, or empty when there is no transcript yet. Same
# slug + newest-jsonl selection hub-status.sh uses; mtime via BSD/GNU stat.
_transcript_idle_seconds() {
  local wt_path="$1" projects_root slug project_dir jsonl mtime
  projects_root="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
  slug="$(printf '%s' "$wt_path" | sed 's/[^A-Za-z0-9]/-/g')"
  project_dir="$projects_root/$slug"
  [ -d "$project_dir" ] || return 0
  jsonl="$(ls -t "$project_dir"/*.jsonl 2>/dev/null | head -1)"
  [ -n "$jsonl" ] || return 0
  mtime="$(stat -f %m "$jsonl" 2>/dev/null || stat -c %Y "$jsonl" 2>/dev/null)"
  [ -n "$mtime" ] || return 0
  printf '%s\n' "$(( $(now_epoch) - mtime ))"
}

# slot_state <worktree-path> <issue> -> done|free|busy. A slot is freed when the
# spoke is done (a TERMINAL marker at the branch tip) or idle longer than
# NIGHT_IDLE_MINUTES; otherwise it is busy and keeps occupying its slot. A spoke
# with no transcript yet (just spawned) reads as busy so the supervisor never
# backfills over a starting spoke.
#
# Terminal markers (issue #40) are ready/<issue> (whole issue done — the
# hub-status.sh mergeable rule), accept/<issue> (built + pushed + agent-reviewed,
# final sign-off inherently human) and blocked/<issue> (stuck — answer + re-queue).
# Each frees the slot. The non-terminal gate/<issue> PLAN park is deliberately NOT
# in this set: a gate-parked spoke is awaiting review and keeps its slot.
# UPGRADE: surface explicit waiting-on-input (open AskUserQuestion / trailing
# notification) as a freed slot once Phase 2's richer markers land; folded into
# idle for now.
slot_state() {
  local wt_path="$1" issue="$2" tip marker age kind
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    for kind in ready accept blocked; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      if [ "$marker" = "$tip" ]; then
        printf 'done\n'; return
      fi
    done
  fi
  age="$(_transcript_idle_seconds "$wt_path")"
  if [ -n "$age" ] && [ "$age" -gt $(( NIGHT_IDLE_MINUTES * 60 )) ]; then
    printf 'free\n'; return
  fi
  printf 'busy\n'
}

# slot_counts -> "<busy> <done>" across all in-flight worktrees. busy spokes
# occupy a slot; done spokes are finished work (drop out of tasks_left); free
# (idle) spokes count as neither, so their slot is available for backfill.
slot_counts() {
  local path issue st busy=0 finished=0
  while IFS="$(printf '\t')" read -r path issue; do
    [ -n "$issue" ] || continue
    st="$(slot_state "$path" "$issue")"
    case "$st" in
      busy) busy=$(( busy + 1 )) ;;
      done) finished=$(( finished + 1 )) ;;
    esac
  done < <(inflight_worktrees)
  printf '%s %s\n' "$busy" "$finished"
}

# kickoff_for <issue> -> the spoke's first prompt. Placeholder: the standard
# start-task gate-`plan` kickoff with the issue number substituted. The
# night-specific kickoff + agent gate-review is Phase 2 (out of scope here).
kickoff_for() {
  local n="$1"
  cat <<EOF
You're in a dedicated worktree for issue #$n (Gate: plan). Run /source to anchor to
issue #$n and read it. Before touching code, break the issue body into a task ledger
(TaskCreate, or TodoWrite on older runtimes) — one todo per subtask × the solo-cycle
steps that apply (ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

This task's gate is plan: the PLAN gate comes first — explore the code and print the
full implementation plan (files, approach, test strategy, open questions) as a normal
visible message, and WAIT for approval before writing code (before GREEN). Do not
defer the plan into an approval card — the message itself is the plan. Park there
rather than blocking: emit the gate/$n marker
(bash .ai-toolkit/scripts/spoke-ready.sh --gate $n) so the hub sees you parked,
and proceed into the cycle once approved.

Then implement it following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). Push
your own branch on every subtask without asking; when your ledger shows the issue's
acceptance criteria are all met, that is the final subtask — push and emit the ready/$n
marker, also without asking. Do NOT self-land — the hub lands #$n.
EOF
}

# dispatch_issue <issue> — spawn a spoke for the issue via worktree-new.sh.
# Resolution order: $WT_NEW, then a sibling, then the synced .ai-toolkit/scripts
# copy. worktree-new.sh creates feature/<issue>-<slug>, copies .claude/, and
# launches the seeded agent (all tmux/VS Code side effects live there).
dispatch_issue() {
  local issue="$1"
  local main_root="${MAIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
  local wt_new="${WT_NEW:-$SCRIPT_DIR/worktree-new.sh}"
  if [ ! -x "$wt_new" ] && [ -x "$main_root/.ai-toolkit/scripts/worktree-new.sh" ]; then
    wt_new="$main_root/.ai-toolkit/scripts/worktree-new.sh"
  fi
  if [ ! -x "$wt_new" ]; then
    log "cannot find worktree-new.sh (set WT_NEW) — skipping #$issue"
    return 1
  fi
  log "→ dispatch #$issue"
  "$wt_new" "$issue" --type feature --prompt "$(kickoff_for "$issue")"
}

# --- supervisor tick ----------------------------------------------------------

# supervise_tick — one supervisor pass: read the queue + in-flight set, recompute
# the adaptive concurrency target, and dispatch pending issues into free slots,
# honoring the strict launch cutoff. (Slot-free detection for in-flight spokes
# arrives in ST3; here every in-flight spoke is counted as occupying a slot.)
supervise_tick() {
  local now queue inflight time_left queue_count busy_count done_count
  local tasks_left target free_slots attempts dispatched n
  now="$(now_epoch)"
  queue="$(queue_issues)"
  inflight="$(inflight_issues)"
  time_left="$(minutes_until "$NIGHT_END" "$now")"

  if launch_cutoff_reached "$time_left"; then
    log "launch cutoff: ${time_left}m left < T_task ${NIGHT_TASK_MINUTES}m — not starting new spokes"
    return 0
  fi

  queue_count="$(printf '%s\n' "$queue" | grep -c '^[0-9]' || true)"
  read -r busy_count done_count <<<"$(slot_counts)"
  # Remaining work = queued issues not yet done; done spokes await the hub's land.
  tasks_left=$(( queue_count - done_count ))
  [ "$tasks_left" -lt 0 ] && tasks_left=0
  target="$(night_target "$tasks_left" "$time_left")"
  # Only busy spokes hold a slot; done/idle ones free theirs for backfill.
  free_slots=$(( target - busy_count ))
  [ "$free_slots" -lt 0 ] && free_slots=0
  log "tick: queue=$queue_count busy=$busy_count done=$done_count time_left=${time_left}m target=$target free=$free_slots"

  # Bound the work to free_slots ATTEMPTS, not successes: a systemic dispatch
  # failure (e.g. worktree-new.sh missing) then can't hammer the whole queue —
  # the next tick retries the still-pending issues.
  attempts=0
  dispatched=0
  while IFS= read -r n; do
    [ -n "$n" ] || continue
    printf '%s\n' "$inflight" | grep -qxF "$n" && continue   # already in flight (idempotent skip)
    [ "$attempts" -ge "$free_slots" ] && break
    attempts=$(( attempts + 1 ))
    dispatch_issue "$n" && dispatched=$(( dispatched + 1 ))
  done <<EOF
$queue
EOF
  log "dispatched $dispatched spoke(s) this tick ($attempts attempted)"
}

# night_done -> true when the supervisor has nothing left to do: the launch
# cutoff has passed (no window to start anything tonight), or the queue is fully
# drained with nothing still in flight. Ends the loop instead of spinning.
night_done() {
  local time_left queue_count inflight_count
  time_left="$(minutes_until "$NIGHT_END" "$(now_epoch)")"
  launch_cutoff_reached "$time_left" && return 0
  queue_count="$(queue_issues | grep -c '^[0-9]' || true)"
  inflight_count="$(inflight_issues | grep -c '^[0-9]' || true)"
  [ "$queue_count" -eq 0 ] && [ "$inflight_count" -eq 0 ]
}

main() {
  local once=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --once)    once=1; shift ;;
      -h|--help) echo "usage: hub-night.sh [--once]" >&2; return 0 ;;
      *)         log "unknown argument: $1"; return 2 ;;
    esac
  done

  MAIN_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    log "not inside a git repository"; return 1
  }

  # The supervisor loop: tick, then (unless --once) sleep and tick again until
  # the night is over or the queue is drained. The termination check runs before
  # the sleep so an already-finished night exits immediately.
  while :; do
    supervise_tick
    [ "$once" -eq 1 ] && break
    night_done && { log "night dispatcher done"; break; }
    sleep "$NIGHT_TICK_SECONDS"
  done
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
