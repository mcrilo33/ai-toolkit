#!/usr/bin/env bash
# POST_LAND_CMD adapter for Jira Cloud: post-land.sh <issue> <tracker-key> <landed-sha>, BASE_BRANCH in the environment.
# Comments on the ticket and, when JIRA_LAND_TRANSITION names one, moves it. Any failure exits non-zero with a one-line reason on stderr.
set -euo pipefail

fail() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }

[ $# -eq 3 ] || fail "usage: post-land.sh <issue> <tracker-key> <landed-sha>"
issue="$1" key="$2" sha="$3"
[[ $issue =~ ^[0-9]+$ ]] || fail "bad issue number: $issue"
[[ $key =~ ^[A-Z][A-Z0-9_]+-[0-9]+$ ]] || fail "bad tracker key: $key"
[[ $sha =~ ^[0-9a-f]{7,64}$ ]] || fail "bad sha: $sha"

# land.sh reads the local env file without exporting it, so this script reads it the same way: caller env > ai-toolkit.local.env.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for lib in "$here/../../../../.ai-toolkit/scripts/lib.sh" "$here/../../../../scripts/lib.sh"; do   # a host project, then the toolkit checkout
  if [ -f "$lib" ]; then . "$lib"; load_env; break; fi
done

site="${JIRA_SITE:-}" email="${JIRA_EMAIL:-}" want="${JIRA_LAND_TRANSITION:-}" base="${BASE_BRANCH:-}"
[[ $site =~ ^[A-Za-z0-9.-]+$ ]] || fail "JIRA_SITE is missing or not a host name (set it in .ai-toolkit/ai-toolkit.local.env)"
case "$email" in '' | *[[:space:]\"\\:]*) fail "JIRA_EMAIL is missing or has a space, quote, backslash or colon" ;; esac
[ -n "$base" ] || fail "BASE_BRANCH is not set"

token="$(security find-generic-password -a "${USER:-$(id -un)}" -s JIRA_API_TOKEN -w 2> /dev/null)" || token=""
[ -n "$token" ] && [[ $token != *[[:cntrl:]]* ]] || fail "no usable JIRA_API_TOKEN in the Keychain (security add-generic-password -a \"\$USER\" -s JIRA_API_TOKEN -w)"
token="${token//\\/\\\\}"; token="${token//\"/\\\"}"   # curl config syntax

# api <METHOD> <path> [json]: prints the response. The credentials go to curl on stdin, never in argv.
api() {
  local args=(--fail --max-time 20 -sS -X "$1" -H 'Accept: application/json') out rc=0
  [ $# -ge 3 ] && args+=(-H 'Content-Type: application/json' --data "$3")
  out="$(printf 'user = "%s:%s"\n' "$email" "$token" | curl --config - "${args[@]}" "https://$site/rest/api/3/issue/$key$2" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "$1 $key$2: ${out%%$'\n'*}${out:+ }(curl exit $rc)"
  printf '%s' "$out"
}

repo="$(git remote get-url origin 2> /dev/null | sed -E 's#^.*[:/]([^/:]+/[^/]+)$#\1#; s#\.git$##')" || repo=""
[[ $repo =~ ^[^/]+/[^/]+$ ]] || repo=""

body="$(jq -nc --arg base "$base" --arg sha "$sha" --arg ref "$repo#$issue" '
  def text($t): {type: "text", text: $t};
  def code($t): text($t) + {marks: [{type: "code"}]};
  {body: {type: "doc", version: 1, content: [{type: "paragraph", content: [
    text("Landed on "), code($base), text(" in "), code($sha), text(" (GitHub "), code($ref), text("); it reaches the next branch with its PR.")]}]}}')"
api POST /comment "$body" > /dev/null

[ -n "$want" ] || exit 0
list="$(api GET /transitions)"
id="$(jq -r --arg n "$want" 'first(.transitions[] | select(.name | ascii_downcase == ($n | ascii_downcase)) | .id) // empty' <<< "$list")" || fail "unreadable transitions of $key"
if [ -z "$id" ]; then
  names="$(jq -r '[.transitions[].name] | join(", ")' <<< "$list" 2> /dev/null)" || names="?"
  fail "no transition named '$want' on $key; available: $names"
fi
api POST /transitions "$(jq -nc --arg id "$id" '{transition: {id: $id}}')" > /dev/null
