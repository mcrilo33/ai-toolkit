#!/usr/bin/env bash
#
# worktree-land.sh — land a finished task branch from the hub (main checkout).
# The deterministic half of the land-task skill (`/land <id>`): the hub starts
# and ends tasks; spokes only execute. Run it FROM the hub, on the default
# branch, after the spoke has pushed — never from inside a worktree.
#
# Usage:
#   scripts/worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--keep-branch] [--test-cmd <cmd>]
#
#   <issue|slug|branch|path>  anything that identifies the task worktree
#   --skip-tests              land without running the suite on the merged hub
#   --keep-branch             keep the branch after landing (passed to worktree-done.sh)
#   --test-cmd <cmd>          suite command (default: `pytest -q` when pytest exists)
#
# Sequence, each step aborting safely on failure:
#   guards  hub on default branch + clean; worktree resolved, clean, fully pushed
#   merge   --ff-only when possible, else a merge commit (plain `git merge`)
#   gate    full suite on the merged hub; on failure `git reset --keep` back
#   ship    push origin <default> → worktree-done.sh → `gh issue close` (numeric ids)
#   tmux    kill the task's window in session 0 when its pane path is gone
#
set -euo pipefail

WT_PROG="worktree-land"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

TARGET=""
SKIP_TESTS=""
KEEP_BRANCH=""
TEST_CMD=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-tests)  SKIP_TESTS=1; shift ;;
    --keep-branch) KEEP_BRANCH=1; shift ;;
    --test-cmd)    [ "$#" -ge 2 ] || wt_die "--test-cmd needs a value"; TEST_CMD="$2"; shift 2 ;;
    --test-cmd=*)  TEST_CMD="${1#--test-cmd=}"; shift ;;
    -*)            wt_die "unknown option: $1 (supported: --skip-tests, --keep-branch, --test-cmd)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $1"
      TARGET="$1"; shift
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-land.sh <issue|slug|branch|path> [--skip-tests] [--keep-branch] [--test-cmd <cmd>]"

# --- guards: the hub ----------------------------------------------------------
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
[ "$(wt_realpath "$(git rev-parse --show-toplevel)")" = "$REPO_ROOT" ] \
  || wt_die "landing is hub-side — run from the main checkout ($REPO_ROOT), not a worktree"
cd "$REPO_ROOT"

# Default branch: origin/HEAD when set, else the conventional main/master.
DEFAULT="$(git symbolic-ref -q --short refs/remotes/origin/HEAD || true)"
DEFAULT="${DEFAULT#origin/}"
if [ -z "$DEFAULT" ]; then
  if git show-ref --verify --quiet refs/heads/main; then DEFAULT="main"
  elif git show-ref --verify --quiet refs/heads/master; then DEFAULT="master"
  else wt_die "could not determine the default branch"
  fi
fi
HUB_BRANCH="$(git symbolic-ref --short -q HEAD || true)"
[ "$HUB_BRANCH" = "$DEFAULT" ] \
  || wt_die "hub is on '${HUB_BRANCH:-detached HEAD}' — land from the default branch '$DEFAULT'"
[ -z "$(git status --porcelain -uno)" ] \
  || wt_die "hub checkout is dirty — commit or stash before landing"

# Resolve the suite command BEFORE merging, so a missing one can never strand a merge.
if [ -z "$SKIP_TESTS" ] && [ -z "$TEST_CMD" ]; then
  command -v pytest >/dev/null 2>&1 && TEST_CMD="pytest -q"
  [ -n "$TEST_CMD" ] || wt_die "no test command found — pass --test-cmd <cmd> or --skip-tests"
fi

# --- guards: the spoke ----------------------------------------------------------
if ! WT_DIR="$(wt_resolve "$TARGET" "$REPO_ROOT")"; then
  wt_warn "no single worktree matches '$TARGET'. Existing task worktrees:"
  wt_print_worktrees "$REPO_ROOT"
  wt_die "pass one of the paths above, or its issue number / slug / branch."
fi
WT_BRANCH=""
while IFS=$'\t' read -r wt br; do
  if [ "$wt" = "$WT_DIR" ]; then
    WT_BRANCH="$br"
    break
  fi
done < <(wt_task_worktrees "$REPO_ROOT")
[ -n "$WT_BRANCH" ] || wt_die "worktree $WT_DIR is on a detached HEAD — nothing to land"

# Untracked files count as dirty: `git worktree remove` would refuse them later,
# and a stray WIP file is exactly what landing must not destroy.
[ -z "$(git -C "$WT_DIR" status --porcelain)" ] \
  || wt_die "worktree $WT_DIR has uncommitted or untracked changes — finish or stash them on the spoke"

git fetch origin --quiet 2>/dev/null || true
UPSTREAM="$(git rev-parse --symbolic-full-name "${WT_BRANCH}@{upstream}" 2>/dev/null || true)"
[ -n "$UPSTREAM" ] || wt_die "branch $WT_BRANCH has never been pushed — the spoke's push is its ship gate"
AHEAD="$(git rev-list --count "${UPSTREAM}..${WT_BRANCH}")"
[ "$AHEAD" -eq 0 ] || wt_die "branch $WT_BRANCH is $AHEAD commit(s) ahead of $UPSTREAM — push from the spoke first"
# Behind is just as fatal as ahead: landing a reduced local branch would later
# prune the remote ref and silently lose the commits only the remote still has.
BEHIND="$(git rev-list --count "${WT_BRANCH}..${UPSTREAM}")"
[ "$BEHIND" -eq 0 ] || wt_die "branch $WT_BRANCH is $BEHIND commit(s) behind $UPSTREAM — the remote has work this checkout lacks; reconcile on the spoke first"

# Issue number = leading number of the branch slug (feature/42-foo → 42);
# ad-hoc branches have none and skip the issue close.
BSLUG="${WT_BRANCH##*/}"
ISSUE="${BSLUG%%-*}"
[[ "$ISSUE" =~ ^[0-9]+$ ]] || ISSUE=""

# --- merge ----------------------------------------------------------------------
PRE_SHA="$(git rev-parse HEAD)"
echo "→ merging $WT_BRANCH into $DEFAULT"
if ! git merge --no-edit "$WT_BRANCH"; then
  git merge --abort 2>/dev/null || true
  wt_die "merge of $WT_BRANCH conflicts with $DEFAULT — rebase the spoke on $DEFAULT, push, and re-run"
fi
MERGED_SHA="$(git rev-parse HEAD)"

# --- gate: full suite on the merged hub ------------------------------------------
if [ -n "$SKIP_TESTS" ]; then
  SUITE_RESULT="skipped (--skip-tests)"
  echo "→ skipping test suite (--skip-tests)"
else
  echo "→ running test suite: $TEST_CMD"
  if ! bash -c "$TEST_CMD"; then
    wt_warn "suite FAILED on the merged $DEFAULT — rolling back: git reset --keep $PRE_SHA"
    git reset --keep "$PRE_SHA" \
      || wt_die "rollback failed — hub is still on the merged commit; reset by hand: git reset --keep $PRE_SHA"
    wt_die "landing aborted; nothing was pushed. Fix on the spoke, push, and re-run."
  fi
  SUITE_RESULT="passed"
fi

# --- ship -------------------------------------------------------------------------
echo "→ pushing $DEFAULT to origin"
git push origin "$DEFAULT" \
  || wt_die "push failed — the merge is local-only. Fix the remote and re-run, or back out: git reset --keep $PRE_SHA"

bash "$SCRIPT_DIR/worktree-done.sh" "$WT_DIR" ${KEEP_BRANCH:+--keep-branch}

if [ -n "$ISSUE" ]; then
  if command -v gh >/dev/null 2>&1; then
    if gh issue close "$ISSUE" --comment "Landed on $DEFAULT in $MERGED_SHA (suite: $SUITE_RESULT)."; then
      echo "✓ closed issue #$ISSUE"
    else
      wt_warn "couldn't close issue #$ISSUE — close it by hand: gh issue close $ISSUE"
    fi
  else
    wt_warn "gh not found — close issue #$ISSUE by hand"
  fi
fi

# --- tmux: kill the task's stranded window in session 0 ---------------------------
# Spokes live as windows of session 0 named "<id>" or "<id>-<slug>". A window is
# stranded when its pane's cwd vanished with the worktree; live windows are kept.
TAG="${ISSUE:-$BSLUG}"
cleanup_tmux() {
  command -v tmux >/dev/null 2>&1 || return 0
  local win name path
  while IFS=$'\t' read -r win name path; do
    [ -n "$win" ] || continue
    case "$name" in
      "$TAG"|"$TAG"-*) ;;
      *) continue ;;
    esac
    if [ ! -d "$path" ]; then
      if tmux kill-window -t "$win" 2>/dev/null; then
        echo "✓ killed stranded tmux window '$name' ($win)"
      else
        wt_warn "couldn't kill tmux window '$name' ($win) — close it by hand"
      fi
    fi
  done < <(tmux list-windows -t 0 -F $'#{window_id}\t#{window_name}\t#{pane_current_path}' 2>/dev/null || true)
}
cleanup_tmux

# --- report -------------------------------------------------------------------------
echo
echo "✓ landed $WT_BRANCH"
echo "  merged:  $MERGED_SHA"
echo "  suite:   $SUITE_RESULT"
echo "  pushed:  origin/$DEFAULT"
exit 0
