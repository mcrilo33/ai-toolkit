#!/usr/bin/env bash
# hub-otel-watch.sh — keep the OTel collector + bridge alive for the spoke lifetime.
#
# The otelcol collector (:4317/:4318/:4418/:8889) and the Langfuse message bridge
# (:4319) must be up the WHOLE time any spoke runs, or that spoke's traces are lost
# (issue #115). worktree-new.sh only ensures them once, at spawn — nothing restarts a
# collector that crashes mid-run, and a spoke relaunched outside worktree-new.sh (a
# manual dead-pane relaunch) runs no preflight and streams into a dead port.
#
# This is the hub-side watchdog (sibling of hub-ready-watch.sh): each run, when ≥1
# spoke pane is live, it ensures both are up — RECYCLING a dead/stale one via
# worktree-lib's ensure paths (wt_otel_collector_preflight now removes an
# Exited/Created/Dead lf-collector before relaunching, #115) — and otherwise does
# nothing. One-shot by default (run it on the hub, e.g. on a /loop); with
# `--daemon` it self-loops for the spoke lifetime and exits when the last spoke
# pane closes — worktree-new.sh arms that mode automatically at spawn (#138), so
# a machine sleep/wake no longer needs a human to re-arm capture.
#
# Best-effort and idempotent: it reuses the SAME ensure paths as worktree-new.sh, so
# it never starts a second collector/bridge and never errors out the loop. A quiet
# no-op when no spoke runs or both are already healthy. Opt-in: the preflights are a
# no-op unless AI_TOOLKIT_OTEL=1, and they warn (rather than recover) when
# LANGFUSE_BASIC_AUTH is unset — so run this with the same env a spoke gets.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- source worktree-lib.sh (wt_realpath + the OTel ensure paths) --------------
# Same dual-layout resolution as hub-afk.sh: the ai-toolkit checkout
# (scripts/worktree-lib.sh, four levels up from this hub script) and a synced target
# (co-located flat in .ai-toolkit/scripts/). HUB_OTEL_WT_LIB wins for tests.
_TOPLEVEL="$(git rev-parse --show-toplevel 2>/dev/null || true)"
HUB_OTEL_RESOLVED_WT_LIB=""
for _cand in \
  "${HUB_OTEL_WT_LIB:-}" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_TOPLEVEL:+$_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_TOPLEVEL:+$_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; HUB_OTEL_RESOLVED_WT_LIB="$_cand"; break; fi
done
unset _cand

# --- self-recycle source bundle (#190) ----------------------------------------
# The daemon is itself a long-running process running the bash it was started with;
# a land rewrites these files on the hub checkout. The bundle is this script plus
# the worktree-lib.sh it sourced (where the ensure paths live). It is stamped at
# daemon start and re-checked each tick — see _watch_source_hash / _watch_reexec.
_HUB_OTEL_SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
_HUB_OTEL_SOURCE_FILES=("$_HUB_OTEL_SELF" "$HUB_OTEL_RESOLVED_WT_LIB")

# The hub checkout — the repo_root the preflights launch/mount the collector and
# bridge against. Overridable (tests set it directly); resolves to the current
# worktree's main root otherwise.
MAIN_ROOT="${MAIN_ROOT:-$(wt_main_root 2>/dev/null || git rev-parse --show-toplevel 2>/dev/null || true)}"

# _spoke_worktree_paths -> the linked (spoke) worktree paths, one per line: every
# `git worktree` EXCEPT the hub main checkout itself (compared canonically so a
# symlinked root — /tmp → /private/tmp on macOS — doesn't misclassify the hub as a
# spoke). Split out so spoke_pane_live is unit-testable without a real repo.
_spoke_worktree_paths() {
  local main_rp p
  main_rp="$(wt_realpath "$MAIN_ROOT")"; main_rp="${main_rp:-$MAIN_ROOT}"
  git -C "$MAIN_ROOT" worktree list --porcelain 2>/dev/null \
    | awk '/^worktree /{print $2}' \
    | while IFS= read -r p; do
        [ -n "$p" ] || continue
        [ "$(wt_realpath "$p")" != "$main_rp" ] && printf '%s\n' "$p"
      done
}

# _pane_paths -> the current path of every tmux pane across all sessions, one per
# line (empty when tmux is absent). Split out so spoke_pane_live is testable with no
# real tmux.
_pane_paths() {
  command -v tmux >/dev/null 2>&1 || return 0
  tmux list-panes -a -F '#{pane_current_path}' 2>/dev/null
}

# spoke_pane_live -> rc 0 when at least one tmux pane sits inside a spoke worktree
# (i.e. a spoke is running), else rc 1. Paths are canonicalized on both sides
# (wt_realpath, falling back to the literal when a path can't be resolved) so a
# symlinked worktree root still correlates its pane. Best-effort: no spokes, no
# panes, or no tmux all read as "no spoke live".
spoke_pane_live() {
  local spokes canon="" s pane rp
  spokes="$(_spoke_worktree_paths)"
  [ -n "$spokes" ] || return 1
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    rp="$(wt_realpath "$s")"; canon+="${rp:-$s}"$'\n'
  done <<<"$spokes"
  while IFS= read -r pane; do
    [ -n "$pane" ] || continue
    rp="$(wt_realpath "$pane")"; rp="${rp:-$pane}"
    grep -qxF "$rp" <<<"$canon" && return 0
  done < <(_pane_paths)
  return 1
}

# ensure_otel_stack -> bring the collector up, THEN the bridge, against MAIN_ROOT via
# the shared preflights. Order matters: the collector forks LLM I/O + audit events to
# the bridge, so it must be ensured first (wt_otel_collector_preflight's documented
# contract). Each preflight is idempotent — up-and-current is left untouched, a
# dead/stale one is recycled, and a down one (re)launched.
ensure_otel_stack() {
  wt_otel_collector_preflight "$MAIN_ROOT"
  wt_otel_bridge_preflight "$MAIN_ROOT"
}

# _ensure_or_notice -> the live-tick action, shared by the one-shot main and the
# daemon loop. When native OTel is opted out (AI_TOOLKIT_OTEL != 1) the preflights
# would silently no-op and the live spoke's traces are lost — the exact footgun
# #115 exists to prevent — so surface a one-line stderr notice in that case (it is
# NOT the "no spoke / already healthy" silent path). Otherwise ensure the stack.
_ensure_or_notice() {
  if [ "${AI_TOOLKIT_OTEL:-}" != "1" ]; then
    printf '%s\n' "hub-otel-watch: a spoke is live but AI_TOOLKIT_OTEL!=1 — collector/bridge not ensured; that spoke's traces are lost (export AI_TOOLKIT_OTEL=1 to enable)" >&2
    return 0
  fi
  ensure_otel_stack
}

# main -> one-shot: ensure the stack exactly when a spoke is live; a silent no-op
# otherwise. Always returns 0: the watchdog must never error out the /loop that
# drives it.
main() {
  spoke_pane_live || return 0
  _ensure_or_notice
  return 0
}

# --- daemon mode (#138) -------------------------------------------------------
# Machine sleep kills the collector out from under live spokes and nothing
# re-arms on wake unless a human remembers to /loop this script. `--daemon` makes
# the loop self-driving: worktree-new.sh arms it at every spoke spawn
# (wt_otel_watch_arm), it re-ensures the stack each tick while ≥1 spoke pane is
# live, and it tears itself down once the last spoke pane has been gone for the
# idle grace. A nohup-detached loop is suspended across sleep and resumes on
# wake, so the first post-wake tick recycles a dead collector with no human in
# the loop.

# The git common dir of the hub — shared across worktrees, per-repo — where the
# daemon's pidfile and logfile default to (same home as hub-ready-watch's seen
# file). Falls back to /tmp when MAIN_ROOT is not a repo (never fails the arm).
_watch_common_dir() {
  local d
  d="$(git -C "$MAIN_ROOT" rev-parse --git-common-dir 2>/dev/null)" || { echo /tmp; return; }
  case "$d" in
    /*) ;;
    *) d="$MAIN_ROOT/$d" ;;
  esac
  printf '%s\n' "$d"
}

# Timestamped log line (LC_ALL=C: locale-formatted dates have burned us before).
_watch_log() { printf 'hub-otel-watch: [%s] %s\n' "$(LC_ALL=C date '+%F %T')" "$*"; }

# _watch_source_hash -> the current stamp of the daemon's own source bundle
# (delegates to worktree-lib's wt_source_hash). Split out so the self-recycle
# decision is overridable in tests without real files.
_watch_source_hash() { wt_source_hash "${_HUB_OTEL_SOURCE_FILES[@]}"; }

# _watch_reexec -> replace this daemon with a fresh copy running the on-disk
# (post-land) code. `exec` preserves the pid, so the pidfile keeps naming a live
# process and no second daemon is armed; the `--reexec` flag (passed ONLY here, not
# an ambient env var) tells the new _daemon to reclaim its own pidfile rather than
# refuse as "already running". First `bash -n`-checks the whole bundle: a dead
# watchdog is worse than a stale one (#115), so if a land shipped a parse-broken
# script we keep running the current (working) code and return — the loop retries
# next tick until a good version lands. Split out so the recycle branch is testable.
_watch_reexec() {
  local f
  for f in "${_HUB_OTEL_SOURCE_FILES[@]}"; do
    [ -f "$f" ] || continue
    if ! bash -n "$f" 2>/dev/null; then
      _watch_log "on-disk source changed but $f fails to parse — NOT re-exec'ing; keeping current code"
      return 0
    fi
  done
  _watch_log "source changed on disk (a land) — re-exec'ing into fresh code"
  exec bash "$_HUB_OTEL_SELF" --daemon --reexec
}

# _watch_loop -> tick every HUB_OTEL_WATCH_INTERVAL seconds (default 30): on a
# live tick run the same ensure path as the one-shot main (recovery output —
# "→ started lf-collector…" — flows through to the caller/logfile, which is what
# makes a recovery observable); on an idle tick count toward the exit grace.
# Exits 0 after HUB_OTEL_WATCH_IDLE_TICKS consecutive idle ticks (default 3 —
# grace for transient tmux blips and the spawn race); a live tick resets the
# counter. Never fatal: ensure failures are best-effort and the loop keeps going.
_watch_loop() {
  local baseline="${1:-}" cur
  local interval="${HUB_OTEL_WATCH_INTERVAL:-30}" max_idle="${HUB_OTEL_WATCH_IDLE_TICKS:-3}" idle=0
  _watch_log "watch loop started (pid $$, interval ${interval}s, idle grace ${max_idle} ticks)"
  while :; do
    if spoke_pane_live; then
      idle=0
      _ensure_or_notice
      # #190: a land rewrote our own source on disk → re-exec into it so the ensure
      # paths we run go live with no human recycle. Only on a spoke-live tick (idle
      # ticks are about to tear down anyway), and only when the fresh stamp is
      # present AND differs — a content hash so an identical rewrite never flaps, and
      # a transient empty stamp (hasher blip) is not mistaken for a change. Empty
      # baseline (no hasher / one-shot callers) opts out entirely.
      cur="$(_watch_source_hash)"
      if [ -n "$baseline" ] && [ -n "$cur" ] && [ "$cur" != "$baseline" ]; then
        _watch_reexec  # exec's into fresh code; returns only if it won't parse
      fi
    else
      idle=$((idle + 1))
      if [ "$idle" -ge "$max_idle" ]; then
        _watch_log "no spoke pane live for ${max_idle} ticks — exiting"
        return 0
      fi
    fi
    sleep "$interval"
  done
}

# _daemon -> singleton wrapper around _watch_loop. The pidfile (default
# <git-common-dir>/hub-otel-watch.pid, HUB_OTEL_WATCH_PIDFILE override) makes N
# spoke spawns arm exactly one watchdog: when it names a still-live pid (kill -0
# — NOT pgrep, whose locale failure reads as "not running") refuse to start and
# leave the other daemon's pidfile alone; a stale pidfile (dead pid) is
# reclaimed. The loop's output appends to the logfile (default
# <git-common-dir>/hub-otel-watch.log, HUB_OTEL_WATCH_LOG override) so a
# recovery is auditable after the fact. Always returns 0.
_daemon() {
  local reexec="${1:-}" common pidfile logfile pid baseline
  common="$(_watch_common_dir)"
  pidfile="${HUB_OTEL_WATCH_PIDFILE:-$common/hub-otel-watch.pid}"
  logfile="${HUB_OTEL_WATCH_LOG:-$common/hub-otel-watch.log}"
  # A re-exec (self-recycle into post-land code) keeps this pid, so the pidfile it
  # left behind names a live process — us. The `--reexec` flag, passed ONLY by
  # _watch_reexec, tells us to reclaim our own file instead of refusing; a fresh arm
  # never passes it, so an ambient env can't bypass the singleton guard.
  if [ "$reexec" != "--reexec" ] && [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      printf '%s\n' "hub-otel-watch: already running (pid $pid, pidfile $pidfile)"
      return 0
    fi
  fi
  printf '%s' "$$" >"$pidfile"
  # Claimed: from here on this shell owns the pidfile, so remove it on exit. The
  # path rides a global — a function-local is out of scope when the trap fires.
  _WATCH_PIDFILE="$pidfile"
  trap 'rm -f "$_WATCH_PIDFILE"' EXIT
  printf '%s\n' "hub-otel-watch: daemon armed (pid $$, log $logfile)"
  # Stamp the source bundle NOW so the loop re-execs when a later land moves it.
  baseline="$(_watch_source_hash)"
  _watch_loop "$baseline" >>"$logfile" 2>&1
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    --daemon) _daemon "${2:-}" ;;
    *) main "$@" ;;
  esac
fi
