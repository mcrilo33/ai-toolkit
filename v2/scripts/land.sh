#!/usr/bin/env bash
# land.sh [--local-gate] [--review] [--dispatch D] <issue>: land one finished spoke (06 section 4 steps 11-14).
# Run it from the main checkout, on BASE_BRANCH. Gate on the EXACT tip that will be merged: CI (`gh run list --commit`)
# or, before cutover, --local-gate = $CHECK_CMD in the spoke worktree on the merged tip. Main moved -> merge it on the
# spoke, push, gate again. Then ff main, push, close the issue, delete the branch, release the worker, remove the worktree.
# --review is WP2's hook: $REVIEW_CMD (default review.sh) <issue> must exit 0 on APPROVE; skipped by default until WP2.
# Exit: 0 landed, 1 error, 2 refused (precondition), 3 review not approved, 4 gate red/timeout, 5 merge conflict,
#       6 landed but a cleanup step failed (main is already pushed; finish by hand).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
local_gate=0; review=0; disp=""; n=""
while [ $# -gt 0 ]; do
  case "$1" in
    --local-gate) local_gate=1 ;;
    --review) review=1 ;;
    --dispatch) disp="${2:-}"; shift ;;
    [0-9]*) n="$1" ;;
    *) usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] <issue>" ;;
  esac; shift
done
[ -n "$n" ] || usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] <issue>"
bail() { local rc="$1"; shift; printf '%s: %s\n' "${0##*/}" "$*" >&2; exit "$rc"; }

# --- preconditions (exit 2): nothing is touched before these hold
abs() { git rev-parse --path-format=absolute "$1"; }
[ "$(abs --git-dir)" = "$(abs --git-common-dir)" ] || bail 2 "refused: run me from the main checkout, not a worktree"
[ "$(git branch --show-current)" = "$BASE_BRANCH" ] || bail 2 "refused: the main checkout is not on $BASE_BRANCH"
[ -z "$(git status --porcelain --untracked-files=no)" ] || bail 2 "refused: the main checkout has uncommitted changes"
o="$(orca_json worktree show --worktree "issue:$n")" || bail 2 "refused: no Orca worktree is linked to issue $n"
wt="$(printf '%s' "$o" | jq -r '.result.worktree.path')"
branch="$(printf '%s' "$o" | jq -r '.result.worktree.branch | sub("^refs/heads/"; "")')"
[ -z "$(git -C "$wt" status --porcelain --untracked-files=no)" ] || bail 2 "refused: the spoke worktree is dirty"
git fetch -q origin
tip="$(git -C "$wt" rev-parse HEAD)"
[ "$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)" = "$tip" ] || bail 2 "refused: $branch is not pushed (origin/$branch != spoke HEAD)"

# --- review (WP2 hook point), then the merge-and-gate loop
if [ "$review" = 1 ]; then "${REVIEW_CMD:-$here/review.sh}" "$n" || bail 3 "review did not approve issue $n"; fi

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

for round in 1 2 3; do
  if moved; then
    git -C "$wt" merge -q --no-edit "origin/$BASE_BRANCH" > /dev/null \
      || { git -C "$wt" merge --abort; bail 5 "merge conflict: $BASE_BRANCH into $branch; resolve on the spoke, push, land again"; }
    git -C "$wt" push -q origin "HEAD:refs/heads/$branch" || die "cannot push the merged $branch"
    tip="$(git -C "$wt" rev-parse HEAD)"
  fi
  gate "$tip"
  moved || break
  [ "$round" -lt 3 ] || bail 4 "$BASE_BRANCH keeps moving; land again"
done

# --- land: from here on main is the source of truth, so a cleanup failure is reported, not fatal
before="$(git rev-parse HEAD)"
git merge -q --ff-only "$tip" || bail 2 "refused: local $BASE_BRANCH has diverged from $branch"
if ! git push -q origin "$BASE_BRANCH"; then
  git reset -q --hard "$before"
  die "push of $BASE_BRANCH was rejected; local $BASE_BRANCH restored, nothing closed"
fi
sha="$(git rev-parse HEAD)"; bad=0
step() { "$@" > /dev/null || { warn "cleanup failed: $*"; bad=1; }; }
step gh issue close "$n" -c "landed in $sha"
step git push -q origin --delete "$branch"
if [ -z "$disp" ]; then
  disp="$(orca_json orchestration worker-list ${RUN:+--run "$RUN"} | jq -r --arg p "::$wt" '[.result.workers[] | select(.resource.worktreeId | endswith($p)) | .dispatchId] | .[]')" || disp=""
fi
for d in $disp; do step orca_mutate orchestration worker-release --dispatch "$d"; done
[ -n "$disp" ] || { warn "no dispatch found for $wt: worker-release skipped"; bad=1; }
step orca_json worktree rm --worktree "issue:$n" --run-hooks
printf 'landed #%s in %s\n' "$n" "$sha"
[ "$bad" = 0 ] || bail 6 "landed $sha but cleanup is incomplete"
