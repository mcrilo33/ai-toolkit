#!/usr/bin/env bash
# Shared utilities for ai-toolkit hook scripts.
# Source this file at the top of each hook:
#   HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$HOOK_DIR/lib/utils.sh"

set -euo pipefail

# ── Unified telemetry span layer ────────────────────────────────────
# Source the (self-contained) emit layer from this lib's own directory — both
# files are synced together into the same hooks/lib/ dir — and arm the per-hook
# span so every hook that sources utils.sh emits one kind=hook span at exit.
_UTILS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=telemetry.sh
source "$_UTILS_LIB_DIR/telemetry.sh"
telemetry_arm_hook_span

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

# ── Recognize a `git commit` invocation ──────────────────────────────
# Same boundary-aware matching as is_git_commit_or_add, narrowed to the
# commit subcommand (the commit-time hooks must not fire on `git add`).
# The boundary class additionally covers `$(` / backtick substitutions and
# env-assignment prefixes (`VAR=1 git commit`); newlines are boundaries too
# (grep matches each line's `^` independently).
is_git_commit() {
  local cmd="$1"
  printf '%s' "$cmd" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[^[:space:]]+|--[^[:space:]]+|-C[[:space:]]+[^[:space:]]+))*[[:space:]]+commit\b'
}

# ── Recognize a `git push` / `gh pr create|merge` invocation ─────────
# The shipping-gate analogue of is_git_commit_or_add: matches the command
# anywhere at a command boundary (start of string/line, or after `;`, `&`,
# `|`, `&&`, `||`, `$(`, or backtick), tolerating env-assignment prefixes,
# so chained or prefixed forms are not bypassed: `cd x && git push`,
# `true; git push`, `VAR=1 git push`, `git -C path push`, `gh pr create`.
# Returns 0 on match.
is_git_push_or_pr() {
  local cmd="$1"
  printf '%s' "$cmd" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(git([[:space:]]+(-[^[:space:]]+|--[^[:space:]]+|-C[[:space:]]+[^[:space:]]+))*[[:space:]]+push\b|gh[[:space:]]+pr[[:space:]]+(create|merge)\b)'
}

# ── Recognize a branch-CREATING checkout/switch ──────────────────────
# Matches branch creation at a command boundary (same boundary-awareness as
# is_git_commit), tolerating leading options and env-assignment prefixes, so
# chained/prefixed forms are not bypassed: `cd x && git checkout -b y`,
# `git -C path switch -c y`. Covers the spellings:
#   checkout -b|-B|--orphan     switch -c|-C|--create|--force-create
#   branch <name>               (bare create; `git branch -d`/-a/--list etc. excluded)
# A plain branch switch (`git checkout main`, `git switch x`) and a bare
# `git branch` listing do NOT match — only branch creation, which on the hub
# belongs in a worktree. Returns 0 on match. Note: `git worktree add -b` is the
# sanctioned dispatch path and is NOT matched (its subcommand is `worktree`).
is_git_branch_create() {
  local cmd="$1"
  printf '%s' "$cmd" | grep -qE '(^|[;&|`]|\$\()[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+(-[^[:space:]]+|--[^[:space:]]+|-C[[:space:]]+[^[:space:]]+))*[[:space:]]+(checkout([[:space:]]+-[^[:space:]]+)*[[:space:]]+(-[a-zA-Z]*[bB]|--orphan)\b|switch([[:space:]]+-[^[:space:]]+)*[[:space:]]+(-[a-zA-Z]*[cC]|--create|--force-create)\b|branch[[:space:]]+[^-[:space:]])'
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
  local content="$1" pattern desc
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

# ── Opt-in telemetry decision record ────────────────────────────────
# Back-compat shim. A hook no longer writes a per-decision line here; instead
# every hook invocation emits ONE kind=hook span at exit (see telemetry.sh).
# This records the decision so that span's `status` reflects deny/warn rather
# than the default success. Kept so existing deny()/warn() callers are unchanged
# and any external caller keeps working. Invisible + no-op when telemetry is off
# (telemetry_set_status only mutates a shell var; nothing is written here).
telemetry_event() {
  telemetry_set_status "${1:-success}"
}

# ── Deny output (cross-platform) ────────────────────────────────────
# Works for Copilot, Cursor, and Claude preToolUse hooks.
deny() {
  local reason="$1"
  telemetry_event "deny"
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
  telemetry_event "warn"
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

# ── Review-evidence artifact helpers (diff-bound reviewer separation) ─
# A code-review APPROVE is recorded as `.review/<diff_hash>.json` in the repo.
# The diff hash binds the approval to the EXACT content reviewed: the push hook
# recomputes the hash of the pushed range and refuses to ship unless a matching
# APPROVE artifact exists. This closes the "type the trailer, skip the review"
# hole. It does NOT prove a *different agent* authored the artifact — that is
# unprovable by a local hook (see reviewer-sep-warn header).
#
# CRITICAL invariant (verified by spike): the hash MUST be computed identically
# at review time and push time. The only recipe that matches across clean adds,
# modifies, renames, and CRLF files is:
#   • review time:  stage all (git add -A), then `git diff --cached -M <BASE>`
#   • push time:     `git diff -M <BASE>..HEAD`
# both with --no-color --no-ext-diff, the `.review/` pathspec exclusion, and
# LF-normalization, hashed with sha256. Renames only pair once staged/committed,
# and untracked adds are invisible to a non-cached diff — hence the mandatory
# `git add -A` before review-time hashing.

# sha256 of stdin, portable across Linux (sha256sum) and macOS (shasum -a 256).
sha256_stdin() {
  if command -v sha256sum &>/dev/null; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

# Resolve the base ref a change branches from. Tries the same chain as the
# code-review agent recipe so review-time and push-time hashes agree: tracked
# upstream → origin/main → origin/HEAD. Prints empty on total failure (caller
# must then degrade to allow — never a false block).
review_base_ref() {
  local root="$1" base
  base=$(git -C "$root" merge-base '@{upstream}' HEAD 2>/dev/null) \
    || base=$(git -C "$root" merge-base origin/main HEAD 2>/dev/null) \
    || base=$(git -C "$root" merge-base origin/HEAD HEAD 2>/dev/null) \
    || base=""
  echo "$base"
}

# Compute the content-bound diff hash. mode ∈ {staged, range}.
#   staged: `git diff --cached -M <base>`   (review time, after `git add -A`)
#   range:  `git diff -M <base>..HEAD`       (push time)
# Prints the 64-char sha256, or empty on git failure / empty base.
review_diff_hash() {
  local root="$1" base="$2" mode="$3" spec
  [ -z "$base" ] && { echo ""; return 0; }
  case "$mode" in
    staged) spec="--cached $base" ;;
    range)  spec="$base..HEAD" ;;
    *) echo ""; return 0 ;;
  esac
  # shellcheck disable=SC2086
  git -C "$root" diff --no-color --no-ext-diff -M $spec -- . ':(exclude).review/' 2>/dev/null \
    | sed -e 's/\r$//' \
    | sha256_stdin
}

# Read the review artifact for a given hash; prints its JSON or empty.
read_review_artifact() {
  local root="$1" hash="$2" path
  path="$root/.review/$hash.json"
  [ -f "$path" ] && cat "$path" || echo ""
}

# Extract the verdict field (APPROVE | REQUEST_CHANGES) from artifact JSON.
review_artifact_verdict() {
  local json="$1"
  [ -z "$json" ] && { echo ""; return 0; }
  if command -v jq &>/dev/null; then
    echo "$json" | jq -r '.verdict // empty' 2>/dev/null
  else
    echo "$json" | grep -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# ── Find project root (walk up to .git) ─────────────────────────────
# Stops at the first ancestor containing a .git entry — a DIRECTORY (normal
# checkout) OR a FILE (a linked worktree / submodule gitlink). Honoring the
# file form is essential inside a worktree: its .git is a gitlink file, so a
# directory-only test walks straight past it to the next real .git up the tree
# (e.g. a dotfiles repo at $HOME), misresolving the root for every hook that
# relies on this. `-e` matches either form.
find_project_root() {
  local dir="${1:-$(pwd)}"
  while [ "$dir" != "/" ]; do
    [ -e "$dir/.git" ] && { echo "$dir"; return; }
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
# one of: PASS | FAIL | BOOTSTRAP | BREACH. BREACH (issue #31) signals the node
# mutated the real repo and was rolled back — callers must treat it as a block,
# never PASS/FAIL.
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
  local root="$1" node="$2" runner raw rc before changed
  runner=$(detect_pytest "$root")
  [ -z "$runner" ] && { echo "BOOTSTRAP"; return 0; }

  # Issue #31: snapshot the real repo's integrity markers around the node run, the
  # same tripwire the gate uses. The env -u strip below keeps a git-shelling node
  # in its own tmpdir; if a node escapes anyway and mutates THIS repo, restore the
  # snapshot and report BREACH so the caller blocks instead of trusting the run.
  before="$(cd "$root" && tripwire_capture)"

  # -p no:cacheprovider: never write a cache (sandbox-safe, no side effects).
  # --no-header -q: quiet. Run only the named node.
  # env -u GIT_*: this backstop runs from the pre-push hook, where git exports
  # GIT_DIR/GIT_WORK_TREE/etc.; drop them for the pytest child (defense-in-depth
  # behind tests/conftest.py, issue #30) so a node that shells out to git hits
  # its own tmpdir, never the REAL repo. The GIT_CONFIG_* KEY/VALUE family that
  # `env -u` can't glob is left to the conftest layer.
  raw=$(cd "$root" && PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
        env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
            -u GIT_COMMON_DIR -u GIT_NAMESPACE -u GIT_PREFIX \
            -u GIT_CONFIG -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_SYSTEM -u GIT_CONFIG_COUNT \
        $runner -p no:cacheprovider --no-header -q "$node" 2>&1) && rc=0 || rc=$?

  # Tripwire verdict takes precedence over the pass/fail outcome: a node that
  # corrupted the repo is untrustworthy regardless of its exit code.
  if ! changed="$(cd "$root" && tripwire_check "$before")"; then
    _tripwire_report_breach "$changed" "restoring the snapshot — verdict BREACH (the caller blocks)."
    # `|| true`: never let a restore hiccup abort under set -e before the verdict
    # is printed — an empty verdict matches no caller arm and would silently NOT
    # block, the exact failure mode this guards against.
    (cd "$root" && tripwire_restore "$before") || true
    echo "BREACH"
    return 0
  fi

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

# ── Review-stamp signature helpers (HMAC-signed review approval) ─────
# The review-stamp MCP server signs each artifact with
# HMAC-SHA256(key, "<diff_hash>:<verdict>") keyed by REVIEW_STAMP_KEY. These
# helpers let the push gate verify that signature: forging an APPROVE now
# requires the signing key, not just a file write.

# Resolve the signing/verification key: env REVIEW_STAMP_KEY first, else the
# macOS Keychain item REVIEW_STAMP_KEY, else empty (caller degrades).
review_stamp_key() {
  if [ -n "${REVIEW_STAMP_KEY:-}" ]; then
    printf '%s' "$REVIEW_STAMP_KEY"
    return 0
  fi
  if command -v security &>/dev/null; then
    security find-generic-password -a "$USER" -s REVIEW_STAMP_KEY -w 2>/dev/null || true
    return 0
  fi
  echo ""
}

# Verify an artifact signature. Usage:
#   review_stamp_verify_sig <hash> <verdict> <signature> <key>
# Recomputes HMAC-SHA256(key, "<hash>:<verdict>") and compares in constant
# time. Returns 0 when the signature matches, non-zero otherwise (including
# when python3 is unavailable — an unverifiable signature must never pass).
#
# The key and candidate signature travel via the ENVIRONMENT, never argv:
# argv is visible to every same-user process (ps / /proc), which would leak
# the key beyond the documented Keychain ceiling.
review_stamp_verify_sig() {
  local hash="$1" verdict="$2" signature="$3" key="$4"
  command -v python3 &>/dev/null || return 1
  printf '%s' "$hash:$verdict" \
    | REVIEW_STAMP_VERIFY_KEY="$key" REVIEW_STAMP_VERIFY_SIG="$signature" python3 -c '
import hashlib, hmac, os, sys

expected = hmac.new(
    os.environ["REVIEW_STAMP_VERIFY_KEY"].encode(),
    sys.stdin.buffer.read(),
    hashlib.sha256,
).hexdigest()
sys.exit(0 if hmac.compare_digest(expected, os.environ["REVIEW_STAMP_VERIFY_SIG"]) else 1)
' 2>/dev/null
}

# Extract the signature field from artifact JSON (empty when absent).
review_artifact_signature() {
  local json="$1"
  [ -z "$json" ] && { echo ""; return 0; }
  if command -v jq &>/dev/null; then
    echo "$json" | jq -r '.signature // empty' 2>/dev/null
  else
    echo "$json" | grep -oE '"signature"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*: *"//;s/"$//'
  fi
}

# ── Repo-integrity tripwire (issue #31) ─────────────────────────────
# Belt-and-suspenders for the #29/#30 isolation-breach CLASS. The GIT_DIR leak
# corrupted the real repo SILENTLY — a fixture's git call moved `main` and
# flipped core.bare during the pre-push gate, unnoticed until found by hand. The
# env strip (#30) closed the KNOWN vector; this tripwire catches ANY future one.
#
# Wrap the gate's pytest run: snapshot the real repo's integrity markers before,
# re-read after, and if anything moved, ABORT the push and RESTORE the snapshot.
# Markers (cheap — one show-ref + two config reads per side):
#   • HEAD + every local ref tip   (git show-ref --head)
#   • core.bare
#   • core.worktree
# The pytest child runs with GIT_* unset (#30), so a hermetic test that
# creates/deletes its OWN tmpdir repo never touches these markers — only a real
# escape into THIS repo trips it.
#
# Live siblings (issue #135): worktrees share this ref store, so a live sibling
# spoke committing during a long gate moves a `refs/heads/*` marker without any
# escape. tripwire_check treats a fast-forward advance (or creation) of a branch
# checked out in another registered worktree as benign, and tripwire_restore
# never orphans commits — it refuses to rewind any ref to a strict ancestor of
# its current tip and never deletes a ref a registered worktree has checked out
# (the abort, not the rewind, is the protection).
#
# Snapshot format (one marker per line, parseable by check/restore):
#   ref <sha> <refname>        # HEAD line included via --head
#   cfg core.bare <value|—>
#   cfg core.worktree <value|—>
TRIPWIRE_UNSET='—'            # sentinel for a config marker that is not set
TRIPWIRE_BREACH_RC=97        # exit code on breach; outside pytest's 0-5 range

# Capture the integrity markers for the repo of the current git context (the
# real repo the hook targets). Read-only.
tripwire_capture() {
  local line bare worktree
  while IFS= read -r line; do
    [ -n "$line" ] && printf 'ref %s\n' "$line"
  done < <(git show-ref --head 2>/dev/null || true)
  bare="$(git config --get core.bare 2>/dev/null || printf '%s' "$TRIPWIRE_UNSET")"
  worktree="$(git config --get core.worktree 2>/dev/null || printf '%s' "$TRIPWIRE_UNSET")"
  printf 'cfg core.bare %s\n' "$bare"
  printf 'cfg core.worktree %s\n' "$worktree"
}

# Branches checked out in registered worktrees OTHER than the current one —
# the live sibling spokes sharing this ref store (issue #135). Worktrees share
# `.git/refs`, so these refs legitimately move while a long gate runs: a live
# spoke committing mid-gate is a fast-forward advance of exactly one of them.
# The current worktree's own branch is deliberately NOT listed — nothing else
# may move it during the gate, so an advance there is still an escape (the
# classic #31 sneak commit).
tripwire_sibling_worktree_refs() {
  local cur
  cur="$(git symbolic-ref -q HEAD 2>/dev/null || true)"
  git worktree list --porcelain 2>/dev/null \
    | awk '$1=="branch" {print $2}' \
    | grep -vxF "${cur:-refs/__none__}" || true
}

# A changed ref marker is benign iff it is a branch checked out in a live
# sibling worktree that either fast-forwarded (snapshot tip is an ancestor of
# the new tip — a spoke committed) or appeared during the run (the hub spawned
# a new spoke). Anything else — cfg drift, HEAD moves, sibling rewinds or
# deletions, refs no worktree has checked out — has no live-spoke explanation
# and stays a breach.
_tripwire_benign_ref_change() {
  local name="$1" before="$2" after="$3" siblings="$4" b_sha a_sha
  case "$name" in refs/heads/*) ;; *) return 1 ;; esac
  printf '%s\n' "$siblings" | grep -qxF "$name" || return 1
  b_sha="$(printf '%s\n' "$before" | awk -v r="$name" '$1=="ref" && $3==r {print $2; exit}')"
  a_sha="$(printf '%s\n' "$after"  | awk -v r="$name" '$1=="ref" && $3==r {print $2; exit}')"
  [ -n "$a_sha" ] || return 1                        # deleted → breach
  [ -z "$b_sha" ] && return 0                        # created by a spawning sibling
  git merge-base --is-ancestor "$b_sha" "$a_sha" 2>/dev/null   # FF advance only
}

# Compare a prior snapshot ($1) against the markers now. Prints the names of the
# markers that changed (e.g. `refs/heads/main`, `core.bare`) and returns 1 when
# anything changed, 0 when the repo is intact. Benign moves of live sibling
# worktree refs (issue #135, see _tripwire_benign_ref_change) are not changes.
tripwire_check() {
  local before="$1" after raw siblings survivors name
  after="$(tripwire_capture)"
  if [ "$before" = "$after" ]; then
    return 0
  fi
  raw="$(awk '
    NR==FNR { b[$0] = 1; next }
            { a[$0] = 1 }
    END {
      for (l in b) if (!(l in a)) mark(l)
      for (l in a) if (!(l in b)) mark(l)
      for (k in ch) print k
    }
    function mark(line, p) {
      if (line ~ /^ref /)      { split(line, p, " "); ch[p[3]] = 1 }
      else if (line ~ /^cfg /) { split(line, p, " "); ch[p[2]] = 1 }
      else                       ch[line] = 1
    }
  ' <(printf '%s\n' "$before") <(printf '%s\n' "$after"))"
  siblings="$(tripwire_sibling_worktree_refs)"
  survivors=""
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    _tripwire_benign_ref_change "$name" "$before" "$after" "$siblings" && continue
    survivors+="${name}"$'\n'
  done <<< "$raw"
  if [ -z "$survivors" ]; then
    return 0
  fi
  printf '%s' "$survivors"
  return 1
}

_tripwire_restore_cfg() {
  local key="$1" val="$2"
  if [ -z "$val" ] || [ "$val" = "$TRIPWIRE_UNSET" ]; then
    git config --unset "$key" 2>/dev/null || true
  else
    git config "$key" "$val" 2>/dev/null || true
  fi
}

# Read a captured config marker's value from the snapshot, verbatim. The value
# is everything after the `cfg <key> ` prefix, so a path containing spaces (a
# core.worktree) round-trips intact rather than being truncated at the first
# space. Prints the sentinel TRIPWIRE_UNSET when the key was not captured.
_tripwire_cfg_value() {
  local snapshot="$1" key="$2" line
  line="$(printf '%s\n' "$snapshot" | grep -m1 -F "cfg $key " || true)"
  if [ -z "$line" ]; then
    printf '%s' "$TRIPWIRE_UNSET"
    return 0
  fi
  printf '%s' "${line#cfg "$key" }"
}

# Restore the markers captured in $1 after a breach: reset each ref to its
# snapshot tip, delete refs that appeared during the run, and restore
# core.bare/core.worktree. Best-effort — leaves the repo as the snapshot found it.
#
# No-data-loss rule (issue #135): restore never ORPHANS commits. A ref whose
# snapshot tip is a strict ancestor of its current tip only gained commits —
# rewinding it destroys them (four live spokes were rewound this way), so it is
# left in place with a warning; the caller's abort is the protection. A ref
# that LOST commits (snapshot ahead of, or diverged from, the tip) is genuine
# corruption and is still restored. An appeared ref checked out in a registered
# worktree is a live spoke's anchor and is never deleted.
#
# HEAD scope: a moved BRANCH ref is restored directly, and HEAD (symbolic) then
# follows its branch. A breach that re-points HEAD's own symbolic target (a stray
# `git checkout`/detach) is still DETECTED — the HEAD line in the snapshot differs,
# so the push is aborted — but its symref is not auto-rewound here; the abort,
# not the rewind, is the protection. The #29/#30 incident moved a branch ref and
# flipped core.bare, both fully restored.
tripwire_restore() {
  local before="$1" kind sha name snap_refs wt_refs cur_sha cur_ref
  wt_refs="$(git worktree list --porcelain 2>/dev/null | awk '$1=="branch" {print $2}' || true)"
  # Reset every snapshot ref to its captured tip (HEAD is symbolic — it follows
  # its branch, so resetting the branch ref restores it) — unless that would
  # rewind the ref to a strict ancestor of where it is now.
  while read -r kind sha name; do
    [ "$kind" = "ref" ] || continue
    [ "$name" = "HEAD" ] && continue
    cur_sha="$(git rev-parse -q --verify "$name" 2>/dev/null || true)"
    if [ -n "$cur_sha" ] && [ "$cur_sha" != "$sha" ] \
       && git merge-base --is-ancestor "$sha" "$cur_sha" 2>/dev/null; then
      echo "tripwire: NOT rewinding $name to $sha — strict ancestor of its tip $cur_sha (would orphan commits); the abort is the protection." >&2
      continue
    fi
    git update-ref "$name" "$sha" 2>/dev/null || true
  done <<< "$before"
  # Drop refs that appeared during the run (present now, absent in the snapshot),
  # except a branch checked out in a registered worktree — a live spoke's anchor.
  snap_refs="$(printf '%s\n' "$before" | awk '$1=="ref" && $3!="HEAD" {print $3}')"
  while read -r cur_sha cur_ref; do
    [ -n "$cur_ref" ] || continue
    if printf '%s\n' "$snap_refs" | grep -qxF "$cur_ref"; then
      continue
    fi
    if printf '%s\n' "$wt_refs" | grep -qxF "$cur_ref"; then
      echo "tripwire: NOT deleting $cur_ref — checked out in a registered worktree." >&2
      continue
    fi
    git update-ref -d "$cur_ref" 2>/dev/null || true
  done < <(git show-ref 2>/dev/null || true)
  # Restore the config markers (whitespace-preserving extraction).
  _tripwire_restore_cfg core.bare "$(_tripwire_cfg_value "$before" core.bare)"
  _tripwire_restore_cfg core.worktree "$(_tripwire_cfg_value "$before" core.worktree)"
}

# Print a breach report to stderr: the header, each changed marker (one per
# line), and the caller's action line. Shared by run_under_tripwire (the gate)
# and run_pytest_node (the red-proof backstop) so both speak the same language.
_tripwire_report_breach() {
  local changed="$1" action="$2" m
  {
    echo "tripwire: REPO-INTEGRITY BREACH — the test run mutated THIS repo:"
    while IFS= read -r m; do
      [ -n "$m" ] && echo "tripwire:   - $m"
    done <<< "$changed"
    echo "tripwire: a test escaped isolation and wrote to the real repo (issue #31)."
    echo "tripwire: $action"
  } >&2
}

# Run "$@" under the tripwire. On a clean run, returns the command's own exit
# code. On a breach (the run changed THIS repo's markers), restores the snapshot,
# prints which markers moved to stderr, and returns TRIPWIRE_BREACH_RC so the
# caller aborts the push.
run_under_tripwire() {
  local before changed rc=0
  before="$(tripwire_capture)"
  "$@" || rc=$?
  if changed="$(tripwire_check "$before")"; then
    return "$rc"
  fi
  _tripwire_report_breach "$changed" "restoring the snapshot and ABORTING the push."
  tripwire_restore "$before"
  return "$TRIPWIRE_BREACH_RC"
}
