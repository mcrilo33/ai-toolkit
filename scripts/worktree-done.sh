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

# --- guard: role, not directory (issue #26) -------------------------------------
# A spoke must not tear down its own worktree (it strands the very tmux window
# it runs in). worktree-new.sh stamps the spoke session with WT_SPOKE, which
# rides every command it runs, so refuse here before any work. No override flag.
# The hub is user-started and never carries WT_SPOKE, so it tears down freely —
# including worktree-land.sh's internal call, which inherits the hub's clean env.
[ -z "${WT_SPOKE:-}" ] \
  || wt_die "this is the spoke session for '$WT_SPOKE' — teardowns run on the hub. Emit your ready/<issue> marker (your push is your ship gate); the hub will tear it down after landing."

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

# --- telemetry: teardown lifecycle marker + script run-node ------------------
# Emit BEFORE removal so the worktree's spoke_run_id file is still readable. The
# script span is this control script as a trace node, sharing the marker's name
# (emission-link basis). No-op unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-done" "teardown" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "worktree-done" "success" "$WT_T0" "$WT_DIR"

# --- fold the folder out of the VS Code review window ------------------------
# The mirror of worktree-new.sh's direct append (issue #134): edit the review
# workspace file's `folders` array — dropping this worktree's entry and sweeping
# any entry whose path is gone from disk (self-healing for past `code --remove`
# misses). The CLI call survives strictly as the missing/unparseable-file
# fallback. Still runs BEFORE the on-disk delete below, while $WT_DIR exists, so
# the target entry matches by path and the fallback's `code --remove` resolves
# it (issue #43). Gated on `--no-code`; the helper is called in a conditional
# (set -e) and every failure warns, never aborts teardown.
if [ -z "$NO_CODE" ]; then
  WS_FILE="$(wt_workspace_file "$REPO_ROOT")"
  if wt_workspace_remove "$WS_FILE" "$WT_DIR"; then
    echo "→ removed folder from your review workspace file: $WS_FILE (dead entries swept)"
  elif command -v code >/dev/null 2>&1; then
    echo "→ removing folder from your VS Code review window (code --remove)"
    code --remove "$WT_DIR" \
      || wt_warn "couldn't remove the folder from VS Code — right-click it → Remove Folder from Workspace."
  fi
fi

# --- revoke the /quick hub-guard escape hatch (issue #89) --------------------
# worktree-quick.sh drops a `hub-guard-allow` marker in the common git-dir to let
# the hub session commit into the worktree; teardown is its cleanup, so clear it
# here. A no-op for a normal spoke (the marker only exists for a /quick lane).
# Revoke BEFORE the removal below: a failed `git worktree remove` aborts via
# wt_die, and a marker left behind would silently disable hub-guard on `main`.
# Warn-only — a stray marker must never block teardown.
COMMON_GIT_DIR="$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)"
if [ -n "$COMMON_GIT_DIR" ] && [ -e "$COMMON_GIT_DIR/hub-guard-allow" ]; then
  rm -f "$COMMON_GIT_DIR/hub-guard-allow" \
    && echo "  revoked hub-guard bypass (hub-guard-allow)." \
    || wt_warn "couldn't remove hub-guard-allow — delete it by hand: rm \"$COMMON_GIT_DIR/hub-guard-allow\""
fi

echo "→ removing worktree: $WT_DIR"
if ! git worktree remove $FORCE "$WT_DIR"; then
  if [ -z "$FORCE" ]; then
    wt_die "remove failed — if it has uncommitted changes, re-run with --force once you're sure."
  else
    wt_die "remove failed even with --force — see git's message above (worktree may be locked, in use, or a submodule)."
  fi
fi

git worktree prune

# --- leftover-dir sweep (issue #134) ------------------------------------------
# A lingering shell cwd or gitignored runtime files can leave the directory on
# disk even when git deregistered the worktree (#122 left ai-toolkit-122
# behind). Any telemetry ingest has already completed by now (worktree-land
# ingests before invoking this script), so a raw rm -rf of the leftover is
# safe. If even that fails, warn LOUDLY but keep going — a stuck directory
# must never abort the branch pruning below.
if [ -e "$WT_DIR" ]; then
  wt_warn "git deregistered the worktree but the directory survived — sweeping it (rm -rf)."
  rm -rf "$WT_DIR" || true
  if [ -e "$WT_DIR" ]; then
    wt_warn "✗ LEFTOVER DIRECTORY SURVIVED: $WT_DIR"
    wt_warn "  a process is probably holding a cwd inside it — close it, then remove it by hand: rm -rf \"$WT_DIR\""
  fi
fi
echo "✓ removed: $WT_DIR"

# --- scrub the removed path from VS Code's "Open Recent" list (issue #103) ---
# `code --remove` folds the folder out of the live window, but VS Code keeps the
# path in its global recent-folders history forever — one stale entry per spoke.
# The CLI exposes no way to drop a single recent entry, so vscode-prune-recent.py
# edits the state store directly. Run AFTER the on-disk removal (an aborted
# removal keeps the worktree, so its recent entry should stay). Skip when VS Code
# is live: it rewrites storage.json on flush, so our edit would be lost or race.
# Gated on --no-code with the rest of the VS Code teardown.
prune_vscode_recent() {
  local wt_path="$1"
  local vscode_dir="${AI_TOOLKIT_VSCODE_DIR:-$HOME/Library/Application Support/Code}"
  local storage="$vscode_dir/User/globalStorage/storage.json"
  [ -f "$storage" ] || return 0   # no state store (CLI-only host) — nothing to scrub
  local lock="$vscode_dir/code.lock"
  if [ -f "$lock" ]; then
    local pid
    pid="$(cat "$lock" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "  VS Code is running — left its Open Recent list untouched (close it to scrub the entry)."
      return 0
    fi
  fi
  python3 "$SCRIPT_DIR/vscode-prune-recent.py" "$storage" "$wt_path" \
    || wt_warn "couldn't scrub $wt_path from VS Code's Open Recent list."
}
if [ -z "$NO_CODE" ]; then
  prune_vscode_recent "$WT_DIR"
fi

# --- prune the branch, but only when it is fully merged ----------------------
# The integration target is the resolved base branch (wt_base_branch, issue
# #117). A branch that is its ancestor is fully merged and safe to delete;
# otherwise keep it and print the hint.
prune_branch() {
  if [ -z "$WT_BRANCH" ]; then
    return                       # detached worktree — no branch to prune
  fi
  if [ -n "$KEEP_BRANCH" ]; then
    echo "  branch $WT_BRANCH kept (--keep-branch)."
    return
  fi
  # Merged-ness is measured against the RESOLVED base branch (issue #117), not
  # whatever branch the hub's HEAD happens to be on.
  local base_branch
  base_branch="$(wt_base_branch "$REPO_ROOT")"
  if ! git merge-base --is-ancestor "$WT_BRANCH" "$base_branch" 2>/dev/null; then
    echo "  branch $WT_BRANCH is not merged into ${base_branch} — kept."
    echo "  Push and merge it first, then re-run; or abandon it with: git branch -D \"$WT_BRANCH\""
    return
  fi
  # Local first. `git branch -d`'s own merged check considers only HEAD (or an
  # upstream), so with the hub parked off the base it would refuse a branch the
  # gate above already PROVED merged into the base (issue #117); -D is safe
  # here because that ancestor proof is the authority. If even -D refuses,
  # leave origin/<branch> alone too rather than delete a remote whose local
  # counterpart we couldn't remove.
  if ! git branch -D "$WT_BRANCH"; then
    wt_warn "couldn't delete local branch $WT_BRANCH — see git's message above; leaving origin/$WT_BRANCH (if any) in place."
    return
  fi
  echo "  pruned merged branch $WT_BRANCH."
  if git show-ref --verify --quiet "refs/remotes/origin/$WT_BRANCH"; then
    if wt_git_push origin --delete "$WT_BRANCH" >/dev/null 2>&1; then
      echo "  deleted origin/$WT_BRANCH."
    else
      wt_warn "couldn't delete origin/$WT_BRANCH — delete by hand: git push origin --delete \"$WT_BRANCH\""
    fi
  fi
}
prune_branch

[ -n "$STANDING_INSIDE" ] && wt_warn "your shell is inside the removed worktree; run: cd \"$REPO_ROOT\""
exit 0
