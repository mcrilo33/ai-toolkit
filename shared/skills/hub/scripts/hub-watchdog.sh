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
#   HUB_WATCHDOG_ORIG_SCRIPT      the CHECKOUT path of this script when we were armed from a
#                                 private copy — the bundle the self-recycle hashes (#296)
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

# Our ORIGIN: the checkout path a land rewrites, as opposed to the private copy we may be
# EXECUTING from. The drain arms us out of hub-afk.sh's frozen self-copy (/tmp/hub-afk-self.*/,
# #133) and hands the origin over in HUB_WATCHDOG_ORIG_SCRIPT — the same contract hub-afk.sh's
# own AFK_ORIG_SCRIPT carries. Absent (a standalone arm straight from the checkout) we ARE the
# origin. A stale override naming no file is IGNORED rather than trusted: hashing a path that
# does not exist stamps every tick alike, which is the dead self-recycle this fixes (#296).
_WD_ORIGIN_SELF="$_WD_SELF"
if [ -n "${HUB_WATCHDOG_ORIG_SCRIPT:-}" ] && [ -f "${HUB_WATCHDOG_ORIG_SCRIPT:-}" ]; then
  _WD_ORIGIN_SELF="$HUB_WATCHDOG_ORIG_SCRIPT"
fi
_WD_ORIGIN_DIR="$(cd "$(dirname "$_WD_ORIGIN_SELF")" && pwd)" || _WD_ORIGIN_DIR="$HUB_WATCHDOG_SCRIPT_DIR"

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

# _wd_origin_of <path> -> <path>'s counterpart in the ORIGIN bundle when one exists, else <path>
# unchanged. Maps by basename: a self-copy is a flat `cp <dir>/*.sh` of the origin dir, so a
# copied sibling and its origin differ only in directory. A lib resolved from OUTSIDE the copy
# (the checkout-layout worktree-lib.sh, reached via the toplevel ladder) has no counterpart there
# and is already an origin path — keep it. Identity when we were not armed from a copy.
_wd_origin_of() {
  local p="${1:-}" cand
  [ -n "$p" ] || return 0
  cand="$_WD_ORIGIN_DIR/${p##*/}"
  if [ "$cand" != "$p" ] && [ -f "$cand" ]; then printf '%s\n' "$cand"; return; fi
  printf '%s\n' "$p"
}

# The daemon's own source bundle: this script + the libs it sources, resolved to their ORIGIN
# paths. Stamped at daemon start and re-checked each tick so a land of the watchdog's own code
# re-execs it live (#251/#190). Hashing the paths we EXECUTE from instead would watch the frozen
# self-copy the drain armed us out of — a bundle no land ever rewrites, so the stamp could never
# move and the recycle was structurally dead whenever the drain armed us, i.e. always (#296).
_WD_SOURCE_FILES=(
  "$(_wd_origin_of "$_WD_SELF")"
  "$(_wd_origin_of "$_WD_RESOLVED_BROKER")"
  "$(_wd_origin_of "$_WD_RESOLVED_INJECT")"
  "$(_wd_origin_of "$_WD_RESOLVED_WT_LIB")"
)

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
# The generation stamp: the origin source hash the CURRENTLY-armed daemon booted with (#296
# AC2). Lets a fresh arm attempt tell "a live daemon I can prove is behind a land" from "a live
# daemon I have no evidence about" without asking the live process anything.
_wd_genfile()   { printf '%s\n' "${HUB_WATCHDOG_GENFILE:-$(_wd_common_dir)/hub-watchdog.gen}"; }
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

# _wd_fresh_copy -> a private copy of the ORIGIN bundle in a fresh tmp dir, printing the copied
# hub-watchdog.sh path (empty on failure). The daemon must EXECUTE from a copy and never from the
# checkout: bash lazily re-reads a running script past main(), so the NEXT land rewriting the
# origin under a live daemon corrupts the interpreter mid-run (the #133 death, which is why
# hub-afk.sh has _afk_exec_self_copy at all). The whole sibling set rides along via a flat glob,
# not an enumerated list, so a helper moved to a new sibling file needs no registration (#262).
_wd_fresh_copy() {
  local dir copy
  dir="$(mktemp -d "${TMPDIR:-/tmp}/hub-watchdog-self.XXXXXX" 2>/dev/null)" || return 0
  copy="$dir/${_WD_ORIGIN_SELF##*/}"
  cp "$_WD_ORIGIN_DIR"/*.sh "$dir"/ 2>/dev/null || cp "$_WD_ORIGIN_SELF" "$copy" 2>/dev/null || return 0
  [ -f "$copy" ] || cp "$_WD_ORIGIN_SELF" "$copy" 2>/dev/null || return 0
  printf '%s\n' "$copy"
}

# _wd_reexec -> replace this daemon with a fresh copy running the on-disk (post-land) code.
# `exec` preserves the pid, so the pidfile keeps naming a live process and no second daemon is
# armed; the `--reexec` flag (passed ONLY here) tells the fresh _wd_daemon to reclaim its own
# pidfile rather than refuse as "already running". First `bash -n`-checks the whole bundle: a
# DEAD watchdog is worse than a stale one, so if a land shipped parse-broken code we keep
# running the current (working) code and return — the loop retries next tick. Both the check and
# the exec read the ORIGIN (#296): re-exec'ing our own frozen copy would re-run the very code the
# land replaced, under a freshly-stamped baseline — a silent no-op that looks like a recycle.
_wd_reexec() {
  local f fresh
  for f in "${_WD_SOURCE_FILES[@]}"; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    if ! bash -n "$f" 2>/dev/null; then
      _wd_log "on-disk source changed but $f fails to parse — NOT re-exec'ing; keeping current code"
      return 0
    fi
  done
  _wd_log "source changed on disk (a land) — re-exec'ing into fresh code"
  # Fail-OPEN to the origin path itself when no copy could be made: running the landed code
  # unprotected still beats patrolling forever on code a land already replaced.
  fresh="$(_wd_fresh_copy)"
  [ -n "$fresh" ] || fresh="$_WD_ORIGIN_SELF"
  # HUB_WATCHDOG_SCRIPT_DIR is stripped so the fresh copy resolves its OWN dir — inheriting ours
  # would pin the next generation's sibling ladder back to the dir we are leaving. The origin
  # rides along so it keeps hashing the checkout rather than the copy we just made.
  exec env -u HUB_WATCHDOG_SCRIPT_DIR HUB_WATCHDOG_ORIG_SCRIPT="$_WD_ORIGIN_SELF" \
    bash "$fresh" --daemon --reexec
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

# _wd_park_log_onset <issue> -> the onset ts of the CURRENT park as RECORDED in the #300 transition
# log (the `parked`/`parked-gate` transition's own ts), or empty when the log records no park state
# (#304). This is the #265 zero-grace fix made log-native: the onset was recorded by the actor at
# park time, not reconstructed from a slot_state side-effect. Gated on the current log state being a
# park — afk_state_onset returns the LAST transition's ts regardless of kind, so a later
# `pushed`/`landed` transition's ts must not masquerade as a park onset. Empty (no API / not parked
# in the log) falls back to the epoch path, per the #300 unknown-never-fires-nor-suppresses contract.
_wd_park_log_onset() {
  local issue="$1"
  command -v afk_current_state >/dev/null 2>&1 || return 0
  command -v afk_state_onset >/dev/null 2>&1 || return 0
  case "$(afk_current_state "$issue" 2>/dev/null)" in
    parked | parked-gate) afk_state_onset "$issue" 2>/dev/null ;;
  esac
}

# _wd_park_base <wt> <issue> -> the epoch the park-unanswered ceiling measures FROM:
# max(current episode onset, answer delivery). An answer delivered BEFORE the current park began
# cannot count against it (#283) — that was the #276 false-fire: one answered plan gate, then ten
# productive minutes, and the ceiling still measured from that one delivery. The onset comes
# log-first from the recorded park transition (#304); on an unknown log it falls back to
# note_park_episode, which re-stamps the epoch when the pending park's context changes, so the onset
# names the episode actually pending. A delivery INSIDE the episode is the more recent word and
# wins. Empty when neither is measurable (the detector then cannot fire — same contract as
# _afk_ceiling_epoch).
_wd_park_base() {
  local wt="$1" issue="$2" onset attempt
  onset="$(_wd_park_log_onset "$issue")"
  if [ -z "$onset" ]; then
    if command -v note_park_episode >/dev/null 2>&1; then
      onset="$(note_park_episode "$wt" "$issue" 2>/dev/null)"
    else
      onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
    fi
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

# _wd_episode_service <issue> -> delivered | dropped | none | unknown: the CURRENT park episode's
# service state, read from the transition log's episode-keyed answer-lane events (#304). This is the
# log-native replacement for the two side-channels the pre-#304 branch split read — the
# answer-attempt epoch (a delivery) and the answer-drop file (a drop) — unified into ONE
# episode-scoped read, so a delivery/drop from a RESOLVED park (a different episode key) can never be
# misattributed to the pending one (the canonical #241/#283/#288 bug). `unknown` (no read API, or the
# log records no park episode) means fall back to the epoch side-channels — NEVER a firing NOR a
# suppression basis on its own (#300). Gated on the current log state being a park so a stale episode
# left by a resolved park cannot be read as live.
#   delivered — the episode's last answer-lane event is a delivery/compute (#283 stale-attempt turf)
#   dropped   — the episode's last answer-lane event is a computed-then-dropped answer (#288 turf)
#   none      — the episode is recorded but carries no answer-lane event yet (never-attempted)
_wd_episode_service() {
  local issue="$1" ep ev
  command -v afk_current_state >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  case "$(afk_current_state "$issue" 2>/dev/null)" in
    parked | parked-gate) ;;
    *) printf 'unknown\n'; return 0 ;;
  esac
  command -v afk_current_episode >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  command -v afk_last_service_event >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  ep="$(afk_current_episode "$issue" 2>/dev/null)"
  [ -n "$ep" ] || { printf 'unknown\n'; return 0; }
  ev="$(afk_last_service_event "$issue" "$ep" answer 2>/dev/null)"
  [ -n "$ev" ] || { printf 'none\n'; return 0; }
  case "$ev" in
    *'"event":"answer_dropped"'*) printf 'dropped\n' ;;
    *) printf 'delivered\n' ;;
  esac
}

# _wd_delivered_in_episode <wt> <issue> <attempt> <onset> -> rc 0 when a delivery landed INSIDE the
# current park episode (the #283 stale-attempt branch), rc 1 for never-attempted. Log-first: a
# `delivered` episode-service reading decides it, and `none`/`dropped` decide never-attempted; only an
# `unknown` log falls back to the epoch compare (_wd_park_attempt_in_episode), preserving the pre-#304
# behavior when the log is absent. The single branch oracle both the detector and the reason consult,
# so the reported branch can never disagree with the one that fired.
_wd_delivered_in_episode() {
  local wt="$1" issue="$2" attempt="$3" onset="$4"
  case "$(_wd_episode_service "$issue")" in
    delivered) return 0 ;;
    none | dropped) return 1 ;;
  esac
  _wd_park_attempt_in_episode "$attempt" "$onset"
}

# _wd_park_attempt_in_episode <attempt> <onset> -> true when a delivery landed INSIDE the current
# park episode (attempt >= onset, or onset unmeasurable): the same rule _wd_park_base measures by,
# and the branch split _wd_park_unanswered_reason reports on (#283/#288). Takes the ALREADY-READ
# values rather than re-reading them, so a caller that captured attempt/onset once (for a printed
# reason, or after refreshing the episode via _wd_park_base) can never have the branch decision
# disagree with a second, independently-timed read of the same epochs (#288 review). The epoch
# FALLBACK for _wd_delivered_in_episode when the transition log is unknown (#304).
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
  if ! _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset" \
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
  local wt="$1" issue="$2" now="$3" base attempt onset svc
  command -v slot_state >/dev/null 2>&1 || return 1
  # Cheap pre-check BEFORE the slot_state/lane/base probe chain (each a tmux capture-pane call,
  # #269's load-flake class): the vast majority of parked spokes never had a drop recorded at all.
  # #304: a drop is on record either as an episode-keyed `answer_dropped` lane event (log-native) OR
  # as the pre-#304 answer-drop file; bail only when NEITHER exists rather than repeating
  # _wd_detect_park_unanswered's entire probe chain for nothing (#288 review).
  svc="$(_wd_episode_service "$issue")"
  if [ "$svc" != dropped ]; then
    command -v _answer_drop_state_file >/dev/null 2>&1 || return 1
    [ -f "$(_answer_drop_state_file "$issue")" ] || return 1
  fi
  [ "$(slot_state "$wt" "$issue")" = "waiting" ] || return 1
  _wd_park_is_answer_lane "$wt" "$issue" || return 1
  base="$(_wd_park_base "$wt" "$issue")"   # refreshes the episode onset BEFORE it is read below
  attempt="$(read_answer_attempt "$issue" 2>/dev/null)"
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset" && return 1   # a delivery landed -> stale-attempt's turf
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_PARK_CEILING" || return 1
  # A drop is proven by the log episode (dropped) or the pre-#304 drop file.
  [ "$svc" = dropped ] || [ -n "$(_wd_park_drop_info "$wt" "$issue")" ]
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
  # The branch decision is shared with the detector via _wd_delivered_in_episode (log-first, epoch
  # fallback), fed the SAME attempt/onset locals used for the printed text below, so the reported
  # branch can never disagree with the one that actually fired.
  if _wd_delivered_in_episode "$wt" "$issue" "$attempt" "$onset"; then
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

# _wd_current_state <issue> -> the spoke's last RECORDED lifecycle state from the #300 transition
# log (afk_current_state), or "unknown" when the read API is absent (a standalone watchdog with no
# gate-broker/worktree-lib in scope) or nothing has been recorded. Wrapped rather than calling
# afk_current_state inline so every detector reads the log the same guarded way and a missing API
# degrades to "unknown" — which per #300's contract is NEVER a firing basis NOR a suppression basis
# on its own (each caller falls back to today's side-effect inference on unknown).
_wd_current_state() {
  command -v afk_current_state >/dev/null 2>&1 || { printf 'unknown\n'; return 0; }
  afk_current_state "$1" 2>/dev/null
}

# _wd_state_phase_fresh <issue> <now> -> true when the RECORDED phase's onset (afk_state_onset) is
# still within HUB_WATCHDOG_IDLE_CEILING — i.e. the landing/pushing phase is plausibly in progress,
# not stuck. This BOUNDS the dead-pane defer so it can never silence the reaper-miss backstop forever
# (the sibling _wd_land_in_flight bounds its own defer for exactly this reason — "must never silence
# condition 2 forever"). spoke-push/worktree-land record landing/pushing INTENT-FIRST and leave them
# stuck on a mid-phase crash (#299's silent-stall class), so a phase older than the ceiling has run
# impossibly long: re-arm the backstop. An unmeasurable onset fails toward re-arming (return 1),
# never toward an unbounded silence — the same fail-toward-firing direction _wd_land_in_flight takes
# on an unmeasurable mtime. The worst-case legitimate gate (a first-push seed, tens of minutes) sits
# well under the ceiling, so a healthy phase never re-arms early; only a genuinely stuck one does.
_wd_state_phase_fresh() {
  local onset now="$2"
  onset="$(afk_state_onset "$1" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) return 1 ;; esac
  ! _wd_epoch_stale "$onset" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
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
# _wd_dead_idle_base <issue> -> "<kind>\t<epoch>" — the epoch condition 2's ceiling measures FROM
# and WHICH clock it is, or empty when neither is measurable (#297). max(dispatch, progress), the
# same base the drain's reaper uses (_afk_ceiling_epoch): progress alone is stamped only on a
# branch-tip ADVANCE, so a pane that crashes BEFORE its first commit leaves it empty forever and
# _wd_epoch_stale reads empty as never-fire — condition 2 was structurally blind to the very reaper
# miss it exists to catch. Dispatch is the floor: a spoke has been running since it was dispatched
# whether or not it ever committed.
# Duplicates _afk_ceiling_epoch's max() rather than calling it because the reason string must name
# the WINNER (base=dispatch@N vs base=progress@N tells a triaging human whether the spoke ever
# committed at all). The two are pinned to agree by
# test_dead_idle_base_epoch_agrees_with_the_real_reaper_ceiling — they must never diverge on WHEN a
# spoke is over its ceiling, or the watchdog and the reaper disagree about the same spoke.
# Neither measurable ⇒ empty ⇒ the detector cannot fire, preserving _afk_ceiling_epoch's contract:
# never invent a ceiling with no clock behind it. Both reads are case-screened, so an absent reader
# (a standalone watchdog with no gate-broker) degrades to the other rather than erroring.
_wd_dead_idle_base() {
  local issue="$1" now="${2:-$(_wd_now)}" d p
  d="$(read_dispatch_epoch "$issue" 2>/dev/null)"
  p="$(read_progress_epoch "$issue" 2>/dev/null)"
  case "$d" in '' | *[!0-9]*) d=0 ;; esac
  case "$p" in '' | *[!0-9]*) p=0 ;; esac
  case "$now" in '' | *[!0-9]*) now=0 ;; esac
  # A FUTURE-dated epoch is unmeasurable, not fresh — and taking max() would let it OUTRANK a
  # genuinely stale clock and silence this condition for the whole window (now-epoch goes negative,
  # which is never > the ceiling). That is the unbounded silence #284 was: worse than the blindness
  # this fix removes. Drop each skewed epoch so the other still measures — the same fail-toward-
  # firing rule _wd_land_in_flight applies to its log mtime 40 lines above. Reachable via a clock
  # that steps forward (VM resume, a bad NTP source) while spokes stamp dispatch, then corrects.
  # This screen is ALSO why this cannot simply call _afk_ceiling_epoch: the reaper's base has no
  # such bound, and tier-2 exists to not go blind in the same breath as tier-1.
  [ "$now" -gt 0 ] && [ "$d" -gt "$now" ] && d=0
  [ "$now" -gt 0 ] && [ "$p" -gt "$now" ] && p=0
  # Ties go to progress: at equal epochs it is the more specific statement (the spoke actually
  # committed), and it keeps the label stable for a spoke whose first commit lands on its
  # dispatch second.
  if [ "$p" -gt 0 ] && [ "$p" -ge "$d" ]; then printf 'progress\t%s\n' "$p"; return 0; fi
  [ "$d" -gt 0 ] && printf 'dispatch\t%s\n' "$d"
  return 0
}

_wd_detect_dead_idle() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}" \
        base="${5-$(_wd_dead_idle_base "$2" "$3")}"
  command -v slot_state >/dev/null 2>&1 || return 1
  [ -z "$(_spoke_pane_target "$wt" 2>/dev/null)" ] || return 1   # a live pane is the reaper's job
  [ -n "$done_epoch" ] && return 1                               # done-stamped ⇒ not a reaper miss
  # done ⇒ terminal, waiting ⇒ PARKED — neither is a hang. `waiting` must be excluded explicitly
  # now that the base falls back to dispatch (#297): a spoke parked at a gate before its first
  # commit has no progress epoch, so the old progress-only base made this structurally silent for
  # it; measuring from dispatch arms it, and the revive seam checks slot_state nowhere — a spoke
  # parked at an UNAPPROVED plan gate would be resumed headless and implement unreviewed work.
  # A parked spoke is conditions 1/3's (park-unanswered / stale-marker), exactly as the drain's own
  # recover_dead_panes skips it: `case "$state" in done | waiting) continue`.
  case "$(slot_state "$wt" "$issue")" in done | waiting) return 1 ;; esac
  _wd_epoch_stale "${base#*$'\t'}" "$now" "$HUB_WATCHDOG_IDLE_CEILING"
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
# The base is read from the SAME _wd_dead_idle_base the detector measured, and the label names
# which clock won (#297): `base=dispatch@N` says the spoke never committed at all — a materially
# different story from a stalled-after-progress `base=progress@N`, and the first thing a human needs
# when triaging. The activity noun follows the base for the same reason: reporting "last progress"
# for a spoke that never made any is how a reader concludes the epoch is broken rather than absent.
# Takes the detector's ALREADY-MEASURED base as $5 for the same reason it takes the pre-read done
# epoch as $4: a live re-read here can disagree with the epoch that actually fired (a concurrent
# revive stamping progress, or a fresh arm clearing both), emitting a self-contradictory ledger line
# — "ceiling 3600s breached" beside a 5s age. Falls back to a live read for a direct caller.
_wd_dead_idle_reason() {
  local wt="$1" issue="$2" now="$3" done_epoch="${4-$(_wd_done_epoch "$2")}" \
        base="${5-$(_wd_dead_idle_base "$2" "$3")}" kind epoch
  kind="${base%%$'\t'*}"; epoch="${base#*$'\t'}"
  # The activity noun follows the base: reporting "last progress" for a spoke that never made any
  # is how a reader concludes the epoch is BROKEN rather than absent — the opposite of the #290 AC4
  # diagnosability this line exists for.
  case "$kind" in
    dispatch)
      printf 'reaper missed a dead/idle pane: no pane, dispatched (no commit yet) %s ago' \
        "$(_wd_age_seconds "$epoch" "$now")" ;;
    progress)
      printf 'reaper missed a dead/idle pane: no pane, last progress %s ago' \
        "$(_wd_age_seconds "$epoch" "$now")" ;;
    *)
      kind="none"; epoch=""
      printf 'reaper missed a dead/idle pane: no pane, no dispatch or progress epoch to measure from' ;;
  esac
  printf ' (ceiling %ss; base=%s@%s, slot_state=%s, done-epoch=%s, last-action=%s)' \
    "$HUB_WATCHDOG_IDLE_CEILING" "$kind" "${epoch:-?}" "$(slot_state "$wt" "$issue" 2>/dev/null)" \
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

# _wd_terminal_marker_kind <wt> <issue> -> `ready`, `accept`, or empty — WHICH terminal marker sits
# at the branch tip (#292). slot_state reads BOTH as `done`, which is why condition 4 could not tell
# them apart: ready/ means the drain SHOULD have landed and did not (a genuine shortfall, #274's
# class), while accept/ is the deliberate human-eyeball terminal the drain MUST NOT land (auto_land
# lands only _ready_at_tip: "accept/ awaits a human sign-off").
# READY IS CHECKED FIRST, mirroring slot_state's own `for kind in ready accept` precedence: when
# both tags sit at the tip, the watchdog must classify off the SAME marker that made the spoke read
# `done`, or the two disagree about what the spoke even is.
# Empty when neither is at the tip (or the probe fails) — callers must then keep the historical
# afk-defect path, never the quieter accept one: an unreadable marker must not silence a defect.
_wd_terminal_marker_kind() {
  local wt="$1" issue="$2" kind
  for kind in ready accept; do
    _wd_tag_at_tip "$wt" "$kind" "$issue" && { printf '%s\n' "$kind"; return 0; }
  done
  return 0
}

# _wd_log_terminal_kind <issue> -> `ready`, `accept`, or empty — the terminal marker kind read from
# the #300 transition log's RECORDED state instead of the tip tag (#303/#292). spoke-ready records
# `accepted` as a state DISTINCT from `ready` (its _tlog_state_for_kind), so this is the log-native,
# structurally-unambiguous classifier: a human-sign-off close is `accepted`, which the caller routes
# to the non-escalating accept-unsigned path — a tag that is stale or mid-move can no longer make it
# read as a ready drain shortfall. Empty when the log records no terminal state (unknown, or a
# non-terminal state like landing/pushing), so the caller falls back to the tip-tag probe
# (_wd_terminal_marker_kind) — never firing NOR reclassifying on unknown alone (#300 contract). Maps
# the recorded `accepted` to the classifier's `accept` vocabulary so both readers speak one language.
_wd_log_terminal_kind() {
  case "$(_wd_current_state "$1")" in
    ready)    printf 'ready\n' ;;
    accepted) printf 'accept\n' ;;
  esac
}

# _wd_terminal_onset <issue> -> the onset ts of the last transition ONLY when it is a terminal marker
# (ready|accepted); empty otherwise (#303). This is the log-native replacement for condition 4's
# done-epoch staleness base: the transition is stamped when the spoke actually reached ready/accepted
# (spoke-ready), whereas the done epoch is a slot_state side-effect that pre-ages during a pre-ready
# park (#263). Empty for a non-terminal/unknown state so the caller falls back to the done epoch —
# the log adds precision on the healthy path without ever becoming a firing basis on its own.
_wd_terminal_onset() {
  case "$(_wd_current_state "$1")" in
    ready | accepted) afk_state_onset "$1" 2>/dev/null ;;
  esac
}

# _wd_base_name <wt> -> the bare base-branch name (no origin/ prefix); defaults to main.
_wd_base_name() {
  local wt="$1" base=""
  command -v wt_base_branch >/dev/null 2>&1 && base="$(wt_base_branch "$wt" 2>/dev/null || true)"
  [ -n "$base" ] || base=main
  printf '%s\n' "$base"
}

# _wd_own_commits <wt> -> how many commits the branch has that the base's history does not — i.e.
# what the spoke itself authored. Zero is the #286 close-without-code shape: the spoke judged the
# work already shipped and minted accept/ at the branch point, so a land would merge nothing and
# close the issue as SHIPPED — not the decision actually pending.
# Excludes BOTH refs/heads/<base> AND refs/remotes/origin/<base>, whichever exist, rather than
# measuring `<land-base>..HEAD`. Two ways that single-base measure lies, in opposite directions:
#   * spokes branch from origin/<base> (wt_base_start_point, "the hub's local base may lag or carry
#     unpushed work"), while _wd_land_base_ref PREFERS the local ref — so a lagging local base makes
#     a genuinely empty branch report the base's own missing commits as "own work", silently
#     dropping the zero-diff clause on exactly #292's headline scenario;
#   * and a base that has already absorbed the branch reports 0 for real authored work, which would
#     tell a human not to land it.
# Subtracting every base ref that exists answers the question actually asked — did this spoke write
# anything the base does not already have — under both skews.
# Empty when nothing is measurable, so the reason OMITS the claim rather than asserting a count it
# did not measure: silence is the safe direction here, a wrong "no own commits" is not.
_wd_own_commits() {
  local wt="$1" base ref n
  local -a nots=()
  base="$(_wd_base_name "$wt")"
  for ref in "refs/heads/$base" "refs/remotes/origin/$base"; do
    git -C "$wt" show-ref --verify --quiet "$ref" 2>/dev/null && nots+=("$ref")
  done
  [ "${#nots[@]}" -gt 0 ] || return 0
  n="$(git -C "$wt" rev-list --count HEAD --not "${nots[@]}" 2>/dev/null)" || return 0
  case "$n" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$n"
}

# _wd_accept_unsigned_reason <wt> <issue> [conflicts] -> the honest remediation for an accept/ spoke.
# NOT "human land": on #286 that told a human to land a zero-diff branch, which would have closed
# the issue as shipped when the pending decision was "confirm the duplicate finding, or reject it
# and re-kick". Same reason-honesty contract #285 established for the conflicted case, one
# marker-kind over — and for the same reason it must ALSO carry #285's conflict list when there is
# one: accept/ is normally eyeball-THEN-land, and condition 4 is the only thing that probes
# mergeability for it (auto_land never touches an accept/ branch, so it never routes a conflict
# resolution either). Dropping the files would send a human who approves the code straight into an
# unannounced conflict — exactly the misdirection #285 exists to prevent.
# <conflicts> and <base> are passed in already-measured rather than re-probed: the base that is
# PRINTED must be the one the count was measured against (the measured-base contract this file
# documents at _wd_dead_idle_reason), and the reason re-runs every tick as a _wd_fire argument.
_wd_accept_unsigned_reason() {
  local wt="$1" issue="$2" conflicts="${3:-}" own base
  base="$(_wd_land_base_ref "$wt")"
  printf 'accept/%s awaits human sign-off (escalate-only: the drain must NOT land accept/ — it is the human-eyeball terminal)' "$issue"
  own="$(_wd_own_commits "$wt")"
  [ "$own" = "0" ] && printf ' — no own commits vs %s: confirm close-without-code, do not land' "$base"
  [ -n "$conflicts" ] && printf ' — NOTE: the branch also conflicts with %s on: %s — resolve on the spoke before any land' \
    "$base" "$conflicts"
  return 0
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
# #303 (#300 step 4): staleness is measured from the RECORDED terminal transition (ready/accepted
# onset, _wd_terminal_onset) — stamped by spoke-ready when the spoke actually reached the state — in
# preference to the DONE epoch. The done epoch is a slot_state side-effect and remains the FALLBACK
# when the log is unknown/absent (never fire on unknown alone, #300): a standalone watchdog, or a
# spoke that pre-dates the writer layer, still measures from the epoch exactly as before. Both bases
# already dodge the #263 pre-ready-park pre-aging the progress epoch caused — the done epoch is
# stamped on the first `done` tick, the transition when the state is entered, so neither reads stale
# during the park. The LAND-lane servicing defer (#285 AC5) and the mergeability probe both live in
# the DISPATCHER, NOT here: a servicing tick must neither fire NOR clear the fire-dedup marker
# (clearing mid-service would let one persistent conflict re-fire and double-count, #263), so it is
# gated BEFORE this detector — mirroring how the answer lane defers the INTERVENTION, not the detector.
_wd_detect_mergeable_skipped() {
  local wt="$1" issue="$2" now="$3" base
  command -v slot_state >/dev/null 2>&1 || return 1
  [ "$(slot_state "$wt" "$issue")" = "done" ] || return 1
  _wd_tag_at_tip "$wt" blocked "$issue" && return 1              # deliberately blocked → not a skip
  base="$(_wd_terminal_onset "$issue")"                          # #303: the recorded ready/accepted onset
  [ -n "$base" ] || base="$(read_done_epoch "$issue" 2>/dev/null)"   # unknown log → today's done-epoch base
  _wd_epoch_stale "$base" "$now" "$HUB_WATCHDOG_LAND_CEILING" || return 1
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
# _wd_revive_marker <issue> -> the once-per-window revive BUDGET marker (#297). The
# `wd-fire-dedup-` stem is deliberate: _clear_progress_state's wd-fire-dedup-* glob drops it on a
# fresh arm, so the budget gets a per-window lifetime with no edit to that file. It is a BUDGET, not
# a firing — nothing calls `_wd_clear_fired revive`, so the spawn stays spent for the whole window
# even after the condition resolves, mirroring the drain's resumed-<issue> ("a second crash
# escalates to a human", hub-afk.sh's _afk_already_resumed). The dead-pane sweep's narrower
# wd-fire-dedup-dead-pane-* glob does not match this stem.
# The DIRECTORY resolves like _wd_filed_marker's (state-dir first), NOT like _wd_fired_marker's
# (ledger-dir first): the only glob that clears this lives in _clear_progress_state and looks solely
# in _afk_state_dir, so minting the marker beside a relocated HUB_WATCHDOG_LEDGER would strand it
# past every future arm and leave that spoke permanently un-revivable. A budget that outlives its
# window is a worse failure than one cleared early — the ledger firing is what a human reads either
# way, but a stranded budget silently disables the whole lane.
_wd_revive_marker() {
  local dir
  if command -v _afk_state_dir >/dev/null 2>&1; then dir="$(_afk_state_dir)"; else dir="$(_wd_common_dir)"; fi
  printf '%s\n' "$dir/wd-fire-dedup-revive-$1"
}
_wd_already_revived() { [ -f "$(_wd_revive_marker "$1")" ]; }

# _wd_mark_revived <issue> <pid> -> record `<ts>\t<pid>` for this window's revive, non-zero when the
# record could NOT be written. The caller must then refuse to spawn: an unrecordable budget is an
# unbounded one, and this lane's failure directions are asymmetric — every other miss costs one
# deferred revive, while this one costs the #297 spawn storm (a fresh headless claude every tick,
# forever). Fail closed. The pid is the liveness half: a revive that orphans a headless run leaves
# an operator something to inspect and kill instead of hunting stray processes by hand.
_wd_mark_revived() {
  local m; m="$(_wd_revive_marker "$1")"
  mkdir -p "$(dirname "$m")" 2>/dev/null || true
  printf '%s\t%s\n' "$(_wd_now)" "${2:--}" > "$m" 2>/dev/null || return 1
  [ -f "$m" ]
}

_wd_intervene_revive() {   # claude --continue revive in the worktree
  local wt="$1" issue="$2" pid=""
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
  # #297: ONE revive per issue per armed window. The dedup marker in _wd_fire gates only the ledger
  # append, so before this the intervention re-ran every tick the condition held — and since no
  # revive advances the epoch the detector measures, and a headless claude creates no pane, the
  # condition held forever: a fresh run per minute, dozens concurrent. A second crash is now a
  # human's call, exactly as it is for the drain's own resume lane.
  #
  # NOT paired with a "the drain is working this issue" defer keyed on _wd_last_action: that record
  # is the drain's LAST action, not its CURRENT one, and is never cleared mid-window — so the
  # drain's own give-up label (`warn-park #<issue>`) would read as "busy here" and disable this lane
  # for the rest of the window, on exactly the abandoned spoke tier-2 exists to catch. The narrow
  # drain-resume overlap this would have covered is bounded to a single extra run by the budget
  # below; _wd_land_in_flight (above) still covers the mid-land race, on a freshness-bounded signal.
  if _wd_already_revived "$issue"; then
    _wd_log "deferring revive on #$issue — this window's revive budget is already spent"
    return 0
  fi
  # Claim the budget BEFORE launching, and refuse to launch if it cannot be recorded: the record is
  # the only thing bounding this lane, so an unwritable state dir must cost a revive, not restore
  # the spawn storm. The seam owns its whole launch (tests + operator overrides), so it claims here.
  if [ -n "${HUB_WATCHDOG_REVIVE_CMD:-}" ]; then
    _wd_mark_revived "$issue" "-" || { _wd_log "refusing revive on #$issue — could not record the revive budget"; return 0; }
    bash -c "$HUB_WATCHDOG_REVIVE_CMD" hub-watchdog "$wt" "$issue" >/dev/null 2>&1 || true
    return 0
  fi
  command -v claude >/dev/null 2>&1 || return 0
  # A worktree already torn down cannot be revived into. Checked BEFORE the claim: the spawn below
  # is async, so this is the one launch failure observable in time to keep the budget unspent.
  [ -d "$wt" ] || { _wd_log "deferring revive on #$issue — worktree $wt is gone"; return 0; }
  _wd_mark_revived "$issue" pending \
    || { _wd_log "refusing revive on #$issue — could not record the revive budget"; return 0; }
  # `exec` so the backgrounded subshell BECOMES claude: $! is then the revived run's own pid, not a
  # short-lived wrapper's — the pid recorded below has to be the one an operator can kill. The
  # subshell keeps the cd off the caller's cwd.
  # TRADE-OFF: a claude that dies at startup still spends the window's budget, where hub-afk's
  # resume_spoke retries (it launches a tmux window and can read the failure synchronously; we
  # detach a headless run and cannot). Bounded-without-retry is the deliberate choice: the ledger
  # firing + filed defect that precede this call are what put a human on the spoke, and retry-per-
  # tick without a backoff is the defect being fixed. The `pending` claim above stands if we die here.
  ( cd "$wt" 2>/dev/null && exec nohup claude --continue >/dev/null 2>&1 ) &
  pid=$!
  _wd_mark_revived "$issue" "$pid" || true
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
    _wd_clear_fired accept-unsigned "$issue"   # #292: same lane, same landmark → same re-arm
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

# _wd_blocked_tag_epoch <wt> <issue> -> when blocked/<issue> was raised, or empty. `creatordate`
# reads the tagger date of an ANNOTATED tag (what spoke-ready.sh actually emits: `git tag -f -a`)
# and falls back to the commit date for a lightweight one, so both shapes answer.
_wd_blocked_tag_epoch() {
  local wt="$1" issue="$2" ts
  ts="$(git -C "$wt" for-each-ref --format='%(creatordate:unix)' "refs/tags/blocked/${issue}" 2>/dev/null)"
  case "$ts" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$ts"
}

# _wd_escalation_is_live <wt> <issue> -> true when the blocked/<issue> tag at the tip belongs to the
# park episode CURRENTLY pending, rather than an older, already-answered one.
# Why this is not just "the tag is at the tip": a blocked tag is only ever cleared by a later COMMIT
# (both _clear_stale_blocked_marker and _wd_intervene_reconcile gate on the tag being a STRICT
# ancestor of the tip). A human answering clears nothing; the spoke resuming clears nothing. So a
# spoke that escalates, gets answered, resumes and re-parks on a NEW question — all before its first
# commit, the common shape, since escalations usually precede any RED/GREEN — would carry its old
# tag at the tip forever and have EVERY later park classified novel-decision. That silences real
# drain defects and inflates the #251 autonomy score: the dangerous direction, since the score's
# whole purpose is to be honest about afk's shortfalls.
# The park onset names the episode actually pending (stamp-once, re-stamped when the park's context
# changes — #283). An onset strictly NEWER than the tag means the pending park began after the
# escalation, so this is a different question and its non-answer is a real defect. Unmeasurable
# either side ⇒ trust the tag (the historic reading); ties ⇒ live, since the escalation is stamped
# during the episode it belongs to.
_wd_escalation_is_live() {
  local wt="$1" issue="$2" tag_ts onset
  tag_ts="$(_wd_blocked_tag_epoch "$wt" "$issue")"
  case "$tag_ts" in '' | *[!0-9]*) return 0 ;; esac
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  case "$onset" in '' | *[!0-9]*) return 0 ;; esac
  [ "$onset" -gt "$tag_ts" ] && return 1
  return 0
}

# _wd_classify <condition> <issue> [wt] -> the firing's class. <wt> is optional: supervisor-dead
# has no worktree, and a direct caller may omit it (the tag check is then simply skipped).
_wd_classify() {
  local condition="$1" issue="$2" wt="${3:-}"
  if [ -n "${HUB_WATCHDOG_CLASSIFY_CMD:-}" ]; then
    bash -c "$HUB_WATCHDOG_CLASSIFY_CMD" hub-watchdog "$condition" "$issue" "$wt" 2>/dev/null; return
  fi
  case "$condition" in
    accept-unsigned)
      # #292: accept/<N> at the tip is the drain behaving CORRECTLY — spoke-ready's human-eyeball
      # terminal, which auto_land deliberately never lands ("accept/ awaits a human sign-off"). The
      # wait is a by-design human decision, not a drain shortfall, so it must not auto-file a bug
      # against afk (which spawned a fresh bug-scoper per accept-spoke per run — this issue's own
      # provenance). The escalation still ledgers and still raises the landmark; only the CLASS
      # changes, and only FILING is class-gated (_wd_fire).
      # It does NOT spare the #251 autonomy score, contrary to what #292 assumed: _wd_autonomy_score
      # counts _wd_intervention_count (every ledger line), not the class-filtered _wd_defect_count,
      # so a novel-decision docks the score exactly like a defect. That is pre-existing and shared
      # with the park-unanswered escape hatch — _wd_report deliberately prints `interventions` and
      # `defects_filed` as separate figures — so making the score class-aware is a change to #251's
      # instrument, not something to smuggle in here. Tracked separately.
      printf 'novel-decision\n'; return ;;
    park-unanswered)
      # A deliberate escalation is a real human call, not an afk bug. TWO signals say so, and only
      # the second was checked before (#297): the blocked/<issue> TAG at the spoke's tip — what
      # spoke-ready actually emits, and the signal the dispatcher already trusts in
      # _wd_detect_mergeable_skipped — and the durable local record, which gate-broker-markers.sh
      # writes ONLY when that tag's push FAILS (the #109 fallback). Reading the record alone meant
      # the COMMON case (the push succeeded, so no file exists) was misfiled as an afk-defect: a
      # bogus auto-filed bug against afk, and the #251 autonomy score docked for the reasoner
      # behaving correctly.
      # Two bounds keep this from over-silencing, both in the direction that MATTERS (a wrong
      # novel-decision hides a real defect and flatters the autonomy score):
      #   * AT-TIP, not merely present — a tag the spoke has committed past is stale (#103), and
      #     live state wins, so that firing IS a real defect;
      #   * and the escalation must still be the LIVE one (see _wd_escalation_is_live).
      # NOT applied to park-undeliverable: that tag is emitted BY a delivery failure (gate-broker's
      # _escalate_blocked when the inject cannot be verified), so reading it as "a human call" would
      # silence exactly the defect class #288 AC3 added that condition to surface — the drain
      # failing to deliver an answer is afk's shortfall, not a novel human decision, and it must
      # keep filing. The durable record is skipped there for the identical reason: same writer.
      if [ -n "$wt" ] && _wd_tag_at_tip "$wt" blocked "$issue" && _wd_escalation_is_live "$wt" "$issue"; then
        printf 'novel-decision\n'; return
      fi
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
# <wt> ($4) is optional and threaded through to _wd_classify, which needs it to read the
# blocked/<issue> tag at the spoke's tip (#297). supervisor-dead has no worktree and passes none.
_wd_fire() {
  local condition="$1" issue="$2" reason="$3" wt="${4:-}" lf klass marker
  marker="$(_wd_fired_marker "$condition" "$issue")"
  [ -f "$marker" ] && return 0   # already fired this unresolved occurrence — dedupe ledger + file
  klass="$(_wd_classify "$condition" "$issue" "$wt")"
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
  local now="${1:-$(_wd_now)}" state="${2:-$(_wd_drain_state)}" wt issue wd_conflicts wd_done wd_base wd_state wd_kind wd_tag_kind
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
    # #297: measure condition 2's base ONCE per tick and thread it into both the detector and the
    # reason, exactly as wd_done is. Re-reading it in the reason lets a concurrent progress stamp or
    # a fresh arm render a base that did not fire — a ledger line that contradicts its own ceiling.
    # Per-iteration local, passed explicitly: not a tick-global (the #241 cross-pass leak trap).
    wd_base="$(_wd_dead_idle_base "$issue" "$now")"
    # #303 (#300 step 4): the spoke's last RECORDED state, read ONCE per tick and threaded into the
    # detectors below (exactly as wd_done/wd_base are). Per-iteration local, not a tick-global (the
    # #241 cross-pass leak trap). "unknown" when the log is absent — each reader falls back to its
    # side-effect inference there, never firing NOR suppressing on unknown alone (#300 contract).
    wd_state="$(_wd_current_state "$issue")"
    # Each detector: fire (deduped by _wd_fire's marker) + intervene when it trips; else clear the
    # firing marker so a genuinely resolved-then-recurring condition re-fires (#263).
    # park-undeliverable (#288 AC3) is checked FIRST: a serviced-but-never-deliverable park must
    # never ALSO read as the misleading never-attempted label.
    if _wd_detect_park_undeliverable "$wt" "$issue" "$now"; then
      _wd_fire park-undeliverable "$issue" "$(_wd_park_undeliverable_reason "$wt" "$issue" "$now")" "$wt"
      _wd_intervene_answer "$wt" "$issue"
      _wd_clear_fired park-unanswered "$issue"
    elif _wd_detect_park_unanswered "$wt" "$issue" "$now"; then
      _wd_fire park-unanswered "$issue" "$(_wd_park_unanswered_reason "$wt" "$issue" "$now")" "$wt"
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
    elif { [ "$wd_state" = landing ] || [ "$wd_state" = pushing ]; } && _wd_state_phase_fresh "$issue" "$now"; then
      # #303: the transition log RECORDS the spoke in a known multi-minute phase (landing or
      # pushing) that is still plausibly in progress (_wd_state_phase_fresh bounds it), so a gone
      # pane + stale epoch here is the phase, not a reaper miss — structurally unable to be a dead
      # pane. This is the #290/#301 residual gap: it catches what _wd_land_in_flight cannot (a
      # clobbered last-action, no fresh land log, an in-flight PUSH the land signal never covered)
      # because the state was RECORDED by the actor, not inferred from silence. The freshness bound is
      # load-bearing: without it a mid-phase crash (state stuck at landing/pushing) would silence this
      # backstop forever (#299). DEFER like the land-in-flight arm above — neither fire NOR clear the
      # dedup marker (the #263 double-count hazard). Never silent (#300): when the epoch-inference
      # WOULD have fired, log the divergence so a suppressed fire is auditable rather than invisible.
      if _wd_detect_dead_idle "$wt" "$issue" "$now" "$wd_done" "$wd_base"; then
        _wd_log "dead-pane suppressed on #$issue: transition-log state '$wd_state' — epoch-inference would have fired (divergence: log wins, #290/#300)"
      fi
      :
    elif _wd_detect_dead_idle "$wt" "$issue" "$now" "$wd_done" "$wd_base"; then
      _wd_fire dead-pane "$issue" "$(_wd_dead_idle_reason "$wt" "$issue" "$now" "$wd_done" "$wd_base")" "$wt"
      _wd_intervene_revive "$wt" "$issue"
    else
      _wd_clear_fired dead-pane "$issue"
    fi
    if _wd_detect_stale_marker "$wt" "$issue"; then
      _wd_fire stale-marker "$issue" "stale blocked/ marker the drain did not reconcile" "$wt"
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
      # #292: read WHICH terminal marker is at the tip before labeling. slot_state reads ready/ and
      # accept/ alike as `done`, but they are opposites: ready/ is a drain that should have landed
      # and didn't; accept/ is the human-eyeball terminal the drain must NOT land. Only the ready/
      # (or unreadable — never assume the quiet path) branch keeps the historical afk-defect
      # treatment. All three escalate via the SAME needs-human-land tag (#272: no second
      # tripwire-racing tag) and _wd_clear_landed_landmarks self-clears it once the human closes
      # the issue; only the reason and class differ.
      # #285: probe ACTUAL mergeability before labeling — for BOTH marker kinds. An accept/ branch
      # is normally eyeball-then-land, and this is the only probe it ever gets, so its reason must
      # name the conflicting files too or an approving human walks into an unannounced conflict.
      wd_conflicts="$(_wd_land_conflicts "$wt")"; wd_conflicts="${wd_conflicts% }"
      # #303 (#300 step 4): classify accept-vs-ready from the RECORDED transition (log-authoritative,
      # #292) with the tip-tag probe as the unknown/absent fallback — never reclassify on unknown
      # alone (#300). On a log-vs-tag disagreement, log a divergence line and let the LOG win: the
      # recorded state is what the actor set, and `accepted` is the SAFE direction (escalate-only,
      # never auto-landed), whereas a tag can be stale or mid-move. Silent on agreement (healthy path).
      wd_kind="$(_wd_log_terminal_kind "$issue")"
      wd_tag_kind="$(_wd_terminal_marker_kind "$wt" "$issue")"
      if [ -n "$wd_kind" ] && [ "$wd_kind" != "$wd_tag_kind" ]; then
        _wd_log "auto-land-skipped classify divergence on #$issue: transition-log='$wd_kind' tip-tag='${wd_tag_kind:-none}' — log wins (#292/#300)"
      fi
      [ -n "$wd_kind" ] || wd_kind="$wd_tag_kind"   # unknown log → today's tip-tag probe
      if [ "$wd_kind" = "accept" ]; then
        _wd_fire accept-unsigned "$issue" "$(_wd_accept_unsigned_reason "$wt" "$issue" "$wd_conflicts")" "$wt"
        _wd_clear_fired auto-land-skipped "$issue"   # not a drain skip → drop any stale skip firing
        _wd_clear_fired conflicted-land "$issue"
      else
        # A conflicted ready/ branch fires a DISTINCT `conflicted-land` reason naming the files (a
        # human following "mergeable" walks into the same conflict); a truly-mergeable one keeps the
        # historical auto-land-skipped. All three escalate via the SAME needs-human-land tag (no
        # second tripwire-racing tag, #272) — only the reason and class differ, so the
        # ledger/defect is honest about what the human must do.
        if [ -n "$wd_conflicts" ]; then
          _wd_fire conflicted-land "$issue" "branch conflicts with $(_wd_land_base_ref "$wt") on: $wd_conflicts — resolve on the spoke (merge the base branch), do not blind-land" "$wt"
          _wd_clear_fired auto-land-skipped "$issue"   # not a clean skip → drop any stale skip firing
        else
          _wd_fire auto-land-skipped "$issue" "mergeable branch un-landed > ${HUB_WATCHDOG_LAND_CEILING}s (escalate-only: human land)" "$wt"
          _wd_clear_fired conflicted-land "$issue"     # cleanly mergeable now → drop any stale conflict firing
        fi
        _wd_clear_fired accept-unsigned "$issue"       # a ready/ tip is not an accept wait
      fi
      _wd_intervene_landmark "$wt" "$issue"
    else
      _wd_clear_fired auto-land-skipped "$issue"
      _wd_clear_fired conflicted-land "$issue"
      _wd_clear_fired accept-unsigned "$issue"
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
# _wd_daemon_is_stale <genfile> -> true when a LIVE daemon's recorded generation stamp differs
# from the CURRENT origin hash (#296 AC2). An unreadable/empty genfile — an old daemon that
# predates this stamp, or the very first arm — reads as UNMEASURABLE, never stale: this mirrors
# the fail-safe default everywhere else in this file (an epoch we can't read never fires, an
# unknown park lane never fires). We only ever recycle a daemon we can PROVE is behind.
_wd_daemon_is_stale() {
  local genfile="$1" recorded cur
  [ -f "$genfile" ] || return 1
  recorded="$(cat "$genfile" 2>/dev/null)"
  [ -n "$recorded" ] || return 1
  cur="$(_wd_source_hash)"
  [ -n "$cur" ] || return 1
  [ "$cur" != "$recorded" ]
}

# _wd_recycle_stale_pid <pid> -> TERM a live daemon _wd_daemon_is_stale already proved is
# behind, wait a bounded grace for its own EXIT trap to release the pidfile, then KILL a
# survivor. Falls through either way — the caller's normal claim path below reclaims the
# pidfile exactly as it already does for a plain dead pid.
_wd_recycle_stale_pid() {
  local pid="$1" grace="${HUB_WATCHDOG_RECYCLE_GRACE:-5}" waited=0
  case "$grace" in '' | *[!0-9]*) grace=5 ;; esac
  kill -TERM "$pid" 2>/dev/null
  while [ "$waited" -lt "$grace" ] && _wd_pid_alive "$pid"; do
    sleep 1 2>/dev/null || true
    waited=$((waited + 1))
  done
  _wd_pid_alive "$pid" && kill -KILL "$pid" 2>/dev/null
  return 0
}

# _wd_singleton_guard <pidfile> <genfile> -> 0 (proceed: claim/relaunch) when the pidfile is
# absent, names a dead pid, or names a live pid _wd_daemon_is_stale can PROVE is behind (which
# this also recycles, as a side effect, before returning). 1 (refuse — a live, unmeasurable-or-
# current daemon already holds the slot) otherwise. Shared by both real entry points (#296
# AC2 review): _wd_daemon (the foreground `--daemon` loop) and _wd_arm (the detached `--arm`
# launcher the drain actually calls every tick) must agree on this decision, or a fix to one
# and not the other leaves the daemon un-recyclable through the path that matters in production.
_wd_singleton_guard() {
  local pidfile="$1" genfile="$2" pid
  [ -f "$pidfile" ] || return 0
  pid="$(cat "$pidfile" 2>/dev/null)"
  _wd_pid_alive "$pid" || return 0
  if _wd_daemon_is_stale "$genfile"; then
    _wd_log "live daemon (pid $pid) is running a generation a land already replaced — recycling"
    _wd_recycle_stale_pid "$pid"
    return 0
  fi
  return 1
}

# _wd_daemon [--reexec] -> singleton wrapper around _wd_loop. _wd_singleton_guard makes N arms
# arm exactly one daemon: refuse (leaving the other daemon's pidfile alone) unless the pidfile
# is absent, dead, or PROVEN stale (recycled as a side effect, #296 AC2). A re-exec keeps this
# pid, so `--reexec` reclaims our own file instead of guarding at all. Loop output appends to
# the logfile so a recovery is auditable after the fact. Always returns 0.
_wd_daemon() {
  local reexec="${1:-}" pidfile logfile genfile pid baseline
  pidfile="$(_wd_pidfile)"
  logfile="$(_wd_logfile)"
  genfile="$(_wd_genfile)"
  if [ "$reexec" != "--reexec" ] && ! _wd_singleton_guard "$pidfile" "$genfile"; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    printf '%s\n' "hub-watchdog: already running (pid $pid, pidfile $pidfile)"
    return 0
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
  printf '%s' "$baseline" > "$genfile" 2>/dev/null || true
  _wd_loop "$baseline" >> "$logfile" 2>&1
  return 0
}

# _wd_arm -> nohup-detach the daemon so it survives THIS session's death (the whole point:
# ScheduleWakeup/Monitor die with the session; an OS process does not). Shares _wd_singleton_
# guard with _wd_daemon (#296 AC2 review): a live pidfile means one is already armed UNLESS its
# generation is PROVEN stale, in which case the guard recycles it and we relaunch fresh — this
# is the real entry point the drain calls every tick, so the recycle logic is inert everywhere
# else if it only lived in _wd_daemon's own guard. Best-effort; never fails the caller (the
# drain co-arms it each tick, so a transient miss self-heals).
_wd_arm() {
  local pidfile pid
  pidfile="$(_wd_pidfile)"
  if ! _wd_singleton_guard "$pidfile" "$(_wd_genfile)"; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    printf '%s\n' "hub-watchdog: already armed (pid $pid)"
    return 0
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
    -h | --help) sed -n '2,53p' "$_WD_SELF" ;;
    *)         _wd_status ;;
  esac
fi
