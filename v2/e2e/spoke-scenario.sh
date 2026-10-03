#!/usr/bin/env bash
# End-to-end acceptance (06 section 7) against a LOCAL scratch repo with a local bare origin and a stubbed gh: no GitHub.
# WP1 = steps 1-7: dispatch.sh --next, the PLAN gate answered by this script (the human's job), worker_done, land.sh
# --local-gate. Review (WP2), Langfuse (D1) and the coordinator loop (WP2) are out. This script owns its OWN scratch Run
# (run-create) and never touches another Run. Run it from a dedicated Orca terminal: that terminal is the Run owner, e.g.
#   orca terminal create --worktree active --command "bash v2/e2e/spoke-scenario.sh"
# Exits non-zero on the first failed assert and prints the Orca object ids. Cleans up after itself.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
H="$ORCA_TERMINAL_HANDLE"
# Fixed path: Claude's workspace-trust entry is keyed on the git root, so one path means one entry, ever.
S=/private/tmp/aitk-e2e-scratch; E="$S.e2e"; rm -rf "$S" "$S.git" "$E"; mkdir -p "$S" "$E"
repo_id=""; wt=""; run=""; disp=""; br=""; msg=""; delivery=""; step=0

say() { printf '== step %s: %s\n' "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s worktree=%s run=%s dispatch=%s\n' "$step" "$*" "$repo_id" "$wt" "$run" "$disp"; exit 1; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
cleanup() {
  local d w ws=""
  for d in $([ -z "$run" ] || orca_json orchestration worker-list --run "$run" 2> /dev/null | jq -r '.result.workers[]?.dispatchId' 2> /dev/null); do
    orca orchestration worker-stop --dispatch "$d" --json > /dev/null 2>&1 || true   # only this script's own Run
    orca orchestration worker-release --dispatch "$d" --json > /dev/null 2>&1 || true
  done
  # Every non-main worktree of the scratch repo, found by listing: dispatch.sh may have died after worktree create.
  for w in $([ -z "$repo_id" ] || orca_json worktree list --repo "path:$S" 2> /dev/null | jq -r '.result.worktrees[]? | select(.isMainWorktree | not) | .path' 2> /dev/null); do
    ws="$(dirname "$w")"
    orca worktree rm --worktree "path:$w" --force --run-hooks --json > /dev/null 2>&1 || true
  done
  [ -z "$repo_id" ] || orca project setup-delete --setup "$repo_id" --json > /dev/null 2>&1 || true
  [ -z "$wt" ] || ws="$(dirname "$wt")"
  case "$ws" in */"$(basename "$S")") rm -rf "$ws" ;; esac   # Orca's per-repo workspace dir
  rm -rf "$S" "$S.git" "$E"
}
trap cleanup EXIT
# await <type> <seconds>: wait for a <type> message in the scratch Run; sets $msg and $delivery (acked by `ack`).
await() {
  local i out; msg=""
  for ((i = 0; i < $2; i += 30)); do
    out="$(orca_json orchestration check --run "$run" --terminal "$H" --wait --types "$1" --timeout-ms 30000 2> /dev/null)" || true
    msg="$(printf '%s' "$out" | jq -c --arg t "$1" '[.result.messages[]? | select(.type == $t)][0] // empty' 2> /dev/null)" || msg=""
    delivery="$(printf '%s' "$out" | jq -r '.result.deliveryId // empty' 2> /dev/null)" || delivery=""
    [ -z "$msg" ] || return 0
  done
  return 1
}
ack() { [ -z "$delivery" ] || orca_json orchestration check --run "$run" --terminal "$H" --ack "$delivery" > /dev/null; }

step=1; say "preflight: orca ready, install.sh, scratch repo registered"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
bash "$V2/scripts/install.sh" > /dev/null 2>&1 || fail "install.sh failed"
git init -q -b main "$S" && git init -q --bare "$S.git" && git -C "$S" remote add origin "$S.git"
mkdir -p "$S/.ai-toolkit/bin" "$S/.ai-toolkit/scripts" "$S/.claude/hooks"   # the layout sync.sh will produce (06 section 6)
cp "$V2"/scripts/*.sh "$S/.ai-toolkit/scripts/"; cp "$V2/bin/claude-spoke" "$S/.ai-toolkit/bin/"
cp "$V2/settings/ai-toolkit.env" "$S/.ai-toolkit/ai-toolkit.env"
echo '#!/bin/sh' > "$S/.claude/hooks/guard.sh"
# Stub GitHub for dispatch/land/setup: AI_TOOLKIT_GH in the local env (setup runs in Orca's terminal, not this PATH).
body="Create hello.txt in the repo root containing exactly the word hello, then commit it.\n\nScope: hello.txt\nGate: plan\nModel: $SPOKE_MODEL ${E2E_EFFORT:-low}"
jq -n --arg b "$(printf '%b' "$body")" '{number: 1, title: "Add hello.txt", body: $b}' > "$E/issue.json"
jq -c '[{number, body, labels: {nodes: []}, blockedBy: {nodes: []}}]' "$E/issue.json" > "$E/nodes.json"
# shellcheck disable=SC2016
printf '#!/bin/sh\necho "$*" >> %s/gh.log\ncase "$1 $2" in "issue view") cat %s/issue.json ;; "api graphql") cat %s/nodes.json ;; esac\n' "$E" "$E" "$E" > "$E/gh"
chmod +x "$E/gh"
printf 'AI_TOOLKIT_GH=%s/gh\nCHECK_CMD="test -f hello.txt"\n' "$E" > "$S/.ai-toolkit/ai-toolkit.local.env"
# A target's .ai-toolkit/ is untracked (the user's global gitignore lists it), so a new worktree has no
# copy: the hooks run from the main checkout. Orca's runner is a bash script, so $ORCA_ROOT_PATH expands.
# shellcheck disable=SC2016
printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: $ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh\n  archive: $ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh\n' > "$S/orca.yaml"
printf '.claude/\n.ai-toolkit/\n' > "$S/.gitignore"   # explicit: never rely on a global gitignore
git -C "$S" add -A && git -C "$S" commit -q -m "chore: scratch repo" && git -C "$S" push -q -u origin main
repo_id="$(orca_json repo add --path "$S" | jq -r '.result.repo.id')"
[ -n "$repo_id" ] && [ "$repo_id" != null ] || fail "orca repo add"

step=2; say "provisioned (sync.sh arrives in WP4; the issue is a stubbed gh, a real GitHub repo is not needed)"
step=3; say "dispatch.sh --next: worktree, setup, two-step launch, worker-start, issue linked"
run="$(orca_mutate orchestration run-create --objective "wp1 e2e" --from "$H" | jq -r '.result.run.id')"
[ -n "$run" ] && [ "$run" != null ] || fail "run-create"
cd "$S"
out="$(RUN="$run" "$S/.ai-toolkit/scripts/dispatch.sh" --next)" || fail "dispatch.sh --next exited $?"
disp="$(printf '%s' "$out" | jq -r .dispatch)"; wt="$(printf '%s' "$out" | jq -r .worktree)"
[ "$(printf '%s' "$out" | jq -r .issue)" = 1 ] && [ -n "$disp" ] && [ -d "$wt" ] || fail "dispatch output: $out"

step=4; say "provisioned by setup.sh, issue linked in-progress, agent working with the spoke_run_id OTel attribute"
[ -d "$wt/.claude/hooks" ] || fail ".claude/hooks missing: setup did not run"
[ -s "$wt/.ai-toolkit/spoke-run-id" ] || fail "spoke-run-id missing"
grep -q 'Add hello.txt' "$wt/.ai-toolkit/task.md" || fail "task.md has no issue"
orca_json worktree show --worktree issue:1 | jq -e '.result.worktree | .linkedIssue == 1 and .workspaceStatus == "in-progress"' > /dev/null || fail "issue 1 is not linked in-progress"
agent_working() { orca_json worktree ps | jq -e --arg p "$wt" '[.. | objects | select(.path? == $p) | .agents[]? | select(.state == "working")] | length > 0' > /dev/null; }
wait_for 180 agent_working || fail "agent never reported state working"
id="$(cat "$wt/.ai-toolkit/spoke-run-id")"
claude_env() {   # the environment of the claude process running in $wt
  local pid
  for pid in $(pgrep -x claude); do
    [ "$(lsof -a -p "$pid" -d cwd -Fn 2> /dev/null | sed -n 's/^n//p')" = "$wt" ] && { ps eww -p "$pid" | tr ' ' '\n'; return 0; }
  done
  return 1
}
claude_env | grep -q "^OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=$id" || fail "claude runs without the spoke_run_id OTel attribute"

step=5; say "PLAN gate: the worker asks before coding; this script replies approve (the human's job)"
await question 600 || fail "no question within 10 min"
[ ! -e "$wt/hello.txt" ] || fail "hello.txt exists before the gate was answered"
orca_mutate orchestration reply --run "$run" --from "$H" --id "$(printf '%s' "$msg" | jq -r .id)" --body approve > /dev/null || fail "reply"
ack

step=6; say "worker_done succeeded; branch pushed with hello.txt"
await worker_done 900 || fail "no worker_done within 15 min"
printf '%s' "$msg" | jq -e '.payload | (if type == "string" then fromjson else . end) | .outcome == "succeeded"' > /dev/null || fail "worker_done: $msg"
ack
br="$(git -C "$wt" branch --show-current)"
git -C "$S.git" rev-parse -q --verify "refs/heads/$br" > /dev/null || fail "branch $br was not pushed"
git -C "$S.git" diff --name-only main "$br" | grep -qx hello.txt || fail "hello.txt is not in the branch diff"

step=7; say "land.sh --local-gate: main ff + pushed, issue closed, branch/worktree gone, worker released"
RUN="$run" "$S/.ai-toolkit/scripts/land.sh" --local-gate 1 || fail "land.sh exited $?"
[ "$(git -C "$S.git" show main:hello.txt)" = hello ] || fail "origin/main has no hello.txt"
[ "$(git -C "$S" rev-parse HEAD)" = "$(git -C "$S.git" rev-parse main)" ] || fail "local main is not at origin/main"
! git -C "$S.git" rev-parse -q --verify "refs/heads/$br" > /dev/null || fail "remote branch $br still exists"
grep -q "^issue close 1 -c landed in $(git -C "$S.git" rev-parse main)" "$E/gh.log" || fail "issue was not closed with the landed sha"
[ "$(orca_json worktree list --repo "path:$S" | jq '[.result.worktrees[] | select(.linkedIssue == 1)] | length')" = 0 ] || fail "worktree for issue 1 still listed"
[ ! -d "$wt" ] || fail "worktree directory still exists"
[ "$(orca_json orchestration worker-list --run "$run" --terminal-state active | jq '.result.workers | length')" = 0 ] || fail "a worker terminal is still active"
echo "PASS steps 1-7 (spoke_run_id=$id)"
