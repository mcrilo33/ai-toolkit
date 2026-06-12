#!/usr/bin/env bash
# delegation-gate-warn — delegation-hint hook for specialist agents.
#
# During editing (most commands) the hints are advisory warnings, emitted once
# per repo. At the SHIPPING gate (git push / gh pr create|merge) the unresolved
# code-review hint is promoted on Cursor to a hard DENY via ship_gate_enforce
# (see lib/utils.sh); the planner hint stays advisory — file count is a proxy:
#   • Cursor (beforeShellExecution): hard DENY (exit 2) — ship blocked until the
#     code-review is addressed.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Resolve git dir for stable per-repo warning cache.
PROJECT_ROOT=$(project_root_from_payload "$INPUT")
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

# Shipping-gate messages are accumulated and enforced once at the end so a
# Cursor DENY can carry every unresolved hint in a single agent_message rather
# than halting on the first.
SHIP_GATE_MSG=""
ship_gate_add() {
  local message="$1"
  if [ -n "$SHIP_GATE_MSG" ]; then
    SHIP_GATE_MSG="$SHIP_GATE_MSG

$message"
  else
    SHIP_GATE_MSG="$message"
  fi
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
if is_git_push_or_pr "$COMMAND"; then
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

# planner — advisory nudge only. File count is a proxy, so it never blocks the
# ship, and it is skipped for mechanical changes (rename/move/format).
if [ "$CHANGED_COUNT" -ge 3 ] && [ "$REFACTOR_PATTERN" -eq 0 ]; then
  warn_once "planner" "⚠ $CHANGED_COUNT files changed. If the path isn't obvious or the change crosses module/API/data boundaries, spawn planner before writing more code."
fi

# code-review — enforced at push/PR, never silenced
if [ "$SHIPPING" -eq 1 ] && [ "$CHANGED_COUNT" -ge 1 ]; then
  ship_gate_add "⚠ STOP. Do NOT push yet. Spawn code-review on the current diff first, then push once it passes."
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
  warn_once "tdd-red" "⚠ Source changed with no test yet. Write a failing test first (inline is fine) — the RED commit needs a Tested-RED trailer that red-proof-verify can prove fails."
fi

# tdd-green
if [ "$RUNS_TESTS" -eq 1 ] && [ "$TESTS_TOUCHED" -eq 1 ]; then
  warn_once "tdd-green" "⚠ Tests present and running. Implement the minimal code to make them pass (inline is fine); commit-gauntlet lints and typechecks the changed lines."
fi

# tdd-refactor
if [ "$REFACTOR_PATTERN" -eq 1 ] && [ "$RUNS_TESTS" -eq 1 ]; then
  warn_once "tdd-refactor" "⚠ STOP. Tests are green and cleanup is in progress. Spawn tdd-refactor now — do not refactor in the main agent loop."
fi

# Enforce accumulated shipping-gate hints once: DENY on Cursor, warn elsewhere.
if [ -n "$SHIP_GATE_MSG" ]; then
  ship_gate_enforce "$INPUT" "$SHIP_GATE_MSG"
fi

exit 0