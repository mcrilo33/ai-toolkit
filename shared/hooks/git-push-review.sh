#!/usr/bin/env bash
# git-push-review — PreToolUse hook
# Warns before git push by showing a summary of what will be pushed.
# Non-blocking: writes to stderr so the agent sees the context.
#
# Exit 0 = allow (always, this is advisory)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_bash_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git push commands
echo "$COMMAND" | grep -qE '^\s*git\s+push\b' || exit 0

# Show what's about to be pushed
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
REMOTE=$(echo "$COMMAND" | grep -oE 'git push\s+(\S+)' | awk '{print $3}')
REMOTE="${REMOTE:-origin}"

# Count unpushed commits
UNPUSHED=$(git log --oneline "${REMOTE}/${BRANCH}..HEAD" 2>/dev/null | head -10) || true
COUNT=0
if [ -n "$UNPUSHED" ]; then
  COUNT=$(echo "$UNPUSHED" | wc -l | tr -d ' ')
fi

if [ "$COUNT" -gt 0 ]; then
  warn "── git push review ──"
  warn "Branch: $BRANCH → $REMOTE"
  warn "Commits to push ($COUNT):"
  echo "$UNPUSHED" | while IFS= read -r line; do
    warn "  $line"
  done
  warn "─────────────────────"
fi

# Check for force push without lease
if echo "$COMMAND" | grep -qE '\b--force\b' && ! echo "$COMMAND" | grep -qE '\b--force-with-lease\b'; then
  warn "⚠ Force push detected. Consider using --force-with-lease instead."
fi

exit 0
