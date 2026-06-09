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

# ── Normalize shell-escaped quotes in a command string ──────────────
# Some agent runtimes serialize a Bash tool call with backslash-escaped inner
# quotes (e.g. -m \"feat: x\"). Those backslashes are escaping artifacts, not
# literal content, and break quote-aware parsing. Strip \" -> " and \' -> '.
# Safe for match-only hooks (grep for flags) and required for subject parsing.
normalize_escaped_quotes() {
  printf '%s' "$1" | sed 's/\\"/"/g; s/\\'\''/'\''/g'
}

# ── Get the command from Bash tool input ─────────────────────────────
get_bash_command() {
  local input="$1"
  local tool_input cmd
  tool_input=$(get_tool_input "$input")
  if command -v jq &>/dev/null; then
    cmd=$(echo "$tool_input" | jq -r '.command // empty' 2>/dev/null)
  else
    cmd=$(echo "$tool_input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*: *"//;s/"$//')
  fi
  normalize_escaped_quotes "$cmd"
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

# ── Cursor dedicated-event accessors ────────────────────────────────
# Cursor 3.7.21 dedicated events (beforeShellExecution/afterFileEdit/
# beforeReadFile) carry the REAL payload at the TOP LEVEL — unlike the generic
# preToolUse/postToolUse path, which delivers an internal scratch payload under
# tool_input. These accessors prefer the top-level shape and fall back to the
# Claude/Copilot tool_input shape, so the same script works on every platform.

# ── Detect the dedicated event name (Cursor only) ───────────────────
# Empty on Claude/Copilot generic calls (they do not set hook_event_name to a
# dedicated-event value). Used to branch behavior per platform.
get_hook_event() {
  local input="$1"
  json_field "$input" "hook_event_name"
}

# ── True when running under a Cursor dedicated event ────────────────
# Returns 0 (success) if hook_event_name is one of the dedicated events.
on_cursor_dedicated_event() {
  local input="$1" event
  event=$(get_hook_event "$input")
  case "$event" in
    beforeShellExecution|afterShellExecution|afterFileEdit|beforeReadFile) return 0 ;;
    *) return 1 ;;
  esac
}

# ── Get the shell command (cross-platform) ──────────────────────────
# beforeShellExecution: top-level .command. Claude/Copilot: tool_input.command
# (via get_bash_command). Both paths normalize shell-escaped quotes.
get_shell_command() {
  local input="$1" cmd=""
  if command -v jq &>/dev/null; then
    cmd=$(echo "$input" | jq -r '.command // empty' 2>/dev/null)
  fi
  if [ -z "$cmd" ]; then
    # Fall back to the Claude/Copilot tool_input.command shape.
    get_bash_command "$input"
    return
  fi
  normalize_escaped_quotes "$cmd"
}

# ── Recognize a `git add` / `git commit` invocation ─────────────────
# Matches the subcommand anywhere in the command string so chained or prefixed
# forms are not bypassed: `cd sub && git add`, `git -C path commit`,
# `foo; git add -A`, `git --no-pager commit`. The `git` token must be at a
# command boundary (start, or after a shell separator) to avoid matching
# substrings like `mygit`. Returns 0 on match.
is_git_commit_or_add() {
  local cmd="$1"
  printf '%s' "$cmd" | grep -qE '(^|[;&|]|&&|\|\|)[[:space:]]*git([[:space:]]+(-[^[:space:]]+|--[^[:space:]]+|-C[[:space:]]+[^[:space:]]+))*[[:space:]]+(add|commit)\b'
}

# ── Get the edited file path (cross-platform) ───────────────────────
# afterFileEdit: top-level .file_path. Claude/Copilot: tool_input.file_path
# (via get_file_path).
get_edit_file_path() {
  local input="$1" path=""
  if command -v jq &>/dev/null; then
    path=$(echo "$input" | jq -r '.file_path // empty' 2>/dev/null)
  fi
  if [ -z "$path" ]; then
    get_file_path "$input"
    return
  fi
  echo "$path"
}

# ── Get the new content written (cross-platform) ────────────────────
# afterFileEdit: concatenated .edits[].new_string. Claude/Copilot:
# tool_input.content (Write) or tool_input.new_string (Edit).
get_edit_new_content() {
  local input="$1" content="" tool_input
  if command -v jq &>/dev/null; then
    content=$(echo "$input" | jq -r '
      if (.edits | type) == "array"
      then ([.edits[]?.new_string] | join(""))
      else empty end' 2>/dev/null)
  fi
  if [ -z "$content" ]; then
    tool_input=$(get_tool_input "$input")
    if command -v jq &>/dev/null; then
      content=$(echo "$tool_input" | jq -r '.content // .new_string // empty' 2>/dev/null)
    fi
  fi
  echo "$content"
}

# ── Resolve the project root from the payload (never trust cwd) ─────
# Cursor's beforeShellExecution reports an EMPTY cwd, so do NOT rely on it.
# Prefer $CURSOR_PROJECT_DIR, then payload .workspace_roots[0], then walk up
# to the nearest .git.
project_root_from_payload() {
  local input="${1:-}" root=""
  if [ -n "${CURSOR_PROJECT_DIR:-}" ]; then
    echo "$CURSOR_PROJECT_DIR"
    return
  fi
  if [ -n "$input" ] && command -v jq &>/dev/null; then
    root=$(echo "$input" | jq -r '.workspace_roots[0] // empty' 2>/dev/null)
  fi
  if [ -n "$root" ] && [ "$root" != "null" ]; then
    echo "$root"
    return
  fi
  find_project_root "$(pwd)"
}

# ── Guard against the internal scratch path ─────────────────────────
# The generic postToolUse/Write path delivers paths like
# ~/.cursor/projects/<proj>/agent-tools/<uuid>.txt — internal scratch, not the
# real edit. Scripts should early-exit when handed one. Returns 0 if the path
# matches the scratch pattern.
is_agent_tools_path() {
  local path="$1"
  case "$path" in
    */.cursor/*/agent-tools/*) return 0 ;;
    *) return 1 ;;
  esac
}

# ── Hardcoded-secret detection ──────────────────────────────────────
# Shared by secrets-scan (pre-write / commit-time deny) and secrets-scan-revert
# (afterFileEdit containment). Each entry: ERE pattern<TAB>description.
secret_patterns() {
  cat <<'PATTERNS'
sk-[a-zA-Z0-9]{20,}	OpenAI API key
sk-proj-[a-zA-Z0-9_-]{20,}	OpenAI project key
AKIA[0-9A-Z]{16}	AWS Access Key ID
ghp_[a-zA-Z0-9]{36}	GitHub personal access token
gho_[a-zA-Z0-9]{36}	GitHub OAuth token
ghs_[a-zA-Z0-9]{36}	GitHub server token
github_pat_[a-zA-Z0-9_]{22,}	GitHub fine-grained PAT
glpat-[a-zA-Z0-9_-]{20,}	GitLab personal access token
xoxb-[0-9]{10,}-[a-zA-Z0-9]{20,}	Slack bot token
xoxp-[0-9]{10,}-[a-zA-Z0-9]{20,}	Slack user token
sk_live_[a-zA-Z0-9]{24,}	Stripe secret key
pk_live_[a-zA-Z0-9]{24,}	Stripe publishable key
sq0csp-[a-zA-Z0-9_-]{40,}	Square credential
AIza[0-9A-Za-z_-]{35}	Google API key
ya29\.[0-9A-Za-z_-]+	Google OAuth token
eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}	JWT token (if long)
npm_[a-zA-Z0-9]{36}	npm access token
pypi-AgEIcH[a-zA-Z0-9_-]{50,}	PyPI API token
SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}	SendGrid API key
key-[a-zA-Z0-9]{32}	Mailgun API key
PATTERNS
}

# ── Scan a blob of text for a hardcoded secret ──────────────────────
# Prints the description of the FIRST matching pattern (and returns 0) or
# returns 1 if none match.
scan_for_secret() {
  local content="$1" pattern desc entry
  [ -z "$content" ] && return 1
  while IFS=$'\t' read -r pattern desc; do
    [ -z "$pattern" ] && continue
    if printf '%s' "$content" | grep -qE "$pattern"; then
      printf '%s' "$desc"
      return 0
    fi
  done < <(secret_patterns)
  return 1
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

# ── Shipping-gate enforcement (platform-aware) ──────────────────────
# The advisory push/PR hooks (red-proof, reviewer-sep, delegation,
# git-push-review) were warn-only. On Cursor's dedicated beforeShellExecution
# event the agent CAN be blocked, so an unmet shipping condition is promoted to
# a hard DENY (message lands in agent_message). On every other platform/event
# (Claude/Copilot preToolUse, native git hooks) the historical advisory
# behavior is preserved: warn and continue.
#
# Usage: ship_gate_enforce "$INPUT" "<message>"
#   - On Cursor beforeShellExecution: deny() (exit 2) — does not return.
#   - Otherwise: warn() and return 0.
ship_gate_enforce() {
  local input="$1" message="$2"
  if [ "$(get_hook_event "$input")" = "beforeShellExecution" ]; then
    deny "$message"
  fi
  warn "$message"
}

# ── Log to stderr (debugging) ───────────────────────────────────────
log() {
  echo "[Hook] $*" >&2
}

# ── Detect project formatter ────────────────────────────────────────
# Preferred stack: ruff format (Python), prettier (JS/TS). Respects project
# config if present, otherwise falls back to PATH.
detect_formatter() {
  local dir="${1:-.}"
  # Python — ruff format first (harmonized: single tool for lint + format)
  if [ -f "$dir/ruff.toml" ] || ([ -f "$dir/pyproject.toml" ] && grep -q '\[tool\.ruff\]' "$dir/pyproject.toml" 2>/dev/null); then
    echo "ruff"
  elif command -v ruff &>/dev/null; then
    echo "ruff"
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

# ── Extract Tested-RED: node IDs from a commit-message command string ─
# Reads the (quote-normalized) `git commit` command and prints every pytest
# node ID carried by a `Tested-RED:` trailer, one per line. A node ID is the
# token following the trailer keyword, up to the next quote/whitespace. Used by
# the RED-proof verifier to know which test(s) to execute.
extract_tested_red_nodes() {
  local command="$1"
  # grep exits 1 when there is no trailer; that is a normal "no nodes" result,
  # not an error, so swallow it (|| true) to stay safe under `set -e`.
  printf '%s\n' "$command" \
    | grep -oiE 'Tested-RED:[[:space:]]*[^[:space:]"'"'"']+' \
    | sed -E 's/^[Tt]ested-[Rr][Ee][Dd]:[[:space:]]*//' || true
}

# ── Detect a pytest runner for a project ────────────────────────────
# Prints the command prefix used to invoke pytest, or empty if none resolves.
# Honors a project virtualenv binary first, then `python -m pytest`, then a
# bare `pytest` on PATH. Empty output ⇒ caller must degrade (cannot prove).
detect_pytest() {
  local dir="${1:-.}"
  if [ -x "$dir/.venv/bin/pytest" ]; then
    echo "$dir/.venv/bin/pytest"
  elif command -v pytest &>/dev/null; then
    echo "pytest"
  elif command -v python3 &>/dev/null && python3 -c 'import pytest' &>/dev/null; then
    echo "python3 -m pytest"
  elif command -v python &>/dev/null && python -c 'import pytest' &>/dev/null; then
    echo "python -m pytest"
  else
    echo ""
  fi
}

# ── Classify a pytest run for RED/GREEN proof ───────────────────────
# Runs a SINGLE pytest node and maps the outcome to a proof verdict, printed as
# one of: PASS | FAIL | BOOTSTRAP.
#
# The hard part is telling a genuine RED state apart from an environment that
# cannot run the test at all — the former must adjudicate, the latter must
# degrade (never a false block).
#
# pytest exit codes (https://docs.pytest.org): 0 all passed, 1 tests failed,
# 2 interrupted, 3 internal error, 4 usage error, 5 no tests collected.
#   • 0 → PASS (trustworthy).
#   • 1 → FAIL (trustworthy — assertion failed).
#   • 2-5 → AMBIGUOUS. A canonical red-before-green test imports a symbol that
#     does not exist yet, which pytest reports as a COLLECTION ImportError and
#     exits 4/5 — that IS a legitimate RED (FAIL), not a bootstrap problem. So
#     on 2-5 we inspect the output: an ImportError / ModuleNotFoundError /
#     NameError / AttributeError at collection is the missing implementation
#     under test → FAIL. Anything else (no runner, syntax error in harness,
#     missing third-party dep, internal error) → BOOTSTRAP → degrade to allow.
#
# The project root is placed on PYTHONPATH so first-party packages import the
# way they would under a normal project pytest config (rootdir is not always on
# sys.path for a bare repo).
#
# Usage: run_pytest_node <project_root> <node_id>
run_pytest_node() {
  local root="$1" node="$2" runner raw rc
  runner=$(detect_pytest "$root")
  [ -z "$runner" ] && { echo "BOOTSTRAP"; return 0; }

  # -p no:cacheprovider: never write a cache (sandbox-safe, no side effects).
  # --no-header -q: quiet. Run only the named node.
  raw=$(cd "$root" && PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
        $runner -p no:cacheprovider --no-header -q "$node" 2>&1) && rc=0 || rc=$?

  case "$rc" in
    0) echo "PASS" ; return 0 ;;
    1) echo "FAIL" ; return 0 ;;
  esac

  # Ambiguous exit (2-5): a collection error naming a missing symbol of the code
  # under test is a real RED; anything else is an environment failure.
  if echo "$raw" | grep -qE '(ModuleNotFoundError|ImportError|NameError|AttributeError)'; then
    echo "FAIL"
  else
    echo "BOOTSTRAP"
  fi
}
