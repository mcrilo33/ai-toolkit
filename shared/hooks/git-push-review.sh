#!/usr/bin/env bash
# git-push-review — shipping-gate hook (git push).
# Shows a summary of what will be pushed. The summary is always advisory
# (logged for context). The force-push-without-lease check is a real gate:
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): a force push WITHOUT --force-with-lease is
#     a hard DENY (exit 2) — it can silently overwrite others' work.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git push commands
echo "$COMMAND" | grep -qE '^\s*git\s+push\b' || exit 0

# Resolve the repo from the payload — Cursor's beforeShellExecution reports an
# empty cwd, so the ambient working directory cannot be trusted.
PROJECT_ROOT=$(project_root_from_payload "$INPUT")

# Show what's about to be pushed
BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
# grep returns non-zero when the push has no explicit remote arg (e.g. bare
# `git push`); guard with || true so set -e does not abort the hook.
REMOTE=$(echo "$COMMAND" | grep -oE 'git push\s+(\S+)' | awk '{print $3}' || true)
REMOTE="${REMOTE:-origin}"

# Count unpushed commits
UNPUSHED=$(git -C "$PROJECT_ROOT" log --oneline "${REMOTE}/${BRANCH}..HEAD" 2>/dev/null | head -10) || true
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

# Check for force push without lease. On Cursor this is a hard DENY; elsewhere
# advisory. --force-with-lease is the safe form and is never gated here.
# Use plain substring matching (-- guards the leading dashes); the \b regex form
# does not match "--force" because both the space and the dash are non-word
# characters, so no word boundary exists between them.
if echo "$COMMAND" | grep -q -- '--force' && ! echo "$COMMAND" | grep -q -- '--force-with-lease'; then
  ship_gate_enforce "$INPUT" "Force push without --force-with-lease can silently overwrite others' work. Use 'git push --force-with-lease' instead."
fi

exit 0
