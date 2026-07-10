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
#   commit-msg  → commit-quality + commit-gauntlet + red-proof-verify
#                 (all three need the message: commit-quality validates format +
#                  issue-anchor; commit-gauntlet needs it for the Tested-RED
#                  carve-out; red-proof-verify RUNS each Tested-RED node and
#                  requires it to FAIL — the native backstop for the CC-only
#                  PreToolUse hook, whose `if:` filter misses chained/prefixed
#                  commits and whose crash/malformed paths fail open (issue #210).
#                  At commit-msg both the staged index AND the message file
#                  exist, so this is the correct stage for all three.)
#   pre-push    → red-proof-warn + reviewer-sep-warn (advisory; read git log)
#                 + test-select (BLOCKING: the single owner of test execution —
#                 a tiered, diff-aware suite; non-zero exit aborts the push, #19)
#
# Deliberately NOT wired natively: block-no-verify (issue #211). It is the sole
# defense against `git commit|push --no-verify` — the flag that skips exactly the
# commit-msg and pre-push hooks this script installs — so a native copy would be
# skipped by the very command it must catch. It therefore lives ONLY as an agent
# PreToolUse hook with no backstop here, which is why that script fails closed
# (crash/malformed payload → exit 2) rather than trusting a native safety net.
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
    # Restore any foreign hook we moved aside at install (overwriting the now
    # block-stripped remnant, i.e. the leftover shebang line).
    if [ -f "$HOOKS_DST/$h.ai-toolkit-foreign" ]; then
      mv "$HOOKS_DST/$h.ai-toolkit-foreign" "$HOOKS_DST/$h"
    fi
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
   "$SHARED_HOOKS/red-proof-verify.sh" \
   "$SHARED_HOOKS/red-proof-warn.sh" \
   "$SHARED_HOOKS/reviewer-sep-warn.sh" \
   "$SHARED_HOOKS/anti-gutting-scan.sh" \
   "$SHARED_HOOKS/test-select.sh" "$SCRIPTS_DST/"
# telemetry.sh is not optional: utils.sh sources it unconditionally, so an
# install without it ships hooks that die at source-time (the 2026-07-04 hub
# outage — every push from every worktree failed on the missing file).
# enabled.sh (the #154 on/off switch) ships here too so a fresh install carries
# the switch; utils.sh and the hook wrappers source it defensively, so a stale
# install lacking it degrades to ENABLED rather than crashing.
cp "$SHARED_HOOKS/lib/utils.sh" "$SHARED_HOOKS/lib/telemetry.sh" \
   "$SHARED_HOOKS/lib/gate-stamp.sh" \
   "$SHARED_HOOKS/lib/test-reverse-index.sh" \
   "$SHARED_HOOKS/lib/enabled.sh" "$SCRIPTS_DST/lib/"
chmod +x "$SCRIPTS_DST"/*.sh
info "Copied cage scripts → $SCRIPTS_DST"

# JSON-escape a string for embedding in the synthesized payload.
# Uses jq if available, else a minimal sed fallback.
emit_commit_msg_hook() {
  cat <<'HOOK'
#!/usr/bin/env bash
# >>> ai-toolkit cage >>>
# Native commit-msg hook: runs commit-quality + commit-gauntlet + red-proof-verify
# against a synthesized Bash-tool payload built from the real commit message ($1).
# red-proof-verify is the native backstop for the CC-only PreToolUse hook (#210):
# native git fires this on every commit, so chained/prefixed/env-assigned forms
# the agent `if:` filter misses are still gated here.
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$SELF_DIR/ai-toolkit-scripts"

# Global on/off switch (#154): when the toolkit is disabled, pass through —
# the commit proceeds ungated. This guards the whole native hook regardless of
# the cage scripts below (which also self-skip via utils.sh).
if [ -f "$SCRIPTS/lib/enabled.sh" ]; then
  # shellcheck source=/dev/null
  source "$SCRIPTS/lib/enabled.sh"
  ai_toolkit_enabled || exit 0
fi

MSG_FILE="$1"

# Build a representative `git commit` command containing the message as a -m
# arg so the cage scripts parse subject, anchor, and Tested-RED identically to
# the agent path. Strip comment lines git would drop.
MSG_BODY=$(grep -v '^[[:space:]]*#' "$MSG_FILE" || true)
if command -v jq >/dev/null 2>&1; then
  CMD=$(jq -nr --arg m "$MSG_BODY" '"git commit -m " + ($m | @json)')
  PAYLOAD=$(jq -nc --arg c "$CMD" '{tool_name:"Bash", tool_input:{command:$c}}')
else
  ESC=$(printf '%s' "$MSG_BODY" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ')
  PAYLOAD="{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"$ESC\\\"\"}}"
fi

for s in commit-quality commit-gauntlet red-proof-verify; do
  # Fail CLOSED, mirroring the pre-push test-select check: a configured gate that
  # is missing or not executable cannot run, so block the commit rather than let
  # it proceed ungated (re-run install-git-hooks.sh to restore it). A silent skip
  # here would mean a deleted/de-executable'd gate waves every commit through.
  if [ ! -x "$SCRIPTS/$s.sh" ]; then
    echo "commit-msg: $s.sh is missing or not executable — re-run scripts/install-git-hooks.sh" >&2
    exit 1
  fi
  printf '%s' "$PAYLOAD" | "$SCRIPTS/$s.sh" || exit $?
done

# Chain a pre-existing foreign commit-msg hook (moved aside by the installer) as a
# SEPARATE process so it keeps its own shebang, shell options, and argv — inlining
# it under our `set -euo pipefail` could turn a benign hook (e.g. an unmatched
# `grep -q`) into a commit-blocker. Its non-zero exit still blocks, fail-closed.
FOREIGN="${BASH_SOURCE[0]}.ai-toolkit-foreign"
if [ -x "$FOREIGN" ]; then
  "$FOREIGN" "$@" || exit $?
fi
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

# Global on/off switch (#154): when the toolkit is disabled, pass through — the
# push proceeds ungated. Exit BEFORE draining stdin (git does not require the
# hook to consume it) so a disabled toolkit runs zero gate logic.
if [ -f "$SCRIPTS/lib/enabled.sh" ]; then
  # shellcheck source=/dev/null
  source "$SCRIPTS/lib/enabled.sh"
  ai_toolkit_enabled || exit 0
fi

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

# Anti-gutting tripwire: scan the pushed diff for test-gutting signatures (added
# sys.exit(0)/skip/xfail/assert True, or net-deleted assertions). The scan is
# advisory — it prints findings and exits 0, surfacing the smell without gating a
# human's test edit. A missing scan is skipped (defense-in-depth, not the primary
# gate — test-select below is the blocking owner of test execution).
if [ -x "$SCRIPTS/anti-gutting-scan.sh" ]; then
  printf '%s\n' "$PREPUSH_REFS" | "$SCRIPTS/anti-gutting-scan.sh" || exit $?
fi

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

# Chain a pre-existing foreign pre-push hook (moved aside by the installer) as a
# SEPARATE process, re-feeding git's ref lines on its stdin (we drained them above)
# and passing its argv. A here-string (not a `printf | hook` pipe) feeds stdin so a
# foreign hook that ignores stdin cannot SIGPIPE the writer and, under pipefail,
# abort the push on a code the hook itself never returned. Runs only once our
# blocking gate has passed; the foreign hook's own non-zero exit still aborts.
FOREIGN="${BASH_SOURCE[0]}.ai-toolkit-foreign"
if [ -x "$FOREIGN" ]; then
  "$FOREIGN" "$@" <<<"$PREPUSH_REFS" || exit $?
fi
exit 0
# <<< ai-toolkit cage <<<
HOOK
}

install_hook() {
  local name="$1" emit_fn="$2"
  local dst="$HOOKS_DST/$name"
  mkdir -p "$HOOKS_DST"
  if [ -f "$dst" ] && ! grep -qF "$MARK" "$dst"; then
    # Pre-existing non-ours hook: move it ASIDE to a sidecar and install our block
    # in its place. Appending our block after the foreign hook (the old behavior)
    # left it unreachable whenever the foreign hook ended in `exit 0` (the common
    # case) — the gate silently never ran, yet install reported success. Our block
    # invokes the sidecar as a SEPARATE process (see the emitted hook), so the
    # foreign hook keeps its own shebang, shell options, argv, and stdin instead of
    # inheriting our `set -euo pipefail`. `mv` preserves the original mode, so a
    # foreign hook the user had disabled (exec bit cleared) stays disabled — the
    # emitted `[ -x ]` guard then skips it. The sidecar survives re-install (the
    # MARK branch below leaves it untouched) and is restored on --uninstall.
    warn "Existing $name hook found — chaining it after the ai-toolkit block"
    mv "$dst" "$dst.ai-toolkit-foreign"
  fi
  remove_block "$dst" 2>/dev/null || true
  "$emit_fn" > "$dst"
  chmod +x "$dst"
  info "Installed native $name hook"
}

install_hook commit-msg emit_commit_msg_hook
install_hook pre-push  emit_pre_push_hook

# --- managed runtime-artifact excludes (issue #206) --------------------------
# The pre-push test gate writes .testmondata* (the testmon DB plus its -shm/-wal
# WAL sidecars), and under AI_TOOLKIT_OTEL per-run OTel artifacts land under
# .ai-toolkit/. On a checkout without a committed .gitignore for these, the
# untracked files make `git status --porcelain` non-empty, which the #172 ready
# gate reads as a dirty tree and refuses ready/<N> — stalling a drain.
# worktree-new.sh already seeds these into a spoke worktree's info/exclude; do the
# same here for an EXISTING checkout (the hub, or a pre-#206 worktree). The
# entries are local to the git dir, never committed, and appended at most once.
EXCLUDE_FILE="$(git -C "$TARGET" rev-parse --git-path info/exclude 2>/dev/null || true)"
if [ -n "$EXCLUDE_FILE" ]; then
  case "$EXCLUDE_FILE" in /*) ;; *) EXCLUDE_FILE="$TARGET/$EXCLUDE_FILE" ;; esac
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  for entry in '.testmondata*' '.ai-toolkit/'; do
    grep -qxF "$entry" "$EXCLUDE_FILE" 2>/dev/null \
      || printf '%s\n' "$entry" >> "$EXCLUDE_FILE"
  done
  info "Seeded runtime-artifact excludes (.testmondata*, .ai-toolkit/) into info/exclude"
fi

echo ""
info "ai-toolkit cage installed as native git hooks in $TARGET"
warn "Blocking gates (commit-quality, commit-gauntlet, red-proof-verify) now enforce on real git commit."
warn "The pre-push test gate (test-select) blocks the push when the selected tests fail."
warn "Advisory gates (red-proof-warn, reviewer-sep-warn) run on git push and never block."
echo "  Uninstall with: scripts/install-git-hooks.sh --uninstall $TARGET"
