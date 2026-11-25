#!/usr/bin/env bash
# Intake for Jira Cloud: read-ticket.sh <tracker-key> prints the ticket's summary, type, status and description (flattened from ADF to plain text).
# Acceptance criteria are usually in the description. Any failure exits non-zero with a one-line reason on stderr.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/jira-lib.sh"

[ $# -eq 1 ] || fail "usage: read-ticket.sh <tracker-key>"
key="$1"
jira_key "$key"
jira_config
jira_token

ticket="$(api GET '?fields=summary,description,issuetype,status')"
jq -r '
  def txt:
    if type != "object" then ""
    elif .type == "text" then .text
    elif .type == "hardBreak" then "\n"
    elif (.type | IN("mention", "emoji", "inlineCard", "status")) then (.attrs.text // .attrs.shortName // .attrs.url // "")
    else ([.content[]? | txt] | join("")) as $c
      | if .type == "listItem" then "- " + ($c | rtrimstr("\n")) + "\n"
        elif (.type | IN("paragraph", "heading", "codeBlock", "tableRow")) then $c + "\n"
        else $c end
    end;
  "Summary: \(.fields.summary)\nType: \(.fields.issuetype.name)\nStatus: \(.fields.status.name)\n\nDescription:\n" + (.fields.description | txt | rtrimstr("\n"))' <<< "$ticket" || fail "unreadable ticket $key"
