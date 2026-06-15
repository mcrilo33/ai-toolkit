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
: "${NIGHT_SPOKE_MAX_MINUTES:=180}"   # wall-clock ceiling per spoke (~2x T_task)
: "${NIGHT_STATE_DIR:=}"              # where per-spoke dispatch epochs persist
# NIGHT_MAX_BUDGET_USD (optional)     # best-effort in-process --max-budget-usd cap

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

# spoke_over_ceiling <dispatch_epoch> <now> — true (exit 0) when a spoke has run
# longer than NIGHT_SPOKE_MAX_MINUTES. This is the RELIABLE runtime ceiling: a
# doom-loop or hung spoke that never emits a terminal marker is reaped rather than
# burning the subscription until 07:00 (launch_cutoff only gates NEW launches; the
# --max-budget-usd backstop may not bind on a subscription). Strict ">", and an
# empty/unknown epoch is never over the ceiling (can't measure -> don't reap).
spoke_over_ceiling() {
  local epoch="$1" now="$2"
  # Numeric guard: an empty/corrupt epoch or a non-numeric clock reads as "not
  # over" (can't measure) — never abort the supervisor. A bareword inside $(( ))
  # is fatal under `set -u` on bash 3.2, and it would exit 0 (a silent death).
  case "$epoch" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  [ "$(( (now - epoch) / 60 ))" -gt "$NIGHT_SPOKE_MAX_MINUTES" ]
}

# now_epoch -> current time, overridable via NIGHT_NOW for tests/cron.
now_epoch() {
  printf '%s\n' "${NIGHT_NOW:-$(date +%s)}"
}

# _night_state_dir -> where per-spoke dispatch epochs persist. NIGHT_STATE_DIR
# wins; otherwise a per-repo dir under the git dir (out of the work tree, survives
# a supervisor restart). The epochs must persist so the wall-clock ceiling is
# measured from the real launch, not reset to "now" on every crash/restart.
_night_state_dir() {
  if [ -n "$NIGHT_STATE_DIR" ]; then printf '%s\n' "$NIGHT_STATE_DIR"; return; fi
  local root="${MAIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
  printf '%s\n' "${root}/.git/ai-toolkit-night"
}

# stamp_dispatch_epoch <issue> — record a spoke's launch time (idempotent: a
# re-dispatch after a crash overwrites with the new launch).
stamp_dispatch_epoch() {
  local dir; dir="$(_night_state_dir)"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\n' "$(now_epoch)" > "$dir/dispatch-$1.epoch" 2>/dev/null || true
}

# read_dispatch_epoch <issue> -> the persisted launch epoch, or empty if unknown.
read_dispatch_epoch() {
  local f; f="$(_night_state_dir)/dispatch-$1.epoch"
  [ -f "$f" ] && cat "$f" 2>/dev/null || true
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
  local wt_path="$1" issue="$2" tip marker age kind epoch
  tip="$(git -C "$wt_path" rev-parse HEAD 2>/dev/null)"
  if [ -n "$tip" ]; then
    # A TERMINAL marker at the tip -> done (finished; frees the slot).
    for kind in ready accept blocked; do
      marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)"
      if [ "$marker" = "$tip" ]; then
        printf 'done\n'; return
      fi
    done
    # gate/<issue> at the tip -> parked: the spoke stopped ON PURPOSE awaiting
    # review (PLAN gate). It is not hung — keep its slot and never reap it.
    marker="$(git -C "$wt_path" rev-parse -q --verify "refs/tags/gate/${issue}^{commit}" 2>/dev/null)"
    if [ "$marker" = "$tip" ]; then
      printf 'parked\n'; return
    fi
  fi
  # Over the wall-clock ceiling -> reap, even while transcript-active (a slow loop
  # still burns the subscription). Measured from the persisted dispatch epoch.
  epoch="$(read_dispatch_epoch "$issue")"
  if spoke_over_ceiling "$epoch" "$(now_epoch)"; then
    printf 'reap\n'; return
  fi
  # Idle past NIGHT_IDLE_MINUTES with no marker -> hung -> reap. (A just-spawned
  # spoke with no transcript yet reads as busy so it is never reaped mid-start.)
  age="$(_transcript_idle_seconds "$wt_path")"
  if [ -n "$age" ] && [ "$age" -gt $(( NIGHT_IDLE_MINUTES * 60 )) ]; then
    printf 'reap\n'; return
  fi
  printf 'busy\n'
}

# slot_counts -> "<busy> <done>" across all in-flight worktrees. busy and parked
# spokes occupy a slot (a parked spoke will resume after review — don't backfill
# over it); done spokes are finished work (drop out of tasks_left). A lingering
# 'reap' state (a reap that could not complete) counts as busy, defensively, so the
# supervisor never backfills over a process that might still be alive — once
# reap_overrun_spokes succeeds the spoke reads 'done' instead.
slot_counts() {
  local path issue st busy=0 finished=0
  while IFS="$(printf '\t')" read -r path issue; do
    [ -n "$issue" ] || continue
    st="$(slot_state "$path" "$issue")"
    case "$st" in
      busy|parked|reap) busy=$(( busy + 1 )) ;;
      done) finished=$(( finished + 1 )) ;;
    esac
  done < <(inflight_worktrees)
  printf '%s %s\n' "$busy" "$finished"
}

# --- reaping a hung / overrun spoke ------------------------------------------

# _spoke_ready_bin -> path to spoke-ready.sh, so the hub can emit blocked/<issue>
# on a reaped spoke's behalf. Same resolution order as worktree-new.sh: $SPOKE_READY,
# a sibling, then the synced .ai-toolkit/scripts copy.
_spoke_ready_bin() {
  local main_root="${MAIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}"
  local sr="${SPOKE_READY:-$SCRIPT_DIR/spoke-ready.sh}"
  if [ ! -x "$sr" ] && [ -x "$main_root/.ai-toolkit/scripts/spoke-ready.sh" ]; then
    sr="$main_root/.ai-toolkit/scripts/spoke-ready.sh"
  fi
  printf '%s\n' "$sr"
}

# _kill_spoke_window <issue> — best-effort terminate the spoke's tmux window so a
# reaped spoke stops spending. Windows are named "<issue>-<slug>" (worktree-new.sh).
# No tmux, or no matching window, is a harmless no-op.
_kill_spoke_window() {
  local issue="$1" target name
  command -v tmux >/dev/null 2>&1 || return 0
  tmux list-windows -a -F '#{session_name}:#{window_index} #{window_name}' 2>/dev/null \
  | while read -r target name; do
      case "$name" in
        "${issue}-"*) tmux kill-window -t "$target" 2>/dev/null || true ;;
      esac
    done
}

# reap_spoke <wt_path> <issue> <reason> — terminate a hung/overrun spoke: kill its
# tmux window and emit blocked/<issue> on its behalf (blocked is durability-exempt,
# so it lands over the spoke's incomplete, possibly un-pushed work). The worktree is
# LEFT in place so it doubles as the idempotency record (the still-open issue is not
# re-dispatched) and the morning land-triage can inspect it. All best-effort — a reap
# failure logs and never aborts the supervisor.
reap_spoke() {
  local wt_path="$1" issue="$2" reason="$3" sr
  log "→ reap #$issue: $reason"
  _kill_spoke_window "$issue"
  sr="$(_spoke_ready_bin)"
  if [ -x "$sr" ]; then
    ( cd "$wt_path" && "$sr" --blocked "$issue" -m "$reason" ) \
      || log "reap: could not emit blocked/$issue"
  else
    log "reap: spoke-ready.sh not found — cannot emit blocked/$issue"
  fi
}

# reap_overrun_spokes — one pass over the in-flight set, reaping every spoke whose
# slot_state is 'reap' (over the wall-clock ceiling, or idle with no marker). Runs
# at the START of each tick so slot_counts then reads the reaped spokes as 'done'
# (blocked/<issue> at the tip) and their slots free for backfill.
reap_overrun_spokes() {
  local path issue st reason
  while IFS="$(printf '\t')" read -r path issue; do
    [ -n "$issue" ] || continue
    st="$(slot_state "$path" "$issue")"
    [ "$st" = "reap" ] || continue
    if spoke_over_ceiling "$(read_dispatch_epoch "$issue")" "$(now_epoch)"; then
      reason="time ceiling: ran >${NIGHT_SPOKE_MAX_MINUTES}m without finishing"
    else
      reason="went idle >${NIGHT_IDLE_MINUTES}m with no terminal marker — likely hung"
    fi
    reap_spoke "$path" "$issue" "$reason"
  done < <(inflight_worktrees)
}

# kickoff_for <issue> -> the spoke's first prompt: the NIGHT kickoff (Phase 2).
# The behavioral inversion of the daytime kickoff — judgment gates route to an
# independent adversarial reviewer (gate action = agent-review), and uncertainty
# escalates to PARK, never to "ask" (which hangs the slot until morning) or "guess".
# See solo-cycle's "Gate action — who services a parked gate".
kickoff_for() {
  local n="$1"
  cat <<EOF
You're in a dedicated worktree for issue #$n (Gate: plan), running UNATTENDED in night
mode. Run /source to anchor to issue #$n and read it. Before touching code, break the
issue body into a task ledger (TaskCreate, or TodoWrite on older runtimes) — one todo
per subtask × the solo-cycle steps that apply (ANCHOR/RED/GREEN/REVIEW/PUSH), exactly
one in_progress.

THE NIGHT RULE — park, never ask, never guess. Nobody is awake. Any decision that
needs human judgment you cannot resolve, you STOP CLEANLY and emit a terminal marker;
you never block waiting for input (that hangs your slot until morning) and you never
guess. Keep a STATUS.md heartbeat and /compact when context grows.

Night gate action is AGENT-REVIEW, not a human pause. At each JUDGMENT gate, spawn an
INDEPENDENT adversarial reviewer — a fresh code-review subagent (model: opus), never
yourself grading your own work — prompted to REFUTE, approving only on strong evidence.
The revise loop is bounded to TWO ROUNDS; if it still refuses on the 2nd revision, PARK
(do not loop a third time, do not lower the bar). If a reviewer cannot be spawned
non-interactively, PARK rather than wait.

- PLAN gate (first, before writing code): explore the code and print the full plan
  (files, approach, test strategy, open questions) as a normal visible message — the
  message itself is the plan, not an approval card. Then agent-review it (correct,
  complete, in scope?). Approve → proceed to RED. Refuted after two rounds → park as
  blocked/$n.
- RED / REVIEW (code): agent-review the failing test, then the implementation. The
  code review must confirm the impl did NOT gut the tests to go green (no sys.exit(0),
  no deleted or weakened assertions, no added skip/xfail) — the anti-gutting pre-push
  tripwire enforces this under NIGHT and will refuse such a push.
- DRAFT / acceptance gate (inherently human): build + push, then ALWAYS park as
  accept/$n — the final sign-off is a person's.

Implement following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH), pushing your
own branch each subtask without asking. End with exactly one TERMINAL marker, emitted
through the one allowlistable script (never a hand-written git tag/push chain):

- ready/$n  — all gates auto-passed, machine-verifiable (tests green); emit it on the
              FINAL push (the push script pushes the branch, then emits ready/$n):
              bash .ai-toolkit/scripts/spoke-push.sh --ready $n
- accept/$n — built + pushed + agent-reviewed, final sign-off inherently human:
              bash .ai-toolkit/scripts/spoke-ready.sh --accept $n -m "<what to eyeball>"
- blocked/$n — stuck (ambiguity, a reviewer that refused twice, suspected cheating, or
              a budget/time ceiling): bash .ai-toolkit/scripts/spoke-ready.sh --blocked $n -m "<the blocker>"

Each terminal marker frees your supervisor slot. Do NOT self-land — the hub lands #$n.
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
  # Best-effort in-process budget cap for the night spoke (subscription may not
  # meter it; the supervisor reap is the reliable ceiling). NIGHT_MAX_BUDGET_USD
  # unset -> day-style launch, unchanged. Validate it is a plain number before
  # threading it: WT_AGENT_BUDGET_ARGS is appended UNQUOTED to a shell-eval'd tmux
  # launch, so a non-numeric value must never reach it (it would word-split / inject).
  local budget_args=""
  if [ -n "${NIGHT_MAX_BUDGET_USD:-}" ]; then
    case "$NIGHT_MAX_BUDGET_USD" in
      '' | *[!0-9.]* | *.*.*) log "ignoring non-numeric NIGHT_MAX_BUDGET_USD='$NIGHT_MAX_BUDGET_USD'" ;;
      *) budget_args="--max-budget-usd $NIGHT_MAX_BUDGET_USD" ;;
    esac
  fi
  if [ -n "$budget_args" ]; then
    WT_AGENT_BUDGET_ARGS="$budget_args" \
      "$wt_new" "$issue" --type feature --prompt "$(kickoff_for "$issue")"
  else
    "$wt_new" "$issue" --type feature --prompt "$(kickoff_for "$issue")"
  fi
  local rc=$?
  # Stamp the launch epoch only on a successful spawn, so the wall-clock ceiling
  # is measured from a spoke that actually started.
  [ "$rc" -eq 0 ] && stamp_dispatch_epoch "$issue"
  return "$rc"
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
  # Reap hung / over-ceiling spokes FIRST (before the cutoff check), so a runaway
  # spoke is killed every tick — including the cutoff tick — not only while there
  # is still a launch window. After this pass the reaped spokes read as 'done'.
  reap_overrun_spokes
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
  # busy + parked spokes hold a slot; done spokes (incl. ones reaped this tick)
  # free theirs for backfill.
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
