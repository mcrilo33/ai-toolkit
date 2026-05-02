#!/usr/bin/env bash
# quality-gate — PostToolUse hook
# Runs linter and typechecker on edited files after Edit/Write.
# Warns the agent about issues so it can fix them immediately.
#
# Non-blocking: writes warnings to stderr.
# Exit 0 = always
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
FILE_PATH=$(get_file_path "$INPUT")

[ -z "$FILE_PATH" ] && exit 0
[ -f "$FILE_PATH" ] || exit 0

EXT="${FILE_PATH##*.}"
PROJECT_ROOT=$(find_project_root "$(dirname "$FILE_PATH")")
LINTER=$(detect_linter "$PROJECT_ROOT")
TYPECHECKER=$(detect_typechecker "$PROJECT_ROOT")

ISSUES=""

# ── Linting ─────────────────────────────────────────────────────────
# Harmonized stack: ruff (Python), eslint/biome (JS/TS)
case "$LINTER" in
  ruff)
    if command -v ruff &>/dev/null; then
      case "$EXT" in
        py|pyi)
          RESULT=$(ruff check "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nLint: $RESULT"
          ;;
      esac
    fi
    ;;
  eslint)
    if command -v eslint &>/dev/null || [ -x "$PROJECT_ROOT/node_modules/.bin/eslint" ]; then
      ESLINT="${PROJECT_ROOT}/node_modules/.bin/eslint"
      command -v eslint &>/dev/null && ESLINT="eslint"
      case "$EXT" in
        ts|tsx|js|jsx)
          RESULT=$($ESLINT --no-color "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nLint: $RESULT"
          ;;
      esac
    fi
    ;;
  biome)
    if command -v biome &>/dev/null || [ -x "$PROJECT_ROOT/node_modules/.bin/biome" ]; then
      BIOME="${PROJECT_ROOT}/node_modules/.bin/biome"
      command -v biome &>/dev/null && BIOME="biome"
      case "$EXT" in
        ts|tsx|js|jsx)
          RESULT=$($BIOME lint "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nLint: $RESULT"
          ;;
      esac
    fi
    ;;
esac

# ── Type checking ───────────────────────────────────────────────────
# Harmonized stack: pyright (Python, same engine as Pylance), tsc (TS)
case "$TYPECHECKER" in
  pyright)
    if command -v pyright &>/dev/null; then
      case "$EXT" in
        py|pyi)
          RESULT=$(pyright "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nTypecheck: $RESULT"
          ;;
      esac
    fi
    ;;
  tsc)
    if command -v tsc &>/dev/null || [ -x "$PROJECT_ROOT/node_modules/.bin/tsc" ]; then
      TSC="${PROJECT_ROOT}/node_modules/.bin/tsc"
      command -v tsc &>/dev/null && TSC="tsc"
      case "$EXT" in
        ts|tsx)
          RESULT=$($TSC --noEmit --pretty false "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nTypecheck: $RESULT"
          ;;
      esac
    fi
    ;;
  mypy)
    if command -v mypy &>/dev/null; then
      case "$EXT" in
        py|pyi)
          RESULT=$(mypy "$FILE_PATH" 2>&1) || ISSUES="$ISSUES\nTypecheck: $RESULT"
          ;;
      esac
    fi
    ;;
esac

# ── Report ──────────────────────────────────────────────────────────
if [ -n "$ISSUES" ]; then
  warn "── quality-gate issues in $(basename "$FILE_PATH") ──"
  echo -e "$ISSUES" | while IFS= read -r line; do
    [ -n "$line" ] && warn "  $line"
  done
  warn "──────────────────────────────────────────────"
fi

exit 0
