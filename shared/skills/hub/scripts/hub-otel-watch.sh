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
# nothing. Run it on the hub (main checkout), ideally on a /loop.
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
for _cand in \
  "${HUB_OTEL_WT_LIB:-}" \
  "$SCRIPT_DIR/worktree-lib.sh" \
  "$SCRIPT_DIR/../../../../scripts/worktree-lib.sh" \
  "${_TOPLEVEL:+$_TOPLEVEL/scripts/worktree-lib.sh}" \
  "${_TOPLEVEL:+$_TOPLEVEL/.ai-toolkit/scripts/worktree-lib.sh}"; do
  if [ -n "$_cand" ] && [ -f "$_cand" ]; then . "$_cand"; break; fi
done
unset _cand

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
    printf '%s\n' "$canon" | grep -qxF "$rp" && return 0
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

# main -> ensure the stack exactly when a spoke is live; a silent no-op otherwise.
main() {
  spoke_pane_live || return 0
  ensure_otel_stack
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
