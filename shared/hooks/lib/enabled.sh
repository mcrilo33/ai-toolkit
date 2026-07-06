#!/usr/bin/env bash
# enabled.sh — the ONE canonical resolver for the toolkit's global on/off switch
# (issue #154). Sourced by BOTH enforcement surfaces so they can never disagree
# on whether the toolkit is "on":
#   - the native git hooks (commit-msg, pre-push) via install-git-hooks.sh, and
#   - the Claude Code hooks, via lib/utils.sh (which every guard/marker/telemetry
#     hook sources).
# Also the toggle CLI (on|off|status|check), exposed on $PATH as `ai-toolkit`.
#
# It is intentionally self-contained (no utils.sh / telemetry.sh deps) — mirroring
# base-branch.sh (issue #117) — so a hook can source it before anything else and a
# disabled toolkit skips even the telemetry-arming that utils.sh does at source.
#
# Precedence (first-decisive) — like base-branch.sh, but with the MARKER made
# DECISIVE over git config: sync-to-repo.sh re-materializes
# `git config ai-toolkit.enabled` from settings/ai-toolkit.yml on every sync, so a
# manual off stored in git config would be silently clobbered by the next sync.
# The <git-common-dir> marker is sync-safe, so it wins:
#   1. <git-common-dir>/ai-toolkit-off present            ⇒ DISABLED (decisive)
#   2. else git config --local --get ai-toolkit.enabled   ⇒ false/0/off ⇒ DISABLED
#                                                            true/1/on   ⇒ ENABLED
#   3. else                                                ⇒ ENABLED (default)
#
# The toggle is scoped to the CLONE, never machine-global: the marker lives at the
# shared git-common-dir and git config is read --local, so every linked worktree of
# the clone sees the same state. `ai-toolkit off` drops the marker (survives
# re-syncs); `ai-toolkit on` removes it.

# Absolute path to the off-marker for the repo at $1 (default "."). Prints the
# path; returns non-zero (and prints nothing) when $1 is not a git repo.
_ai_toolkit_marker() {
  local root="${1:-.}" gcd
  gcd="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$gcd" ] || return 1
  case "$gcd" in /*) ;; *) gcd="$root/$gcd" ;; esac
  printf '%s/ai-toolkit-off' "$gcd"
}

# ai_toolkit_enabled [root] → 0 (ENABLED) / 1 (DISABLED). See precedence above.
# Never fails for a non-repo: with no marker and no config it returns ENABLED, so
# a hook sourcing this outside a repo behaves exactly as today.
ai_toolkit_enabled() {
  local root="${1:-.}" marker v
  marker="$(_ai_toolkit_marker "$root" 2>/dev/null)" || marker=""
  [ -n "$marker" ] && [ -e "$marker" ] && return 1
  v="$(git -C "$root" config --local --get ai-toolkit.enabled 2>/dev/null)" || v=""
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    false|0|off|no|disabled) return 1 ;;
    true|1|on|yes|enabled)   return 0 ;;
  esac
  return 0
}

# ai_toolkit_off [root] — drop the sync-safe off marker (disable enforcement).
ai_toolkit_off() {
  local root="${1:-.}" marker
  marker="$(_ai_toolkit_marker "$root")" || { echo "ai-toolkit: not a git repository" >&2; return 1; }
  : > "$marker"
}

# ai_toolkit_on [root] — remove the off marker (revert to the config/default).
ai_toolkit_on() {
  local root="${1:-.}" marker
  marker="$(_ai_toolkit_marker "$root")" || { echo "ai-toolkit: not a git repository" >&2; return 1; }
  rm -f "$marker"
}

# ai_toolkit_status [root] — print the effective state and where it comes from.
ai_toolkit_status() {
  local root="${1:-.}" marker v
  marker="$(_ai_toolkit_marker "$root" 2>/dev/null)" || marker=""
  if [ -n "$marker" ] && [ -e "$marker" ]; then
    printf 'AI-TOOLKIT: OFF — gates/guards/telemetry bypassed (marker: %s)\n' "$marker"
    return 0
  fi
  v="$(git -C "$root" config --local --get ai-toolkit.enabled 2>/dev/null)" || v=""
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    false|0|off|no|disabled)
      printf 'AI-TOOLKIT: OFF — gates/guards/telemetry bypassed (git config ai-toolkit.enabled=%s)\n' "$v"
      return 0 ;;
  esac
  printf 'AI-TOOLKIT: ON\n'
}

# CLI dispatch — used when this file is executed (not sourced).
_ai_toolkit_cli() {
  local cmd="${1:-status}"
  shift || true
  local root="${1:-$PWD}"
  case "$cmd" in
    check)  ai_toolkit_enabled "$root" ;;
    on)     ai_toolkit_on "$root" && echo "AI-TOOLKIT: ON" ;;
    off)    ai_toolkit_off "$root" && ai_toolkit_status "$root" ;;
    status) ai_toolkit_status "$root" ;;
    -h|--help|help)
      echo "usage: ai-toolkit {on|off|status|check} [repo-root]" ;;
    *)
      echo "ai-toolkit: unknown command '$cmd' (on|off|status|check)" >&2
      return 2 ;;
  esac
}

# Run the CLI only when executed directly; when sourced, just define functions.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  _ai_toolkit_cli "$@"
fi
