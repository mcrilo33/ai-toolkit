#!/usr/bin/env bash
#
# worktree-done.sh — tear down a task worktree created by worktree-new.sh.
# Run from anywhere inside the repo (any worktree). Resolves the target against
# the live `git worktree list`, so you can pass the issue number, the slug, the
# branch name, or the path — whichever you remember.
#
# Usage:
#   scripts/worktree-done.sh <issue|slug|branch|path> [--force] [--no-code] [--keep-branch]
#
#   <issue|slug|branch|path>  anything that identifies the worktree
#   --force                   remove even with uncommitted/unpushed changes
#   --no-code                 don't fold the folder out of VS Code (code --remove)
#   --keep-branch             keep the branch even when it is fully merged
#                             (all flags are position-independent)
#
# It refuses to remove a worktree with a dirty tree unless --force is given.
# Teardown is the mirror of worktree-new.sh: it folds the folder out of the VS
# Code review window (code --remove) and prunes the branch — local and origin —
# but ONLY when the branch is fully merged into the hub's current branch, so an
# abandoned teardown never loses unmerged work.
#
set -euo pipefail

WT_PROG="worktree-done"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# Span start clock for the lifecycle/teardown span emitted before removal.
WT_T0="$(wt_now_ms)"

# Position-independent flag parsing; reject unknown options instead of swallowing
# them into the target.
TARGET=""
FORCE=""
NO_CODE=""
KEEP_BRANCH=""
for arg in "$@"; do
  case "$arg" in
    --force)       FORCE="--force" ;;
    --no-code)     NO_CODE=1 ;;
    --keep-branch) KEEP_BRANCH=1 ;;
    -*)            wt_die "unknown option: $arg (supported: --force, --no-code, --keep-branch)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $arg"
      TARGET="$arg"
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-done.sh <issue|slug|branch|path> [--force] [--no-code] [--keep-branch]"

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

# Capture the worktree's branch BEFORE removal — afterwards git no longer
# associates the path with a branch, so this is our only chance to learn it.
# wt_task_worktrees emits "path<TAB>branch"; detached worktrees yield an empty
# branch (nothing to prune). WT_DIR is the verbatim path wt_resolve printed from
# this same stream, so a literal match is exact — never canonicalize here: a
# missing directory (the --force case) resolves to an empty path that would
# collide with any OTHER absent worktree and prune the wrong branch.
WT_BRANCH=""
while IFS=$'\t' read -r wt br; do
  if [ "$wt" = "$WT_DIR" ]; then
    WT_BRANCH="$br"
    break
  fi
done < <(wt_task_worktrees "$REPO_ROOT")

# --- telemetry: lifecycle/teardown span --------------------------------------
# Emit BEFORE removal so the worktree's spoke_run_id file is still readable.
# No-op unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-done" "teardown" "success" "$WT_T0" "$WT_DIR"

echo "→ removing worktree: $WT_DIR"
if ! git worktree remove $FORCE "$WT_DIR"; then
  if [ -z "$FORCE" ]; then
    wt_die "remove failed — if it has uncommitted changes, re-run with --force once you're sure."
  else
    wt_die "remove failed even with --force — see git's message above (worktree may be locked, in use, or a submodule)."
  fi
fi

git worktree prune
echo "✓ removed: $WT_DIR"

# --- fold the folder out of the VS Code review window ------------------------
# The mirror of worktree-new.sh's `code --add`. Gated on `--no-code` and the
# presence of the `code` CLI; a failed call warns, never aborts teardown.
if [ -z "$NO_CODE" ] && command -v code >/dev/null 2>&1; then
  echo "→ removing folder from your VS Code review window (code --remove)"
  code --remove "$WT_DIR" \
    || wt_warn "couldn't remove the folder from VS Code — right-click it → Remove Folder from Workspace."
fi

# --- prune the branch, but only when it is fully merged ----------------------
# We removed the worktree from the hub (now cwd), so HEAD here is the hub's
# current branch — the integration target. A branch that is its ancestor is
# fully merged and safe to delete; otherwise keep it and print the hint.
prune_branch() {
  if [ -z "$WT_BRANCH" ]; then
    return                       # detached worktree — no branch to prune
  fi
  if [ -n "$KEEP_BRANCH" ]; then
    echo "  branch $WT_BRANCH kept (--keep-branch)."
    return
  fi
  local hub_branch
  hub_branch="$(git symbolic-ref --short -q HEAD || true)"
  if [ -z "$hub_branch" ] || ! git merge-base --is-ancestor "$WT_BRANCH" "$hub_branch"; then
    echo "  branch $WT_BRANCH is not merged into ${hub_branch:-HEAD} — kept."
    echo "  Push and merge it first, then re-run; or abandon it with: git branch -D \"$WT_BRANCH\""
    return
  fi
  # Local first: if `git branch -d` (the safe form, a second net under the
  # merge-base gate) refuses, leave origin/<branch> alone too rather than delete
  # a remote whose local counterpart we couldn't remove.
  if ! git branch -d "$WT_BRANCH"; then
    wt_warn "couldn't delete local branch $WT_BRANCH — see git's message above; leaving origin/$WT_BRANCH (if any) in place."
    return
  fi
  echo "  pruned merged branch $WT_BRANCH."
  if git show-ref --verify --quiet "refs/remotes/origin/$WT_BRANCH"; then
    if git push origin --delete "$WT_BRANCH" >/dev/null 2>&1; then
      echo "  deleted origin/$WT_BRANCH."
    else
      wt_warn "couldn't delete origin/$WT_BRANCH — delete by hand: git push origin --delete \"$WT_BRANCH\""
    fi
  fi
}
prune_branch

[ -n "$STANDING_INSIDE" ] && wt_warn "your shell is inside the removed worktree; run: cd \"$REPO_ROOT\""
exit 0
