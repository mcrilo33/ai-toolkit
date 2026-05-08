#!/usr/bin/env bash
# delegation-gate-warn — PreToolUse hook
# Emits warning-only delegation hints for specialist agents.
# Non-blocking by design: always exits 0.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_bash_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Resolve git dir for stable per-repo warning cache.
PROJECT_ROOT=$(find_project_root "$(pwd)")
GIT_DIR_RAW=$(git -C "$PROJECT_ROOT" rev-parse --git-dir 2>/dev/null || true)

if [ -n "$GIT_DIR_RAW" ]; then
  case "$GIT_DIR_RAW" in
    /*) GIT_DIR="$GIT_DIR_RAW" ;;
    *) GIT_DIR="$PROJECT_ROOT/$GIT_DIR_RAW" ;;
  esac
else
  GIT_DIR="$PROJECT_ROOT/.git"
fi

STATE_DIR="$GIT_DIR/.ai-toolkit-hooks/delegation-hints"
mkdir -p "$STATE_DIR" 2>/dev/null || true

warn_once() {
  local key="$1"
  local message="$2"
  local safe_key
  safe_key=$(echo "$key" | tr -c 'a-zA-Z0-9._-' '_')
  local marker="$STATE_DIR/$safe_key"
  [ -f "$marker" ] && return 0
  warn "$message"
  : > "$marker" 2>/dev/null || true
}

# Always warn regardless of prior markers — used at shipping gates (commit/push)
# so that ignored hints are repeated at the last moment before code is shipped.
warn_always() {
  local message="$1"
  warn "$message"
}

changed_files() {
  git -C "$PROJECT_ROOT" diff --name-only --diff-filter=ACMR HEAD 2>/dev/null || true
}

CHANGED=$(changed_files)
CHANGED_COUNT=0
if [ -n "$CHANGED" ]; then
  CHANGED_COUNT=$(echo "$CHANGED" | sed '/^$/d' | wc -l | tr -d ' ')
fi

TESTS_TOUCHED=0
if echo "$CHANGED" | grep -qE '(^|/)(tests?/|test_.*\.py$|.*\.test\.(ts|tsx|js|jsx)$|.*\.spec\.(ts|tsx|js|jsx)$)'; then
  TESTS_TOUCHED=1
fi

DOCS_ONLY=0
if [ "$CHANGED_COUNT" -gt 0 ] && ! echo "$CHANGED" | grep -qE -v '(^docs/|\.md$)'; then
  DOCS_ONLY=1
fi

SOURCE_TOUCHED=0
if echo "$CHANGED" | grep -qE '\.(py|pyi|ts|tsx|js|jsx|go|rs|java)$'; then
  SOURCE_TOUCHED=1
fi

SECURITY_TOUCHED=0
if echo "$CHANGED $COMMAND" | grep -qiE '(auth|oauth|jwt|token|secret|password|credential|permission|payment|pii|encryption)'; then
  SECURITY_TOUCHED=1
fi

DEVOPS_TOUCHED=0
if echo "$CHANGED $COMMAND" | grep -qiE '(\.github/workflows/|Dockerfile|docker-compose|compose\.ya?ml|k8s|kubectl|helm|terraform|tfvars|ansible|deploy)'; then
  DEVOPS_TOUCHED=1
fi

RUNS_TESTS=0
if echo "$COMMAND" | grep -qiE '(^|[[:space:]])(pytest|npm[[:space:]]+test|pnpm[[:space:]]+test|yarn[[:space:]]+test|go[[:space:]]+test|cargo[[:space:]]+test)\b'; then
  RUNS_TESTS=1
fi

SHIPPING=0
if echo "$COMMAND" | grep -qiE '^\s*(git\s+push\b|gh\s+pr\s+(create|merge)\b)'; then
  SHIPPING=1
fi

COMMITTING=0
if echo "$COMMAND" | grep -qiE '^\s*git\s+commit\b'; then
  COMMITTING=1
fi

REFACTOR_PATTERN=0
if echo "$COMMAND" | grep -qiE '(\bgit\s+mv\b|\bmv\b|\brename\b|\bsed\s+-i\b|\bperl\s+-pi\b|\brefactor\b)'; then
  REFACTOR_PATTERN=1
fi

ARCHITECT_PATTERN=0
if echo "$COMMAND" | grep -qiE '(\barchitecture\b|\barchitect\b|\badr\b|\bdesign\s+doc\b|\bdata\s+model\b)'; then
  ARCHITECT_PATTERN=1
fi

DEBUG_PATTERN=0
if echo "$COMMAND" | grep -qiE '(traceback|stack\s*trace|failing|failed|error)'; then
  DEBUG_PATTERN=1
fi

# planner — warn once during editing, always at commit if still unresolved
if [ "$CHANGED_COUNT" -ge 3 ]; then
  if [ "$COMMITTING" -eq 1 ] || [ "$SHIPPING" -eq 1 ]; then
    warn_always "⚠ STOP. $CHANGED_COUNT files changed and planner was never spawned. Do NOT ship. Spawn planner now, get an implementation plan, then resume."
  else
    warn_once "planner" "⚠ STOP. Multi-file change detected ($CHANGED_COUNT files). Spawn planner now before writing any more code."
  fi
fi

# code-review — always warn at push/PR, never silenced
if [ "$SHIPPING" -eq 1 ] && [ "$CHANGED_COUNT" -ge 1 ]; then
  warn_always "⚠ STOP. Do NOT push yet. Spawn code-review on the current diff first, then push once it passes."
fi

# debug
if [ "$RUNS_TESTS" -eq 1 ] || [ "$DEBUG_PATTERN" -eq 1 ]; then
  warn_once "debug" "⚠ STOP. Failure or test signals detected. Spawn debug now to isolate the root cause before making changes."
fi

# devops
if [ "$DEVOPS_TOUCHED" -eq 1 ]; then
  warn_once "devops" "⚠ STOP. CI/CD or infrastructure files detected. Spawn devops now before modifying these files."
fi

# documentation
if [ "$DOCS_ONLY" -eq 1 ]; then
  warn_once "documentation" "⚠ STOP. Docs-only scope detected. Spawn documentation now — do not edit docs files directly in the main agent loop."
fi

# refactor
if [ "$REFACTOR_PATTERN" -eq 1 ]; then
  warn_once "refactor" "⚠ STOP. Cross-cutting rename or restructure detected. Spawn refactor now to handle this safely across all files."
fi

# security-reviewer
if [ "$SECURITY_TOUCHED" -eq 1 ]; then
  warn_once "security-reviewer" "⚠ STOP. Security-sensitive area detected. Spawn security-reviewer in parallel before proceeding."
fi

# architect
if [ "$ARCHITECT_PATTERN" -eq 1 ]; then
  warn_once "architect" "⚠ STOP. Architecture or design signals detected. Spawn architect first — do not implement before the structure is decided."
fi

# tdd-red
if [ "$SOURCE_TOUCHED" -eq 1 ] && [ "$TESTS_TOUCHED" -eq 0 ]; then
  warn_once "tdd-red" "⚠ STOP. Source files changed but no tests written yet. Spawn tdd-red now to write failing tests before any implementation."
fi

# tdd-green
if [ "$RUNS_TESTS" -eq 1 ] && [ "$TESTS_TOUCHED" -eq 1 ]; then
  warn_once "tdd-green" "⚠ STOP. Test changes + test run detected. Spawn tdd-green now to implement the minimal code that makes the tests pass."
fi

# tdd-refactor
if [ "$REFACTOR_PATTERN" -eq 1 ] && [ "$RUNS_TESTS" -eq 1 ]; then
  warn_once "tdd-refactor" "⚠ STOP. Tests are green and cleanup is in progress. Spawn tdd-refactor now — do not refactor in the main agent loop."
fi

exit 0