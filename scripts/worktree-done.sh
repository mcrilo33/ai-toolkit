#!/usr/bin/env bash
#
# worktree-done.sh — tear down a task worktree created by worktree-new.sh.
# Run from anywhere inside the repo (any worktree). Resolves the target against
# the live `git worktree list`, so you can pass the issue number, the slug, the
# branch name, or the path — whichever you remember.
#
# Usage:
#   scripts/worktree-done.sh <issue|slug|branch|path> [--force] [--no-hooks]
#
#   <issue|slug|branch|path>  anything that identifies the worktree
#   --force                   remove even with uncommitted/unpushed changes
#   --no-hooks                skip Orca's archive hook (only the land script passes it: land
#                             ingested the spoke already, so the hook would ingest it twice)
#                             (all flags are position-independent)
#
# Teardown is `orca worktree rm` (Orca owns the checkout). The branch is pruned -- local and origin --
# ONLY when fully merged into the hub's base, so an abandoned teardown never loses unmerged work:
# Orca may delete the local branch itself, so tip and merged-ness are recorded BEFORE the removal
# and an unmerged branch is put back after it.
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
RUN_HOOKS="--run-hooks"
for arg in "$@"; do
  case "$arg" in
    --force)       FORCE="--force" ;;
    --no-hooks)    RUN_HOOKS="" ;;
    -*)            wt_die "unknown option: $arg (supported: --force, --no-hooks)" ;;
    *)
      [ -z "$TARGET" ] || wt_die "unexpected extra argument: $arg"
      TARGET="$arg"
      ;;
  esac
done
[ -n "$TARGET" ] || wt_die "usage: worktree-done.sh <issue|slug|branch|path> [--force] [--no-hooks]"

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

# The branch comes from Orca's row for this worktree; a detached worktree has none (nothing to prune).
WT_BRANCH="$(wt_branch_of "$WT_DIR" "$REPO_ROOT")"
BASE_BRANCH="$(wt_base_branch "$REPO_ROOT")"
# Recorded BEFORE the removal: Orca may delete the local branch (even an unmerged one), and the
# merged-ness proof is measured against the RESOLVED base branch (issue #117), not the hub's HEAD.
TIP=""
MERGED=""
if [ -n "$WT_BRANCH" ]; then
  TIP="$(git rev-parse -q --verify "refs/heads/$WT_BRANCH" 2>/dev/null || true)"
  git merge-base --is-ancestor "$WT_BRANCH" "$BASE_BRANCH" 2>/dev/null && MERGED=1
fi

# --- telemetry: teardown lifecycle marker + script run-node ------------------
# Emit BEFORE removal so the worktree's spoke_run_id file is still readable. The
# script span is this control script as a trace node, sharing the marker's name
# (emission-link basis). No-op unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-done" "teardown" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "worktree-done" "success" "$WT_T0" "$WT_DIR"

# --- revoke the /quick hub-guard escape hatch (issue #89) --------------------
# worktree-quick.sh drops a `hub-guard-allow` marker in the common git-dir to let
# the hub session commit into the worktree; teardown is its cleanup, so clear it
# here. A no-op for a normal spoke (the marker only exists for a /quick lane).
# Revoke BEFORE the removal below: a refused `orca worktree rm` aborts via
# wt_die, and a marker left behind would silently disable hub-guard on `main`.
# Warn-only — a stray marker must never block teardown.
COMMON_GIT_DIR="$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)"
if [ -n "$COMMON_GIT_DIR" ] && [ -e "$COMMON_GIT_DIR/hub-guard-allow" ]; then
  rm -f "$COMMON_GIT_DIR/hub-guard-allow" \
    && echo "  revoked hub-guard bypass (hub-guard-allow)." \
    || wt_warn "couldn't remove hub-guard-allow — delete it by hand: rm \"$COMMON_GIT_DIR/hub-guard-allow\""
fi

# `worktree rm` can outlive the CLI's ~30 s limit (the archive hook may take 120 s): a drop is settled
# by listing worktrees (never re-issuing the rm). "Gone" needs a real list that still shows the main
# worktree but not $WT_DIR -- a garbled or empty reply is "unknown".
_wt_gone() {
  orca_json worktree list --repo "path:$REPO_ROOT" \
    && printf '%s' "$ORCA_OUT" | jq -e --arg p "$WT_DIR" '(.result.worktrees | type == "array")
         and any(.result.worktrees[]; .isMainWorktree == true) and all(.result.worktrees[]; .path != $p)' >/dev/null
}
echo "→ removing worktree: $WT_DIR"
orca_call_settled _wt_gone worktree rm --worktree "path:$WT_DIR" ${FORCE:+"$FORCE"} ${RUN_HOOKS:+"$RUN_HOOKS"} \
  || wt_die "orca did not remove $WT_DIR: ${ORCA_ERR:-$ORCA_OUT}$([ -n "$FORCE" ] || printf ' -- if it has uncommitted changes, re-run with --force once you are sure')"
echo "✓ removed: $WT_DIR"

# --- prune the branch, but only when it is fully merged ----------------------
# The integration target is the resolved base branch (wt_base_branch, issue
# #117). A branch that is its ancestor is fully merged and safe to delete;
# otherwise keep it and print the hint.
prune_branch() {
  if [ -z "$WT_BRANCH" ]; then
    return                       # detached worktree — no branch to prune
  fi
  if [ -z "$MERGED" ]; then
    # Orca may have deleted it; the contract is that an unmerged branch is never lost.
    if [ -n "$TIP" ] && ! git show-ref --verify --quiet "refs/heads/$WT_BRANCH"; then
      git branch "$WT_BRANCH" "$TIP" \
        || wt_warn "orca deleted unmerged branch $WT_BRANCH and it could not be restored — recreate it: git branch \"$WT_BRANCH\" $TIP"
    fi
    echo "  branch $WT_BRANCH is not merged into ${BASE_BRANCH} — kept."
    echo "  Push and merge it first, then re-run; or abandon it with: git branch -D \"$WT_BRANCH\""
    return
  fi
  # Local first. `git branch -d`'s own merged check considers only HEAD (or an
  # upstream), so with the hub parked off the base it would refuse a branch the
  # gate above already PROVED merged into the base (issue #117); -D is safe
  # here because that ancestor proof is the authority. If even -D refuses,
  # leave origin/<branch> alone too rather than delete a remote whose local
  # counterpart we couldn't remove.
  # Keep it if it moved on during the removal (a live spoke can commit during the hook): the merged
  # proof was for $TIP only.
  if [ -n "$TIP" ] && [ "$(git rev-parse -q --verify "refs/heads/$WT_BRANCH" || echo "$TIP")" != "$TIP" ]; then
    wt_warn "branch $WT_BRANCH gained commits while it was being removed -- kept; merge it or abandon it with: git branch -D \"$WT_BRANCH\""
    return
  fi
  if git show-ref --verify --quiet "refs/heads/$WT_BRANCH" && ! git branch -D "$WT_BRANCH"; then
    wt_warn "couldn't delete local branch $WT_BRANCH — see git's message above; leaving origin/$WT_BRANCH (if any) in place."
    return
  fi
  echo "  pruned merged branch $WT_BRANCH."
  if git show-ref --verify --quiet "refs/remotes/origin/$WT_BRANCH"; then
    # Defense-in-depth (issue #195): the ancestor proof above is about the LOCAL
    # branch — the remote ref may hold commits this checkout never fetched, and
    # deleting it would be the silent data-loss the land guards exist to stop.
    # Refresh the remote state (one retry for a transient blip, the #119 class;
    # --prune so a remote branch already deleted collapses its tracking ref
    # instead of feeding the checks below as a stale ghost) and require the
    # freshly-fetched remote ref itself to be merged; a failed fetch skips the
    # delete loudly — stale remote state never feeds it.
    if ! git fetch origin --prune --quiet 2>/dev/null && ! git fetch origin --prune --quiet 2>/dev/null; then
      wt_warn "fetch failed — kept remote origin/$WT_BRANCH (its merged-ness can't be verified against stale remote state, issue #195); once origin is reachable, delete it by hand if it still exists: git push origin --delete \"$WT_BRANCH\""
      return
    fi
    if ! git show-ref --verify --quiet "refs/remotes/origin/$WT_BRANCH"; then
      return  # the fetch pruned it — the remote branch is already gone, nothing to delete
    fi
    # Merged-ness of the remote ref, against the local base or — when this
    # checkout's base is behind — the freshly-fetched origin/<base>, where a
    # concurrent hub may have integrated the spoke's late push just as durably.
    if ! git merge-base --is-ancestor "refs/remotes/origin/$WT_BRANCH" "$BASE_BRANCH" 2>/dev/null \
       && ! git merge-base --is-ancestor "refs/remotes/origin/$WT_BRANCH" "refs/remotes/origin/$BASE_BRANCH" 2>/dev/null; then
      wt_warn "kept remote origin/$WT_BRANCH — it has commits not in $BASE_BRANCH; reconcile it by hand"
      return
    fi
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
