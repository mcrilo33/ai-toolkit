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

# Only act on git commit commands.
# Boundary-aware gate: chained/prefixed forms (`true; git commit -m x`) must
# not bypass. The -m message extraction below scans the whole command string,
# so it works regardless of where the commit appears.
is_git_commit "$COMMAND" || exit 0

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
  # ── Scoped exemption: docs/chore doc-only commits (issue #10) ───────
  # This is a scoped exemption for micro/express spokes, not a general bypass.
  # Three-lane triage: a docs: or chore: commit whose entire staged set consists
  # of non-executable documentation files (.md/.markdown/.txt/.rst) outside
  # top-level scripts/, shared/hooks/, tests/, and any */scripts/ directory
  # does not require an issue anchor. Source-code commits still require anchoring.
  # Fail closed: an empty staged index (e.g. git commit -am at PreToolUse time,
  # or git unavailable) is never treated as "all documentation".
  DOC_ONLY_EXEMPT=0
  if echo "$MSG" | grep -qE '^(docs|chore)(\([a-zA-Z0-9._-]+\))?(!)?: '; then
    # The staged index is only authoritative for a PLAIN `git commit` with message
    # flags. Deny the exemption when the command uses -a/-i/-p/--all/--include/
    # --interactive/--patch/--amend (these commit more than the index), or when a
    # trailing pathspec is present (commits the named worktree paths, not the index).
    # Strip quoted strings first so a quoted message containing -am etc. is ignored,
    # then check for the unsafe flag forms.
    CMD_STRIPPED=$(echo "$COMMAND" | sed -E "s/\"[^\"]*\"//g; s/'[^']*'//g")
    UNSAFE_SHORT=$(echo "$CMD_STRIPPED" | grep -E '(^|[[:space:]])-[a-zA-Z]*[aip]' || true)
    UNSAFE_LONG=$(echo "$CMD_STRIPPED" | grep -E '(^|[[:space:]])--(all|include|interactive|patch|amend)([[:space:]=]|$)' || true)
    # Detect trailing pathspec: after the word `commit`, every token must start with `-`.
    UNSAFE_PATHSPEC=0
    _SEEN_COMMIT=0
    for _TOKEN in $CMD_STRIPPED; do
      if [ "$_SEEN_COMMIT" -eq 1 ]; then
        case "$_TOKEN" in
          -*) ;;
          *) UNSAFE_PATHSPEC=1; break ;;
        esac
      fi
      if [ "$_TOKEN" = "commit" ]; then _SEEN_COMMIT=1; fi
    done
    if [ -z "$UNSAFE_SHORT" ] && [ -z "$UNSAFE_LONG" ] && [ "$UNSAFE_PATHSPEC" -eq 0 ]; then
      STAGED=$(git -C "$PROJECT_ROOT" diff --cached --name-only 2>/dev/null || true)
      if [ -n "$STAGED" ]; then
        # Check every staged path is a doc-only path
        NON_DOC=$(echo "$STAGED" | grep -vE '\.(md|markdown|txt|rst)$' || true)
        # Exclude shared/hooks/ and tests/
        EXCLUDED_DIR=$(echo "$STAGED" | grep -E '^(shared/hooks/|tests/)' || true)
        # Exclude any */scripts/ directory segment
        SCRIPTS_SUBDIR=$(echo "$STAGED" | grep -E '(^|/)scripts/' || true)
        # Every staged file must be a plain file (mode 100644) or deletion (000000).
        # Executable files (100755) and symlinks (120000) are not documentation.
        RAW_MODES=$(git -C "$PROJECT_ROOT" diff --cached --raw 2>/dev/null || true)
        UNSAFE_MODE=$(echo "$RAW_MODES" | awk '{print $2}' | grep -vE '^(100644|000000)$' || true)
        if [ -z "$NON_DOC" ] && [ -z "$EXCLUDED_DIR" ] && [ -z "$SCRIPTS_SUBDIR" ] && [ -z "$UNSAFE_MODE" ]; then
          DOC_ONLY_EXEMPT=1
        fi
      fi
    fi
  fi
  if [ "$DOC_ONLY_EXEMPT" -eq 0 ]; then
    deny "Commit is not anchored to an issue.
Every change must trace to a documented issue. Provide one of:
  • a branch named with the issue ID — e.g. feature/142-add-login or fix/PROJ-12-bug
  • an anchor in the commit message — e.g. a second -m \"Closes #142\" (also: Fixes, Resolves, Refs)
Branch: ${BRANCH:-unknown}"
  fi
fi

exit 0
