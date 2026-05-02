#!/usr/bin/env bash
# post-edit-format — PostToolUse hook
# Auto-formats the edited file using the project's configured formatter.
# Runs after Edit/Write tool calls.
#
# Non-blocking: failures are logged but don't interrupt the agent.
# Exit 0 = always (postToolUse hooks can't block)
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
FILE_PATH=$(get_file_path "$INPUT")

[ -z "$FILE_PATH" ] && exit 0
[ -f "$FILE_PATH" ] || exit 0

EXT="${FILE_PATH##*.}"
PROJECT_ROOT=$(find_project_root "$(dirname "$FILE_PATH")")
FORMATTER=$(detect_formatter "$PROJECT_ROOT")

# ── Format by detected tool ─────────────────────────────────────────
# Harmonized stack: black (Python), prettier (JS/TS)
case "$FORMATTER" in
  black)
    if command -v black &>/dev/null; then
      case "$EXT" in
        py|pyi)
          black --quiet --line-length 88 "$FILE_PATH" 2>/dev/null && log "Formatted with black: $FILE_PATH"
          ;;
      esac
    fi
    ;;
  prettier)
    if command -v prettier &>/dev/null || [ -x "$PROJECT_ROOT/node_modules/.bin/prettier" ]; then
      PRETTIER="${PROJECT_ROOT}/node_modules/.bin/prettier"
      command -v prettier &>/dev/null && PRETTIER="prettier"
      case "$EXT" in
        ts|tsx|js|jsx|json|css|html|md|yaml|yml)
          $PRETTIER --write "$FILE_PATH" 2>/dev/null && log "Formatted with prettier: $FILE_PATH"
          ;;
      esac
    fi
    ;;
  biome)
    if command -v biome &>/dev/null || [ -x "$PROJECT_ROOT/node_modules/.bin/biome" ]; then
      BIOME="${PROJECT_ROOT}/node_modules/.bin/biome"
      command -v biome &>/dev/null && BIOME="biome"
      case "$EXT" in
        ts|tsx|js|jsx|json|css)
          $BIOME check --write "$FILE_PATH" 2>/dev/null && log "Formatted with biome: $FILE_PATH"
          ;;
      esac
    fi
    ;;
  clang-format)
    if command -v clang-format &>/dev/null; then
      case "$EXT" in
        c|cpp|cc|h|hpp)
          clang-format -i "$FILE_PATH" 2>/dev/null && log "Formatted with clang-format: $FILE_PATH"
          ;;
      esac
    fi
    ;;
  *)
    # No formatter detected — try language-specific defaults
    case "$EXT" in
      py|pyi)
        command -v black &>/dev/null && black --quiet --line-length 88 "$FILE_PATH" 2>/dev/null && log "Formatted with black (fallback): $FILE_PATH"
        ;;
      go)
        command -v gofmt &>/dev/null && gofmt -w "$FILE_PATH" 2>/dev/null && log "Formatted with gofmt: $FILE_PATH"
        ;;
      rs)
        command -v rustfmt &>/dev/null && rustfmt "$FILE_PATH" 2>/dev/null && log "Formatted with rustfmt: $FILE_PATH"
        ;;
      ts|tsx|js|jsx|json|css|html|md|yaml|yml)
        [ -x "$PROJECT_ROOT/node_modules/.bin/prettier" ] && "$PROJECT_ROOT/node_modules/.bin/prettier" --write "$FILE_PATH" 2>/dev/null && log "Formatted with prettier (fallback): $FILE_PATH"
        ;;
    esac
    ;;
esac

exit 0
