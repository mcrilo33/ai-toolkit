#!/usr/bin/env bash
# post-edit-format — file-edit hook.
# Auto-formats the edited file using the project's configured formatter.
#
# Cursor runs this on afterFileEdit (real file_path); Claude/Copilot run it on
# postToolUse (tool_input.file_path). get_edit_file_path handles both. On
# afterFileEdit there is no agent-visible output channel, so this is a pure
# side-effect (the file is formatted on disk); the agent is not messaged.
#
# Non-blocking: failures are logged but don't interrupt the agent.
# Exit 0 = always.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
FILE_PATH=$(get_edit_file_path "$INPUT")

[ -z "$FILE_PATH" ] && exit 0
# The generic postToolUse/Write path can hand us the runtime's internal scratch
# file instead of the real edit — never format that.
is_agent_tools_path "$FILE_PATH" && exit 0
[ -f "$FILE_PATH" ] || exit 0

EXT="${FILE_PATH##*.}"
PROJECT_ROOT=$(project_root_from_payload "$INPUT")
[ -d "$PROJECT_ROOT" ] || PROJECT_ROOT=$(find_project_root "$(dirname "$FILE_PATH")")
FORMATTER=$(detect_formatter "$PROJECT_ROOT")

# ── Format by detected tool ─────────────────────────────────────────
# Harmonized stack: ruff format (Python), prettier (JS/TS)
case "$FORMATTER" in
  ruff)
    if command -v ruff &>/dev/null; then
      case "$EXT" in
        py|pyi)
          ruff format --quiet "$FILE_PATH" 2>/dev/null && log "Formatted with ruff: $FILE_PATH"
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
        command -v ruff &>/dev/null && ruff format --quiet "$FILE_PATH" 2>/dev/null && log "Formatted with ruff (fallback): $FILE_PATH"
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
