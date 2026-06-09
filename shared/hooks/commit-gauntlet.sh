#!/usr/bin/env bash
# commit-gauntlet — PreToolUse hook
# Runs the project's linter and typechecker on STAGED files before a commit.
# Blocks the commit if any staged file has lint or type errors.
#
# This is the blocking, staged-scope sibling of quality-gate (which only
# warns per-edit). It is the local enforcement of the verification-loop
# gauntlet at the commit boundary.
#
# Fairness / TDD design (see also red-proof-warn, tdd-workflow):
#   • LINT is scoped to CHANGED LINES (git diff --cached -U0 hunks). A clean
#     addition is never blocked by pre-existing lint debt elsewhere in the
#     same file. Whole-file checks are unfair on legacy files.
#   • TYPECHECK is SKIPPED for RED commits — those carrying a `Tested-RED:`
#     trailer. A failing test that imports a not-yet-implemented symbol is the
#     expected state of red-before-green; blocking it would contradict the
#     cage's own TDD workflow. Lint still runs on RED commits.
#
# Degrades gracefully: if no linter/typechecker is configured, the tool is not
# installed, or the typechecker fails to BOOTSTRAP (e.g. pyright cannot write
# its cache in a sandbox), the relevant check is SKIPPED — never a failure.
#
# Exit 2 = block (real lint/type failure on changed code)
# Exit 0 = allow (pass, no staged files, no tools, RED carve-out, non-commit)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands.
echo "$COMMAND" | grep -qE '^\s*git\s+commit\b' || exit 0

# Resolve the repo from the payload (do not trust cwd — empty on Cursor's
# beforeShellExecution). Falls back to walking up from cwd.
PROJECT_ROOT=$(project_root_from_payload "$INPUT")

# Staged files = the index (commit hasn't run yet at PreToolUse).
# ACMR: Added, Copied, Modified, Renamed — skip Deleted (nothing to lint).
STAGED=$(git -C "$PROJECT_ROOT" diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true)
[ -z "$STAGED" ] && exit 0

LINTER=$(detect_linter "$PROJECT_ROOT")
TYPECHECKER=$(detect_typechecker "$PROJECT_ROOT")

# Nothing to enforce with — degrade gracefully, allow the commit.
if [ -z "$LINTER" ] && [ -z "$TYPECHECKER" ]; then
  exit 0
fi

# RED carve-out: a commit whose message carries a `Tested-RED:` trailer is a
# red-before-green test commit. Skip TYPECHECK (unresolved imports expected);
# keep LINT active. The trailer is matched against the full command string
# (already quote-normalized by get_bash_command).
RED_COMMIT=0
if echo "$COMMAND" | grep -qiE '(^|[[:space:]"'"'"'])Tested-RED:[[:space:]]*\S'; then
  RED_COMMIT=1
fi

ISSUES=""

# changed_lines <file> → prints the set of added/changed line numbers in the
# staged version of <file>, derived from unified-diff hunk headers (-U0).
# Used to scope lint output to the lines this commit actually touched.
changed_lines() {
  local file="$1"
  git -C "$PROJECT_ROOT" diff --cached -U0 --diff-filter=ACMR -- "$file" 2>/dev/null \
    | awk '
      /^@@/ {
        # @@ -old +new[,count] @@   — parse the +new range
        match($0, /\+[0-9]+(,[0-9]+)?/)
        spec = substr($0, RSTART+1, RLENGTH-1)
        n = split(spec, a, ",")
        start = a[1]
        len = (n > 1 ? a[2] : 1)
        for (i = 0; i < len; i++) print start + i
      }'
}

# lint_filter <file> <raw-lint-output>
# Keeps only lint findings whose line number is in the changed-line set.
# ruff/eslint emit "path:line:col: ...". If we cannot parse a line number,
# keep the finding (fail safe toward reporting). A clean changed-line set with
# only pre-existing violations elsewhere yields no kept findings → no block.
lint_filter() {
  local file="$1" raw="$2"
  local changed
  changed=$(changed_lines "$file" | tr '\n' ' ')
  [ -z "$changed" ] && { printf '%s' ""; return 0; }
  # Pass the changed-line set as a single space-separated arg (no embedded
  # newlines — awk -v cannot hold a multiline value).
  printf '%s\n' "$raw" | awk -v changed="$changed" '
    BEGIN { n = split(changed, arr, " "); for (i=1;i<=n;i++) if (arr[i] != "") keep[arr[i]]=1 }
    {
      # extract the first  :<num>:  as the line number
      if (match($0, /:[0-9]+:/)) {
        ln = substr($0, RSTART+1, RLENGTH-2)
        if (ln in keep) print
      }
    }'
}

run_lint() {
  local file="$1" abs="$2" ext="$3"
  local raw="" filtered=""
  case "$LINTER" in
    ruff)
      command -v ruff &>/dev/null || return 0
      case "$ext" in py|pyi)
        raw=$(ruff check "$abs" 2>&1) || true
      ;; *) return 0 ;; esac
      ;;
    eslint)
      local bin="$PROJECT_ROOT/node_modules/.bin/eslint"
      command -v eslint &>/dev/null && bin="eslint"
      { [ -x "$bin" ] || command -v eslint &>/dev/null; } || return 0
      case "$ext" in ts|tsx|js|jsx)
        raw=$("$bin" --no-color "$abs" 2>&1) || true
      ;; *) return 0 ;; esac
      ;;
    biome)
      local bin="$PROJECT_ROOT/node_modules/.bin/biome"
      command -v biome &>/dev/null && bin="biome"
      { [ -x "$bin" ] || command -v biome &>/dev/null; } || return 0
      case "$ext" in ts|tsx|js|jsx)
        raw=$("$bin" lint "$abs" 2>&1) || true
      ;; *) return 0 ;; esac
      ;;
    *) return 0 ;;
  esac
  [ -z "$raw" ] && return 0
  filtered=$(lint_filter "$file" "$raw")
  if [ -n "$filtered" ]; then
    ISSUES="$ISSUES\n[$file] Lint:\n$filtered"
  fi
  return 0
}

# A typechecker invocation that failed to start (rather than finding type
# errors) should degrade to SKIP. Detect common bootstrap/startup failures.
typecheck_bootstrap_failed() {
  echo "$1" | grep -qiE 'cannot (find|open|write)|permission denied|EACCES|ENOENT|cache|bootstrap|failed to (start|download|install)|no such file or directory'
}

run_typecheck() {
  local file="$1" abs="$2" ext="$3"
  [ "$RED_COMMIT" -eq 1 ] && return 0   # RED carve-out: skip typecheck
  local raw="" rc=0
  case "$TYPECHECKER" in
    pyright)
      command -v pyright &>/dev/null || return 0
      case "$ext" in py|pyi) ;; *) return 0 ;; esac
      raw=$(pyright "$abs" 2>&1) && rc=0 || rc=$?
      ;;
    mypy)
      command -v mypy &>/dev/null || return 0
      case "$ext" in py|pyi) ;; *) return 0 ;; esac
      raw=$(mypy "$abs" 2>&1) && rc=0 || rc=$?
      ;;
    tsc)
      local bin="$PROJECT_ROOT/node_modules/.bin/tsc"
      command -v tsc &>/dev/null && bin="tsc"
      { [ -x "$bin" ] || command -v tsc &>/dev/null; } || return 0
      case "$ext" in ts|tsx) ;; *) return 0 ;; esac
      raw=$("$bin" --noEmit --pretty false "$abs" 2>&1) && rc=0 || rc=$?
      ;;
    *) return 0 ;;
  esac
  [ "$rc" -eq 0 ] && return 0
  if typecheck_bootstrap_failed "$raw"; then
    warn "commit-gauntlet: $TYPECHECKER could not start (bootstrap/cache failure) — typecheck SKIPPED for $file."
    return 0
  fi
  ISSUES="$ISSUES\n[$file] Typecheck:\n$raw"
  return 0
}

run_checks() {
  local file="$1"
  local abs="$PROJECT_ROOT/$file"
  [ -f "$abs" ] || return 0
  local ext="${file##*.}"
  run_lint "$file" "$abs" "$ext"
  run_typecheck "$file" "$abs" "$ext"
}

# Run all checks under a wall-clock budget. If the budget itself trips,
# warn (do not block) so a slow machine can never permanently wedge commits.
SECONDS=0
TIMED_OUT=0
while IFS= read -r file; do
  [ -z "$file" ] && continue
  if [ "$SECONDS" -ge 55 ]; then
    TIMED_OUT=1
    break
  fi
  run_checks "$file"
done <<< "$STAGED"

if [ "$TIMED_OUT" -eq 1 ]; then
  warn "commit-gauntlet: time budget exceeded — skipping remaining staged files (commit allowed)."
fi

if [ -n "$ISSUES" ]; then
  RED_NOTE=""
  [ "$RED_COMMIT" -eq 1 ] && RED_NOTE=" (RED commit: typecheck skipped, lint still enforced)"
  deny "commit-gauntlet blocked the commit — lint/type errors on changed lines$RED_NOTE:
$(echo -e "$ISSUES")

Fix the issues above and re-stage, or run the linter/typechecker locally to reproduce."
fi

exit 0
