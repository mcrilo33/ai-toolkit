#!/usr/bin/env bash
# console-log-warn — file-edit hook.
# Warns when debug/logging statements are added to edited files.
# Detects: console.log, console.debug, print(), debugger, etc.
#
# Cursor runs this on afterFileEdit (real edits[].new_string); Claude/Copilot
# run it on postToolUse (tool_input.content / .new_string). get_edit_new_content
# handles both.
#
# NOTE: afterFileEdit has NO agent-visible output channel, so on Cursor the
# warning is logged to the Hooks output (stderr) only — the agent does NOT see
# it. On Claude/Copilot the warning is surfaced to the agent as before.
#
# Non-blocking: advisory warning to stderr. Exit 0 = always.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)

FILE_PATH=$(get_edit_file_path "$INPUT")
# Ignore the runtime's internal scratch files (wrong path/content).
is_agent_tools_path "$FILE_PATH" && exit 0

NEW_CONTENT=$(get_edit_new_content "$INPUT")
[ -z "$NEW_CONTENT" ] && exit 0

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
