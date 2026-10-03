#!/usr/bin/env bash
# answer.sh <worktree> < question: the auto-answerer for a spoke's gate (06 section 4 step 7, D5). Headless, read-only claude
# in the spoke's worktree, driven by the afk-answering rule. stdout = the reply body (`approve` | `revise: <change>`), then any
# `WARN:` lines for the human. Exit 0 answered; 1 no usable answer (garbage, claude failed, rule missing): the caller escalates
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
out="$(cd "$wt" && claude -p --model "$ANSWER_MODEL" --append-system-prompt-file "$rule" --allowedTools Read,Grep,Glob --no-session-persistence)" || die "claude failed"
last="$(printf '%s\n' "$out" | sed '/^[[:space:]]*$/d' | tail -n 1)"
body="$(printf '%s' "$last" | sed -n 's/^ANSWER:[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/\1/p')"
case "$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')" in
  approve) body=approve ;;
  revise:*[![:space:]]*) body="revise: $(printf '%s' "${body#*:}" | sed 's/^[[:space:]]*//')" ;;
  *) die "no usable ANSWER line: '$last'" ;;
esac
printf '%s\n' "$body"
printf '%s\n' "$out" | sed -n 's/^\(WARN:.*[^[:space:]]\)[[:space:]]*$/\1/p'
