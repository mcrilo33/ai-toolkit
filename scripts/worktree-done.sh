#!/usr/bin/env bash
#
# worktree-done.sh — tear down a task worktree created by worktree-new.sh.
# Run from anywhere inside the repo (any worktree). Resolves the target against
# the live `git worktree list`, so you can pass the issue number, the slug, the
# branch name, or the path — whichever you remember.
#
# Usage:
#   scripts/worktree-done.sh <issue|slug|branch|path> [--force]
#
#   <issue|slug|branch|path>  anything that identifies the worktree
#   --force                   remove even with uncommitted/unpushed changes
#                             (position-independent)
#
# It refuses to remove a worktree with a dirty tree unless --force is given,
# and never deletes the branch (push first, then merge/PR, then prune by hand).
#
set -euo pipefail

WT_PROG="worktree-done"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# Position-independent flag parsing; reject unknown options instead of swallowing
# them into the target.
TARGET=""
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE="--force" ;;
    -*)      wt_die "unknown option: $arg (only --force is supported)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $arg"
      TARGET="$arg"
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-done.sh <issue|slug|branch|path> [--force]"

git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
INVOCATION_PWD="$(pwd -P)"

# Resolve the target against live worktrees. On no/ambiguous match, show what
# exists so the user can pick — never a dead-end path error.
if ! WT_DIR="$(wt_resolve "$TARGET" "$REPO_ROOT")"; then
  wt_warn "no single worktree matches '$TARGET'. Existing task worktrees:"
  wt_print_worktrees "$REPO_ROOT"
  wt_die "pass one of the paths above, or its issue number / slug / branch."
fi

[ "$WT_DIR" = "$REPO_ROOT" ] && wt_die "refusing to remove the main checkout"

# Operate from the main checkout so we never stand inside the worktree we remove.
cd "$REPO_ROOT"
case "$INVOCATION_PWD/" in
  "$WT_DIR"/*) STANDING_INSIDE=1 ;;
  *)           STANDING_INSIDE="" ;;
esac

echo "→ removing worktree: $WT_DIR"
if ! git worktree remove $FORCE "$WT_DIR"; then
  if [ -z "$FORCE" ]; then
    wt_die "remove failed — if it has uncommitted changes, re-run with --force once you're sure."
  else
    wt_die "remove failed even with --force — see git's message above (worktree may be locked, in use, or a submodule)."
  fi
fi

git worktree prune
echo "✓ removed. Branch is kept — delete it after merge with: git branch -d <branch>"
echo "  In your VS Code review window, remove the stale folder: right-click it → Remove Folder from Workspace."
[ -n "$STANDING_INSIDE" ] && wt_warn "your shell is inside the removed worktree; run: cd \"$REPO_ROOT\""
exit 0
