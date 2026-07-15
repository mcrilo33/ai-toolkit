#!/usr/bin/env bash
# hub-watchdog.sh — tier-2 scripted supervision over the /afk drain (issue #251).
#
# The /afk drain (hub-afk.sh) is tier 1: dispatch → answer → land → reap, autonomously.
# The hub-watchdog is tier 2 — an OS-level loop, co-located with the drain keeper, that on
# each tick detects intervention-worthy conditions the drain FELL SHORT on, takes the safe
# scripted fallback, AND records every intervention as an afk defect signal.
#
# The design principle (the novel part): a *correct* drain never needs the watchdog, so
# EVERY watchdog firing is a bug report against afk, and a run with zero firings means afk is
# autonomous for that workload. The watchdog measures its own obsolescence.
#
# This file is built up across the #251 subtasks:
#   * (this subtask) the OS-level daemon skeleton — a self-looping, pidfile-singleton,
#     source-recycling process with its own heartbeat that cross-checks the drain supervisor;
#   * (later) the 5 detection conditions + scripted interventions, the intervention-ledger,
#     the headless bug-scoper filing/dedup, and the per-run autonomy score.
#
# Liveness / who-watches-the-watchdog: this is an OS-level process (nohup-detached, NOT a
# claude session — ScheduleWakeup/Monitor die with the session), with a heartbeat file the
# drain keeper cross-checks; the recursion bottoms out at a dumb launchd/cron re-arm.
#
# Modelled on hub-otel-watch.sh's proven daemon scaffold: a pidfile singleton (kill -0, never
# pgrep — the locale trap), an `exec … --reexec` self-recycle when a land rewrites its own
# source on disk, and an idle-teardown once the drain is off.
#
# Usage:
#   hub-watchdog.sh --arm         # nohup-detach the daemon (singleton); survives this session
#   hub-watchdog.sh --daemon      # run the self-looping daemon in the foreground
#   hub-watchdog.sh --once        # one tick, no loop (cron / tests)
#   hub-watchdog.sh --status      # report the observed drain + watchdog state
#   hub-watchdog.sh --report      # the morning artifact: interventions, defects, autonomy score
#
# Knobs (env, with defaults):
#   HUB_WATCHDOG_INTERVAL=60      loop tick interval (seconds)
#   HUB_WATCHDOG_IDLE_TICKS=3     consecutive drain-off ticks before the daemon tears down
#   HUB_WATCHDOG_PIDFILE          daemon pidfile   [default: <git-common-dir>/hub-watchdog.pid]
#   HUB_WATCHDOG_LOG              daemon logfile   [default: <git-common-dir>/hub-watchdog.log]
#   HUB_WATCHDOG_HEARTBEAT        heartbeat file   [default: <git-common-dir>/.hub-watchdog-heartbeat]
#   HUB_WATCHDOG_LEDGER           intervention-ledger [default: <drain-state-dir>/intervention-ledger.jsonl]
#   HUB_WATCHDOG_PARK_CEILING=600    a waiting spoke unanswered this long ⇒ the drain fell short
#   HUB_WATCHDOG_IDLE_CEILING=3600   a dead/idle spoke unrevived this long ⇒ the reaper missed it
#   HUB_WATCHDOG_LAND_CEILING=900    a ready-at-tip branch un-landed this long ⇒ auto-land skipped
#   HUB_WATCHDOG_FILE=1          auto-file afk-defects via the headless bug-scoper (0 disables)
#   HUB_WATCHDOG_COARM=1         (read by hub-afk.sh) co-arm this watchdog alongside the drain
#   AFK_STATE / AFK_HEARTBEAT     the drain's state + heartbeat files it cross-checks (must
#                                 match hub-afk.sh's contract — same <git-common-dir> defaults)
#   *_CMD seams (tests / overrides): HUB_WATCHDOG_ANSWER_CMD / _REVIVE_CMD / _RECONCILE_CMD /
#                                 _LANDMARK_CMD / _REARM_CMD (interventions); _CLASSIFY_CMD /
#                                 _SCOPER_CMD / _DEDUP_CMD / _LABEL_CMD (the instrument)
#   HUB_WATCHDOG_WT_LIB           override the sourced worktree-lib.sh (tests)
set -uo pipefail

HUB_WATCHDOG_SCRIPT_DIR="${HUB_WATCHDOG_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# Absolute path to THIS script, so a re-exec / nohup-arm resolves regardless of cwd.
_WD_SELF="$HUB_WATCHDOG_SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

# --- source worktree-lib.sh (wt_source_hash, wt_realpath) + hub-inject.sh ------
# Same dual-layout ladder as the hub siblings: the checkout (scripts/ four levels up) and a
# synced .ai-toolkit/scripts/ target (co-located). HUB_WATCHDOG_WT_LIB / AFK_WT_LIB win for tests.
# Every loop below captures the resolved path BEFORE `. "$cand"` and uses a WD-prefixed loop
# variable: a sourced lib's own preamble may run an `unset _cand` (gate-broker unconditionally,
# hub-inject when it also sources worktree-lib) that would otherwise clobber a shared-name loop
# var mid-iteration and hit an unbound-variable error under set -u.
_WD_TOPLEVEL="${_WD_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
_WD_RESOLVED_WT_LIB=""
for _wdwl in \
  "${HUB_WATCHDOG_WT_LIB:-}" \
  "${AFK_WT_LIB:-}" \
  "$HUB_WATCHDOG_SCRIPT_DIR/worktree-lib.sh" \
  "$HUB_WATCHDOG_SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_wdwl" ] && [ -f "$_wdwl" ]; then _WD_RESOLVED_WT_LIB="$_wdwl"; . "$_wdwl"; break; fi
done
unset _wdwl 2>/dev/null || true
# hub-inject.sh — the shared injector the watchdog's answer/revive interventions use (a
# co-located sibling). Sourced best-effort; a detector that needs it existence-checks first.
_WD_RESOLVED_INJECT=""
for _wdinj in "${AFK_HUB_INJECT:-}" "$HUB_WATCHDOG_SCRIPT_DIR/hub-inject.sh"; do
  if [ -n "$_wdinj" ] && [ -f "$_wdinj" ]; then _WD_RESOLVED_INJECT="$_wdinj"; . "$_wdinj"; break; fi
done
unset _wdinj 2>/dev/null || true
unset _cand
# gate-broker.sh — the drain's state-reader API (inflight_worktrees, slot_state, the
# answer-attempt/progress epochs, _afk_state_dir) the detectors cross-check against the SAME
# truth the drain reads, so the two tiers can never disagree on what a spoke's state is. A
# co-located sibling; it re-pulls worktree-lib + hub-inject idempotently. Sourced best-effort:
# a detector existence-checks before use, so a standalone watchdog still runs (detectors no-op).
# AFK_GATE_BROKER wins for tests.
_WD_RESOLVED_BROKER=""
# A distinct loop var (not _cand): gate-broker.sh's preamble runs an unconditional
# `unset _cand` that would clobber a shared-name loop variable mid-iteration under set -u.
# Capture the resolved path BEFORE sourcing for the same reason.
for _wdgb in \
  "${AFK_GATE_BROKER:-}" \
  "$HUB_WATCHDOG_SCRIPT_DIR/gate-broker.sh" \
  "$HUB_WATCHDOG_SCRIPT_DIR/../../../../scripts/gate-broker.sh"; do
  if [ -n "$_wdgb" ] && [ -f "$_wdgb" ]; then _WD_RESOLVED_BROKER="$_wdgb"; . "$_wdgb"; break; fi
done
unset _wdgb 2>/dev/null || true

# Guarded log fallback (hub-inject/gate-broker may already provide one; same stderr contract).
declare -F log >/dev/null 2>&1 || log() { printf '%s\n' "$*" >&2; }

# The daemon's own source bundle: this script + the libs it sources. Stamped at daemon start
# and re-checked each tick so a land of the watchdog's own code re-execs it live (#251/#190).
_WD_SOURCE_FILES=("$_WD_SELF" "$_WD_RESOLVED_BROKER" "$_WD_RESOLVED_INJECT" "$_WD_RESOLVED_WT_LIB")

# --- paths --------------------------------------------------------------------
# The hub's git common dir — shared across worktrees, per-repo — where the daemon's pidfile,
# logfile and heartbeat default. Falls back to /tmp when not in a repo (never fails the arm).
_wd_common_dir() {
  local d
  d="$(git rev-parse --git-common-dir 2>/dev/null)" || { printf '%s\n' /tmp; return; }
  case "$d" in /*) ;; *) d="$(pwd)/$d" ;; esac
  printf '%s\n' "$d"
}
_wd_pidfile()   { printf '%s\n' "${HUB_WATCHDOG_PIDFILE:-$(_wd_common_dir)/hub-watchdog.pid}"; }
_wd_logfile()   { printf '%s\n' "${HUB_WATCHDOG_LOG:-$(_wd_common_dir)/hub-watchdog.log}"; }
_wd_heartbeat_file() { printf '%s\n' "${HUB_WATCHDOG_HEARTBEAT:-$(_wd_common_dir)/.hub-watchdog-heartbeat}"; }
# The DRAIN's state + heartbeat files — the same contract hub-afk.sh writes, so the cross-check
# reads live truth. AFK_STATE / AFK_HEARTBEAT override exactly as they do in hub-afk.sh.
_wd_afk_state_file()     { printf '%s\n' "${AFK_STATE:-$(_wd_common_dir)/.afk-state}"; }
_wd_afk_heartbeat_file() { printf '%s\n' "${AFK_HEARTBEAT:-$(_wd_common_dir)/.afk-heartbeat}"; }

# --- helpers ------------------------------------------------------------------
_wd_now() { printf '%s\n' "${AFK_NOW:-$(date +%s)}"; }
# Timestamped log line (LC_ALL=C: locale-formatted dates have burned this repo before).
_wd_log() { printf 'hub-watchdog: [%s] %s\n' "$(LC_ALL=C date '+%F %T')" "$*"; }

# _wd_pid_alive <pid> -> true when <pid> is a live process. Empty / non-numeric is never alive
# (guards `kill` against a bareword and a truncated partial heartbeat). kill -0, NOT pgrep.
_wd_pid_alive() {
  case "${1:-}" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$1" 2>/dev/null
}

# _wd_write_heartbeat -> stamp "<pid> <now>" so the drain keeper (and a who-watches re-arm) can
# tell a LIVE watchdog from a stale pidfile. Best-effort; never aborts a tick.
_wd_write_heartbeat() {
  printf '%s %s\n' "$$" "$(_wd_now)" > "$(_wd_heartbeat_file)" 2>/dev/null || true
}

# _wd_drain_state -> off | live | stale: the ground truth of the tier-1 drain, cross-checking
# the drain's .afk-state against its heartbeat pid (mirrors hub-afk.sh's afk_supervisor_state
# so the two tiers agree):
#   off   — no drain window armed (.afk-state empty/absent) → nothing to watch.
#   live  — a window is armed AND the drain heartbeat pid is a live process.
#   stale — a window is armed but the heartbeat pid is gone / missing — the drain crashed and
#           the state file is lying (the tier-2 "supervisor died" trigger, subtask 3).
_wd_drain_state() {
  local statef hbf hb pid
  statef="$(_wd_afk_state_file)"
  [ -s "$statef" ] || { printf 'off\n'; return; }
  hbf="$(_wd_afk_heartbeat_file)"
  hb="$([ -f "$hbf" ] && head -n1 "$hbf" 2>/dev/null || true)"
  pid="${hb%% *}"
  if _wd_pid_alive "$pid"; then printf 'live\n'; else printf 'stale\n'; fi
}

# _wd_source_hash -> the current stamp of the daemon's own source bundle (delegates to
# worktree-lib's wt_source_hash). Split out so the self-recycle decision is overridable in tests.
_wd_source_hash() {
  command -v wt_source_hash >/dev/null 2>&1 || { printf '\n'; return; }
  wt_source_hash "${_WD_SOURCE_FILES[@]}"
}

# _wd_reexec -> replace this daemon with a fresh copy running the on-disk (post-land) code.
# `exec` preserves the pid, so the pidfile keeps naming a live process and no second daemon is
# armed; the `--reexec` flag (passed ONLY here) tells the fresh _wd_daemon to reclaim its own
# pidfile rather than refuse as "already running". First `bash -n`-checks the whole bundle: a
# DEAD watchdog is worse than a stale one, so if a land shipped parse-broken code we keep
# running the current (working) code and return — the loop retries next tick.
_wd_reexec() {
  local f
  for f in "${_WD_SOURCE_FILES[@]}"; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    if ! bash -n "$f" 2>/dev/null; then
      _wd_log "on-disk source changed but $f fails to parse — NOT re-exec'ing; keeping current code"
      return 0
    fi
  done
  _wd_log "source changed on disk (a land) — re-exec'ing into fresh code"
  exec bash "$_WD_SELF" --daemon --reexec
}

# --- detection conditions + scripted interventions (issue #251) ---------------
# Each detector fires ONLY when the drain already had its chance and the condition persists
# past a grace margin beyond the drain's OWN threshold — so a CORRECT drain never trips the
# watchdog (every firing is an afk defect). On a firing the dispatcher records it (_wd_fire →
# the intervention-ledger; subtask 4 adds classify + bug-scoper) AND takes the safe scripted
# intervention behind a HUB_WATCHDOG_*_CMD seam (so each is unit-testable without a live
# tmux/gh/claude). All best-effort: a detector/intervention error never aborts a tick.
: "${HUB_WATCHDOG_PARK_CEILING:=600}"   # a waiting spoke unanswered this long ⇒ drain fell short
: "${HUB_WATCHDOG_IDLE_CEILING:=3600}"  # a dead/idle spoke unrevived this long ⇒ reaper missed it
: "${HUB_WATCHDOG_LAND_CEILING:=900}"   # a ready-at-tip branch un-landed this long ⇒ auto-land skipped

# _wd_epoch_stale <epoch> <now> <ceiling> -> true when now-epoch > ceiling. Empty/non-numeric
# reads as NOT stale (can't measure → never fire), guarding set -u arithmetic against a bareword.
_wd_epoch_stale() {
  local epoch="$1" now="$2" ceiling="$3"
  case "$epoch" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  [ "$(( now - epoch ))" -gt "$ceiling" ]
}

# _wd_tag_at_tip <wt> <kind> <issue> -> true when <kind>/<issue> points exactly at HEAD.
_wd_tag_at_tip() {
  local wt="$1" kind="$2" issue="$3" tip tag
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/${kind}/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] && [ "$tag" = "$tip" ]
}

# _wd_blocked_stale <wt> <issue> -> true when blocked/<issue> is a STRICT ancestor of the tip:
# the spoke committed on top, so the drain's reconcile_markers should have cleared it already.
_wd_blocked_stale() {
  local wt="$1" issue="$2" tag tip
  tag="$(git -C "$wt" rev-parse -q --verify "refs/tags/blocked/${issue}^{commit}" 2>/dev/null)" || return 1
  [ -n "$tag" ] || return 1
  tip="$(git -C "$wt" rev-parse -q --verify HEAD 2>/dev/null)" || return 1
  [ "$tag" != "$tip" ] || return 1
  git -C "$wt" merge-base --is-ancestor "$tag" "$tip" 2>/dev/null
}

# _wd_issue_open <issue> -> best-effort "is this GitHub issue still open?" A closed issue was
# landed (not skipped), so condition 4 must not fire on it. gh unavailable / a failed query
# reads as OPEN (fire) — the drain-skip signal already gated it; HUB_WATCHDOG_ISSUE_STATE_CMD
# overrides for tests (echo the state: open|closed).
_wd_issue_open() {
  local issue="$1" state
  if [ -n "${HUB_WATCHDOG_ISSUE_STATE_CMD:-}" ]; then
    state="$(bash -c "$HUB_WATCHDOG_ISSUE_STATE_CMD" hub-watchdog "$issue" 2>/dev/null)"
  else
    command -v gh >/dev/null 2>&1 || return 0
    state="$(gh issue view "$issue" --json state -q .state 2>/dev/null)"
  fi
  case "$state" in [Cc]losed | CLOSED) return 1 ;; *) return 0 ;; esac
}

# --- the 5 detectors (predicates over the drain's own state readers) -----------
# Read-only EXCEPT condition 1, which notes the current park episode as it measures (#283) — the
# same way slot_state stamps the epochs it reads. So a detector is safe to run on a tick, but NOT
# speculatively (a dry-run / status probe would re-stamp the park-onset clock it reads).
# _wd_park_lane <wt> <issue> -> permission | gate | question | unknown: WHOSE lane the pending park
# belongs to (#283). Probes STRUCTURALLY, in _broker_park_signature's own precedence order, rather
# than parsing that signature: a gate-tagged park whose plan artifact is unreadable hashes to EMPTY
# (`gate:` -> ''), which would read as `unknown` here and — under the "unknown never fires" rule
# below — silence #265's never-attempted strand. _gate_parked is a tag-at-tip check, immune to
# artifact readability, so the strand keeps firing. Permission wins over a gate tag still at the
# tip, exactly as the broker orders it.
_wd_park_lane() {
  local wt="$1" issue="$2"
  if command -v _permission_pending >/dev/null 2>&1 && _permission_pending "$wt"; then
    printf 'permission\n'; return
  fi
  if command -v _gate_parked >/dev/null 2>&1 && _gate_parked "$wt" "$issue"; then
    printf 'gate\n'; return
  fi
  if command -v extract_pending_question >/dev/null 2>&1 &&
     [ -n "$(extract_pending_question "$wt" 2>/dev/null)" ]; then
    printf 'question\n'; return
  fi
  printf 'unknown\n'
}

# _wd_park_is_answer_lane <wt> <issue> -> true for a park the ANSWER lane owns (a plan gate or a
# question). A permission dialog is the BROKER's lane with its own timers and its own re-answer
# ceiling; the watchdog must not answer it (#271) and must not measure it against the answer
# ceiling. `unknown` is NOT the answer lane: slot_state's `waiting` is derived from these same
# three probes, so waiting-with-no-lane means the park resolved between the two calls — a race,
# not a strand. The deliberate trade-off (journaled per AC3): a churning permission dialog is now
# unbounded BY THE WATCHDOG. That is on purpose — it is the broker's lane, and its re-answer
# ceiling escalating to blocked/<issue> is what bounds it. A second ceiling here would re-create
# the lane confusion this fix exists to remove.
_wd_park_is_answer_lane() {
  case "$(_wd_park_lane "$1" "$2")" in gate | question) return 0 ;; esac
  return 1
}

# _wd_park_base <wt> <issue> -> the epoch the park-unanswered ceiling measures FROM:
# max(current episode onset, answer delivery). An answer delivered BEFORE the current park began
# cannot count against it (#283) — that was the #276 false-fire: one answered plan gate, then ten
# productive minutes, and the ceiling still measured from that one delivery. note_park_episode
# re-stamps the onset when the pending park's context changes, so the onset names the episode
# actually pending; a delivery INSIDE it is the more recent word and wins. Empty when neither is
# measurable (the detector then cannot fire — same contract as _afk_ceiling_epoch).
_wd_park_base() {
  local wt="$1" issue="$2" onset attempt
  if command -v note_park_episode >/dev/null 2>&1; then
    onset="$(note_park_episode "$wt" "$issue" 2>/dev/null)"
  else
    onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  fi
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) onset=0 ;; esac
  case "$attempt" in '' | *[!0-9]*) attempt=0 ;; esac
  [ "$attempt" -gt "$onset" ] && onset="$attempt"
  [ "$onset" -gt 0 ] && printf '%s\n' "$onset"
  return 0
}

# _wd_last_decision_ts <issue> -> the ts of the drain's most recent decision-journal record for
# this issue, or empty. The journal is the drain's own "I acted on this spoke" evidence (#241),
# keyed by issue and already written per broker decision — no new plumbing needed. Append-only and
# chronological, so the LAST match is the newest.
# CONTRACT: this parses the record's field ORDER (ts first) written by _broker_journal_line
# (gate-broker-answerer.sh) — the journal's sole writer. A reorder there would make this return
# empty for every issue and silently disable the servicing suppression, so the pairing is pinned
# end-to-end by test_last_decision_ts_reads_a_record_written_by_the_real_journal_writer.
_wd_last_decision_ts() {
  local issue="$1" f
  command -v _broker_journal_file >/dev/null 2>&1 || return 0
  f="$(_broker_journal_file)"
  [ -f "$f" ] || return 0
  grep -F "\"issue\":\"$issue\"" "$f" 2>/dev/null | sed -n 's/^{"ts":\([0-9][0-9]*\).*/\1/p' | tail -n1
}

# _wd_drain_touched_recently <issue> <now> -> true when the drain acted on this issue within the
# ceiling window: a broker decision for it, or a progress-epoch advance. This is what separates
# "being handled" from "abandoned" (#283) — on #276 the broker approved a tier-3 command 12s
# BEFORE the firing and three more within 8 minutes after it.
# Measured as RECENCY, not "after the base", on purpose: the answerer journals its own delivery a
# beat AFTER stamp_answer_attempt, so an "after the base" test would read the delivery's own record
# as servicing and a genuinely stranded park could never fire (AC2).
_wd_drain_touched_recently() {
  local issue="$1" now="$2" cand newest=""
  for cand in "$(_wd_last_decision_ts "$issue")" "$(read_progress_epoch "$issue" 2>/dev/null)"; do
    case "$cand" in '' | *[!0-9]*) continue ;; esac
    if [ -z "$newest" ] || [ "$cand" -gt "$newest" ]; then newest="$cand"; fi
  done
  [ -n "$newest" ] || return 1
  ! _wd_epoch_stale "$newest" "$now" "$HUB_WATCHDOG_PARK_CEILING"
}

# Condition 1: a parked spoke answer_pass left unanswered past the grace margin. Three gates, in
# order: the park must be the ANSWER lane's (#283/#271), the CURRENT episode's base must be older
# than the ceiling (#283/#265 — never zero, and never a delivery from a long-resolved park), and
# the drain must not be visibly servicing the spoke. Only then did the drain genuinely fall short.
_wd_detect_park_unanswered() {
  local wt="$1" issue="$2" now="$3"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  _wd_epoch_stale "$(_wd_park_base "$wt" "$issue")" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  ! _wd_drain_touched_recently "$issue" "$now"
}

# _wd_park_unanswered_reason <wt> <issue> <now> -> the MEASURED firing reason (#265/#283): which
# branch fired (never-attempted vs stale-attempt), the actual age, AND the base it was measured
# from — which epoch, the current park episode, the lane — so a future false positive is
# diagnosable from the ledger line alone (#283 AC5) instead of re-deriving the timeline from four
# state files. Reads the epochs on demand (no cross-pass global — the #241 leak trap); the episode
# onset was already noted by the detector on this tick, so this only reads it back.
_wd_park_unanswered_reason() {
  local wt="$1" issue="$2" now="$3" attempt onset base sig=""   # sig="": the guard below may skip it (set -u)
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  command -v read_park_sig >/dev/null 2>&1 && sig="$(read_park_sig "$issue" 2>/dev/null)"
  sig="${sig#*$'\t'}"                          # drop the tip half of the "<tip>\t<sig>" record
  case "$attempt" in '' | *[!0-9]*) attempt="" ;; esac
  case "$onset" in '' | *[!0-9]*) onset="" ;; esac
  # The delivery counts only when it falls INSIDE the current episode — the same rule _wd_park_base
  # measures by, so the reported branch can never disagree with the epoch that actually fired.
  if [ -n "$attempt" ] && { [ -z "$onset" ] || [ "$attempt" -ge "$onset" ]; }; then
    base="answer-attempt@$attempt"
    printf 'park-unanswered (stale-attempt): last answer delivery %s ago' \
      "$(_wd_age_seconds "$attempt" "$now")"
  else
    base="park-onset@${onset:-?}"
    printf 'park-unanswered (never-attempted): parked %s with no answer delivered' \
      "$(_wd_age_seconds "$onset" "$now")"
  fi
  printf ' (ceiling %ss; base=%s, episode=%s@%s, lane=%s)' \
    "$HUB_WATCHDOG_PARK_CEILING" "$base" "${sig:0:8}" "${onset:-?}" "$(_wd_park_lane "$wt" "$issue")"
}

# _wd_age_seconds <epoch> <now> -> "<n>s" elapsed, or "an unknown time" when unmeasurable
# (empty/non-numeric epoch or now) — guards set -u arithmetic against a bareword.
_wd_age_seconds() {
  local epoch="$1" now="$2"
  case "$epoch" in '' | *[!0-9]*) printf 'an unknown time'; return ;; esac
  case "$now" in '' | *[!0-9]*) printf 'an unknown time'; return ;; esac
  printf '%ss' "$(( now - epoch ))"
}

# Condition 2: a dead/crashed pane recover_dead_panes/reap_pass never revived, past the ceiling.
_wd_detect_dead_idle() {
  local wt="$1" issue="$2" now="$3"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ -z "$(_spoke_pane_target "$wt" 2>/dev/null)" ] || return 1   # a live pane is the reaper's job
  [ "$(slot_state "$wt" "$issue")" = "done" ] && return 1        # terminal ⇒ not a hang
  _wd_epoch_stale "$(read_progress_epoch "$issue" 2>/dev/null)" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
}

# Condition 3: a stale blocked/ marker reconcile_markers should have cleared.
_wd_detect_stale_marker() { _wd_blocked_stale "$1" "$2"; }

# _wd_land_lane_servicing <issue> -> true when the drain's LAND lane has a FRESH armed retry
# (warned-state-<issue>-land next-due in the FUTURE): the drain is actively re-attempting the land
# on its backoff, so condition 4 must defer — the analogue of the answer-lane servicing defer
# (_wd_supervisor_servicing) for the LAND lane (#285 AC5). Reads the same backoff record auto_land
# arms (_afk_warned_next, in scope via gate-broker); absent/elapsed ⇒ not servicing (fall through).
_wd_land_lane_servicing() {
  local issue="$1" now next
  command -v _afk_warned_next >/dev/null 2>&1 || return 1
  next="$(_afk_warned_next "$issue" land 2>/dev/null)"
  case "$next" in '' | *[!0-9]*) return 1 ;; esac
  now="$(_wd_now)"
  [ "$next" -gt "$now" ]
}

# _wd_land_base_ref <wt> -> the ref a land would merge the branch INTO: the local base branch
# (refs/heads/<base>) when present — the same tip worktree-land merges onto — else origin/<base>.
# Resolves <base> via the canonical wt_base_branch (in scope via worktree-lib); defaults to main.
_wd_land_base_ref() {
  local wt="$1" base=""
  command -v wt_base_branch >/dev/null 2>&1 && base="$(wt_base_branch "$wt" 2>/dev/null || true)"
  [ -n "$base" ] || base=main
  if git -C "$wt" show-ref --verify --quiet "refs/heads/$base" 2>/dev/null; then printf '%s\n' "$base"; return; fi
  if git -C "$wt" show-ref --verify --quiet "refs/remotes/origin/$base" 2>/dev/null; then printf 'origin/%s\n' "$base"; return; fi
  printf '%s\n' "$base"
}

# _wd_land_conflicts <wt> -> the space-separated conflicting file(s) when the checked-out branch
# does NOT merge cleanly into the base, else empty. Uses `git merge-tree --write-tree --name-only`
# (git >= 2.38): rc 0 = mergeable (empty), rc 1 = conflict (line 1 is the tree OID; the conflicted
# paths follow up to the first blank line), any other rc = probe unavailable/errored ⇒ empty so a
# tooling gap never mislabels a branch as conflicted (it stays on the mergeable path).
_wd_land_conflicts() {
  local wt="$1" branch base out rc
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  base="$(_wd_land_base_ref "$wt")"
  [ -n "$branch" ] && [ -n "$base" ] || return 0
  out="$(git -C "$wt" merge-tree --write-tree --name-only "$base" "$branch" 2>/dev/null)"; rc=$?
  case "$rc" in
    0) return 0 ;;   # mergeable
    1) ;;            # conflict — extract the file list below
    *) return 0 ;;   # probe error (old git / bad ref) — never claim a conflict
  esac
  printf '%s\n' "$out" | awk 'NR==1{next} /^$/{exit} {printf "%s ", $0}'
}

# Condition 4: a mergeable (ready-at-tip) branch auto_land terminal-skipped. NOT blocked-at-tip
# (a deliberate skip is never a false-skip), issue still open, un-landed past the ceiling.
# Staleness is measured from the DONE epoch — stamped when slot_state first reads the spoke
# `done` — not the progress epoch, which is stamped only on tip advances and pre-ages during a
# pre-ready park (#263): a parked-then-ready spoke would otherwise trip an instant false-skip.
# slot_state itself stamps the done epoch on that first done tick, so the ceiling starts here.
# The LAND-lane servicing defer (#285 AC5) and the mergeability probe both live in the DISPATCHER,
# NOT here: a servicing tick must neither fire NOR clear the fire-dedup marker (clearing mid-service
# would let one persistent conflict re-fire and double-count in the ledger, #263), so it is gated
# BEFORE this detector — mirroring how the answer lane defers the INTERVENTION, not the detector.
_wd_detect_mergeable_skipped() {
  local wt="$1" issue="$2" now="$3"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "done" ] || return 1
  _wd_tag_at_tip "$wt" blocked "$issue" && return 1              # deliberately blocked → not a skip
  _wd_epoch_stale "$(read_done_epoch "$issue" 2>/dev/null)" "$now" "$HUB_WATCHDOG_LAND_CEILING" || return 1
  _wd_issue_open "$issue"                                        # a closed issue was landed, not skipped
}

# Condition 5: the drain supervisor crashed (armed state, dead heartbeat).
_wd_detect_supervisor_dead() { [ "$(_wd_drain_state)" = "stale" ]; }

# --- the 5 scripted interventions (each behind a HUB_WATCHDOG_*_CMD seam) ------
# The seam receives the worktree + issue as positional args ($1 $2 after the argv0 sentinel).
# _wd_last_action -> the drain supervisor's most-recent-action label (hub-afk.sh's #202 record,
# e.g. "answer #5"), read directly from the shared state dir so the watchdog needs no hub-afk.sh
# source. Honors the AFK_LAST_ACTION override exactly as hub-afk.sh does (tests point it at a
# scratch file). Empty when unreadable / the state dir reader is absent (standalone watchdog).
_wd_last_action() {
  local f="${AFK_LAST_ACTION:-}"
  if [ -z "$f" ]; then
    command -v _afk_state_dir >/dev/null 2>&1 || return 0
    f="$(_afk_state_dir)/last-action"
  fi
  [ -f "$f" ] && head -n1 "$f" 2>/dev/null || true
}

# _wd_supervisor_servicing <issue> -> true when the tier-1 drain is CURRENTLY working this
# park, so the watchdog must NOT run a second decide_and_act and race the in-flight answerer
# (#265 AC4; the #89 stale-answer/strand hazard). Two positive signals: a FRESH answer-delivery
# stamp (the supervisor delivered within the ceiling), or — for the never-attempted window where
# no stamp exists yet — a LIVE drain whose LAST ACTION names this issue (any drain pass touching
# it: an `answer` mid-reasoning pre-delivery, or a revive/nudge/land in flight). Deferring on any
# such action is deliberately conservative — never race the drain while it is on this issue. The
# defer can't wedge: the firing is still ledgered, and the answer lane's re-answer ceiling
# (-> blocked/<issue>) plus the supervisor-dead detector bound a genuinely stuck drain. Either ⇒ defer.
_wd_supervisor_servicing() {
  local issue="$1" attempt
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  case "$attempt" in
    '' | *[!0-9]*) ;;   # no delivery yet — fall through to the heartbeat + last-action check
    *) _wd_epoch_stale "$attempt" "$(_wd_now)" "$HUB_WATCHDOG_PARK_CEILING" || return 0 ;;
  esac
  [ "$(_wd_drain_state)" = "live" ] || return 1
  case "$(_wd_last_action)" in *"#$issue") return 0 ;; esac
  return 1
}

_wd_intervene_answer() {   # route to the reasoner/answer lane directly
  local wt="$1" issue="$2"
  # #283 AC4: NEVER inject into a permission dialog — that is the BROKER's lane, with its own
  # classifier, timers and re-answer ceiling. Answering one is how the watchdog ends up servicing a
  # park it does not own (#271) and interrupting a live tool call (#89): on #276 this armed
  # re-answers that churned against a dialog the broker was already clearing. The detector no
  # longer fires on a permission park, so this is the second lock — it guards any direct caller.
  # Gated on `permission` specifically, not on "is answer lane": an UNKNOWN lane keeps the historic
  # behaviour (the detector, not this seam, is where the race-vs-strand call is made).
  if [ "$(_wd_park_lane "$wt" "$issue")" = "permission" ]; then
    _wd_log "deferring answer intervention on #$issue — the park is a permission dialog (broker's lane)"
    return 0
  fi
  # #265 AC4: defer when the supervisor is mid-service on this same park — a second answer here
  # duplicate-injects and races the in-flight answerer (the #89 hazard) + wastes a costly run.
  if _wd_supervisor_servicing "$issue"; then
    _wd_log "deferring answer intervention on #$issue — supervisor is mid-service on this park"
    return 0
  fi
  if [ -n "${HUB_WATCHDOG_ANSWER_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_ANSWER_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  command -v decide_and_act >/dev/null 2>&1 && decide_and_act "$wt" "$issue" >/dev/null 2>&1 || true
}
_wd_intervene_revive() {   # claude --continue revive in the worktree
  local wt="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_REVIVE_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_REVIVE_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  command -v claude >/dev/null 2>&1 && ( cd "$wt" 2>/dev/null && nohup claude --continue >/dev/null 2>&1 & ) || true
}
_wd_intervene_reconcile() {  # clear the stale blocked/ marker (local + remote)
  local wt="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_RECONCILE_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_RECONCILE_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  git -C "$wt" tag -d "blocked/$issue" >/dev/null 2>&1 || true
  git -C "$wt" push origin ":refs/tags/blocked/$issue" >/dev/null 2>&1 || true
}
# ESCALATE-ONLY (#251 final ruling): the watchdog NEVER lands — a tier-2 loop must not ship to
# main ("hub lands, never self-land"). It raises a human land marker (a needs-human-land/<issue>
# tag) so a person lands it; the defect is filed either way (_wd_fire).
_wd_intervene_landmark() {
  local wt="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_LANDMARK_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_LANDMARK_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true; return 0; fi
  git -C "$wt" tag -f "needs-human-land/$issue" >/dev/null 2>&1 || true
}

# _wd_landmark_repo -> a git checkout whose shared ref store holds the needs-human-land/<issue>
# tags. The per-spoke worktree is gone after a land, so the sweep reads the hub toplevel
# (captured at startup); HUB_WATCHDOG_LANDMARK_REPO overrides (tests point it at a scratch repo).
_wd_landmark_repo() { printf '%s\n' "${HUB_WATCHDOG_LANDMARK_REPO:-$_WD_TOPLEVEL}"; }

# _wd_clear_landed_landmarks -> self-clear the escalation (#263). A needs-human-land/<issue>
# raised by condition 4 dangles after the drain lands the branch on its very next tick — neither
# auto_land's success path nor reconcile_markers removes it, so a human is pointed at
# already-shipped work. Each tick, drop every needs-human-land/<issue> whose issue is CLOSED (a
# closed issue was landed): delete the tag local + remote (mirrors _wd_intervene_reconcile). A
# still-open issue keeps its tag — a human genuinely still owes that land. Best-effort throughout.
_wd_clear_landed_landmarks() {
  local repo tag issue
  repo="$(_wd_landmark_repo)"
  [ -n "$repo" ] || return 0
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    issue="${tag#needs-human-land/}"
    case "$issue" in '' | *[!0-9]*) continue ;; esac
    _wd_issue_open "$issue" && continue          # still open ⇒ a human still owes the land
    git -C "$repo" tag -d "$tag" >/dev/null 2>&1 || true
    git -C "$repo" push origin ":refs/tags/$tag" >/dev/null 2>&1 || true
    # The landed issue is gone from the in-flight loop, so the dispatcher's else-clear never runs
    # for it — clear its condition-4 firing markers here so the autonomy score re-arms (#263/#285).
    _wd_clear_fired auto-land-skipped "$issue"
    _wd_clear_fired conflicted-land "$issue"
    _wd_log "cleared resolved landmark $tag (issue #$issue closed/landed)"
  done < <(git -C "$repo" tag -l 'needs-human-land/*' 2>/dev/null)
}
_wd_intervene_rearm() {   # re-arm the crashed drain (self-update aware via hub-afk --reconcile)
  if [ -n "${HUB_WATCHDOG_REARM_CMD:-}" ]; then bash -c "$HUB_WATCHDOG_REARM_CMD" hub-watchdog >/dev/null 2>&1 || true; return 0; fi
  local afk=""
  command -v _afk_find_script >/dev/null 2>&1 && afk="$(_afk_find_script "${HUB_WATCHDOG_AFK_BIN:-}" hub-afk.sh || true)"
  [ -n "$afk" ] && bash "$afk" --reconcile >/dev/null 2>&1 || true
}

# --- the intervention-ledger + firing hook ------------------------------------
# _wd_ledger_file -> the per-run intervention-ledger (one JSONL firing per line). Under the
# drain's state dir so hub-status / the morning report find it; HUB_WATCHDOG_LEDGER overrides.
_wd_ledger_file() {
  if [ -n "${HUB_WATCHDOG_LEDGER:-}" ]; then printf '%s\n' "$HUB_WATCHDOG_LEDGER"; return; fi
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/intervention-ledger.jsonl"
}

# _wd_json_escape <str> -> minimal JSON string-body escape (defer to the broker's when present so
# the ledger and the #241 decision-journal never diverge on escaping).
_wd_json_escape() {
  if command -v _broker_json_escape >/dev/null 2>&1; then _broker_json_escape "$1"; return; fi
  local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"
}

# --- classify + file the defect (the instrument, issue #251) ------------------
# Every firing is classified {afk-defect | novel-decision} so a genuine, first-of-its-kind
# human decision (the reasoner CORRECTLY escalating a real judgment call) is not mis-filed as an
# afk bug. The 5 conditions are drain shortfalls ⇒ afk-defect by default; a park-unanswered whose
# spoke the reasoner deliberately escalated (a blocked/ marker exists) is a novel-decision. The
# HUB_WATCHDOG_CLASSIFY_CMD seam overrides the whole decision (echo the class).
: "${HUB_WATCHDOG_AFK_DEFECT_LABEL:=afk-defect}"

_wd_classify() {
  local condition="$1" issue="$2"
  if [ -n "${HUB_WATCHDOG_CLASSIFY_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_CLASSIFY_CMD" hub-watchdog "$condition" "$issue" 2>/dev/null; return
  fi
  case "$condition" in
    park-unanswered)
      # A blocked/ record means the reasoner made a real human-call escalation — not a bug.
      if command -v _afk_blocked_record >/dev/null 2>&1 && [ -f "$(_afk_blocked_record "$issue" 2>/dev/null)" ]; then
        printf 'novel-decision\n'; return
      fi ;;
  esac
  printf 'afk-defect\n'
}

# _wd_filed_marker <condition> <issue> -> the per-run dedup marker (one file per condition+issue)
# so a persistent condition files ONE defect, not one per tick.
_wd_filed_marker() {
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/wd-filed-$1-$2"
}

# _wd_fired_marker <condition> <issue> -> the per-run FIRING-dedup marker. NOTE: distinct from
# _wd_filed_marker above (near-homonym) — that one dedups the defect FILING (`wd-filed-`), this
# one dedups the ledger FIRING (`wd-fire-dedup-`, a deliberately non-colliding stem so no glob
# conflates the two families). One firing per condition+issue while unresolved: _wd_fire skips
# the ledger append when this exists, so a persistent condition (or an in-flight land racing
# condition 4) logs ONE intervention, not one per tick — the #251 autonomy score is not
# double-penalized (#263). Co-located with the ledger so it inherits the ledger's dir + test
# isolation (HUB_WATCHDOG_LEDGER points at tmp). Per-window state: cleared when the condition
# resolves, and on a fresh drain arm (_clear_progress_state) so it never leaks across windows.
_wd_fired_marker() {
  local dir; dir="$(dirname "$(_wd_ledger_file)")"
  printf '%s\n' "$dir/wd-fire-dedup-$1-$2"
}
_wd_clear_fired() { rm -f "$(_wd_fired_marker "$1" "$2")" 2>/dev/null || true; }

# _wd_seed_afk_defect_label -> create the afk-defect label once per run (a marker dedups). A red
# defect color matching the repo's `bug` convention; best-effort. HUB_WATCHDOG_LABEL_CMD overrides.
_wd_seed_afk_defect_label() {
  local marker
  marker="$(_wd_filed_marker label seeded)"
  [ -f "$marker" ] && return 0
  if [ -n "${HUB_WATCHDOG_LABEL_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_LABEL_CMD" hub-watchdog "$HUB_WATCHDOG_AFK_DEFECT_LABEL" >/dev/null 2>&1 || true
  elif command -v gh >/dev/null 2>&1; then
    gh label create "$HUB_WATCHDOG_AFK_DEFECT_LABEL" --color d73a4a \
      --description "A watchdog-detected afk drain shortfall (issue #251)" --force >/dev/null 2>&1 || true
  fi
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true
  : > "$marker" 2>/dev/null || true
}

# _wd_open_defect_exists <condition> <issue> -> true when an OPEN afk-defect issue already covers
# this firing, so we append/skip instead of filing a duplicate. Searches by the label + the
# condition slug + the source issue number. gh unavailable reads as "no dup" (fall through to
# file); HUB_WATCHDOG_DEDUP_CMD overrides (echo a nonempty match ⇒ exists).
_wd_open_defect_exists() {
  local condition="$1" issue="$2" hits
  if [ -n "${HUB_WATCHDOG_DEDUP_CMD:-}" ]; then
    hits="$(bash -c "$HUB_WATCHDOG_DEDUP_CMD" hub-watchdog "$condition" "$issue" 2>/dev/null)"
    [ -n "$hits" ]; return
  fi
  command -v gh >/dev/null 2>&1 || return 1
  hits="$(gh issue list --state open --label "$HUB_WATCHDOG_AFK_DEFECT_LABEL" \
    --search "$condition #$issue" --json number -q '.[].number' 2>/dev/null)"
  [ -n "$hits" ]
}

# _wd_file_defect <condition> <issue> <reason> -> file (or dedup) the afk-defect via a headless
# bug-scoper on the hub-agent trackable surface. Gated by HUB_WATCHDOG_FILE (default on; the tests
# default it off). Per-run + open-issue deduped. HUB_WATCHDOG_SCOPER_CMD is the dispatch seam.
_wd_file_defect() {
  local condition="$1" issue="$2" reason="$3" marker
  [ "${HUB_WATCHDOG_FILE:-1}" = "1" ] || return 0
  marker="$(_wd_filed_marker "$condition" "$issue")"
  [ -f "$marker" ] && return 0                              # already filed this run
  if _wd_open_defect_exists "$condition" "$issue"; then     # an open afk-defect already covers it
    _wd_log "defect for [$condition] #$issue already open — not duplicating"
    mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
    return 0
  fi
  _wd_seed_afk_defect_label
  local prompt="afk drain shortfall detected by hub-watchdog: [$condition] on #$issue — $reason. \
Investigate why the /afk drain did not self-handle this, derive the Scope:/Gate: footer, and file \
ONE afk-defect issue (label $HUB_WATCHDOG_AFK_DEFECT_LABEL). Dedup against open issues first."
  if [ -n "${HUB_WATCHDOG_SCOPER_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_SCOPER_CMD" hub-watchdog "$condition" "$issue" "$reason" >/dev/null 2>&1 || true
  else
    local ha=""
    command -v _afk_find_script >/dev/null 2>&1 && ha="$(_afk_find_script "${HUB_WATCHDOG_HUB_AGENT:-}" hub-agent.sh || true)"
    if [ -n "$ha" ]; then
      bash "$ha" "scope-${condition}-${issue}" --purpose "afk-defect: $condition #$issue" \
        -- claude -p "$prompt" >/dev/null 2>&1 || true
    fi
  fi
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
  _wd_log "filed afk-defect for [$condition] #$issue via headless bug-scoper"
}

# _wd_fire <condition> <issue> <reason> -> record ONE intervention firing: classify it, append a
# JSONL line to the intervention-ledger (with the class), log it, and — for an afk-defect — file
# it via the headless bug-scoper (deduped). Every firing is a bug report against afk; subtask 5's
# autonomy score counts these lines. Best-effort.
_wd_fire() {
  local condition="$1" issue="$2" reason="$3" lf klass marker
  marker="$(_wd_fired_marker "$condition" "$issue")"
  [ -f "$marker" ] && return 0   # already fired this unresolved occurrence — dedupe ledger + file
  klass="$(_wd_classify "$condition" "$issue")"
  case "$klass" in afk-defect | novel-decision) ;; *) klass="afk-defect" ;; esac
  lf="$(_wd_ledger_file)"
  mkdir -p "$(dirname "$lf")" 2>/dev/null || true
  printf '{"ts":%s,"condition":"%s","issue":"%s","class":"%s","reason":"%s"}\n' \
    "$(_wd_now)" "$(_wd_json_escape "$condition")" "$(_wd_json_escape "$issue")" \
    "$(_wd_json_escape "$klass")" "$(_wd_json_escape "$reason")" >> "$lf" 2>/dev/null || true
  _wd_log "FIRING [$condition] #${issue} (${klass}) — ${reason}"
  [ "$klass" = "afk-defect" ] && _wd_file_defect "$condition" "$issue" "$reason"
  mkdir -p "$(dirname "$marker")" 2>/dev/null || true; : > "$marker" 2>/dev/null || true
  return 0
}

# --- the dispatcher -----------------------------------------------------------
# _wd_run_conditions [now] -> run all 5 detectors; on each firing record it + take the scripted
# intervention. Supervisor-dead is a single global check; the other four run per in-flight spoke.
# Best-effort throughout: a missing drain reader (standalone watchdog) simply skips its condition.
_wd_run_conditions() {
  local now="${1:-$(_wd_now)}" state="${2:-$(_wd_drain_state)}" wt issue wd_conflicts
  # Use the drain state the loop already read (passed as $2) rather than re-probing — the loop
  # reads it once per tick, and a second _wd_drain_state call would double-count under stubs.
  if [ "$state" = "stale" ]; then
    _wd_fire supervisor-dead "-" "drain supervisor heartbeat is stale — the drain crashed"
    _wd_intervene_rearm
  else
    _wd_clear_fired supervisor-dead "-"   # drain recovered → re-arm the firing for a future crash
  fi
  # Self-clear any needs-human-land/<issue> whose issue has since landed/closed (#263). Runs
  # BEFORE the in-flight loop: a landed issue's worktree is already gone, so its dangling
  # escalation would never otherwise be revisited.
  _wd_clear_landed_landmarks
  command -v inflight_worktrees >/dev/null 2>&1 || return 0
  while IFS=$'\t' read -r wt issue; do
    [ -n "$issue" ] || continue
    # Each detector: fire (deduped by _wd_fire's marker) + intervene when it trips; else clear the
    # firing marker so a genuinely resolved-then-recurring condition re-fires (#263).
    if _wd_detect_park_unanswered "$wt" "$issue" "$now"; then
      _wd_fire park-unanswered "$issue" "$(_wd_park_unanswered_reason "$wt" "$issue" "$now")"
      _wd_intervene_answer "$wt" "$issue"
    else
      _wd_clear_fired park-unanswered "$issue"
    fi
    if _wd_detect_dead_idle "$wt" "$issue" "$now"; then
      _wd_fire dead-pane "$issue" "reaper missed a dead/idle pane (> ${HUB_WATCHDOG_IDLE_CEILING}s)"
      _wd_intervene_revive "$wt" "$issue"
    else
      _wd_clear_fired dead-pane "$issue"
    fi
    if _wd_detect_stale_marker "$wt" "$issue"; then
      _wd_fire stale-marker "$issue" "stale blocked/ marker the drain did not reconcile"
      _wd_intervene_reconcile "$wt" "$issue"
    else
      _wd_clear_fired stale-marker "$issue"
    fi
    if _wd_land_lane_servicing "$issue"; then
      # #285 AC5: the drain's LAND lane has a FRESH armed retry — it is still servicing this land.
      # DEFER: neither fire (no false "skipped"/"conflicted-land" while the drain retries) NOR clear
      # a prior firing's dedup marker (clearing mid-service would let one persistent conflict re-fire
      # and double-count in the ledger, corrupting the #263 autonomy score). Mirrors the answer-lane
      # servicing defer; a genuinely stuck land still escalates once the backoff elapses and the
      # drain stops re-arming, when the branches below run.
      :
    elif _wd_detect_mergeable_skipped "$wt" "$issue" "$now"; then
      # #285: probe ACTUAL mergeability before labeling. A conflicted branch fires a DISTINCT
      # `conflicted-land` reason naming the files (a human following "mergeable" walks into the
      # same conflict); a truly-mergeable one keeps the historical auto-land-skipped. Both escalate
      # via the SAME needs-human-land tag (no second tripwire-racing tag, #272) — only the reason
      # differs, so the ledger/defect is honest about what the human must do.
      wd_conflicts="$(_wd_land_conflicts "$wt")"; wd_conflicts="${wd_conflicts% }"
      if [ -n "$wd_conflicts" ]; then
        _wd_fire conflicted-land "$issue" "branch conflicts with $(_wd_land_base_ref "$wt") on: $wd_conflicts — resolve on the spoke (merge the base branch), do not blind-land"
        _wd_clear_fired auto-land-skipped "$issue"   # not a clean skip → drop any stale skip firing
      else
        _wd_fire auto-land-skipped "$issue" "mergeable branch un-landed > ${HUB_WATCHDOG_LAND_CEILING}s (escalate-only: human land)"
        _wd_clear_fired conflicted-land "$issue"     # cleanly mergeable now → drop any stale conflict firing
      fi
      _wd_intervene_landmark "$wt" "$issue"
    else
      _wd_clear_fired auto-land-skipped "$issue"
      _wd_clear_fired conflicted-land "$issue"
    fi
  done < <(inflight_worktrees)
}

# --- the autonomy score + morning report (issue #251) -------------------------
# The whole point: a run with ZERO firings means afk was autonomous for that workload. The
# score = 1 − (interventions / spokes serviced) makes that measurable — 1.0 is the pass
# criterion for "afk autonomous on this backlog". The report is the morning artifact:
# interventions taken, defects filed, and the score, to stdout + a best-effort telemetry span.

# _wd_intervention_count -> firings this run = lines in the intervention-ledger (0 when absent).
# `grep -c` on an existing file with zero matches prints "0" AND exits 1, so a naive
# `[ -f ] && grep -c || echo 0` double-prints "0\n0" and corrupts the report — capture instead.
_wd_intervention_count() {
  local lf n; lf="$(_wd_ledger_file)"
  [ -f "$lf" ] || { printf '0\n'; return; }
  n="$(grep -c . "$lf" 2>/dev/null)" || true
  printf '%s\n' "${n:-0}"
}

# _wd_defect_count -> afk-defect firings (the drain-shortfall subset; novel-decisions excluded).
_wd_defect_count() {
  local lf n; lf="$(_wd_ledger_file)"
  [ -f "$lf" ] || { printf '0\n'; return; }
  n="$(grep -c '"class":"afk-defect"' "$lf" 2>/dev/null)" || true
  printf '%s\n' "${n:-0}"
}

# _wd_spokes_serviced -> distinct spokes the drain dispatched this run = the dispatch-<issue>.epoch
# files in the drain state dir (the SAME record auto_land keys on). 0 when none.
_wd_spokes_serviced() {
  local dir n=0 f
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  for f in "$dir"/dispatch-*.epoch; do [ -e "$f" ] && n=$((n + 1)); done
  printf '%s\n' "$n"
}

# _wd_autonomy_score -> 1 − (interventions / spokes), to 3 decimals. No spokes ⇒ 1.000 when there
# were also no interventions (nothing happened, trivially autonomous), else 0.000 (interventions
# with no serviced spoke — e.g. a bare supervisor-death — is pure non-autonomy).
_wd_autonomy_score() {
  local interventions spokes
  interventions="$(_wd_intervention_count)"
  spokes="$(_wd_spokes_serviced)"
  # LC_ALL=C: a non-C host locale makes awk's %.3f emit a comma decimal (1,000), which breaks
  # every downstream parse of the score — the recurring locale trap in this repo.
  LC_ALL=C awk -v i="$interventions" -v s="$spokes" 'BEGIN {
    if (s == 0) { printf "%.3f\n", (i == 0 ? 1 : 0); exit }
    v = 1 - (i / s); if (v < 0) v = 0; printf "%.3f\n", v
  }'
}

# _wd_report -> the morning artifact: one summary line to stdout + a best-effort telemetry span
# (kind=agent, name hub-watchdog:report) so the score lands in the observability surface too.
_wd_report() {
  local interventions defects spokes score
  interventions="$(_wd_intervention_count)"
  defects="$(_wd_defect_count)"
  spokes="$(_wd_spokes_serviced)"
  score="$(_wd_autonomy_score)"
  printf 'hub-watchdog: interventions=%s defects_filed=%s spokes_serviced=%s autonomy_score=%s\n' \
    "$interventions" "$defects" "$spokes" "$score"
  if [ -z "${HUB_WATCHDOG_NO_TELEMETRY:-}" ] && command -v telemetry_emit_span >/dev/null 2>&1; then
    telemetry_emit_span --kind agent --name "hub-watchdog:report" \
      --attr "autonomy_score=$score" --attr "interventions=$interventions" \
      --attr "spokes_serviced=$spokes" >/dev/null 2>&1 || true
  fi
}

# --- one tick -----------------------------------------------------------------
# _wd_tick <drain-state> -> one supervision pass: observe the drain, then run the 5 detection
# conditions (each firing → a scripted intervention + a defect-record line in the ledger).
_wd_tick() {
  local state="${1:-$(_wd_drain_state)}"
  _wd_log "tick: drain supervisor is ${state}"
  _wd_run_conditions "$(_wd_now)" "$state"
}

# --- the daemon loop ----------------------------------------------------------
# _wd_loop [baseline] -> tick every HUB_WATCHDOG_INTERVAL seconds: stamp the heartbeat, read
# the drain state, and on a non-off tick run _wd_tick + the source self-recycle check. Counts
# consecutive drain-off ticks and exits 0 after HUB_WATCHDOG_IDLE_TICKS of them (the drain is
# done → the watchdog that supervises it stops too). A non-off tick resets the idle counter.
# An empty baseline (tests / cron) opts out of the self-recycle. Never fatal.
_wd_loop() {
  local baseline="${1:-}" cur state
  local interval="${HUB_WATCHDOG_INTERVAL:-60}" max_idle="${HUB_WATCHDOG_IDLE_TICKS:-3}" idle=0
  _wd_log "watchdog loop started (pid $$, interval ${interval}s, idle grace ${max_idle} ticks)"
  while :; do
    _wd_write_heartbeat
    state="$(_wd_drain_state)"
    if [ "$state" != "off" ]; then
      idle=0
      _wd_tick "$state"
      # A land rewrote our own source on disk → re-exec into it. Content hash so an identical
      # rewrite never flaps; a transient empty stamp (hasher blip) is not a change; an empty
      # baseline opts out entirely.
      cur="$(_wd_source_hash)"
      if [ -n "$baseline" ] && [ -n "$cur" ] && [ "$cur" != "$baseline" ]; then
        _wd_reexec  # exec's into fresh code; returns only if it won't parse
      fi
    else
      idle=$((idle + 1))
      if [ "$idle" -ge "$max_idle" ]; then
        _wd_log "drain off for ${max_idle} ticks — exiting"
        return 0
      fi
    fi
    sleep "$interval"
  done
}

# _wd_once -> a single tick with no loop (cron / tests): stamp the heartbeat and run one tick.
_wd_once() {
  _wd_write_heartbeat
  _wd_tick
}

# --- daemon singleton ---------------------------------------------------------
# _wd_daemon [--reexec] -> singleton wrapper around _wd_loop. The pidfile makes N arms arm
# exactly one daemon: when it names a still-live pid (kill -0) refuse to start and leave the
# other daemon's pidfile alone; a stale pidfile (dead pid) is reclaimed. A re-exec keeps this
# pid, so `--reexec` reclaims our own file instead of refusing. Loop output appends to the
# logfile so a recovery is auditable after the fact. Always returns 0.
_wd_daemon() {
  local reexec="${1:-}" pidfile logfile pid baseline
  pidfile="$(_wd_pidfile)"
  logfile="$(_wd_logfile)"
  if [ "$reexec" != "--reexec" ] && [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if _wd_pid_alive "$pid"; then
      printf '%s\n' "hub-watchdog: already running (pid $pid, pidfile $pidfile)"
      return 0
    fi
  fi
  printf '%s' "$$" > "$pidfile"
  # Claimed: this shell owns the pidfile, so remove it on exit. The path rides a global — a
  # function-local is out of scope when the trap fires. TERM/INT traps too: bash does NOT run
  # the EXIT trap on an untrapped fatal signal, so a bare `kill` would strand the pidfile.
  _WD_PIDFILE="$pidfile"
  trap 'rm -f "$_WD_PIDFILE"' EXIT
  trap 'exit 0' TERM INT
  printf '%s\n' "hub-watchdog: daemon armed (pid $$, log $logfile)"
  baseline="$(_wd_source_hash)"
  _wd_loop "$baseline" >> "$logfile" 2>&1
  return 0
}

# _wd_arm -> nohup-detach the daemon so it survives THIS session's death (the whole point:
# ScheduleWakeup/Monitor die with the session; an OS process does not). Singleton-guarded: a
# live pidfile means one is already armed. Best-effort; never fails the caller (the drain
# co-arms it each tick, so a transient miss self-heals).
_wd_arm() {
  local pidfile pid
  pidfile="$(_wd_pidfile)"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if _wd_pid_alive "$pid"; then
      printf '%s\n' "hub-watchdog: already armed (pid $pid)"
      return 0
    fi
  fi
  nohup bash "$_WD_SELF" --daemon >/dev/null 2>&1 &
  printf '%s\n' "hub-watchdog: armed (nohup daemon detached)"
}

# _wd_status -> report the observed drain state and whether the watchdog itself is armed.
_wd_status() {
  local drain pidfile pid wd="off"
  drain="$(_wd_drain_state)"
  pidfile="$(_wd_pidfile)"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    _wd_pid_alive "$pid" && wd="live (pid $pid)" || wd="stale (pidfile names a dead pid)"
  fi
  printf 'hub-watchdog: drain=%s watchdog=%s\n' "$drain" "$wd"
}

# --- entry point --------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    --daemon)  _wd_daemon "${2:-}" ;;
    --arm)     _wd_arm ;;
    --once)    _wd_once ;;
    --status)  _wd_status ;;
    --report)  _wd_report ;;
    -h | --help) sed -n '2,51p' "$_WD_SELF" ;;
    *)         _wd_status ;;
  esac
fi
