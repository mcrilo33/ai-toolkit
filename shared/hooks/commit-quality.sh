#!/usr/bin/env bash
# commit-quality — PreToolUse hook
# Validates that git commit messages follow conventional commits format.
# Blocks commits with non-compliant messages.
#
# Conventional commits: type(scope): description
# Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert
#
# Exit 2 = block
# Exit 0 = allow
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_bash_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands
echo "$COMMAND" | grep -qE '^\s*git\s+commit\b' || exit 0

# Extract the commit message from -m flag
MSG=""
if echo "$COMMAND" | grep -qE '\s-m\s'; then
  # Handle: git commit -m "message" and git commit -m 'message'
  MSG=$(echo "$COMMAND" | sed -n "s/.*-m[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p")
fi

# Also handle: git commit -m message (no quotes, single word)
if [ -z "$MSG" ]; then
  MSG=$(echo "$COMMAND" | sed -n 's/.*-m[[:space:]]*\([^[:space:]-][^[:space:]]*\).*/\1/p')
fi

# If no message found (e.g. git commit --amend), allow
[ -z "$MSG" ] && exit 0

# ── Conventional commits validation ─────────────────────────────────
# Pattern: type(optional-scope): description
#   OR: type!: description (breaking change)
VALID_TYPES="feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
PATTERN="^($VALID_TYPES)(\([a-zA-Z0-9._-]+\))?(!)?: .+"

if ! echo "$MSG" | grep -qE "$PATTERN"; then
  deny "Commit message doesn't follow conventional commits format.
Expected: type(scope): description
Types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert
Example: feat(auth): add OAuth2 login flow
Got: $MSG"
fi

# Check message length (subject line should be ≤ 72 chars)
SUBJECT=$(echo "$MSG" | head -1)
if [ ${#SUBJECT} -gt 72 ]; then
  warn "Commit subject is ${#SUBJECT} chars (recommended ≤ 72): $SUBJECT"
fi

exit 0
