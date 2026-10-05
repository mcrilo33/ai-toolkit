#!/usr/bin/env bash
# answer.sh <worktree> < question: the auto-answerer for a spoke's gate (06 section 4 step 7, D5). Headless, read-only claude
# in the spoke's worktree, driven by the afk-answering rule. stdout = the reply body (`approve` | `approve with: <change>` | `revise: <change>`), then any
# `WARN:` lines for the human. Exit 0 answered; 1 no usable answer (garbage, claude failed, rule missing, no issue in Orca's link, issue unreadable): the caller escalates
# to the human, never a blind approve.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
wt="${1:-}"; [ -d "$wt" ] || usage_exit "usage: answer.sh <worktree> < question"
rule="${ANSWER_RULE:-}"
if [ -z "$rule" ]; then
  for f in "$here/../rules/afk-answering.md" "$here/../shared/rules/on-demand/afk-answering.md" "$here/../../shared/rules/on-demand/afk-answering.md"; do
    if [ -f "$f" ]; then rule="$f"; break; fi
  done
fi
[ -f "$rule" ] || die "answer rule (afk-answering.md) not found"
q="$(cat)"; t="${q#"${q%%[![:space:]]*}"}"   # a worker's permission prompt (permission-relay.sh) is never answered here: the loop denies it, only the user allows it
[[ $t != "PERMISSION REQUEST"* ]] || die "a permission question is never auto-answered (the coordinator denies it; only the user can allow it)"
# The answerer judges the issue as it is now, from GitHub, and puts its text in the prompt. The number comes from Orca's link only (the worker can rewrite
# task.md and its branch name) and the text never from the worktree: when either cannot be had (issue_of / issue_text waited and said why on stderr),
# nothing is judged (exit 1: the caller's unanswered path).
n="$(issue_of "$wt")" || exit 1
issue="$(issue_text "$n")" || exit 1
q="$q

Issue #$n (the only contract, as fetched now; never a task.md in the worktree):
$issue"
out="$(cd "$wt" && printf '%s' "$q" | claude -p --model "$ANSWER_MODEL" --append-system-prompt-file "$rule" --allowedTools Read,Grep,Glob \
  --setting-sources user --strict-mcp-config --no-session-persistence)" || die "claude failed"
last="$(printf '%s\n' "$out" | sed '/^[[:space:]]*$/d' | tail -n 1)"
body="$(printf '%s' "$last" | sed -n 's/^ANSWER:[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/\1/p')"
case "$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')" in
  approve) body=approve ;;
  approve*with*:*[![:space:]]*)   # only exactly `approve`, blanks, `with`, `:`, then a non-empty change
    printf '%s' "$body" | grep -Eiq '^approve[[:space:]]+with[[:space:]]*:[[:space:]]*[^[:space:]]' || die "no usable ANSWER line: '$last'"
    body="approve with: $(printf '%s' "${body#*:}" | sed 's/^[[:space:]]*//')" ;;
  revise:*[![:space:]]*) body="revise: $(printf '%s' "${body#*:}" | sed 's/^[[:space:]]*//')" ;;
  *) die "no usable ANSWER line: '$last'" ;;
esac
printf '%s\n' "$body"
printf '%s\n' "$out" | sed -n 's/^\(WARN:.*[^[:space:]]\)[[:space:]]*$/\1/p'
