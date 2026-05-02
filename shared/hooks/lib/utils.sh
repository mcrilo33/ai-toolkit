#!/usr/bin/env bash
# Shared utilities for ai-toolkit hook scripts.
# Source this file at the top of each hook:
#   HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$HOOK_DIR/lib/utils.sh"

set -euo pipefail

# ── Read JSON from stdin (capped at 1MB) ────────────────────────────
read_stdin() {
  head -c 1048576
}

# ── Extract a field from JSON using jq (falls back to grep) ─────────
json_field() {
  local input="$1" field="$2"
  if command -v jq &>/dev/null; then
    echo "$input" | jq -r ".$field // empty" 2>/dev/null
  else
    echo "$input" | grep -o "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# ── Extract nested JSON object as string ─────────────────────────────
json_object() {
  local input="$1" field="$2"
  if command -v jq &>/dev/null; then
    echo "$input" | jq -r ".$field // empty" 2>/dev/null
  else
    echo ""
  fi
}

# ── Detect the tool name (cross-platform) ───────────────────────────
# Copilot: .toolName     Cursor/Claude: .tool_name
get_tool_name() {
  local input="$1"
  local name
  name=$(json_field "$input" "tool_name")
  [ -z "$name" ] && name=$(json_field "$input" "toolName")
  echo "$name"
}

# ── Detect tool args/input (cross-platform) ─────────────────────────
# Copilot: .toolArgs (JSON string)    Cursor/Claude: .tool_input (object)
get_tool_input() {
  local input="$1"
  local args
  args=$(json_object "$input" "tool_input")
  if [ -z "$args" ] || [ "$args" = "null" ]; then
    args=$(json_field "$input" "toolArgs")
  fi
  echo "$args"
}

# ── Get the command from Bash tool input ─────────────────────────────
get_bash_command() {
  local input="$1"
  local tool_input
  tool_input=$(get_tool_input "$input")
  if command -v jq &>/dev/null; then
    echo "$tool_input" | jq -r '.command // empty' 2>/dev/null
  else
    echo "$tool_input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# ── Get file path from Edit/Write tool input ─────────────────────────
get_file_path() {
  local input="$1"
  local tool_input
  tool_input=$(get_tool_input "$input")
  if command -v jq &>/dev/null; then
    echo "$tool_input" | jq -r '.file_path // .path // empty' 2>/dev/null
  else
    echo "$tool_input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# ── Deny output (cross-platform) ────────────────────────────────────
# Works for Copilot, Cursor, and Claude preToolUse hooks.
deny() {
  local reason="$1"
  # Write to stderr for Claude (exit 2 reads stderr)
  echo "[Hook] $reason" >&2
  # Write JSON for Copilot/Cursor
  if command -v jq &>/dev/null; then
    jq -nc --arg r "$reason" '{
      permissionDecision: "deny",
      permissionDecisionReason: $r,
      permission: "deny",
      user_message: $r,
      agent_message: $r
    }'
  else
    echo "{\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"$reason\",\"permission\":\"deny\",\"user_message\":\"$reason\",\"agent_message\":\"$reason\"}"
  fi
  exit 2
}

# ── Warning output (non-blocking, shown to agent) ───────────────────
warn() {
  local message="$1"
  echo "[Hook] $message" >&2
}

# ── Log to stderr (debugging) ───────────────────────────────────────
log() {
  echo "[Hook] $*" >&2
}

# ── Detect project formatter ────────────────────────────────────────
# Preferred stack: black (Python), prettier (JS/TS). Respects project
# config if present, otherwise falls back to PATH.
detect_formatter() {
  local dir="${1:-.}"
  # Python — black first (harmonized across VS Code + Cursor + hooks)
  if [ -f "$dir/pyproject.toml" ] && grep -q '\[tool\.black\]' "$dir/pyproject.toml" 2>/dev/null; then
    echo "black"
  elif command -v black &>/dev/null; then
    echo "black"
  # JS/TS — prettier
  elif [ -f "$dir/.prettierrc" ] || [ -f "$dir/.prettierrc.json" ] || [ -f "$dir/.prettierrc.yml" ] || [ -f "$dir/.prettierrc.js" ] || [ -f "$dir/prettier.config.js" ] || [ -f "$dir/prettier.config.mjs" ]; then
    echo "prettier"
  elif [ -f "$dir/biome.json" ] || [ -f "$dir/biome.jsonc" ]; then
    echo "biome"
  elif [ -x "$dir/node_modules/.bin/prettier" ]; then
    echo "prettier"
  # C/C++
  elif [ -f "$dir/.clang-format" ]; then
    echo "clang-format"
  else
    echo ""
  fi
}

# ── Detect project linter ───────────────────────────────────────────
# Preferred stack: ruff (Python), eslint/biome (JS/TS). Respects project
# config if present, otherwise falls back to PATH.
detect_linter() {
  local dir="${1:-.}"
  # Python — ruff first (harmonized across VS Code + Cursor + hooks)
  if [ -f "$dir/ruff.toml" ]; then
    echo "ruff"
  elif [ -f "$dir/pyproject.toml" ] && grep -q '\[tool\.ruff\]' "$dir/pyproject.toml" 2>/dev/null; then
    echo "ruff"
  elif command -v ruff &>/dev/null; then
    echo "ruff"
  # JS/TS linters
  elif [ -f "$dir/.eslintrc" ] || [ -f "$dir/.eslintrc.json" ] || [ -f "$dir/.eslintrc.js" ] || [ -f "$dir/eslint.config.js" ] || [ -f "$dir/eslint.config.mjs" ]; then
    echo "eslint"
  elif [ -f "$dir/biome.json" ] || [ -f "$dir/biome.jsonc" ]; then
    echo "biome"
  else
    echo ""
  fi
}

# ── Detect typechecker ──────────────────────────────────────────────
# Preferred stack: pyright (Python, same engine as Pylance), tsc (TS).
detect_typechecker() {
  local dir="${1:-.}"
  # TypeScript
  if [ -f "$dir/tsconfig.json" ]; then
    echo "tsc"
  # Python — pyright first (harmonized: same engine as VS Code Pylance)
  elif [ -f "$dir/pyrightconfig.json" ] || ([ -f "$dir/pyproject.toml" ] && grep -q '\[tool\.pyright\]' "$dir/pyproject.toml" 2>/dev/null); then
    echo "pyright"
  elif command -v pyright &>/dev/null; then
    echo "pyright"
  elif [ -f "$dir/pyproject.toml" ] && grep -q '\[tool\.mypy\]' "$dir/pyproject.toml" 2>/dev/null; then
    echo "mypy"
  elif command -v mypy &>/dev/null; then
    echo "mypy"
  else
    echo ""
  fi
}

# ── Find project root (walk up to .git) ─────────────────────────────
find_project_root() {
  local dir="${1:-$(pwd)}"
  while [ "$dir" != "/" ]; do
    [ -d "$dir/.git" ] && { echo "$dir"; return; }
    dir=$(dirname "$dir")
  done
  echo "$(pwd)"
}
