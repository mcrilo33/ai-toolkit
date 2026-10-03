#!/usr/bin/env bash
# End-to-end acceptance (06 section 7) against a LOCAL scratch repo with a local bare origin and a stubbed gh: no GitHub.
# WP2 = steps 1-7 UNATTENDED: coordinator.sh (in its own Orca terminal on the scratch main, own scratch Run) dispatches the issue,
# answers the PLAN gate (answer.sh, real claude -p), reviews (review.sh, real claude -p, Opus) and lands (land.sh --local-gate).
# E2E_ANSWER=human: the coordinator only notifies and this script replies after the ack. E2E_NEGATIVE=1: a REVIEW_CMD stub rejects
# once, so the re-dispatch to the same worker is proven live. Langfuse (D1) is out. Run it from a dedicated Orca terminal, e.g.
#   orca terminal create --worktree active --command "bash v2/e2e/spoke-scenario.sh"
# Exits non-zero on the first failed assert and prints the Orca object ids. Cleans up after itself.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
# Fixed path: Claude's workspace-trust entry is keyed on the git root, so one path means one entry, ever.
S=/private/tmp/aitk-e2e-scratch; E="$S.e2e"; rm -rf "$S" "$S.git" "$E"; mkdir -p "$S" "$E"
repo_id=""; wt=""; run=""; coord=""; br=1-add-hello-txt; step=0

say() { printf '== step %s: %s\n' "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s worktree=%s run=%s coordinator=%s\n  coordinator log: %s\n' "$step" "$*" "$repo_id" "$wt" "$run" "$coord" "$(tail -n 5 "$E/coord.log" 2> /dev/null | tr '\n' '|')"; exit 1; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
cleanup() {
  local d w ws=""
  [ -z "$coord" ] || orca terminal close --terminal "$coord" --json > /dev/null 2>&1 || true
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
step=1; say "preflight: orca ready, install.sh, scratch repo registered"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
bash "$V2/scripts/install.sh" > /dev/null 2>&1 || fail "install.sh failed"
git init -q -b main "$S" && git init -q --bare "$S.git" && git -C "$S" remote add origin "$S.git"
mkdir -p "$S/.ai-toolkit/bin" "$S/.ai-toolkit/scripts" "$S/.claude/hooks"   # the layout sync.sh will produce (06 section 6)
cp "$V2"/scripts/*.sh "$S/.ai-toolkit/scripts/"; cp "$V2/bin/claude-spoke" "$S/.ai-toolkit/bin/"
cp "$V2/settings/ai-toolkit.env" "$S/.ai-toolkit/ai-toolkit.env"
echo '#!/bin/sh' > "$S/.claude/hooks/guard.sh"
mkdir -p "$S/.claude/agents" "$S/.ai-toolkit/rules"   # what sync.sh will ship: the review agent and the answering rule
cp "$V2/../shared/agents/code-review.md" "$S/.claude/agents/"; cp "$V2/../shared/rules/on-demand/afk-answering.md" "$S/.ai-toolkit/rules/"
# Stub GitHub for dispatch/land/setup: AI_TOOLKIT_GH in the local env (setup runs in Orca's terminal, not this PATH).
body="Create hello.txt in the repo root containing exactly the word hello, then commit it.\n\nScope: hello.txt\nGate: plan\nModel: $SPOKE_MODEL ${E2E_EFFORT:-low}"
jq -n --arg b "$(printf '%b' "$body")" '{number: 1, title: "Add hello.txt", body: $b}' > "$E/issue.json"
jq -c '[{number, body, labels: {nodes: []}, blockedBy: {nodes: []}}]' "$E/issue.json" > "$E/nodes.json"
# shellcheck disable=SC2016
# graphql lists the open issues: empty once `issue close` was logged, like GitHub (else the coordinator would dispatch it again)
printf '#!/bin/sh\necho "$*" >> %s/gh.log\ncase "$1 $2" in "issue view") cat %s/issue.json ;; "api graphql") if grep -q "^issue close" %s/gh.log; then echo "[]"; else cat %s/nodes.json; fi ;; esac\n' "$E" "$E" "$E" "$E" > "$E/gh"
chmod +x "$E/gh"
printf 'AI_TOOLKIT_GH=%s/gh\nCHECK_CMD="test -f hello.txt"\nLOCAL_GATE=1\nANSWER_MODEL=%s\n' "$E" "${E2E_ANSWER_MODEL:-claude-sonnet-5-5}" > "$S/.ai-toolkit/ai-toolkit.local.env"
# A target's .ai-toolkit/ is untracked (the user's global gitignore lists it), so a new worktree has no
# copy: the hooks run from the main checkout. Orca's runner is a bash script, so $ORCA_ROOT_PATH expands.
# shellcheck disable=SC2016
printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: $ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh\n  archive: $ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh\n' > "$S/orca.yaml"
printf '.claude/\n.ai-toolkit/\n' > "$S/.gitignore"   # explicit: never rely on a global gitignore
git -C "$S" add -A && git -C "$S" commit -q -m "chore: scratch repo" && git -C "$S" push -q -u origin main
repo_id="$(orca_json repo add --path "$S" | jq -r '.result.repo.id')"
[ -n "$repo_id" ] && [ "$repo_id" != null ] || fail "orca repo add"

step=2; say "provisioned (sync.sh arrives in WP4; the issue is a stubbed gh, a real GitHub repo is not needed)"
step=3; say "coordinator.sh --answer ${E2E_ANSWER:-auto} --cap 1 --drain, started in an Orca terminal on the scratch main"
renv=""
if [ -n "${E2E_NEGATIVE:-}" ]; then   # the first review rejects (exit 3 + BLOCKER lines), the second approves
  printf '#!/bin/sh\nif [ -e %s/rejected ]; then echo "SUMMARY: stub approve"; exit 0; fi\ntouch %s/rejected\necho "BLOCKER: hello.txt:1 - add a second line containing world"\nexit 3\n' "$E" "$E" > "$E/review-stub.sh"
  chmod +x "$E/review-stub.sh"; renv="REVIEW_CMD=$E/review-stub.sh"
fi
: > "$E/coord.log"
coord="$(orca_json terminal create --worktree "path:$S" --title coordinator --command \
  "$renv bash $S/.ai-toolkit/scripts/coordinator.sh --answer ${E2E_ANSWER:-auto} --cap 1 --drain > $E/coord.log 2>&1; echo \$? > $E/coord.rc" | jq -r '.result.terminal.handle // empty')"
[ -n "$coord" ] || fail "terminal create"
wait_for 90 grep -q ' coordinator: run run_' "$E/coord.log" || fail "the coordinator did not start"
run="$(sed -n 's/.* coordinator: run \(run_[0-9a-f]*\).*/\1/p' "$E/coord.log" | head -n 1)"

step=4; say "provisioned by setup.sh, issue linked in-progress, agent working with the spoke_run_id OTel attribute"
has_wt() { wt="$(orca_json worktree show --worktree issue:1 2> /dev/null | jq -r '.result.worktree.path // empty')"; [ -n "$wt" ]; }
wait_for 300 has_wt || fail "no worktree for issue 1 within 5 min"
wait_for 120 test -f "$wt/.ai-toolkit/setup-done" || fail "setup did not finish"
[ -d "$wt/.claude/hooks" ] || fail ".claude/hooks missing: setup did not run"
[ -s "$wt/.ai-toolkit/spoke-run-id" ] || fail "spoke-run-id missing"
grep -q 'Add hello.txt' "$wt/.ai-toolkit/task.md" || fail "task.md has no issue"
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
orca_json worktree show --worktree issue:1 | jq -e '.result.worktree | .linkedIssue == 1 and .workspaceStatus == "in-progress"' > /dev/null || fail "issue 1 is not linked in-progress"

step=5; say "PLAN gate: the worker asks before coding; ${E2E_ANSWER:-auto} answer"
inbox() { orca_json orchestration inbox --limit 200 --full | jq -c --arg r "$run" '[.result.messages[] | select(.run_id == $r)]'; }
question() { inbox | jq -r '[.[] | select(.type == "question")][0].id // empty'; }
replied() { inbox | jq -e '[.[] | select(.type == "status" and (.subject | startswith("Re:")))] | length > 0' > /dev/null; }
asked() { [ -n "$(question)" ]; }
wait_for 600 asked || fail "no question within 10 min"
if [ "${E2E_ANSWER:-auto}" = human ]; then   # the human: reply AFTER the coordinator acked the question without replying
  wait_for 60 grep -q 'waiting for you' "$E/coord.log" || fail "the coordinator did not hand the question to the human"
  sleep 5   # run the very command the coordinator put in the issue comment (gh.log): the human's reply after the ack
  cmd="$(sed -n 's/.*Reply: \(orca orchestration reply --run [^ ]* --from [^ ]* --id [^ ]* --body approve\).*/\1/p' "$E/gh.log" | head -n 1)"
  [ -n "$cmd" ] || fail "no reply command in the issue comment"
  out="$($cmd 2>&1)" || fail "human reply failed: $out"
fi
wait_for 300 replied || fail "the question was never replied"

step=6; say "worker_done succeeded, review APPROVE, coordinator drained (exit 0)"
wait_for 2400 test -s "$E/coord.rc" || fail "the coordinator did not finish within 40 min"
[ "$(cat "$E/coord.rc")" = 0 ] || fail "coordinator exited $(cat "$E/coord.rc")"
inbox | jq -e '[.[] | select(.type == "worker_done" and (.payload | fromjson | .outcome == "succeeded"))] | length > 0' > /dev/null || fail "no worker_done succeeded in the Run"
grep -q '^SUMMARY: ' "$E/coord.log" || fail "no review verdict in the coordinator log"
[ -z "${E2E_NEGATIVE:-}" ] || { grep -q 'round 1' "$E/coord.log" && [ -e "$E/rejected" ] || fail "the rejected review was not re-dispatched"; }

step=7; say "landed: main ff + pushed, issue closed, branch/worktree gone, worker released"
[ "$(git -C "$S.git" show main:hello.txt | head -n 1)" = hello ] || fail "origin/main has no hello.txt"
[ -z "${E2E_NEGATIVE:-}" ] || git -C "$S.git" show main:hello.txt | grep -qx world || fail "the review round was not applied"
[ "$(git -C "$S" rev-parse HEAD)" = "$(git -C "$S.git" rev-parse main)" ] || fail "local main is not at origin/main"
! git -C "$S.git" rev-parse -q --verify "refs/heads/$br" > /dev/null || fail "remote branch $br still exists"
grep -q "^issue close 1 -c landed in $(git -C "$S.git" rev-parse main)" "$E/gh.log" || fail "issue was not closed with the landed sha"
[ "$(orca_json worktree list --repo "path:$S" | jq '[.result.worktrees[] | select(.linkedIssue == 1)] | length')" = 0 ] || fail "worktree for issue 1 still listed"
[ ! -d "$wt" ] || fail "worktree directory still exists"
[ "$(orca_json orchestration worker-list --run "$run" --terminal-state active | jq '.result.workers | length')" = 0 ] || fail "a worker terminal is still active"
echo "PASS steps 1-7 (spoke_run_id=$id, run=$run)"
