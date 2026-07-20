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

# Per-hook config (issue #334): resolve the project root once (for git-config reads
# and the branch lookup below), then honor the per-hook enable switch — a host that
# opts out of commit-quality commits freely. Guarded on the resolver's presence so a
# stale install predating #334 keeps today's unconditional behavior.
PROJECT_ROOT=$(project_root_from_payload "$INPUT")
if command -v ai_toolkit_hook_enabled >/dev/null 2>&1; then
  ai_toolkit_hook_enabled commit-quality "$PROJECT_ROOT" || exit 0
fi

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

# No message flag at all (e.g. git commit --amend, -F <file>, editor) — exempt.
# This is the ONLY exemption: keying it on flag PRESENCE, not on an empty
# extraction, closes issue #227. A quote-leading subject makes the extractor
# below yield empty; the old `[ -z "$MSG" ] && exit 0` misread that as
# "no message" and exited 0 BEFORE the format + anchor gates (fail-OPEN).
[ -z "$FIRST_M" ] && exit 0

# Use ERE (-E): BSD/macOS sed does not support \| alternation in BRE, only GNU
# does. ERE alternation `|` is portable across both. Extract the SUBJECT from the
# first message flag with a quote-TYPE-aware capture: a double-quoted value may
# contain ' and a single-quoted value may contain " — so the inner class excludes
# only the SAME quote as the delimiter. The old single `[\"']([^\"']*)[\"']` shared
# the class across both delimiters, so a subject whose first char was a quote
# (`-m '"add helper'`) captured the empty span before it (issue #227). Try
# double-quoted, then single-quoted, then a bare single word.
MSG=$(echo "$FIRST_M" | sed -nE 's/^[[:space:]](-[a-zA-Z]*m|--message)[[:space:]=]*"([^"]*)".*/\2/p')
if [ -z "$MSG" ]; then
  MSG=$(echo "$FIRST_M" | sed -nE "s/^[[:space:]](-[a-zA-Z]*m|--message)[[:space:]=]*'([^']*)'.*/\2/p")
fi
# Bare single word: ... message
if [ -z "$MSG" ]; then
  MSG=$(echo "$FIRST_M" | sed -nE 's/^[[:space:]](-[a-zA-Z]*m|--message)[[:space:]=]*([^[:space:]"'"'"'][^[:space:]]*).*/\2/p')
fi

# A flag WAS present but extraction is still empty — a quote-leading subject the
# synth wrapped as `-m ""subject"` (unrecoverable textually) or a literal -m "".
# Do NOT exit 0 here (that was the #227 fail-open): fall through so the
# conventional check below denies the empty/non-conventional subject.

# Git-generated messages are exempt: merge commits ("Merge branch '…'", "Merge
# pull request …", "Merge remote-tracking branch …") and native reverts
# ("Revert \"…\"") never follow type(scope): — every conventional-commit linter
# exempts them. Without this, worktree-land's merge into main is denied and the
# land misreads the failed merge as a conflict (the 2026-07-08 drain wedge).
# "Revert " (not lowercase revert:) — git's native subject is `Revert "…"`, but
# the quote normalization above can truncate the extracted MSG at the inner
# quote, so match on the word + space rather than requiring the quote char.
case "$MSG" in
  "Merge "* | "Revert "*) exit 0 ;;
esac

# ── Conventional commits validation ─────────────────────────────────
# Pattern: type(optional-scope): description
#   OR: type!: description (breaking change)
# The allowed types are the configurable per-project list (issue #334); unconfigured
# or on a stale install this is exactly today's fixed conventional set.
if command -v ai_toolkit_hook_commit_types >/dev/null 2>&1; then
  VALID_TYPES="$(ai_toolkit_hook_commit_types "$PROJECT_ROOT")"
else
  VALID_TYPES="feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
fi
PATTERN="^($VALID_TYPES)(\([a-zA-Z0-9._-]+\))?(!)?: .+"

if ! echo "$MSG" | grep -qE "$PATTERN"; then
  # Show the ACTUALLY allowed types (issue #334): the deny message must reflect a
  # host's configured `types` list, not the hardcoded default, or it misleads.
  TYPES_DISPLAY="$(printf '%s' "$VALID_TYPES" | sed 's/|/, /g')"
  deny "Commit message doesn't follow conventional commits format.
Expected: type(scope): description
Types: $TYPES_DISPLAY
Example: ${VALID_TYPES%%|*}(scope): short description
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
#
# Per-hook toggle (issue #334): a host may drop the issue-anchor requirement
# (require_issue_anchor=false) while keeping the format check above. Guarded so a
# stale install predating #334 keeps requiring an anchor (today's behavior).
if command -v ai_toolkit_hook_require_anchor >/dev/null 2>&1 \
   && ! ai_toolkit_hook_require_anchor "$PROJECT_ROOT"; then
  exit 0
fi
# PROJECT_ROOT was resolved from the payload above (workspace_roots /
# $CURSOR_PROJECT_DIR) so the branch lookup does not depend on cwd.
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
    # flags. The exemption is only granted when EVERY token after `commit` is on
    # the known-safe allowlist. An allowlist is used instead of a denylist because
    # git accepts unambiguous long-option prefix abbreviations (--amen → --amend,
    # --patc → --patch) and file-delivered pathspecs (--pathspec-from-file), so
    # enumerating unsafe forms can never be complete. Anything not explicitly
    # recognized — unknown flags, abbreviations, bare tokens, --, chained-command
    # tokens — disqualifies the exemption and fails closed.
    #
    # Tokenization: quoted regions are replaced with the opaque placeholder __QV__
    # rather than deleted. Deleting them would make `git commit "-a" -m "docs: x"`
    # look like a plain `git commit -m`, hiding the dangerous flag. An attached
    # value like --message="docs: x" becomes --message=__QV__ and is still visible
    # as a recognisable token. A quoted flag like "-a" becomes a bare __QV__ token
    # which hits the "anything else" arm and disqualifies.
    #
    # State machine (strictly fail-closed):
    #   GIT   — first token must be exactly `git`; anything else disqualifies.
    #   COMMIT — next token must be exactly `commit`; anything else disqualifies.
    #            This kills chained prefixes (git add && git commit, env FOO=1 git
    #            commit, git -C path commit, etc.) because those extra tokens appear
    #            before `commit` and are not `git`. The sanctioned shape is a
    #            standalone `git commit …`.
    #   ARGS  — subsequent tokens must each be on the explicit allowlist.
    #            _EXPECT_VALUE: when set, the next token is consumed as the message
    #            value regardless of its content (git would do the same).
    # Deliberately uncovered but legitimate spellings that fail closed by design:
    #   combined clusters (-sm), space-separated --author <value>, -S<keyid>.
    CMD_TOKENIZED=$(echo "$COMMAND" | sed -E 's/"[^"]*"/__QV__/g; s/'"'"'[^'"'"']*'"'"'/__QV__/g')
    _CMD_SAFE=1
    _STATE=GIT
    _EXPECT_VALUE=0
    set -f
    for _TOKEN in $CMD_TOKENIZED; do
      case "$_STATE" in
        GIT)
          if [ "$_TOKEN" = "git" ]; then
            _STATE=COMMIT
          else
            _CMD_SAFE=0; break
          fi
          ;;
        COMMIT)
          if [ "$_TOKEN" = "commit" ]; then
            _STATE=ARGS
          else
            _CMD_SAFE=0; break
          fi
          ;;
        ARGS)
          if [ "$_EXPECT_VALUE" -eq 1 ]; then
            _EXPECT_VALUE=0
            continue
          fi
          case "$_TOKEN" in
            -m|--message) _EXPECT_VALUE=1 ;;
            --message=*) ;;
            -q|--quiet|-v|--verbose) ;;
            -s|--signoff|-n|--no-verify) ;;
            -S|--gpg-sign|--gpg-sign=*|--no-gpg-sign) ;;
            --author=*|--date=*|--trailer=*) ;;
            *) _CMD_SAFE=0; break ;;
          esac
          ;;
      esac
    done
    set +f
    if [ "$_STATE" != "ARGS" ]; then _CMD_SAFE=0; fi
    if [ "$_CMD_SAFE" -eq 1 ]; then
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
