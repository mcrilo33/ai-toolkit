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
# Fail-closed contract (configured vs unconfigured):
#   • UNCONFIGURED repo (no linter/typechecker detected): nothing was opted in
#     to → allow. Degrading here is correct, not fail-open.
#   • CONFIGURED tool whose binary cannot resolve (neither a local
#     node_modules/.bin nor PATH) while staged files match its extensions:
#     DENY. The repo opted in to that gate; silently skipping it would be
#     fail-open. (RED commits still skip the typechecker entirely, including
#     this missing-binary check — lint strictness applies even to RED.)
#   • TIME BUDGET (AI_TOOLKIT_GAUNTLET_BUDGET, default 55s): if it trips
#     before all staged files are checked, DENY and advise splitting the
#     commit. An oversized commit that cannot be fully verified must not ship
#     unchecked.
#   • A typechecker that fails to BOOTSTRAP (e.g. pyright cannot write its
#     cache in a sandbox) still degrades to SKIP — the binary exists; the
#     sandbox, not the repo, is at fault.
#
# Exit 2 = block (lint/type failure, configured-but-missing tool, budget trip)
# Exit 0 = allow (pass, no staged files, unconfigured repo, RED carve-out,
#                 non-commit)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands.
# Boundary-aware: chained/prefixed forms (`cd x && git commit`) must not bypass.
is_git_commit "$COMMAND" || exit 0

# Cycle step: a NON-RED commit is the solo-cycle GREEN gate (a RED commit —
# carrying a Tested-RED: trailer — is red-proof-verify's gate, not green). Mark
# before the staged/linter early-exits below so the green step is recorded even
# when the gauntlet then no-ops (e.g. nothing staged, or no linter present).
if ! echo "$COMMAND" | grep -qiE '(^|[[:space:]"'"'"'])Tested-RED:[[:space:]]*\S'; then
  telemetry_mark_step green
fi

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

# A tool the repo CONFIGURED is missing while a staged file matches its
# extensions — fail closed: silently skipping an opted-in gate is fail-open.
missing_tool_deny() {
  local kind="$1" tool="$2" file="$3"
  deny "commit-gauntlet blocked the commit — $kind '$tool' is configured for this repo but its binary cannot be found. Staged file '$file' is in its scope, so the check cannot run.

$tool must be installed (or its configuration removed) before this commit can proceed."
}

run_lint() {
  local file="$1" abs="$2" ext="$3"
  local raw="" filtered=""
  case "$LINTER" in
    ruff)
      case "$ext" in py|pyi) ;; *) return 0 ;; esac
      command -v ruff &>/dev/null || missing_tool_deny "linter" "ruff" "$file"
      raw=$(ruff check "$abs" 2>&1) || true
      ;;
    eslint)
      case "$ext" in ts|tsx|js|jsx) ;; *) return 0 ;; esac
      local bin="$PROJECT_ROOT/node_modules/.bin/eslint"
      command -v eslint &>/dev/null && bin="eslint"
      { [ -x "$bin" ] || command -v eslint &>/dev/null; } \
        || missing_tool_deny "linter" "eslint" "$file"
      raw=$("$bin" --no-color "$abs" 2>&1) || true
      ;;
    biome)
      case "$ext" in ts|tsx|js|jsx) ;; *) return 0 ;; esac
      local bin="$PROJECT_ROOT/node_modules/.bin/biome"
      command -v biome &>/dev/null && bin="biome"
      { [ -x "$bin" ] || command -v biome &>/dev/null; } \
        || missing_tool_deny "linter" "biome" "$file"
      raw=$("$bin" lint "$abs" 2>&1) || true
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
#
# Genuine TYPE ERRORS reuse the same "cannot find" wording as bootstrap
# failures (tsc `error TS2304: Cannot find name 'x'`, `TS2307: Cannot find
# module './y'`, mypy `Cannot find implementation or library stub`). Those
# lines are stripped BEFORE classifying — treating them as bootstrap would
# skip the gate (fail-open) exactly when it must deny. Only the surviving
# lines may mark the run as a bootstrap failure.
typecheck_bootstrap_failed() {
  printf '%s\n' "$1" \
    | grep -viE 'error TS[0-9]+|cannot find (name|module|implementation)' \
    | grep -qiE 'cannot (find|open|write)|permission denied|EACCES|ENOENT|command not found|config file not found|cache|bootstrap|failed to (start|download|install)|no such file or directory|unrecognized arguments'
}

run_typecheck() {
  local file="$1" abs="$2" ext="$3"
  [ "$RED_COMMIT" -eq 1 ] && return 0   # RED carve-out: skip typecheck
  local raw="" rc=0
  case "$TYPECHECKER" in
    pyright)
      case "$ext" in py|pyi) ;; *) return 0 ;; esac
      command -v pyright &>/dev/null || missing_tool_deny "typechecker" "pyright" "$file"
      raw=$(pyright "$abs" 2>&1) && rc=0 || rc=$?
      ;;
    mypy)
      case "$ext" in py|pyi) ;; *) return 0 ;; esac
      command -v mypy &>/dev/null || missing_tool_deny "typechecker" "mypy" "$file"
      raw=$(mypy "$abs" 2>&1) && rc=0 || rc=$?
      ;;
    tsc)
      case "$ext" in ts|tsx) ;; *) return 0 ;; esac
      local bin="$PROJECT_ROOT/node_modules/.bin/tsc"
      command -v tsc &>/dev/null && bin="tsc"
      { [ -x "$bin" ] || command -v tsc &>/dev/null; } \
        || missing_tool_deny "typechecker" "tsc" "$file"
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

# Run all checks under a wall-clock budget (AI_TOOLKIT_GAUNTLET_BUDGET, default
# 55s). If the budget trips before every staged file is checked, DENY: a commit
# too large to verify must not ship unchecked — split it instead.
BUDGET="${AI_TOOLKIT_GAUNTLET_BUDGET:-55}"
SECONDS=0
TIMED_OUT=0
while IFS= read -r file; do
  [ -z "$file" ] && continue
  if [ "$SECONDS" -ge "$BUDGET" ]; then
    TIMED_OUT=1
    break
  fi
  run_checks "$file"
done <<< "$STAGED"

if [ "$TIMED_OUT" -eq 1 ]; then
  deny "commit-gauntlet blocked the commit — time budget (${BUDGET}s) exceeded before all staged files could be checked.

This commit is too large to verify within the budget. Split the commit into smaller, independently verifiable commits and retry."
fi

if [ -n "$ISSUES" ]; then
  RED_NOTE=""
  [ "$RED_COMMIT" -eq 1 ] && RED_NOTE=" (RED commit: typecheck skipped, lint still enforced)"
  deny "commit-gauntlet blocked the commit — lint/type errors on changed lines$RED_NOTE:
$(echo -e "$ISSUES")

Fix the issues above and re-stage, or run the linter/typechecker locally to reproduce."
fi

exit 0
