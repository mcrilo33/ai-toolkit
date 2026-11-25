#!/usr/bin/env bash
# POST_DISPATCH_CMD adapter for Jira Cloud: post-dispatch.sh <issue> <tracker-key>, BASE_BRANCH in the environment.
# Does nothing unless JIRA_START_TRANSITION names a transition (e.g. In Progress): then it moves the ticket. Any failure exits non-zero with a one-line reason on stderr.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/jira-lib.sh"

[ $# -eq 2 ] || fail "usage: post-dispatch.sh <issue> <tracker-key>"
issue="$1" key="$2"
[[ $issue =~ ^[0-9]+$ ]] || fail "bad issue number: $issue"
jira_key "$key"

[ -n "${JIRA_START_TRANSITION:-}" ] || exit 0
jira_config
jira_token
transition_to "$JIRA_START_TRANSITION"
