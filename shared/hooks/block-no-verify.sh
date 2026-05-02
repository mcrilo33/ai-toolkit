#!/usr/bin/env bash
# block-no-verify — PreToolUse hook
# Blocks git commands that bypass hooks with --no-verify or --no-gpg-sign.
# Prevents agents from circumventing pre-commit checks.
#
# Exit 2 = block (preToolUse)
# Exit 0 = allow
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_bash_command "$INPUT")

# No command — not a Bash tool call, allow
[ -z "$COMMAND" ] && exit 0

# Check for --no-verify flag
if echo "$COMMAND" | grep -q -- '--no-verify'; then
  deny "Blocked: --no-verify bypasses git hooks. Remove the flag and let pre-commit checks run."
fi

# Check for force push without --force-with-lease
if echo "$COMMAND" | grep -q -- '--force' && ! echo "$COMMAND" | grep -q -- '--force-with-lease'; then
  if echo "$COMMAND" | grep -qE 'git\s+push'; then
    deny "Blocked: Use --force-with-lease instead of --force to avoid overwriting others' work."
  fi
fi

exit 0
