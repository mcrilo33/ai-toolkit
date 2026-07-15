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
#   HUB_WATCHDOG_LAND_ACTIVE=900     a land-<issue>.log quiet this long ⇒ that land is not in flight
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
: "${HUB_WATCHDOG_LAND_ACTIVE:=900}"    # a land-<issue>.log quiet this long ⇒ that land is NOT in flight

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

# _wd_park_attempt_in_episode <attempt> <onset> -> true when a delivery landed INSIDE the current
# park episode (attempt >= onset, or onset unmeasurable): the same rule _wd_park_base measures by,
# and the branch split _wd_park_unanswered_reason reports on (#283/#288). Takes the ALREADY-READ
# values rather than re-reading them, so a caller that captured attempt/onset once (for a printed
# reason, or after refreshing the episode via _wd_park_base) can never have the branch decision
# disagree with a second, independently-timed read of the same epochs (#288 review).
_wd_park_attempt_in_episode() {
  local attempt="$1" onset="$2"
  case "$attempt" in '' | *[!0-9]*) return 1 ;; esac
  case "$onset" in '' | *[!0-9]*) return 0 ;; esac
  [ "$attempt" -ge "$onset" ]
}

# _wd_park_answer_attempted <wt> <issue> -> true when reanswer-<issue> records at least one
# attempt for the CURRENT (tip, sig) — proof the reasoner ran on THIS exact park, so labeling it
# "never-attempted" would be a lie (#288 AC2). Delegates to read_reanswer_count (gate-broker-
# markers.sh's own reader of the record it writes) rather than hand-parsing the file here.
_wd_park_answer_attempted() {
  local wt="$1" issue="$2" n
  command -v read_reanswer_count >/dev/null 2>&1 || return 1
  n="$(read_reanswer_count "$wt" "$issue" 2>/dev/null)"
  case "$n" in '' | *[!0-9]*) return 1 ;; esac
  [ "$n" -gt 0 ]
}

# _wd_park_warned_backoff_pending <issue> -> true when the default (answer) lane's warned-retry
# backoff is armed and NOT YET due — the drain is paced to retry, not abandoned (#288 AC2). This
# reads a signal _wd_drain_touched_recently structurally cannot: the backoff's own next-due epoch
# can be scheduled further out than the fixed recency window (AFK_WARN_BACKOFF_CAP defaults to
# 1800s, 3x the 600s park ceiling — the #274 land-lane inversion, unfixed here).
_wd_park_warned_backoff_pending() {
  local issue="$1" next
  command -v _afk_warned_next >/dev/null 2>&1 || return 1
  next="$(_afk_warned_next "$issue" 2>/dev/null)"
  case "$next" in '' | *[!0-9]*) return 1 ;; esac
  [ "$next" -gt "$(_wd_now)" ]
}

# _wd_park_drop_info <wt> <issue> -> "<count>\t<reason>" for the CURRENT (tip, sig)'s recorded
# answer-drop episode (issue #288 AC3), or empty when none/stale/unavailable.
_wd_park_drop_info() {
  local wt="$1" issue="$2"
  command -v read_answer_drop >/dev/null 2>&1 || return 0
  read_answer_drop "$wt" "$issue" 2>/dev/null
}

# Condition 1: a parked spoke answer_pass left unanswered past the grace margin. Three gates, in
# order: the park must be the ANSWER lane's (#283/#271), the CURRENT episode's base must be older
# than the ceiling (#283/#265 — never zero, and never a delivery from a long-resolved park), and
# the drain must not be visibly servicing the spoke. Only then did the drain genuinely fall short.
_wd_detect_park_unanswered() {
  local wt="$1" issue="$2" now="$3" base attempt onset
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  base="$(_wd_park_base "$wt" "$issue")"   # refreshes the episode onset (note_park_episode) first
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  _wd_drain_touched_recently "$issue" "$now" && return 1
  # #288 AC2: the never-attempted branch specifically must not fire "no answer delivered" when
  # the drain plainly HAS attempted one on this exact park and is paced to retry — neither drop
  # path journals (so _wd_drain_touched_recently's recency window can't see it), and a warned
  # backoff can legitimately be scheduled past that window. A genuinely-untouched park (no
  # reanswer record at all) still fires unchanged. The drop-evidence case gets its own honest
  # condition (park-undeliverable) below, so this suppression must not engage there either.
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  if ! _wd_park_attempt_in_episode "$attempt" "$onset" \
     && _wd_park_answer_attempted "$wt" "$issue" \
     && _wd_park_warned_backoff_pending "$issue"; then
    return 1
  fi
  return 0
}

# Condition 1b (issue #288 AC3): a never-attempted park the drain DID service (>=1 reasoner run)
# but every delivery was dropped before injection — the #277 shape. Fires INSTEAD of
# park-unanswered whenever a drop is on record for the current episode, regardless of backoff
# phase: a park that has exhausted its backoff and still has nothing to show must still surface,
# just under the honest reason (a serviced-but-undeliverable park must never read as silence).
_wd_detect_park_undeliverable() {
  local wt="$1" issue="$2" now="$3" base attempt onset
  command -v slot_state >/dev/null 2>&1 || return 1
  # Cheap pre-check BEFORE the slot_state/lane/base probe chain (each a tmux capture-pane call,
  # #269's load-flake class): the vast majority of parked spokes never had a drop recorded at
  # all, so bail on a bare file-existence check rather than repeating _wd_detect_park_unanswered's
  # entire probe chain for nothing (#288 review).
  command -v _answer_drop_state_file >/dev/null 2>&1 || return 1
  [ -f "$(_answer_drop_state_file "$issue")" ] || return 1
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  base="$(_wd_park_base "$wt" "$issue")"   # refreshes the episode onset BEFORE it is read below
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  _wd_park_attempt_in_episode "$attempt" "$onset" && return 1   # a delivery landed -> stale-attempt's turf
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  [ -n "$(_wd_park_drop_info "$wt" "$issue")" ]
}

# _wd_park_undeliverable_reason <wt> <issue> <now> -> names the drop count + last drop's own
# verdict, so the ledger line is diagnosable alone (#288 AC3, mirroring #283 AC5's measured-base
# reason).
_wd_park_undeliverable_reason() {
  local wt="$1" issue="$2" now="$3" info count reason onset
  info="$(_wd_park_drop_info "$wt" "$issue")"
  count="${info%%$'\t'*}"
  reason="${info#*$'\t'}"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  printf 'park-undeliverable: %s computed-then-dropped answer(s), last drop: %s (parked %s; ceiling %ss)' \
    "${count:-?}" "${reason:-unknown}" "$(_wd_age_seconds "$onset" "$now")" "$HUB_WATCHDOG_PARK_CEILING"
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
  # The branch decision is shared with _wd_park_attempt_in_episode, fed the SAME attempt/onset
  # locals used for the printed text below, so the reported branch can never disagree with the
  # epoch that actually fired.
  if _wd_park_attempt_in_episode "$attempt" "$onset"; then
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

# _wd_done_epoch <issue> -> the stamped done epoch, or empty when unstamped / the reader is absent
# (a standalone watchdog with no gate-broker). Wrapped rather than calling read_done_epoch inline so
# the detector and _wd_intervene_revive's second lock read the same guarded way.
_wd_done_epoch() {
  command -v read_done_epoch >/dev/null 2>&1 || return 0
  read_done_epoch "$1" 2>/dev/null
}

# _wd_land_log <issue> -> the per-issue land log auto_land writes (<state-dir>/land-<issue>.log,
# hub-afk.sh's #198 record), or empty when the state-dir reader is absent (standalone watchdog).
_wd_land_log() {
  command -v _afk_state_dir >/dev/null 2>&1 || return 0
  printf '%s\n' "$(_afk_state_dir)/land-$1.log"
}

# _wd_file_mtime <path> -> the file's mtime epoch, or empty when unreadable. GNU stat first, BSD
# second — the house idiom (gate-broker-detect.sh's _transcript_mtime): this hub is macOS, CI is
# GNU, and a one-flavor probe reads empty on the other (#289).
_wd_file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

# _wd_land_in_flight <issue> <now> -> true when a land for this issue is running RIGHT NOW, so the
# watchdog must not read its teardown as an abandoned spoke (#290). Two signals: a LIVE drain whose
# last action is `land #<issue>` (auto_land stamps it before the synchronous land, so it names the
# issue for the land's whole duration — the primary signal), or a land-<issue>.log written within
# HUB_WATCHDOG_LAND_ACTIVE (the fallback for a clobbered/stale last-action). Both fail toward NOT
# in flight — a crashed drain's stale last-action, or an unreadable mtime, must never silence
# condition 2 forever.
# NOT the same as _wd_land_lane_servicing (#285), and the two must not be collapsed: that one reads
# the land lane's ARMED RETRY backoff — a land that already FAILED and is scheduled to be
# re-attempted; this one reads a land executing at this instant.
_wd_land_in_flight() {
  local issue="$1" now="$2" log mtime
  if [ "$(_wd_drain_state)" = "live" ]; then
    case "$(_wd_last_action)" in "land #$issue") return 0 ;; esac
  fi
  log="$(_wd_land_log "$issue")"
  [ -n "$log" ] && [ -f "$log" ] || return 1
  mtime="$(_wd_file_mtime "$log")"
  # _wd_epoch_stale reads an unmeasurable epoch as NOT stale, which negates to "in flight" — the
  # wrong direction for a defer. Screen both operands first so only a real, fresh mtime defers.
  case "$mtime" in '' | *[!0-9]*) return 1 ;; esac
  case "$now" in '' | *[!0-9]*) return 1 ;; esac
  # A FUTURE-dated mtime (clock skew, a corrupted timestamp) is unmeasurable, not fresh: negating
  # _wd_epoch_stale on it would defer until wall-clock caught up to the bogus stamp — an unbounded
  # silence rather than the documented HUB_WATCHDOG_LAND_ACTIVE window. Fail toward firing.
  [ "$mtime" -le "$now" ] || return 1
  ! _wd_epoch_stale "$mtime" "$now" "$HUB_WATCHDOG_LAND_ACTIVE"
}

# Condition 2: a dead/crashed pane recover_dead_panes/reap_pass never revived, past the ceiling.
# A DONE-stamped spoke is never a reaper miss (#290): the epoch records that the spoke reached
# terminal state, and a land consuming the ready/<issue> tag flips LIVE slot_state off `done` while
# the worktree still exists — the teardown gap that false-fired on #284.
#
# <done-epoch> MUST be read BEFORE anything on this tick calls slot_state, which is why the
# dispatcher pre-reads it and passes it in rather than letting this read it live: a non-terminal
# slot_state read DELETES the epoch (_afk_note_tip_progress -> clear_done_epoch, deliberate per
# #263 — "non-terminal at this tip ⇒ any live done-epoch is stale"), and the park detectors call
# slot_state ahead of this one every tick. Read live here, the epoch is always already gone and the
# guard is dead code. Omit the argument and it falls back to a live read for a direct caller.
# The guard is therefore self-limiting rather than permanent: it holds only while the last
# slot_state read was terminal, and expires one tick after the spoke is observed non-terminal —
# after which _wd_land_in_flight carries the defer for the rest of the land.
# That defer lives in the DISPATCHER, not here, for the reason documented at condition 4: a
# detector returning 1 falls into the else-branch, which CLEARS the firing-dedup marker — mid-land
# that would let a subsequently-failed land re-fire and double-count (#263).
_wd_detect_dead_idle() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ -z "$(_spoke_pane_target "$wt" 2>/dev/null)" ] || return 1   # a live pane is the reaper's job
  [ -n "$done_epoch" ] && return 1                               # done-stamped ⇒ not a reaper miss
  [ "$(slot_state "$wt" "$issue")" = "done" ] && return 1        # terminal ⇒ not a hang
  _wd_epoch_stale "$(read_progress_epoch "$issue" 2>/dev/null)" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
}

# _wd_dead_idle_reason <wt> <issue> <now> [done-epoch] -> the MEASURED firing reason (#290 AC4):
# the age, plus every input the decision turned on — which epoch it measured from, the live
# slot_state, whether a done epoch was present, and what the drain last did — so a future false
# positive is diagnosable from the ledger line alone instead of re-deriving the timeline from four
# state files. Mirrors _wd_park_unanswered_reason's measured-base contract (#283 AC5); the reason
# field is where this file records a base, so _wd_fire's JSON shape needs no new key.
# Takes the SAME pre-read done epoch the detector was given, so the reported classification cannot
# disagree with the one that actually fired (a live re-read here would come back empty every time —
# the park detectors' slot_state call has already cleared it, per #263).
# UPGRADE: this re-reads slot_state for the diagnostic, and as a command-substitution argument to
# _wd_fire it re-runs every tick the condition holds, not just the deduped first firing — thread
# the detector's own read through if a persistent dead pane ever makes the repeat cost matter. It
# is safe today: every slot_state mutation is stamp-once/clear-idempotent, and the dead-idle path
# has no pane, so _permission_pending short-circuits before any tmux capture (not the #269 class).
_wd_dead_idle_reason() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}" progress
  progress="$(read_progress_epoch "$issue" 2>/dev/null)"
  printf 'reaper missed a dead/idle pane: no pane, last progress %s ago' \
    "$(_wd_age_seconds "$progress" "$now")"
  printf ' (ceiling %ss; base=progress@%s, slot_state=%s, done-epoch=%s, last-action=%s)' \
    "$HUB_WATCHDOG_IDLE_CEILING" "${progress:-?}" "$(slot_state "$wt" "$issue" 2>/dev/null)" \
    "${done_epoch:-none}" "$(_wd_last_action)"
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
  # #290 AC3: NEVER resume a session into a worktree that is finished or being torn down. On #284
  # this launched `nohup claude --continue` inside a worktree the land removed seconds later — a
  # headless run against a vanishing cwd. The detector's done-epoch guard already stops the
  # dispatcher path; this is the second lock, for any DIRECT caller (mirroring the permission-lane
  # re-check in _wd_intervene_answer). Reading the epoch live is right here: a direct caller has not
  # necessarily run the slot_state that would clear it.
  if [ -n "$(_wd_done_epoch "$issue")" ]; then
    _wd_log "deferring revive on #$issue — the spoke is done-stamped (terminal, not a hang)"
    return 0
  fi
  if _wd_land_in_flight "$issue" "$(_wd_now)"; then
    _wd_log "deferring revive on #$issue — a land is in flight (its teardown is removing the worktree)"
    return 0
  fi
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
# _wd_sweep_dead_pane_markers <in-flight issues> -> drop every dangling wd-fire-dedup-dead-pane-<N>
# (#290 AC5). Condition 2 raises no needs-human-land tag, so _wd_clear_landed_landmarks never
# revisits its firings; and the dispatcher's else-clear runs ONLY for in-flight worktrees, which a
# landed issue no longer has — so its marker dangles for the rest of the run and a genuine later
# recurrence would stay deduped into silence. Mirrors the landmark sweep, including its fail-safe:
# _wd_issue_open reads an ambiguous state (gh down, empty query) as OPEN, so an outage never
# mass-clears live markers.
# <in-flight issues> is this tick's space-separated issue list; those are the DISPATCHER's to clear
# (their detector may still be firing, and re-arming the dedup mid-fire would let one unresolved
# condition double-count in the ledger, #263). Scoped to the dead-pane stem on purpose: the other
# conditions' markers dangle the same way, but auto-land-skipped/conflicted-land are already swept
# by the landmark sweep, and widening this glob would re-arm park markers whose detector can still
# fire on a closed issue. Best-effort throughout.
_wd_sweep_dead_pane_markers() {
  local inflight=" ${1:-} " dir f issue
  dir="$(dirname "$(_wd_ledger_file)")"
  [ -d "$dir" ] || return 0
  for f in "$dir"/wd-fire-dedup-dead-pane-*; do
    [ -e "$f" ] || continue
    issue="${f##*-}"
    case "$issue" in '' | *[!0-9]*) continue ;; esac    # never resolve a non-numeric stem as an issue
    # Space-padded on BOTH sides (the list, above, and the pattern): a bare *"$issue"* would let
    # #4 match an in-flight list containing 14 or 284 and silently skip a real dangling marker.
    case "$inflight" in *" $issue "*) continue ;; esac  # still in flight ⇒ the dispatcher's to clear
    _wd_issue_open "$issue" && continue                 # still open ⇒ a real unresolved condition
    _wd_clear_fired dead-pane "$issue"
    _wd_log "cleared dangling dead-pane firing marker for landed/closed #$issue"
  done
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
    park-unanswered | park-undeliverable)
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
  local now="${1:-$(_wd_now)}" state="${2:-$(_wd_drain_state)}" wt issue wd_conflicts wd_done
  local wd_seen=""   # the issues this tick saw in flight — the sweep below leaves them alone
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
    wd_seen="$wd_seen $issue"
    # #290: pre-read the done epoch BEFORE any detector below calls slot_state — a non-terminal
    # slot_state read deletes it (#263), and the park detectors run first, so a read at
    # condition-2 time always comes back empty. Per-iteration local, passed explicitly: not a
    # tick-global (the #241 cross-pass leak trap).
    wd_done="$(_wd_done_epoch "$issue")"
    # Each detector: fire (deduped by _wd_fire's marker) + intervene when it trips; else clear the
    # firing marker so a genuinely resolved-then-recurring condition re-fires (#263).
    # park-undeliverable (#288 AC3) is checked FIRST: a serviced-but-never-deliverable park must
    # never ALSO read as the misleading never-attempted label.
    if _wd_detect_park_undeliverable "$wt" "$issue" "$now"; then
      _wd_fire park-undeliverable "$issue" "$(_wd_park_undeliverable_reason "$wt" "$issue" "$now")"
      _wd_intervene_answer "$wt" "$issue"
      _wd_clear_fired park-unanswered "$issue"
    elif _wd_detect_park_unanswered "$wt" "$issue" "$now"; then
      _wd_fire park-unanswered "$issue" "$(_wd_park_unanswered_reason "$wt" "$issue" "$now")"
      _wd_intervene_answer "$wt" "$issue"
      _wd_clear_fired park-undeliverable "$issue"
    else
      _wd_clear_fired park-unanswered "$issue"
      _wd_clear_fired park-undeliverable "$issue"
    fi
    if _wd_land_in_flight "$issue" "$now"; then
      # #290: a land for this issue is executing RIGHT NOW. Its teardown consumes the ready/<issue>
      # tag and kills the tmux window BEFORE removing the worktree, so a tick inside that gap sees
      # exactly the dead-pane shape on a spoke that is landing successfully — on #284 that burned a
      # revive into a worktree deleted seconds later. DEFER: neither fire NOR clear a prior firing's
      # dedup marker (clearing mid-land would let a subsequently-failed land re-fire and
      # double-count in the ledger, #263). Mirrors the land-lane servicing defer below; a land that
      # genuinely leaves a dead pane behind still fires once the land stops running.
      :
    elif _wd_detect_dead_idle "$wt" "$issue" "$now" "$wd_done"; then
      _wd_fire dead-pane "$issue" "$(_wd_dead_idle_reason "$wt" "$issue" "$now" "$wd_done")"
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
  # #290 AC5: sweep dead-pane firing markers for issues that have since landed. Runs AFTER the loop
  # so it knows which issues were in flight this tick (those are the dispatcher's to clear above).
  # The loop is fed by process substitution, NOT a pipe, so wd_seen survives into this call.
  _wd_sweep_dead_pane_markers "${wd_seen# }"
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
