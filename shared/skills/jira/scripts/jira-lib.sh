#!/usr/bin/env bash
# Plumbing shared by the jira skill's scripts, sourced by each of them: . "$(dirname "${BASH_SOURCE[0]}")/jira-lib.sh"
# Sourcing reads the env files like land.sh and dispatch.sh do (caller env > ai-toolkit.local.env), so JIRA_* set there reach the script.
# The caller sets $key (the ticket) before api / transition_to, then calls jira_config and jira_token before any request.

fail() { printf '%s: %s\n' "${0##*/}" "$*" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for lib in "$here/../../../../.ai-toolkit/scripts/lib.sh" "$here/../../../../scripts/lib.sh"; do   # a host project, then the toolkit checkout
  if [ -f "$lib" ]; then . "$lib"; load_env; break; fi
done
unset here lib

jira_key() { [[ $1 =~ ^[A-Z][A-Z0-9_]+-[0-9]+$ ]] || fail "bad tracker key: $1"; }

jira_config() {
  site="${JIRA_SITE:-}" email="${JIRA_EMAIL:-}"
  [[ $site =~ ^[A-Za-z0-9.-]+$ ]] || fail "JIRA_SITE is missing or not a host name (set it in .ai-toolkit/ai-toolkit.local.env)"
  case "$email" in '' | *[[:space:]\"\\:]*) fail "JIRA_EMAIL is missing or has a space, quote, backslash or colon" ;; esac
}

jira_token() {
  token="$(security find-generic-password -a "${USER:-$(id -un)}" -s JIRA_API_TOKEN -w 2> /dev/null)" || token=""
  [ -n "$token" ] && [[ $token != *[[:cntrl:]]* ]] || fail "no usable JIRA_API_TOKEN in the Keychain (security add-generic-password -a \"\$USER\" -s JIRA_API_TOKEN -w)"
  token="${token//\\/\\\\}"; token="${token//\"/\\\"}"   # curl config syntax
}

# api <METHOD> <path after the ticket> [json]: prints the response. The credentials go to curl on stdin, never in argv.
api() {
  local args=(--fail --max-time 20 -sS -X "$1" -H 'Accept: application/json') out rc=0
  [ $# -ge 3 ] && args+=(-H 'Content-Type: application/json' --data "$3")
  out="$(printf 'user = "%s:%s"\n' "$email" "$token" | curl --config - "${args[@]}" "https://$site/rest/api/3/issue/$key$2" 2>&1)" || rc=$?
  [ "$rc" -eq 0 ] || fail "$1 $key$2: ${out%%$'\n'*}${out:+ }(curl exit $rc)"
  printf '%s' "$out"
}

# transition_to <name>: moves the ticket through the transition of that name (case-insensitive); an unknown name fails and lists the available ones.
transition_to() {
  local list id names
  list="$(api GET /transitions)"
  id="$(jq -r --arg n "$1" 'first(.transitions[] | select(.name | ascii_downcase == ($n | ascii_downcase)) | .id) // empty' <<< "$list")" || fail "unreadable transitions of $key"
  if [ -z "$id" ]; then
    names="$(jq -r '[.transitions[].name] | join(", ")' <<< "$list" 2> /dev/null)" || names="?"
    fail "no transition named '$1' on $key; available: $names"
  fi
  api POST /transitions "$(jq -nc --arg id "$id" '{transition: {id: $id}}')" > /dev/null
}
