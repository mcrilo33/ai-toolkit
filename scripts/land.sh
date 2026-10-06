#!/usr/bin/env bash
# land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only] <issue>: land one finished spoke (06 section 4 steps 11-14).
# LOCAL_GATE=1 (env or local env file) means --local-gate, for callers that pass no flags (the coordinator).
# Run it from the main checkout, on BASE_BRANCH. Gate on the EXACT tip that will be merged: CI (`gh run list --commit`)
# or, before cutover, --local-gate = $CHECK_CMD in the spoke worktree on the merged tip. Main moved -> merge it on the
# spoke, push, gate again. Then ff main, push, close the issue, delete the branch, release the worker, remove the worktree.
# --cleanup-only finishes close/delete/release/rm idempotently for an issue already on main (after exit 6). If the worktree
# is already gone, name it with --branch <b> [--tip <sha>] (tip defaults to origin/<b>): the landed check stays, worker/worktree steps are skipped.
# --review is WP2's hook: $REVIEW_CMD (default review.sh) <issue> <tip> must exit 0 on APPROVE; skipped by default until WP2. The review
# judges the exact captured tip, not the branch name; once it returns, origin/<branch> and the spoke HEAD must still be that tip (exit 2
# otherwise: a sha the worker moved to meanwhile was never reviewed). gate_loop's own merge of origin/<base> stays allowed: it adds only
# base commits, which are already landed and reviewed (it refuses first if the spoke HEAD is no longer the captured tip).
# In the toolkit's own checkout (it carries scripts/sync.sh, shared/ and hooks/claude) the cleanup first re-runs `sync.sh` on it, so the
# installed copies (.claude/{rules/ai-toolkit,skills,agents}, .ai-toolkit/{scripts,hooks,claude-settings.json}) are the landed tip's: the next dispatch, answer and land use
# the landed code. Another repo (no sync sources) is untouched. sync replaces files by rename, so a running coordinator loop keeps its
# own script intact (old code, consistent) while every script it launches next is the new copy. Workers live at that moment keep the
# copies they started with; they get the new ones only when re-dispatched (dispatch.sh --address / --retry-of refresh the worktree).
# A loop keeps its OLD coordinator.sh but calls the NEW dispatch/land/answer/lib: restart it after a land that changed coordinator.sh
# or lib.sh (or any contract between them), since the loop does not re-exec itself.
# Exit: 0 landed, 1 error, 2 refused (precondition), 3 review not approved, 4 gate red/timeout, 5 merge conflict,
#       6 landed but a cleanup step failed, a failed refresh of the installed copies included (main is already pushed; finish by hand,
#         --cleanup-only retries the refresh too).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
local_gate="${LOCAL_GATE:-0}"; review=0; co=0; disp=""; n=""; branch=""; tip=""; gone=0
while [ $# -gt 0 ]; do
  case "$1" in
    --local-gate) local_gate=1 ;;
    --review) review=1 ;;
    --cleanup-only) co=1 ;;
    --dispatch) disp="${2:-}"; shift ;;
    --branch) branch="${2:-}"; shift ;;
    --tip) tip="${2:-}"; shift ;;
    [0-9]*) n="$1" ;;
    *) usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only] <issue>" ;;
  esac; shift
done
[ -n "$n" ] || usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only] <issue>"
bail() { local rc="$1"; shift; printf '%s: %s\n' "${0##*/}" "$*" >&2; exit "$rc"; }

# --- preconditions (exit 2): nothing is touched before these hold
abs() { git rev-parse --path-format=absolute "$1"; }
[ "$(abs --git-dir)" = "$(abs --git-common-dir)" ] || bail 2 "refused: run me from the main checkout, not a worktree"
[ "$(git branch --show-current)" = "$BASE_BRANCH" ] || bail 2 "refused: the main checkout is not on $BASE_BRANCH"
[ -z "$(git status --porcelain --untracked-files=no)" ] || bail 2 "refused: the main checkout has uncommitted changes"
if o="$(orca_json worktree show --worktree "issue:$n" 2> /dev/null)"; then
  wt="$(printf '%s' "$o" | jq -r '.result.worktree.path')"
  branch="$(printf '%s' "$o" | jq -r '.result.worktree.branch | sub("^refs/heads/"; "")')"
  [ -z "$(git -C "$wt" status --porcelain --untracked-files=no)" ] || bail 2 "refused: the spoke worktree is dirty"
  git fetch -q --prune origin
  tip="$(git -C "$wt" rev-parse HEAD)"
elif [ "$co" = 1 ] && [ -n "$branch" ]; then   # the worktree is already gone (exit 6 after a close failure)
  gone=1; wt=""
  git fetch -q --prune origin
  tip="${tip:-$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)}"
  [ -n "$tip" ] || bail 2 "refused: no Orca worktree for issue $n and no origin/$branch: pass --tip <sha>"
else bail 2 "refused: no Orca worktree is linked to issue $n"; fi
[ "$branch" != "$BASE_BRANCH" ] || bail 2 "refused: issue $n's branch is the base branch $BASE_BRANCH"
# Behind origin/main is fine (the gated tip contains it); ahead or diverged would ship commits that never ran the gate.
git merge-base --is-ancestor HEAD "origin/$BASE_BRANCH" || bail 2 "refused: local $BASE_BRANCH is not at origin/$BASE_BRANCH (ahead or diverged)"
if [ "$co" = 1 ]; then git merge-base --is-ancestor "$tip" "origin/$BASE_BRANCH" || bail 2 "refused: $branch is not landed on $BASE_BRANCH"
else [ "$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)" = "$tip" ] || bail 2 "refused: $branch is not pushed (origin/$branch != spoke HEAD)"; fi

# --- review (WP2 hook point), then the merge-and-gate loop
ci_state=""
ci_poll() {   # 0 once the runs for $1 have settled (ci_state = green|red), 1 while none exist yet or any is still running
  ci_state="$(gh run list --commit "$1" --json status,conclusion,url | jq -r '
    if length == 0 or any(.[]; .status != "completed") then "pending"
    elif all(.[]; .conclusion == "success" or .conclusion == "skipped" or .conclusion == "neutral") then "green" else "red" end')" || ci_state=pending
  [ "$ci_state" != pending ]
}
gate() {   # $1 = the exact sha under test
  if [ "$local_gate" = 1 ]; then
    (cd "$wt" && bash -c "$CHECK_CMD") || bail 4 "local gate ($CHECK_CMD) failed on $1"
  else
    wait_until "${CI_TRIES:-120}" "${AI_TOOLKIT_POLL:-15}" ci_poll "$1" || bail 4 "CI for $1 timed out"
    [ "$ci_state" = green ] || bail 4 "CI is red for $1"
  fi
}
moved() { git fetch -q origin || die "git fetch origin failed"; ! git merge-base --is-ancestor "origin/$BASE_BRANCH" "$tip"; }
gate_loop() {
  local _
  for _ in 1 2 3; do
    if moved; then   # merge into the reviewed tip only: a commit the worker added since was never reviewed
      [ "$(git -C "$wt" rev-parse HEAD)" = "$tip" ] || bail 2 "refused: the spoke HEAD moved off the reviewed tip $tip (now $(git -C "$wt" rev-parse HEAD)); land again"
      git -C "$wt" merge -q --no-edit "origin/$BASE_BRANCH" > /dev/null \
        || { git -C "$wt" merge --abort; bail 5 "merge conflict: $BASE_BRANCH into $branch; resolve on the spoke, push, land again"; }
      git -C "$wt" push -q origin "HEAD:refs/heads/$branch" || die "cannot push the merged $branch"
      tip="$(git -C "$wt" rev-parse HEAD)"
    fi
    gate "$tip"
    moved || return 0
  done
  bail 4 "$BASE_BRANCH keeps moving; land again"
}
land() {   # ff main to the gated tip and push it; anything else on main is refused and rolled back
  local before
  before="$(git rev-parse HEAD)"
  git merge -q --ff-only "origin/$BASE_BRANCH" || bail 2 "refused: local $BASE_BRANCH is not at origin/$BASE_BRANCH"
  git merge -q --ff-only "$tip" || bail 2 "refused: local $BASE_BRANCH has diverged from $branch"
  [ "$(git rev-parse HEAD)" = "$tip" ] || { git reset -q --hard "$before"; bail 2 "refused: $BASE_BRANCH is not at the gated tip $tip"; }
  if ! git push -q origin "$BASE_BRANCH"; then
    git reset -q --hard "$before"
    die "push of $BASE_BRANCH was rejected; local $BASE_BRANCH restored, nothing closed"
  fi
}
if [ "$co" = 0 ]; then
  if [ "$review" = 1 ]; then
    "${REVIEW_CMD:-$here/review.sh}" "$n" "$tip" || bail 3 "review did not approve issue $n"
    git fetch -q --prune origin || die "git fetch origin failed"
    pushed="$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)"; head="$(git -C "$wt" rev-parse HEAD)"
    [ "$pushed" = "$tip" ] && [ "$head" = "$tip" ] \
      || bail 2 "refused: $branch moved during the review (reviewed $tip, origin/$branch is ${pushed:-gone}, spoke HEAD is $head); land again to review the new tip"
  fi
  gate_loop
  land
fi

# --- cleanup: main is the source of truth from here on, so a failure is reported (exit 6), not fatal
sha="$tip"; bad=0
step() { "$@" > /dev/null || { warn "cleanup failed: $*"; bad=1; }; }
main="$(dirname "$(abs --git-common-dir)")"
if [ -f "$main/scripts/sync.sh" ] && [ -d "$main/shared" ] && [ -d "$main/hooks/claude" ]; then
  bash "$main/scripts/sync.sh" "$main" > /dev/null \
    || { warn "installed copies NOT refreshed: run scripts/sync.sh . in $main (this land is done; running workers keep their old copies until re-dispatched)"; bad=1; }
fi
step gh issue close "$n" -c "landed in $sha"
status_label remove "$n"
# Delete the remote branch only while it is still at the gated tip: a later worker push must not be dropped.
if git rev-parse -q --verify "refs/remotes/origin/$branch" > /dev/null; then
  step git push -q --force-with-lease="refs/heads/$branch:$tip" origin ":refs/heads/$branch"
fi
if [ -z "$disp" ] && [ "$gone" = 0 ]; then
  disp="$(orca_json orchestration worker-list ${RUN:+--run "$RUN"} | jq -r --arg p "::$wt" '[.result.workers[] | select(.resource.worktreeId | endswith($p)) | .dispatchId] | .[]')" || disp=""
fi
for d in $disp; do step orca_mutate orchestration worker-release --dispatch "$d"; done
if [ "$gone" = 0 ]; then
  [ -n "$disp" ] || { warn "no dispatch found for $wt: worker-release skipped"; bad=1; }
  # The CLI can drop a long rm (~30 s) while Orca finishes it: a worktree that is gone counts as removed.
  orca_json worktree rm --worktree "issue:$n" --run-hooks > /dev/null || ! orca_json worktree show --worktree "issue:$n" > /dev/null 2>&1 \
    || { warn "cleanup failed: worktree rm issue:$n"; bad=1; }
fi
printf 'landed #%s in %s\n' "$n" "$sha"
[ "$bad" = 0 ] || bail 6 "landed $sha but cleanup is incomplete"
