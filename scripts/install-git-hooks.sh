#!/usr/bin/env bash
# install-git-hooks.sh — Wire the ai-toolkit cage hooks as NATIVE git hooks.
#
# Why: the cage scripts normally run as agent preToolUse hooks
# (.cursor/hooks.json / .claude/settings.json). That enforcement depends on the
# agent runtime actually invoking them — which is not guaranteed for every
# runtime or for human-driven git. Installing them as native git hooks makes
# the BLOCKING gates fire on real `git commit` / `git push` regardless of who
# or what drives git.
#
# Mapping (native git hook → cage scripts):
#   commit-msg  → commit-quality + commit-gauntlet
#                 (both need the message: commit-quality validates format +
#                  issue-anchor; commit-gauntlet needs it for the Tested-RED
#                  carve-out. At commit-msg both the staged index AND the
#                  message file exist, so this is the correct stage for both.)
#   pre-push    → red-proof-warn + reviewer-sep-warn (advisory; read git log)
#                 + test-select (BLOCKING: the single owner of test execution —
#                 a tiered, diff-aware suite; non-zero exit aborts the push, #19)
#
# The native hooks synthesize the {"tool_input":{"command":"..."}} JSON the
# cage scripts expect (reusing the exact same scripts — single source of truth)
# and pipe it in. Blocking scripts exit non-zero to abort the git operation.
#
# Usage:
#   scripts/install-git-hooks.sh [target-repo]   # defaults to current repo
#   scripts/install-git-hooks.sh --uninstall [target-repo]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SHARED_HOOKS="$REPO_DIR/shared/hooks"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }

UNINSTALL=0
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    *) TARGET="$arg" ;;
  esac
done

TARGET="${TARGET:-$(pwd)}"
GIT_DIR_PATH=$(git -C "$TARGET" rev-parse --git-dir 2>/dev/null || true)
if [ -z "$GIT_DIR_PATH" ]; then
  error "Not a git repository: $TARGET"
  exit 1
fi
case "$GIT_DIR_PATH" in
  /*) ;; *) GIT_DIR_PATH="$TARGET/$GIT_DIR_PATH" ;;
esac
HOOKS_DST="$GIT_DIR_PATH/hooks"
SCRIPTS_DST="$HOOKS_DST/ai-toolkit-scripts"

MARK="# >>> ai-toolkit cage >>>"
MARK_END="# <<< ai-toolkit cage <<<"

remove_block() {
  # Remove an existing ai-toolkit block from a hook file, leaving other content.
  local file="$1"
  [ -f "$file" ] || return 0
  if grep -qF "$MARK" "$file"; then
    sed -i.bak "/$(printf '%s' "$MARK" | sed 's/[][\.*^$/]/\\&/g')/,/$(printf '%s' "$MARK_END" | sed 's/[][\.*^$/]/\\&/g')/d" "$file"
    rm -f "$file.bak"
  fi
}

if [ "$UNINSTALL" -eq 1 ]; then
  for h in commit-msg pre-push; do
    remove_block "$HOOKS_DST/$h"
  done
  rm -rf "$SCRIPTS_DST"
  info "Uninstalled ai-toolkit native git hooks from $TARGET"
  exit 0
fi

# Copy the cage scripts + lib into the git hooks dir so the native hooks have a
# stable, self-contained path to invoke (independent of the toolkit checkout).
mkdir -p "$SCRIPTS_DST/lib"
cp "$SHARED_HOOKS/commit-quality.sh" \
   "$SHARED_HOOKS/commit-gauntlet.sh" \
   "$SHARED_HOOKS/red-proof-warn.sh" \
   "$SHARED_HOOKS/reviewer-sep-warn.sh" \
   "$SHARED_HOOKS/test-select.sh" "$SCRIPTS_DST/"
cp "$SHARED_HOOKS/lib/utils.sh" "$SCRIPTS_DST/lib/"
chmod +x "$SCRIPTS_DST"/*.sh
info "Copied cage scripts → $SCRIPTS_DST"

# JSON-escape a string for embedding in the synthesized payload.
# Uses jq if available, else a minimal sed fallback.
emit_commit_msg_hook() {
  cat <<'HOOK'
#!/usr/bin/env bash
# >>> ai-toolkit cage >>>
# Native commit-msg hook: runs commit-quality + commit-gauntlet against a
# synthesized Bash-tool payload built from the real commit message ($1).
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$SELF_DIR/ai-toolkit-scripts"
MSG_FILE="$1"

# Build a representative `git commit` command containing the message as a -m
# arg so the cage scripts parse subject, anchor, and Tested-RED identically to
# the agent path. Strip comment lines git would drop.
MSG_BODY=$(grep -v '^[[:space:]]*#' "$MSG_FILE" || true)
if command -v jq >/dev/null 2>&1; then
  CMD=$(jq -nc --arg m "$MSG_BODY" '"git commit -m " + ($m | @json)')
  PAYLOAD=$(jq -nc --arg c "$CMD" '{tool_name:"Bash", tool_input:{command:$c}}')
else
  ESC=$(printf '%s' "$MSG_BODY" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ')
  PAYLOAD="{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"$ESC\\\"\"}}"
fi

for s in commit-quality commit-gauntlet; do
  if [ -x "$SCRIPTS/$s.sh" ]; then
    printf '%s' "$PAYLOAD" | "$SCRIPTS/$s.sh" || exit $?
  fi
done
# <<< ai-toolkit cage <<<
HOOK
}

emit_pre_push_hook() {
  cat <<'HOOK'
#!/usr/bin/env bash
# >>> ai-toolkit cage >>>
# Native pre-push hook, two stages:
#   1. Advisory warnings (red-proof / reviewer-sep) — never block (exit 0 each).
#   2. BLOCKING test gate (test-select) — the single owner of test execution
#      (issue #19). It classifies the pushed diff (fed git's pre-push stdin) and
#      runs the tiered suite; a non-zero exit aborts the push ("one push = one
#      run"). worktree-land threads its --skip-tests/--test-cmd via TEST_SELECT_*.
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$SELF_DIR/ai-toolkit-scripts"

# Drain git's pre-push stdin (the pushed "<local ref> <local sha> <remote ref>
# <remote sha>" lines) once: the test gate needs the SHA range, while the
# advisory scripts take a synthesized Bash payload, not git's stdin.
PREPUSH_REFS="$(cat || true)"

PAYLOAD='{"tool_name":"Bash","tool_input":{"command":"git push"}}'
for s in red-proof-warn reviewer-sep-warn; do
  if [ -x "$SCRIPTS/$s.sh" ]; then
    printf '%s' "$PAYLOAD" | "$SCRIPTS/$s.sh" || true
  fi
done

# Blocking gate: the tiered, diff-aware selector is the single owner of test
# execution. Fail CLOSED — if it is missing or not executable the gate cannot
# run, so refuse the push rather than ship untested (re-run install-git-hooks.sh
# to restore it). Its exit code otherwise becomes the push's: a failing suite
# aborts the push.
if [ ! -x "$SCRIPTS/test-select.sh" ]; then
  echo "pre-push: test-select.sh is missing or not executable — re-run scripts/install-git-hooks.sh" >&2
  exit 1
fi
printf '%s\n' "$PREPUSH_REFS" | "$SCRIPTS/test-select.sh" || exit $?
exit 0
# <<< ai-toolkit cage <<<
HOOK
}

install_hook() {
  local name="$1" emit_fn="$2"
  local dst="$HOOKS_DST/$name"
  mkdir -p "$HOOKS_DST"
  if [ -f "$dst" ] && ! grep -qF "$MARK" "$dst"; then
    # Pre-existing non-ours hook: append our block instead of clobbering.
    warn "Existing $name hook found — appending ai-toolkit block"
    "$emit_fn" | tail -n +2 >> "$dst"   # drop the duplicate shebang
  else
    remove_block "$dst" 2>/dev/null || true
    "$emit_fn" > "$dst"
  fi
  chmod +x "$dst"
  info "Installed native $name hook"
}

install_hook commit-msg emit_commit_msg_hook
install_hook pre-push  emit_pre_push_hook

echo ""
info "ai-toolkit cage installed as native git hooks in $TARGET"
warn "Blocking gates (commit-quality, commit-gauntlet) now enforce on real git commit."
warn "The pre-push test gate (test-select) blocks the push when the selected tests fail."
warn "Advisory gates (red-proof-warn, reviewer-sep-warn) run on git push and never block."
echo "  Uninstall with: scripts/install-git-hooks.sh --uninstall $TARGET"
