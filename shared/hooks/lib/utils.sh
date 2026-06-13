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

# ── Opt-in telemetry event log ──────────────────────────────────────
# Appends one JSON object per event to
# ${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl, ONLY
# when AI_TOOLKIT_TELEMETRY=1 (otherwise a silent no-op that creates nothing).
# Fields are metadata only: ts (ISO-8601 UTC), hook (script basename), decision
# (deny/warn), repo (project-root BASENAME, never a path). NEVER log commands,
# messages, paths, or payload content — deny/warn messages may quote the
# blocked command (secret-leak risk).
# Telemetry must be invisible: zero bytes on stdout/stderr, never changes the
# hook's exit code — the whole body is redirected and failure-swallowed.
# Reads the hook's global $INPUT payload (if set) to resolve the project root.
telemetry_event() {
  [ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || return 0
  local decision="${1:-}" dir ts hook root repo
  {
    dir="${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}"
    mkdir -p "$dir"
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    hook=$(basename "$0")
    root=$(project_root_from_payload "${INPUT:-}")
    repo=$(basename "$root")
    [ -z "$repo" ] && repo="unknown"
    if command -v jq &>/dev/null; then
      jq -nc --arg ts "$ts" --arg hook "$hook" --arg decision "$decision" --arg repo "$repo" \
        '{ts: $ts, hook: $hook, decision: $decision, repo: $repo}' >> "$dir/events.jsonl"
    else
      printf '{"ts":"%s","hook":"%s","decision":"%s","repo":"%s"}\n' \
        "$ts" "$hook" "$decision" "$repo" >> "$dir/events.jsonl"
    fi
  } >/dev/null 2>&1 || true
  return 0
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
  # env -u GIT_*: this backstop runs from the pre-push hook, where git exports
  # GIT_DIR/GIT_WORK_TREE/etc.; drop them for the pytest child (defense-in-depth
  # behind tests/conftest.py, issue #30) so a node that shells out to git hits
  # its own tmpdir, never the REAL repo.
  raw=$(cd "$root" && PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
        env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
            -u GIT_COMMON_DIR -u GIT_NAMESPACE -u GIT_PREFIX \
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
