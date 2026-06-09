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
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Note: get_shell_command reads the dedicated-event top-level .command (Cursor
# beforeShellExecution) and falls back to tool_input.command (Claude/Copilot),
# normalizing shell-escaped quotes (\" -> ", \' -> ') either way, so the
# subject-extraction below sees clean quotes regardless of how the runtime
# serialized the call.

# Only act on git commit commands
echo "$COMMAND" | grep -qE '^\s*git\s+commit\b' || exit 0

# Extract the SUBJECT from the FIRST message flag. With multiple -m flags the
# first is the subject and the rest are body/footer paragraphs (git's own
# semantics), so the conventional-commit check must target the first one.
# sed/BRE are greedy and would grab the LAST flag, so isolate the substring
# starting at the first message flag, then read its first quoted/bare value.
#
# Recognised forms: -m X, -m=X, --message X, --message=X, and combined short
# clusters where m is last (-am, -sm, -nm ...). Combined clusters where m is
# NOT last (e.g. -ma) are invalid git syntax and ignored.
MSG=""
FIRST_M=$(echo "$COMMAND" \
  | grep -oE '([[:space:]](-[a-zA-Z]*m|--message))([[:space:]=]|$).*' \
  | head -1 || true)
if [ -n "$FIRST_M" ]; then
  # Use ERE (-E): BSD/macOS sed does not support \| alternation in BRE, only
  # GNU does. ERE alternation `|` is portable across both.
  # Quoted: ... "..." or ... '...'  (first quoted token only)
  MSG=$(echo "$FIRST_M" | sed -nE "s/^[[:space:]](-[a-zA-Z]*m|--message)[[:space:]=]*[\"']([^\"']*)[\"'].*/\2/p")
  # Bare single word: ... message
  if [ -z "$MSG" ]; then
    MSG=$(echo "$FIRST_M" | sed -nE 's/^[[:space:]](-[a-zA-Z]*m|--message)[[:space:]=]*([^[:space:]"'"'"'][^[:space:]]*).*/\2/p')
  fi
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

# ── Issue-anchor gate ───────────────────────────────────────────────
# Every change must be traceable to an issue. The anchor may live in the
# branch name (e.g. feature/142-x, fix/PROJ-12-y) OR in the commit message
# (Closes/Fixes/Refs #N). If neither is present, block the commit.
#
# Deliberately permissive on UNPARSEABLE messages: this gate only runs on
# the -m text we can read. Editor/-F/-c/heredoc messages already exited
# above, so they are never denied here.
# Resolve the repo from the payload (workspace_roots / $CURSOR_PROJECT_DIR) so
# the branch lookup does not depend on cwd (empty on Cursor beforeShellExecution).
PROJECT_ROOT=$(project_root_from_payload "$INPUT")
BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)

# An issue ID in the branch is a tracker key (PROJ-12) anywhere, OR a bare
# number that is the FIRST segment right after the type prefix
# (feature/142-x, fix/142). Anchoring the bare number to the post-"/" segment
# avoids matching incidental numbers inside a slug (oauth-2-factor, utf-8) or
# version/year tokens (release-2024, go-1-21).
BRANCH_HAS_ISSUE=0
if echo "$BRANCH" | grep -qE '(^|[/_-])[A-Z][A-Z0-9]+-[0-9]+([/_-]|$)' \
   || echo "$BRANCH" | grep -qE '/[0-9]+([/_-]|$)' \
   || echo "$BRANCH" | grep -qE '^[0-9]+([/_-]|$)'; then
  BRANCH_HAS_ISSUE=1
fi

# An anchor in the message: Closes/Close/Closed/Fixes/Fix/Fixed/Resolves/
# Resolved/Refs/Ref followed by #N or a tracker key. The leading
# (^|[^[:alpha:]]) guards against matching the keyword as a substring inside
# a larger word (e.g. "prefix #5" must not satisfy "ref").
MSG_HAS_ISSUE=0
if echo "$COMMAND" | grep -qiE '(^|[^[:alpha:]])(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)|refs?)[[:space:]]+(#[0-9]+|[A-Z][A-Z0-9]+-[0-9]+)'; then
  MSG_HAS_ISSUE=1
fi

if [ "$BRANCH_HAS_ISSUE" -eq 0 ] && [ "$MSG_HAS_ISSUE" -eq 0 ]; then
  deny "Commit is not anchored to an issue.
Every change must trace to a documented issue. Provide one of:
  • a branch named with the issue ID — e.g. feature/142-add-login or fix/PROJ-12-bug
  • an anchor in the commit message — e.g. a second -m \"Closes #142\" (also: Fixes, Resolves, Refs)
Branch: ${BRANCH:-unknown}"
fi

exit 0
