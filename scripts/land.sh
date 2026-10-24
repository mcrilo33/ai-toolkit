#!/usr/bin/env bash
# land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only | --merged] <issue>: land one finished spoke (06 section 4 steps 11-14).
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
# Before the review, every non-merge commit of origin/<base>..tip is rechecked against the hook's rule (only a test: commit may change a test file, TEST_PATHS): a violation exits 2 (the fix would rewrite pushed history).
# In the toolkit's own checkout (it carries scripts/sync.sh, shared/ and hooks/claude) the cleanup first re-runs `sync.sh` on it, so the
# installed copies (.claude/{rules/ai-toolkit,skills,agents}, .ai-toolkit/{scripts,hooks,claude-settings.json}) are the landed tip's: the next dispatch, answer and land use
# the landed code. Another repo (no sync sources) is untouched. sync replaces files by rename, so a running coordinator loop keeps its
# own script intact (old code, consistent) while every script it launches next is the new copy. Workers live at that moment keep the
# copies they started with; they get the new ones only when re-dispatched (dispatch.sh --address / --retry-of refresh the worktree).
# A loop keeps its OLD coordinator.sh but calls the NEW dispatch/land/answer/lib: restart it after a land that changed coordinator.sh
# or lib.sh (or any contract between them), since the loop does not re-exec itself.
# POST_LAND_CMD (a project's own command, empty by default) runs right after the base push (MAIN_BRANCH = BASE_BRANCH; else at --merged), once and never under --cleanup-only, when the issue
# carries `Tracker: <KEY>`: `<cmd> <issue> <key> <sha>` with BASE_BRANCH in its environment. A failure warns and comments on the issue; it never changes the exit code.
# MAIN_BRANCH (settings/ai-toolkit.env) is where an issue is DONE; empty = BASE_BRANCH, everything above is unchanged. MAIN_BRANCH != BASE_BRANCH (an integration branch): the land
# still pushes BASE_BRANCH, but the issue stays OPEN with `status:awaiting-main`, one rolling PR BASE_BRANCH -> MAIN_BRANCH lists `Closes #<n>` for every issue waiting, POST_LAND_CMD
# does not run, and the cleanup runs (the work is on the base). LAND_MODE=pr (a protected base): nothing is pushed to the base; the branch gets its own PR into it (a draft while CI
# gates its tip, then ready: the project's CI must run on draft PRs, jobs skipped on a draft would pass the gate), the issue waits the same way, the worker is released and the branch and worktree are kept for --address. Both exit 7. Humans merge; the toolkit never
# merges into MAIN_BRANCH. --merged <n>: the PR that carries #n is MERGED (a squash merge defeats the landed check, the PR state is the proof; else exit 2, nothing touched): close
# each issue still awaiting main in it (`merged into <MAIN_BRANCH> in <merge sha>`), drop the label, run POST_LAND_CMD <n> <key> <merge sha> (BASE_BRANCH and MAIN_BRANCH in its
# environment), then remove the branch and worktree (pr) or merge origin/MAIN_BRANCH back into BASE_BRANCH and push it (integration: a squash merge leaves the base diverged).
# Exit: 0 landed, 1 error, 2 refused (precondition), 3 review not approved, 4 gate red/timeout, 5 merge conflict,
#       6 landed but a cleanup step failed, a failed refresh of the installed copies included (main is already pushed; finish by hand,
#         --cleanup-only retries the refresh too), 7 landed, awaiting the merge into MAIN_BRANCH (the PR url is the `PR:` line).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
local_gate="${LOCAL_GATE:-0}"; review=0; co=0; merged=0; disp=""; n=""; branch=""; tip=""; gone=0
while [ $# -gt 0 ]; do
  case "$1" in
    --local-gate) local_gate=1 ;;
    --review) review=1 ;;
    --cleanup-only) co=1 ;;
    --merged) merged=1 ;;
    --dispatch) disp="${2:-}"; shift ;;
    --branch) branch="${2:-}"; shift ;;
    --tip) tip="${2:-}"; shift ;;
    [0-9]*) n="$1" ;;
    *) usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only | --merged] <issue>" ;;
  esac; shift
done
[ -n "$n" ] || usage_exit "usage: land.sh [--local-gate] [--review] [--dispatch D] [--cleanup-only | --merged] <issue>"
bail() { local rc="$1"; shift; printf '%s: %s\n' "${0##*/}" "$*" >&2; exit "$rc"; }
resolve_main || usage_exit "LAND_MODE is empty or pr, and pr needs MAIN_BRANCH empty or equal to BASE_BRANCH (LAND_MODE=${LAND_MODE:-}, MAIN_BRANCH=${MAIN_BRANCH:-}, BASE_BRANCH=$BASE_BRANCH)"

post_land() {   # <issue> <sha>: a project's tracker update; the key is validated as one token and passed as argv, never interpolated
  local body key
  [ -n "${POST_LAND_CMD:-}" ] || return 0
  body="$(gh_issue "$1" | jq -r '.body // ""')" || { warn "cannot read issue $1: POST_LAND_CMD skipped"; return 0; }
  key="$(issue_footer "$body" Tracker)"; key="${key%"${key##*[![:space:]]}"}"; [ -n "$key" ] || return 0
  case "$key" in *[!A-Za-z0-9._/#-]*) warn "bad Tracker footer '$key' on issue $1: POST_LAND_CMD skipped"; return 0 ;; esac
  BASE_BRANCH="$BASE_BRANCH" MAIN_BRANCH="$MAIN_BRANCH" bash -c "$POST_LAND_CMD \"\$@\"" post-land "$1" "$key" "$2" >&2 && return 0
  warn "POST_LAND_CMD failed for $key"
  gh issue comment "$1" -b "tracker update failed, do it by hand: $key $2" > /dev/null || warn "cannot comment on #$1"
}
pr_upsert() {   # <head> <base> <title> <body> [--draft]: one open PR per head/base, created or its body updated; sets pr_url and pr_draft (true|false)
  local row
  row="$(gh pr list --head "$1" --base "$2" --state open --json url,isDraft | jq -r '.[0] // empty | "\(.url) \(.isDraft)"')" || { warn "cannot list the pull requests $1 -> $2"; return 1; }
  if [ -n "$row" ]; then
    read -r pr_url pr_draft <<< "$row"
    gh pr edit "$pr_url" --body "$4" > /dev/null || { warn "cannot update $pr_url"; return 1; }
  else
    pr_url="$(gh pr create --head "$1" --base "$2" --title "$3" --body "$4" "${@:5}" | tail -n 1)" || { warn "cannot open the pull request $1 -> $2"; return 1; }
    pr_draft=false; [ "${5:-}" != --draft ] || pr_draft=true
  fi
}
issue_pr() {   # [--draft]: the branch's own PR into the protected base (LAND_MODE=pr)
  local t; t="$(gh_issue "$n" | jq -r '.title // empty')" || t=""
  pr_upsert "$branch" "$BASE_BRANCH" "#$n $t" "Closes #$n. Reviewed tip: $tip" "$@"
}
rolling_pr() {   # the one PR BASE_BRANCH -> MAIN_BRANCH: Closes #<m> for this issue and every issue already waiting
  local ids
  ids="$(gh issue list --label "$AWAITING_LABEL" --state open --limit 100 --json number | jq -r '.[].number')" || { warn "cannot list the issues awaiting main: the rolling PR is not updated"; return 1; }
  pr_upsert "$BASE_BRANCH" "$MAIN_BRANCH" "Promote $BASE_BRANCH into $MAIN_BRANCH" \
    "$(printf '%s\n%s\n' "$n" "$ids" | sed '/^$/d' | sort -un | sed 's/^/Closes #/' | paste -sd' ' -). Humans merge this into $MAIN_BRANCH; the toolkit never does."
}
awaiting_main() {   # the issue stays OPEN and labelled until the PR into MAIN_BRANCH is merged (--merged closes it)
  local msg rc=0
  if [ "$main_kind" = pr ]; then
    issue_pr || return 1
    [ "$pr_draft" != true ] || gh pr ready "$pr_url" > /dev/null || { warn "cannot mark $pr_url ready"; rc=1; }
    msg="$pr_url carries it, open until merged into $MAIN_BRANCH"
  else msg="integrated in $BASE_BRANCH at $tip, open until merged into $MAIN_BRANCH"; fi
  status_label remove "$n"; status_label add "$n" "$AWAITING_LABEL"
  gh issue comment "$n" -b "$msg" > /dev/null || warn "cannot comment on #$n"
  [ "$main_kind" != integration ] || rolling_pr || rc=1
  return "$rc"
}
land_merged() {   # --merged <n>: exits; every step but the proof is retried by running it again
  local pr sha ids m v o bad=0
  [ -n "$main_kind" ] || bail 2 "refused: MAIN_BRANCH is $BASE_BRANCH, the base itself: nothing waits for a merge"
  pr="$(pr_of "$n")" || bail 2 "refused: no pull request into $MAIN_BRANCH carries #$n (or gh failed)"
  [ "$(jq -r .state <<< "$pr")" = MERGED ] || bail 2 "refused: $(jq -r .url <<< "$pr") is not merged"
  sha="$(jq -r '.mergeCommit.oid // empty' <<< "$pr")"; [ -n "$sha" ] || bail 2 "refused: $(jq -r .url <<< "$pr") has no merge commit"
  git fetch -q --prune origin || die "git fetch origin failed"
  git merge-base --is-ancestor HEAD "origin/$BASE_BRANCH" || bail 2 "refused: local $BASE_BRANCH is not at origin/$BASE_BRANCH (ahead or diverged)"
  git merge -q --ff-only "origin/$BASE_BRANCH"
  # The cleanup comes first and the label goes last: the label is what the loop's next sweep retries from, so a step that fails here leaves every issue awaiting main.
  if [ "$main_kind" = pr ] && o="$(orca_json worktree show --worktree "issue:$n" 2> /dev/null)"; then
    branch="$(jq -r '.result.worktree.branch | sub("^refs/heads/"; "")' <<< "$o")"
    if git rev-parse -q --verify "refs/remotes/origin/$branch" > /dev/null; then git push -q origin ":refs/heads/$branch" || { warn "cannot delete origin/$branch"; bad=1; }; fi
    orca_json worktree rm --worktree "issue:$n" --run-hooks > /dev/null || ! orca_json worktree show --worktree "issue:$n" > /dev/null 2>&1 || { warn "cleanup failed: worktree rm issue:$n"; bad=1; }
  fi
  if [ "$main_kind" = integration ] && ! git merge-base --is-ancestor "origin/$MAIN_BRANCH" HEAD; then   # a squash merge leaves the base diverged: bring the true main back so the next rolling PR shows only new work
    git merge -q --no-edit "origin/$MAIN_BRANCH" > /dev/null && git push -q origin "$BASE_BRANCH" \
      || { git merge --abort 2> /dev/null || true; git reset -q --hard "origin/$BASE_BRANCH"; warn "cannot merge origin/$MAIN_BRANCH back into $BASE_BRANCH: do it by hand, the next sweep finishes the rest"; bad=1; }
  fi
  [ "$bad" = 0 ] || bail 6 "merged into $MAIN_BRANCH, but the cleanup is incomplete: the issues stay awaiting main, run --merged again"
  ids="$n"; [ "$main_kind" != integration ] || ids="$(jq -r '.body | [scan("Closes #([0-9]+)") | .[0]] | unique | .[]' <<< "$pr")"
  for m in $ids; do
    v="$(gh issue view "$m" --json state,labels)" || { warn "cannot read #$m: run --merged again"; bad=1; continue; }
    jq -e --arg l "$AWAITING_LABEL" '.labels | map(.name) | index($l)' <<< "$v" > /dev/null || continue   # already handled: the label is the marker
    [ "$(jq -r .state <<< "$v")" != OPEN ] || gh issue close "$m" -c "merged into $MAIN_BRANCH in $sha" > /dev/null || { warn "cannot close #$m: run --merged again"; bad=1; continue; }
    gh issue edit "$m" --remove-label "$AWAITING_LABEL" > /dev/null || { warn "cannot remove $AWAITING_LABEL from #$m: its tracker update waits for the next run"; bad=1; continue; }   # else the next run would repeat post_land
    post_land "$m" "$sha"
    printf 'merged #%s into %s in %s\n' "$m" "$MAIN_BRANCH" "$sha"
  done
  [ "$bad" = 0 ] || bail 6 "merged into $MAIN_BRANCH, but a step failed: see the warnings, run --merged again"
  exit 0
}

# --- preconditions (exit 2): nothing is touched before these hold
abs() { git rev-parse --path-format=absolute "$1"; }
[ "$(abs --git-dir)" = "$(abs --git-common-dir)" ] || bail 2 "refused: run me from the main checkout, not a worktree"
[ "$(git branch --show-current)" = "$BASE_BRANCH" ] || bail 2 "refused: the main checkout is not on $BASE_BRANCH"
[ -z "$(git status --porcelain --untracked-files=no)" ] || bail 2 "refused: the main checkout has uncommitted changes"
[ "$merged" = 0 ] || land_merged
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
if [ "$co" = 1 ]; then
  if [ "$main_kind" = pr ]; then pr_of "$n" > /dev/null || bail 2 "refused: no pull request into $MAIN_BRANCH carries #$n: $branch was not landed"   # the PR is the land
  else git merge-base --is-ancestor "$tip" "origin/$BASE_BRANCH" || bail 2 "refused: $branch is not landed on $BASE_BRANCH"; fi
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
test_files_only_in_test_commits() {   # exit 2 on the first pushed commit that changes a test file without being a test: commit; the hook's rule, so the commit and land checks cannot drift
  local c out hook="$here/../hooks/git/commit-msg"
  if [ -z "${TEST_PATHS+x}" ]; then warn "TEST_PATHS is set nowhere: the test-file check is off"; return 0; fi
  export TEST_PATHS
  for c in $(git rev-list --no-merges "origin/$BASE_BRANCH..$tip"); do
    out="$(git -c core.quotePath=false diff-tree --no-commit-id -r --name-status -M "$c" | bash "$hook" --check <(git log -1 --format=%B "$c") 2>&1)" \
      || bail 2 "refused: commit $(git log -1 --format='%h %s' "$c"): ${out#commit-msg: }"
    [ -z "$out" ] || warn "commit $(git log -1 --format=%h "$c"): ${out#commit-msg: }"
  done
}
if [ "$co" = 0 ]; then
  test_files_only_in_test_commits
  if [ "$review" = 1 ]; then
    "${REVIEW_CMD:-$here/review.sh}" "$n" "$tip" || bail 3 "review did not approve issue $n"
    git fetch -q --prune origin || die "git fetch origin failed"
    pushed="$(git rev-parse -q --verify "refs/remotes/origin/$branch" || true)"; head="$(git -C "$wt" rev-parse HEAD)"
    [ "$pushed" = "$tip" ] && [ "$head" = "$tip" ] \
      || bail 2 "refused: $branch moved during the review (reviewed $tip, origin/$branch is ${pushed:-gone}, spoke HEAD is $head); land again to review the new tip"
  fi
  [ "$main_kind" != pr ] || [ "$local_gate" = 1 ] || issue_pr --draft || die "cannot open the draft pull request for $branch"   # CI runs on pull_request only: the PR must exist for the gate to see the tip
  gate_loop
  [ "$main_kind" = pr ] || land
  [ -n "$main_kind" ] || post_land "$n" "$tip"
fi

# --- cleanup: main is the source of truth from here on, so a failure is reported (exit 6), not fatal
sha="$tip"; bad=0
step() { "$@" > /dev/null || { warn "cleanup failed: $*"; bad=1; }; }
main="$(dirname "$(abs --git-common-dir)")"
if [ "$main_kind" != pr ] && [ -f "$main/scripts/sync.sh" ] && [ -d "$main/shared" ] && [ -d "$main/hooks/claude" ]; then
  bash "$main/scripts/sync.sh" "$main" > /dev/null \
    || { warn "installed copies NOT refreshed: run scripts/sync.sh . in $main (this land is done; running workers keep their old copies until re-dispatched)"; bad=1; }
fi
if [ -n "$main_kind" ]; then step awaiting_main
else step gh issue close "$n" -c "landed in $sha"; status_label remove "$n"; fi
# Delete the remote branch only while it is still at the gated tip: a later worker push must not be dropped. (pr: the branch is the open PR's head, kept.)
if [ "$main_kind" != pr ] && git rev-parse -q --verify "refs/remotes/origin/$branch" > /dev/null; then
  step git push -q --force-with-lease="refs/heads/$branch:$tip" origin ":refs/heads/$branch"
fi
if [ -z "$disp" ] && [ "$gone" = 0 ]; then
  disp="$(orca_json orchestration worker-list ${RUN:+--run "$RUN"} | jq -r --arg p "::$wt" '[.result.workers[] | select(.resource.worktreeId | endswith($p)) | .dispatchId] | .[]')" || disp=""
fi
for d in $disp; do step orca_mutate orchestration worker-release --dispatch "$d"; done
if [ "$gone" = 0 ]; then
  [ -n "$disp" ] || { warn "no dispatch found for $wt: worker-release skipped"; bad=1; }
  # The CLI can drop a long rm (~30 s) while Orca finishes it: a worktree that is gone counts as removed.
  if [ "$main_kind" != pr ]; then   # pr: the worktree is kept for --address
    orca_json worktree rm --worktree "issue:$n" --run-hooks > /dev/null || ! orca_json worktree show --worktree "issue:$n" > /dev/null 2>&1 \
      || { warn "cleanup failed: worktree rm issue:$n"; bad=1; }
  fi
fi
if [ -n "$main_kind" ]; then printf 'landed #%s at %s, awaiting merge into %s\nPR: %s\n' "$n" "$sha" "$MAIN_BRANCH" "${pr_url:-}"
else printf 'landed #%s in %s\n' "$n" "$sha"; fi
[ "$bad" = 0 ] || bail 6 "landed $sha but cleanup is incomplete"
[ -z "$main_kind" ] || exit 7
