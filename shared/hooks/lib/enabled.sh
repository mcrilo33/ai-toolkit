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

# ── Per-hook granular config (issue #334) ───────────────────────────
# The per-project layer atop the global switch: each hook is enabled/disabled and
# (for commit-quality/test-select) rule-configured independently. Precedence
# mirrors the global switch — a sync-safe <git-common-dir> marker is DECISIVE over
# git config (the sync-materialized yml default):
#   1. <git-common-dir>/ai-toolkit-hook-<name>-off present ⇒ DISABLED (decisive)
#   2. else git config --local ai-toolkit.hook.<name>.enabled false/true
#   3. else ⇒ ENABLED (default = today's behavior)
# SECURITY guards default ON regardless of any blanket disable and turn off ONLY
# via their own explicit disable — surfaced LOUDLY (AFK principle #2; fail loud).
# The rule-config getters read the sync-materialized git-config keys with the
# built-in default so an unconfigured host keeps today's behavior.

# Space-separated security-guard names (kept in step with ai_toolkit_config.py).
AI_TOOLKIT_SECURITY_GUARDS="secrets-scan secrets-scan-revert block-no-verify"

_ai_toolkit_is_security_guard() {
  case " $AI_TOOLKIT_SECURITY_GUARDS " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Absolute path to the off-marker for hook $1 in the repo at $2 (default "."). Prints
# the path; returns non-zero (and prints nothing) when $2 is not a git repo.
_ai_toolkit_hook_marker() {
  local name="$1" root="${2:-.}" gcd
  gcd="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$gcd" ] || return 1
  case "$gcd" in /*) ;; *) gcd="$root/$gcd" ;; esac
  printf '%s/ai-toolkit-hook-%s-off' "$gcd" "$name"
}

# Read a per-hook git-config value (best-effort, empty when unset/non-repo).
_ai_toolkit_hook_config() {
  local key="$1" root="${2:-.}"
  git -C "$root" config --local --get "$key" 2>/dev/null || true
}

# ai_toolkit_hook_enabled <name> [root] → 0 ENABLED / 1 DISABLED. See precedence.
# Status-only (no warning): the loud security signal is emitted by
# ai_toolkit_warn_disabled_security_guards / the sync, not here.
ai_toolkit_hook_enabled() {
  local name="$1" root="${2:-.}" marker v
  marker="$(_ai_toolkit_hook_marker "$name" "$root" 2>/dev/null)" || marker=""
  [ -n "$marker" ] && [ -e "$marker" ] && return 1   # marker decisive
  v="$(_ai_toolkit_hook_config "ai-toolkit.hook.$name.enabled" "$root")"
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    false|0|off|no|disabled) return 1 ;;
  esac
  return 0   # true / unset ⇒ ENABLED (security and non-security alike)
}

# Print the loud banner for one explicitly-disabled security guard (to stderr).
_ai_toolkit_warn_security_disabled() {
  printf 'ai-toolkit: SECURITY WARNING: guard %s is explicitly DISABLED — a security guard should stay ON; remove the ai-toolkit-hook-%s-off marker / `git config ai-toolkit.hook.%s.enabled` unless deliberate (AFK #2).\n' \
    "$1" "$1" "$1" >&2
}

# Best-effort (AFK #6): warn LOUDLY for every explicitly-disabled security guard.
# NEVER fails the caller — every probe is guarded so it cannot abort a hook that
# runs this under set -e.
ai_toolkit_warn_disabled_security_guards() {
  local root="${1:-.}" name
  for name in $AI_TOOLKIT_SECURITY_GUARDS; do
    ai_toolkit_hook_enabled "$name" "$root" >/dev/null 2>&1 \
      || _ai_toolkit_warn_security_disabled "$name"
  done
  return 0
}

# Pipe-joined allowed commit types (feat|fix|...), configured or the default.
ai_toolkit_hook_commit_types() {
  local root="${1:-.}" v
  v="$(_ai_toolkit_hook_config ai-toolkit.hook.commit-quality.types "$root")"
  if [ -n "$v" ]; then
    printf '%s' "$v" | tr -s ' ,' '|' | sed 's/^|//; s/|$//'
  else
    printf 'feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert'
  fi
}

# ai_toolkit_hook_require_anchor [root] → 0 (anchor required) / 1 (not). Default required.
ai_toolkit_hook_require_anchor() {
  local root="${1:-.}" v
  v="$(_ai_toolkit_hook_config ai-toolkit.hook.commit-quality.require-anchor "$root")"
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    false|0|off|no|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

# ai_toolkit_hook_test_select_skip [root] → 0 (skip) / 1 (do not skip). Default do-not-skip.
ai_toolkit_hook_test_select_skip() {
  local root="${1:-.}" v
  v="$(_ai_toolkit_hook_config ai-toolkit.hook.test-select.skip "$root")"
  case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
    true|1|on|yes|enabled) return 0 ;;
    *) return 1 ;;
  esac
}

# Print the persistent test-select command (durable TEST_SELECT_CMD), or empty.
ai_toolkit_hook_test_select_command() {
  _ai_toolkit_hook_config ai-toolkit.hook.test-select.command "${1:-.}"
}

# ai_toolkit_hook_off/on/status <name> [root] — the sync-safe per-hook toggle,
# mirroring ai_toolkit_off/on/status (the marker survives re-syncs).
ai_toolkit_hook_off() {
  local name="$1" root="${2:-.}" marker
  marker="$(_ai_toolkit_hook_marker "$name" "$root")" || { echo "ai-toolkit: not a git repository" >&2; return 1; }
  : > "$marker"
}
ai_toolkit_hook_on() {
  local name="$1" root="${2:-.}" marker
  marker="$(_ai_toolkit_hook_marker "$name" "$root")" || { echo "ai-toolkit: not a git repository" >&2; return 1; }
  rm -f "$marker"
}
ai_toolkit_hook_status() {
  local name="$1" root="${2:-.}"
  if ai_toolkit_hook_enabled "$name" "$root"; then
    printf 'AI-TOOLKIT hook %s: ON\n' "$name"
  else
    printf 'AI-TOOLKIT hook %s: OFF (per-project disable)\n' "$name"
  fi
}

# CLI dispatch — used when this file is executed (not sourced).
_ai_toolkit_cli() {
  local cmd="${1:-status}"
  shift || true
  # Per-hook subcommands take a hook NAME as their first arg (not a repo root).
  case "$cmd" in
    hook-check)  ai_toolkit_hook_enabled "${1:?usage: hook-check <name> [root]}" "${2:-$PWD}" ;;
    hook-on)     ai_toolkit_hook_on "${1:?usage: hook-on <name> [root]}" "${2:-$PWD}" \
                   && ai_toolkit_hook_status "$1" "${2:-$PWD}" ;;
    hook-off)    ai_toolkit_hook_off "${1:?usage: hook-off <name> [root]}" "${2:-$PWD}" \
                   && ai_toolkit_hook_status "$1" "${2:-$PWD}" ;;
    hook-status) ai_toolkit_hook_status "${1:?usage: hook-status <name> [root]}" "${2:-$PWD}" ;;
    check|on|off|status|-h|--help|help) _ai_toolkit_cli_global "$cmd" "${1:-$PWD}" ;;
    *)
      echo "ai-toolkit: unknown command '$cmd' (on|off|status|check|hook-on|hook-off|hook-status|hook-check)" >&2
      return 2 ;;
  esac
}

# The global on/off switch CLI (issue #154) — unchanged behavior, split out so the
# per-hook subcommands can share one dispatch entry.
_ai_toolkit_cli_global() {
  local cmd="$1" root="${2:-$PWD}"
  case "$cmd" in
    check)  ai_toolkit_enabled "$root" ;;
    on)     ai_toolkit_on "$root" && ai_toolkit_status "$root" ;;
    off)    ai_toolkit_off "$root" && ai_toolkit_status "$root" ;;
    status) ai_toolkit_status "$root" ;;
    -h|--help|help)
      echo "usage: ai-toolkit {on|off|status|check|hook-{on,off,status,check} <name>} [repo-root]" ;;
  esac
}

# Run the CLI only when executed directly; when sourced, just define functions.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  _ai_toolkit_cli "$@"
fi
