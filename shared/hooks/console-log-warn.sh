#!/usr/bin/env bash
# console-log-warn — PostToolUse hook
# Warns when debug/logging statements are added to edited files.
# Detects: console.log, console.debug, print(), debugger, etc.
#
# Non-blocking: advisory warning to stderr.
# Exit 0 = always
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
TOOL_INPUT=$(get_tool_input "$INPUT")

# Get the new content being written
NEW_CONTENT=""
if command -v jq &>/dev/null; then
  NEW_CONTENT=$(echo "$TOOL_INPUT" | jq -r '.content // .new_string // empty' 2>/dev/null)
fi

[ -z "$NEW_CONTENT" ] && exit 0

FILE_PATH=$(get_file_path "$INPUT")
EXT="${FILE_PATH##*.}"

# ── Language-specific debug patterns ────────────────────────────────
FOUND=""
case "$EXT" in
  ts|tsx|js|jsx|mjs|cjs)
    if echo "$NEW_CONTENT" | grep -qE '\bconsole\.(log|debug|warn|error|info|trace)\b'; then
      FOUND="console.log/debug statement"
    elif echo "$NEW_CONTENT" | grep -qE '^\s*debugger\s*;?\s*$'; then
      FOUND="debugger statement"
    fi
    ;;
  py)
    # Match print() but not print() in comments or docstrings (basic heuristic)
    if echo "$NEW_CONTENT" | grep -qE '^\s*print\s*\('; then
      FOUND="print() statement"
    elif echo "$NEW_CONTENT" | grep -qE '^\s*breakpoint\s*\(\s*\)'; then
      FOUND="breakpoint() call"
    elif echo "$NEW_CONTENT" | grep -qE '^\s*import\s+pdb|^\s*pdb\.set_trace\b'; then
      FOUND="pdb debugger"
    fi
    ;;
  go)
    if echo "$NEW_CONTENT" | grep -qE '\bfmt\.Print(ln|f)?\b'; then
      FOUND="fmt.Print statement"
    fi
    ;;
  rs)
    if echo "$NEW_CONTENT" | grep -qE '\bdbg!\b'; then
      FOUND="dbg! macro"
    elif echo "$NEW_CONTENT" | grep -qE '\bprintln!\b'; then
      FOUND="println! macro"
    fi
    ;;
  java|kt)
    if echo "$NEW_CONTENT" | grep -qE '\bSystem\.out\.print'; then
      FOUND="System.out.print statement"
    fi
    ;;
  rb)
    if echo "$NEW_CONTENT" | grep -qE '^\s*puts\b|^\s*p\b|^\s*binding\.pry\b'; then
      FOUND="debug output (puts/p/binding.pry)"
    fi
    ;;
esac

if [ -n "$FOUND" ]; then
  warn "⚠ $FOUND detected in $(basename "$FILE_PATH"). Remove before committing."
fi

exit 0
