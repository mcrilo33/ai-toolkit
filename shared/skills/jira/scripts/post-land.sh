#!/usr/bin/env bash
# POST_LAND_CMD adapter for Jira Cloud: post-land.sh <issue> <tracker-key> <landed-sha>, BASE_BRANCH in the environment.
# Comments on the ticket and, when JIRA_LAND_TRANSITION names one, moves it. Any failure exits non-zero with a one-line reason on stderr.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/jira-lib.sh"

[ $# -eq 3 ] || fail "usage: post-land.sh <issue> <tracker-key> <landed-sha>"
issue="$1" key="$2" sha="$3"
[[ $issue =~ ^[0-9]+$ ]] || fail "bad issue number: $issue"
jira_key "$key"
[[ $sha =~ ^[0-9a-f]{7,64}$ ]] || fail "bad sha: $sha"

want="${JIRA_LAND_TRANSITION:-}" base="${BASE_BRANCH:-}"
jira_config
[ -n "$base" ] || fail "BASE_BRANCH is not set"
jira_token

repo="$(git remote get-url origin 2> /dev/null | sed -E 's#^.*[:/]([^/:]+/[^/]+)$#\1#; s#\.git$##')" || repo=""
[[ $repo =~ ^[^/]+/[^/]+$ ]] || repo=""

body="$(jq -nc --arg base "$base" --arg sha "$sha" --arg ref "$repo#$issue" '
  def text($t): {type: "text", text: $t};
  def code($t): text($t) + {marks: [{type: "code"}]};
  {body: {type: "doc", version: 1, content: [{type: "paragraph", content: [
    text("Landed on "), code($base), text(" in "), code($sha), text(" (GitHub "), code($ref), text("); it reaches the next branch with its PR.")]}]}}')"
api POST /comment "$body" > /dev/null

[ -z "$want" ] || transition_to "$want"
