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
#
# Knobs (env, with defaults):
#   HUB_WATCHDOG_INTERVAL=60      loop tick interval (seconds)
#   HUB_WATCHDOG_IDLE_TICKS=3     consecutive drain-off ticks before the daemon tears down
#   HUB_WATCHDOG_PIDFILE          daemon pidfile   [default: <git-common-dir>/hub-watchdog.pid]
#   HUB_WATCHDOG_LOG              daemon logfile   [default: <git-common-dir>/hub-watchdog.log]
#   HUB_WATCHDOG_HEARTBEAT        heartbeat file   [default: <git-common-dir>/.hub-watchdog-heartbeat]
#   AFK_STATE / AFK_HEARTBEAT     the drain's state + heartbeat files it cross-checks (must
#                                 match hub-afk.sh's contract — same <git-common-dir> defaults)
#   HUB_WATCHDOG_WT_LIB           override the sourced worktree-lib.sh (tests)
set -uo pipefail

HUB_WATCHDOG_SCRIPT_DIR="${HUB_WATCHDOG_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# Absolute path to THIS script, so a re-exec / nohup-arm resolves regardless of cwd.
_WD_SELF="$HUB_WATCHDOG_SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"

# --- source worktree-lib.sh (wt_source_hash, wt_realpath) + hub-inject.sh ------
# Same dual-layout ladder as the hub siblings: the checkout (scripts/ four levels up) and a
# synced .ai-toolkit/scripts/ target (co-located). HUB_WATCHDOG_WT_LIB / AFK_WT_LIB win for tests.
_WD_TOPLEVEL="${_WD_TOPLEVEL:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
_WD_RESOLVED_WT_LIB=""
for _cand in \
  "${HUB_WATCHDOG_WT_LIB:-}" \
  "${AFK_WT_LIB:-}" \
  "$HUB_WATCHDOG_SCRIPT_DIR/worktree-lib.sh" \
  "$HUB_WATCHDOG_SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_WD_TOPLEVEL:+$_WD_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; _WD_RESOLVED_WT_LIB="$_cand"; break; fi
done
unset _cand
# hub-inject.sh — the shared injector the watchdog's answer/revive interventions use (a
# co-located sibling). Sourced best-effort; a detector that needs it existence-checks first.
_WD_RESOLVED_INJECT=""
for _cand in "${AFK_HUB_INJECT:-}" "$HUB_WATCHDOG_SCRIPT_DIR/hub-inject.sh"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; _WD_RESOLVED_INJECT="$_cand"; break; fi
done
unset _cand

# Guarded log fallback (hub-inject may already provide one; same stderr contract).
declare -F log >/dev/null 2>&1 || log() { printf '%s\n' "$*" >&2; }

# The daemon's own source bundle: this script + the libs it sources. Stamped at daemon start
# and re-checked each tick so a land of the watchdog's own code re-execs it live (#251/#190).
_WD_SOURCE_FILES=("$_WD_SELF" "$_WD_RESOLVED_INJECT" "$_WD_RESOLVED_WT_LIB")

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

# --- one tick -----------------------------------------------------------------
# _wd_tick <drain-state> -> one supervision pass. In this subtask it observes and logs the
# drain state; subtasks 3-5 add the 5 detectors, each firing → scripted intervention + a
# defect-record line in the intervention-ledger + the autonomy tally.
_wd_tick() {
  local state="${1:-$(_wd_drain_state)}"
  _wd_log "tick: drain supervisor is ${state}"
  # UPGRADE: run the 5 detection conditions here (subtask 3) — park-not-answered, dead-pane,
  # stale-marker, mergeable-skipped, supervisor-dead — each recording an intervention firing.
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
    -h | --help) sed -n '2,49p' "$_WD_SELF" ;;
    *)         _wd_status ;;
  esac
fi
