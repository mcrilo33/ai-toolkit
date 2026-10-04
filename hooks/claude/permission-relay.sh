#!/usr/bin/env bash
# permission-relay: PermissionRequest(*). In a worker (the spoke marker) every tool-permission dialog, whatever raised it (a PreToolUse "ask", an `ask`
# rule, a prompt Claude Code raises itself in bypass mode, a subagent's), is put to the Run with `orca orchestration ask` and resolved with the reply:
# exactly `allow` lets the call through, anything else denies. Outside a worker: no output, the local prompt stays. Measured on claude 2.1.289: the event
# fires in bypass mode and an allow/deny from it dismisses the dialog, but a hook that hits its own timeout is killed with NO decision and the dialog stays
# waiting, so the ask is bounded by a kill timer below the hook timeout (settings: 600 s) and every failure (no handle, no Run, orca erroring, timeout,
# unparsable payload, no jq) answers deny. AskUserQuestion and ExitPlanMode are denied without asking: a hook allow does not resolve ExitPlanMode and
# AskUserQuestion needs the user's answers, not a yes. The question's first line, PERMISSION REQUEST, is what coordinator.sh and the coordinate skill key on.
set -eo pipefail
root="${CLAUDE_PROJECT_DIR:-$PWD}"
[ -e "$root/.ai-toolkit/spoke-run-id" ] || exit 0
deny() { printf '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"deny","message":"permission relay: denied (%s). Do not retry the same call; say so in your worker_done report."}}}\n' "$1"; exit 0; }
trap 'deny "the relay itself failed"' ERR
in="$(cat)"; tool="$(jq -er .tool_name <<< "$in")"
case "$tool" in AskUserQuestion | ExitPlanMode) deny "$tool is not a permission: ask the coordinator with your preamble's orchestration ask";; esac
h="${ORCA_TERMINAL_HANDLE:-}"; [ -n "$h" ] || deny "no Orca terminal handle"
reason=unknown; rf="$root/.ai-toolkit/ask-reasons/$(jq -cS '[.tool_name,.tool_input]' <<< "$in" | shasum -a 256 | cut -d' ' -f1)" # why a PreToolUse ask raised it (danger-guard records it)
if [ -f "$rf" ]; then ts="$(head -n1 "$rf")"; case "$ts" in '' | *[!0-9]*) ts=0;; esac; ts=$((10#${ts:0:10})) # a corrupt epoch is stale, never an arithmetic error
  age=$(($(date +%s) - ts)); if [ "$age" -ge 0 ] && [ "$age" -le 600 ]; then reason="$(tail -n +2 "$rf" | tr -s '[:space:]' ' ' | cut -c1-300)"; fi; rm -f "$rf"; fi # one capped line: it cannot pose as a request field
issue="$(sed -n '1s/^# *//p' "$root/.ai-toolkit/task.md" 2> /dev/null || :)"
q="$(jq -r --arg root "$root" --arg issue "${issue:-unknown}" --arg reason "$reason" --argjson cap "${PERMISSION_RELAY_CAP:-6000}" '
  def show: tostring | if length > $cap then .[:$cap] + "\n[truncated: \(length) chars in all]" else . end;
  "PERMISSION REQUEST (not a plan gate: reply allow or deny)", "issue: \($issue)", "worktree: \($root)", "tool: \(.tool_name)", "cwd: \(.cwd // "?")",
  "mode: \(.permission_mode // "?")", "reason: \($reason)",
  (.tool_input // {} | if type == "object" then to_entries[] else {key: "input", value: .} end | "\(.key):", (.value | if type == "string" then . else tojson end | show | split("\n") | map("  " + .) | join("\n")))' <<< "$in")"
o="$(mktemp)"; orca orchestration ask --from "$h" --question "$q" --options allow,deny --timeout-ms "${PERMISSION_RELAY_ASK_MS:-540000}" > "$o" 2> /dev/null & p=$!
{ sleep "${PERMISSION_RELAY_KILL_S:-570}" && touch "$o.killed" && kill -9 $p; } > /dev/null 2>&1 & w=$!   # && : a sleep killed on the normal path must not go on to kill
rc=0; wait $p 2> /dev/null || rc=$?; pkill -P $w 2> /dev/null || :; kill $w 2> /dev/null || :; wait $w 2> /dev/null || :
reply="$(cat "$o")"; killed=0; [ ! -e "$o.killed" ] || killed=1; rm -f "$o" "$o.killed"
[ "$killed" = 0 ] || deny "no answer before the relay timed out"
[ "$rc" = 0 ] || deny "the ask failed: no Run to ask, or orca erroring"
reply="${reply#"${reply%%[![:space:]]*}"}"; reply="${reply%"${reply##*[![:space:]]}"}"
[ "$reply" = allow ] || deny "the Run did not answer allow"
printf '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}\n'
