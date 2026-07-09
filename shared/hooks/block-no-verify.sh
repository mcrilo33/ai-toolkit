#!/usr/bin/env bash
# block-no-verify — PreToolUse hook (Bash).
# Blocks git commands that bypass the hook cage with the hook-skipping flag.
#
# This hook is the SOLE defense against `git commit|push` run with the flag that
# skips verification — the one flag that disarms BOTH native cages (commit-msg
# and pre-push) by git's own design. It CANNOT be installed as a native git hook,
# because that flag skips the very hooks it would live in (see
# scripts/install-git-hooks.sh, where block-no-verify is deliberately absent from
# the native wiring). So it exists ONLY as an agent PreToolUse hook, with no
# native backstop.
#
# Because there is no backstop, this hook FAILS CLOSED. On Claude Code a hook
# exit != 2 is a non-blocking error and the tool call PROCEEDS — so a crash, or a
# malformed payload that yields no command, would otherwise let the bypass flag
# through crash-open (issue #211). Three guards prevent that:
#   1. `trap 'exit 2' ERR` — a set -e command failure (path resolution, a
#      crashing helper) re-exits 2 (deny) rather than a non-2 crash code. EXIT is
#      owned by the telemetry span in utils.sh; ERR is a separate trap slot, so
#      the two compose.
#   2. An explicit `[ -r "$LIB" ]` guard before the source: a missing/unreadable
#      lib makes `source` exit the shell as a special builtin — bypassing ERR and
#      leaving `$?` at 0 in any EXIT trap — so it is caught by hand, not a trap.
#   3. An unreadable/empty command DENIES rather than silently passing — an
#      unparseable Bash payload is exactly where a bypass would hide.
#
# The bypass flag is caught in every boundary form once the hook runs: the scan
# is position-independent, so chained/prefixed/quoted spellings
# (`cd x && git commit ...`, `VAR=1 git commit ...`, `sh -c 'git commit ...'`)
# all match. On Claude the `if:` matcher in settings.json decides WHETHER to
# invoke the hook; that prefix rule is a separate wiring surface (a spoke's Scope
# for #211 is this script, not settings.json).
#
# Exit 2 = block. Exit 0 = allow (no bypass flag present).
set -euo pipefail

# Fail closed as early as possible — before sourcing the lib — so even a
# lib-sourcing or path-resolution failure re-exits 2 (deny), not a crash-open
# non-2 code.
trap 'exit 2' ERR

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOOK_DIR/lib/utils.sh"
if [ ! -r "$LIB" ]; then
  echo "[Hook] Blocked: block-no-verify cannot load its lib ($LIB) — failing closed (this is the sole --no-verify defense)." >&2
  exit 2
fi
source "$LIB"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

# No command extracted. In normal operation the invoking matcher only fires this
# hook on a real command, so an empty extraction means a malformed payload — and
# with no native backstop the safe direction is to DENY, not to pass silently.
if [ -z "$COMMAND" ]; then
  deny "Blocked: could not read the command to check it for a hook-bypass flag. A malformed hook payload fails closed — this is the sole defense and has no native backstop."
fi

# The hook-skipping flag anywhere in the command. A position-independent scan
# catches every boundary form — `cd x && git commit ... `, `VAR=1 git commit ...`,
# `sh -c 'git commit ...'` — all of which carry the literal flag.
if printf '%s' "$COMMAND" | grep -q -- '--no-verify'; then
  deny "Blocked: --no-verify bypasses git hooks. Remove the flag and let pre-commit checks run."
fi

# Force push without --force-with-lease.
if printf '%s' "$COMMAND" | grep -q -- '--force' && ! printf '%s' "$COMMAND" | grep -q -- '--force-with-lease'; then
  if printf '%s' "$COMMAND" | grep -qE 'git[[:space:]]+push'; then
    deny "Blocked: Use --force-with-lease instead of --force to avoid overwriting others' work."
  fi
fi

exit 0
