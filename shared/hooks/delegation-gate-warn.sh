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

# planner
if [ "$CHANGED_COUNT" -ge 3 ]; then
  warn_once "planner" "Routing hint: multi-file change detected ($CHANGED_COUNT files). Consider spawning planner first."
fi

# code-review
if [ "$SHIPPING" -eq 1 ] && [ "$CHANGED_COUNT" -ge 1 ]; then
  warn_once "code-review" "Routing hint: shipping action detected. Consider spawning code-review before push/PR."
fi

# debug
if [ "$RUNS_TESTS" -eq 1 ] || [ "$DEBUG_PATTERN" -eq 1 ]; then
  warn_once "debug" "Routing hint: test/debug workflow detected. Consider spawning debug for faster root-cause analysis."
fi

# devops
if [ "$DEVOPS_TOUCHED" -eq 1 ]; then
  warn_once "devops" "Routing hint: CI/CD or infrastructure signals detected. Consider spawning devops."
fi

# documentation
if [ "$DOCS_ONLY" -eq 1 ]; then
  warn_once "documentation" "Routing hint: docs-only change set detected. Consider spawning documentation."
fi

# refactor
if [ "$REFACTOR_PATTERN" -eq 1 ]; then
  warn_once "refactor" "Routing hint: rename/restructure pattern detected. Consider spawning refactor for cross-cutting changes."
fi

# security-reviewer
if [ "$SECURITY_TOUCHED" -eq 1 ]; then
  warn_once "security-reviewer" "Routing hint: security-sensitive area detected. Consider spawning security-reviewer."
fi

# architect
if [ "$ARCHITECT_PATTERN" -eq 1 ]; then
  warn_once "architect" "Routing hint: architecture/design signals detected. Consider spawning architect before implementation."
fi

# tdd-red
if [ "$SOURCE_TOUCHED" -eq 1 ] && [ "$TESTS_TOUCHED" -eq 0 ]; then
  warn_once "tdd-red" "Routing hint: source changed without tests. Consider spawning tdd-red to define failing tests first."
fi

# tdd-green
if [ "$RUNS_TESTS" -eq 1 ] && [ "$TESTS_TOUCHED" -eq 1 ]; then
  warn_once "tdd-green" "Routing hint: test-execution with test changes detected. Consider spawning tdd-green to make failing tests pass minimally."
fi

# tdd-refactor
if [ "$REFACTOR_PATTERN" -eq 1 ] && [ "$RUNS_TESTS" -eq 1 ]; then
  warn_once "tdd-refactor" "Routing hint: test-run plus refactor pattern detected. Consider spawning tdd-refactor once tests are green."
fi

exit 0