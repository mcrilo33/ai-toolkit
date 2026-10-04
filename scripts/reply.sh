#!/usr/bin/env bash
# reply.sh <run-id> <message-id> approve|'approve with: <change>'|'revise: <change>'|allow|deny: queue a human answer to a gate question (allow|deny: to a
# worker's permission question) for the coordinator (coordinator.sh --reply calls this). Only the Run's bound terminal can reply (Orca attests terminal identity), so the answer is one file per question (name =
# message id, content = the reply line) in a 0700 dir outside every worktree; the coordinator loop sends it at its next wake. Needs no Orca.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
run="${1:-}"; id="${2:-}"; body="${3:-}"
[ -n "$run" ] || usage_exit "--reply needs --run <run-id>"
valid_run "$run" || usage_exit "bad run id '$run'"
case "$id" in '' | *[!A-Za-z0-9_-]*) usage_exit "bad message id '$id'" ;; esac
apw='^approve[[:space:]]+with[[:space:]]*:[[:space:]]*([^[:space:]].*)$'
case "$body" in
  approve | allow | deny) ;;
  approve*with*:*[![:space:]]*) [[ "$body" =~ $apw ]] || usage_exit "the reply is 'approve', 'approve with: <change>', 'revise: <change>', or for a permission question 'allow' or 'deny'"; body="approve with: ${BASH_REMATCH[1]}" ;;
  revise:*[![:space:]]*) body="${body#revise:}"; body="revise: ${body#"${body%%[![:space:]]*}"}" ;;
  *) usage_exit "the reply is 'approve', 'approve with: <change>', 'revise: <change>', or for a permission question 'allow' or 'deny'" ;;
esac
d="$(spool_dir "$run")/replies"; (umask 077; mkdir -p "$d"); chmod 700 "$d"
printf '%s\n' "$body" > "$d/.$id.tmp" && mv "$d/.$id.tmp" "$d/$id"
echo "reply to $id queued: the coordinator sends it at its next wake"
