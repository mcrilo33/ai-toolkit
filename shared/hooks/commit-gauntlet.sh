#!/usr/bin/env bash
# commit-gauntlet — PreToolUse hook
# Runs the project's linter and typechecker on STAGED files before a commit.
# Blocks the commit if any staged file has lint or type errors.
#
# This is the blocking, staged-scope sibling of quality-gate (which only
# warns per-edit). It is the local enforcement of the verification-loop
# gauntlet at the commit boundary.
#
# Degrades gracefully: if no linter/typechecker is configured, or the tool
# is not installed, the relevant check is SKIPPED — absence of a tool is
# never treated as a failure (critical for target repos without the stack).
#
# Exit 2 = block (real lint/type failure)
# Exit 0 = allow (pass, no staged files, no tools, or non-commit command)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_bash_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on git commit commands.
echo "$COMMAND" | grep -qE '^\s*git\s+commit\b' || exit 0

PROJECT_ROOT=$(find_project_root "$(pwd)")

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

ISSUES=""

run_checks() {
  local file="$1"
  local abs="$PROJECT_ROOT/$file"
  [ -f "$abs" ] || return 0
  local ext="${file##*.}"

  # ── Linting ───────────────────────────────────────────────────────
  case "$LINTER" in
    ruff)
      if command -v ruff &>/dev/null; then
        case "$ext" in
          py|pyi)
            RESULT=$(ruff check "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Lint: $RESULT"
            ;;
        esac
      fi
      ;;
    eslint)
      local eslint_bin="$PROJECT_ROOT/node_modules/.bin/eslint"
      if command -v eslint &>/dev/null; then eslint_bin="eslint"; fi
      if [ -x "$eslint_bin" ] || command -v eslint &>/dev/null; then
        case "$ext" in
          ts|tsx|js|jsx)
            RESULT=$("$eslint_bin" --no-color "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Lint: $RESULT"
            ;;
        esac
      fi
      ;;
    biome)
      local biome_bin="$PROJECT_ROOT/node_modules/.bin/biome"
      if command -v biome &>/dev/null; then biome_bin="biome"; fi
      if [ -x "$biome_bin" ] || command -v biome &>/dev/null; then
        case "$ext" in
          ts|tsx|js|jsx)
            RESULT=$("$biome_bin" lint "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Lint: $RESULT"
            ;;
        esac
      fi
      ;;
  esac

  # ── Type checking ─────────────────────────────────────────────────
  case "$TYPECHECKER" in
    pyright)
      if command -v pyright &>/dev/null; then
        case "$ext" in
          py|pyi)
            RESULT=$(pyright "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Typecheck: $RESULT"
            ;;
        esac
      fi
      ;;
    mypy)
      if command -v mypy &>/dev/null; then
        case "$ext" in
          py|pyi)
            RESULT=$(mypy "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Typecheck: $RESULT"
            ;;
        esac
      fi
      ;;
    tsc)
      local tsc_bin="$PROJECT_ROOT/node_modules/.bin/tsc"
      if command -v tsc &>/dev/null; then tsc_bin="tsc"; fi
      if [ -x "$tsc_bin" ] || command -v tsc &>/dev/null; then
        case "$ext" in
          ts|tsx)
            RESULT=$("$tsc_bin" --noEmit --pretty false "$abs" 2>&1) || ISSUES="$ISSUES\n[$file] Typecheck: $RESULT"
            ;;
        esac
      fi
      ;;
  esac
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
  deny "commit-gauntlet blocked the commit — lint/type errors on staged files:
$(echo -e "$ISSUES")

Fix the issues above and re-stage, or run the linter/typechecker locally to reproduce."
fi

exit 0
