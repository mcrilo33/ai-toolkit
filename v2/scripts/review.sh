#!/usr/bin/env bash
# review.sh <issue>: the independent pre-land review (06 section 4 step 11, D6). A fresh read-only claude (REVIEW_MODEL, not the
# worker's) runs the code-review agent in the spoke's worktree on origin/$BASE_BRANCH...<branch>; its final message is the JSON
# verdict {verdict, blockers, warnings, tdd_followed, tests_weakened, summary}. stdout: BLOCKER:/WARNING:/SUMMARY: lines.
# Exit 0 APPROVE; 3 REQUEST_CHANGES (a rejected tdd_followed / tests_weakened or a blocker beats a verdict that says APPROVE);
# 1 error or an unparseable verdict after one retry (never an approve; land.sh treats it as a rejection).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
n="${1:-}"; [ -n "$n" ] || usage_exit "usage: review.sh <issue>"
o="$(orca_json worktree show --worktree "issue:$n")" || die "no Orca worktree is linked to issue $n"
wt="$(printf '%s' "$o" | jq -r '.result.worktree.path')"
branch="$(printf '%s' "$o" | jq -r '.result.worktree.branch | sub("^refs/heads/"; "")')"
prompt="Review the change of branch $branch against origin/$BASE_BRANCH: git diff origin/$BASE_BRANCH...$branch. Intent: .ai-toolkit/task.md. Your final message must be exactly the JSON verdict object of your verdict contract."
# shellcheck disable=SC2016
VALID='type == "object" and (.verdict | IN("APPROVE", "REQUEST_CHANGES")) and (.blockers | type == "array" and all(type == "string"))
  and (.warnings | type == "array" and all(type == "string")) and (.tdd_followed | type == "boolean") and (.tests_weakened | type == "boolean") and (.summary | type == "string")'
v=""
for _ in 1 2; do
  # The prompt goes on stdin: --allowedTools is variadic and would swallow a trailing positional argument.
  out="$(cd "$wt" && printf '%s' "$prompt" | claude -p --model "$REVIEW_MODEL" --agent code-review --no-session-persistence \
    --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)")" || out=""
  v="$(printf '%s\n' "$out" | sed '/^```/d' | jq -c "select($VALID)" 2> /dev/null)" || v=""
  [ -z "$v" ] || break
done
[ -n "$v" ] || die "unparseable review verdict for issue $n"
printf '%s' "$v" | jq -r '(.blockers[] | "BLOCKER: " + gsub("\n"; " ")), (.warnings[] | "WARNING: " + gsub("\n"; " ")), "SUMMARY: " + .summary'
extra="$(printf '%s' "$v" | jq -r 'if .tests_weakened then "BLOCKER: tests_weakened: the diff weakens or skips tests" else empty end,
  if .tdd_followed | not then "BLOCKER: tdd_followed is false: add the failing test that proves the behavior change" else empty end')"
[ -z "$extra" ] || printf '%s\n' "$extra"
printf '%s' "$v" | jq -e '.verdict == "APPROVE" and (.blockers | length == 0)' > /dev/null && [ -z "$extra" ] || exit 3
