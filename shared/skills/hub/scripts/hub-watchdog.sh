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

# --- source the hub-watchdog functional modules (fail-CLOSED per #211) ---------
# hub-watchdog.sh is split into hub-watchdog-detect.sh (the _wd_detect_* conditions + readers)
# and hub-watchdog-intervene.sh (the _wd_intervene_* actions + fire/dedup/defect-filing + the
# autonomy ledger/report), issue #308, so disjoint watchdog subtasks stop colliding on one
# multi-thousand-line Scope: token (AFK Design Principle 7). These are pure function-definition
# files (no top-level work beyond the detector ceiling := guards), sourced AFTER worktree-lib /
# gate-broker / hub-inject / log and BEFORE any function is called — bash resolves calls at call
# time, so cross-module definition order (the entry's own _wd_run_conditions dispatcher included)
# does not matter. Resolution is from HUB_WATCHDOG_SCRIPT_DIR (this file's own dir) FIRST, then
# the checkout and synced .ai-toolkit/scripts layouts. FAIL-CLOSED (constraint 4 / #211): the
# modules ARE the detectors/interventions, so a required module that resolves nowhere sets
# _WD_MODULES_OK=0 and the daemon entry points REFUSE TO RUN rather than patrol blind — a
# detector-less watchdog would silently no-op every condition (AFK Design Principle 2 — fail
# loud, never silently degrade), the exact opposite of a fallback.
_WD_MODULES_OK=1
_WD_MODULE_FILES=()
for _wdmod in detect intervene; do
  _wdm=""
  for _cand in \
    "$HUB_WATCHDOG_SCRIPT_DIR/hub-watchdog-$_wdmod.sh" \
    "$HUB_WATCHDOG_SCRIPT_DIR/../../../../scripts/hub-watchdog-$_wdmod.sh" \
    "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/shared/skills/hub/scripts/hub-watchdog-$_wdmod.sh}" \
    "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/.ai-toolkit/scripts/hub-watchdog-$_wdmod.sh}"; do
    if [ -n "$_cand" ] && [ -f "$_cand" ]; then _wdm="$_cand"; break; fi
  done
  if [ -n "$_wdm" ]; then
    _WD_MODULE_FILES+=("$_wdm")
    . "$_wdm"
  else
    log "hub-watchdog: FATAL required module hub-watchdog-$_wdmod.sh missing/unreadable -- refusing to run"
    _WD_MODULES_OK=0
  fi
done
unset _wdmod _wdm _cand

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
# The #308 detect/intervene modules ride along too: a land rewriting ONLY a module (not the
# entry) must still trip the self-recycle, so their ORIGIN paths join the hashed bundle. The
# empty-array expansion is guarded for bash 3.2, where "${arr[@]}" on an empty array errors
# under set -u (the modules are required, so this is normally two entries).
for _wdmf in ${_WD_MODULE_FILES[@]+"${_WD_MODULE_FILES[@]}"}; do
  _WD_SOURCE_FILES+=("$(_wd_origin_of "$_wdmf")")
done
unset _wdmf

# _wd_require_modules -> 0 when every required module sourced, else 1 (with a loud refusal). The
# real daemon entry points (_wd_once / _wd_daemon / _wd_arm) gate on this so a missing detector/
# intervene module refuses to run rather than patrolling blind — the source loop above already
# logged which module was missing (AFK Design Principle 2 — fail loud).
_wd_require_modules() {
  [ "${_WD_MODULES_OK:-1}" = 1 ] && return 0
  log "hub-watchdog: FATAL a required module did not resolve -- refusing to run (detectors/interventions absent)"
  return 1
}

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
  _wd_require_modules || return 1
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
  _wd_require_modules || return 1
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
  _wd_require_modules || return 1
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
    --report)  _wd_require_modules && _wd_report ;;   # _wd_report lives in the intervene module (#308)
    -h | --help) sed -n '2,53p' "$_WD_SELF" ;;
    *)         _wd_status ;;
  esac
fi
