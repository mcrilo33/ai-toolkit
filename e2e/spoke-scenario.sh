#!/usr/bin/env bash
# End-to-end acceptance (06 section 7) against a LOCAL scratch repo with a local bare origin and a stubbed gh: no GitHub.
# WP2 = steps 1-7 UNATTENDED: coordinator.sh (in its own Orca terminal on the scratch main, own scratch Run) dispatches the issue,
# answers the PLAN gate (answer.sh, real claude -p), reviews (review.sh, real claude -p, Opus) and lands (land.sh --local-gate).
# E2E_ANSWER=human: the coordinator only flags the gate (worktree comment, bell) and this script replies after the ack. E2E_NEGATIVE=1: a REVIEW_CMD stub rejects
# once, so the re-dispatch to the same worker is proven live. Langfuse (D1) is out. Run it from a dedicated Orca terminal, e.g.
#   orca terminal create --worktree active --command "bash e2e/spoke-scenario.sh"   (v2/e2e/ until the cutover)
# Exits non-zero on the first failed assert and prints the Orca object ids. Cleans up after itself.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
# Fixed path: Claude's workspace-trust entry is keyed on the git root, so one path means one entry, ever. The repo is registered
# in Orca ONCE and reused: each run resets it to its root commit and never unregisters (a removed repo leaves a stale card in the UI).
S=/private/tmp/aitk-e2e-scratch; E="$S.e2e"; rm -rf "$E"; mkdir -p "$S" "$E"
repo_id=""; wt=""; run=""; coord=""; br=1-add-hello-txt; step=0

say() { printf '== step %s: %s\n' "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s worktree=%s run=%s coordinator=%s\n  coordinator log: %s\n' "$step" "$*" "$repo_id" "$wt" "$run" "$coord" "$(tail -n 5 "$E/coord.log" 2> /dev/null | tr '\n' '|')"
  cp "$E/coord.log" "$S.last-coordinator.log" 2> /dev/null && echo "  full coordinator log: $S.last-coordinator.log"; exit 1; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
drop_worktrees() {   # every non-main worktree of the scratch repo, terminals first; found by listing: dispatch.sh may have died after worktree create
  local w
  for w in $([ -z "$repo_id" ] || orca_json worktree list --repo "path:$S" 2> /dev/null | jq -r '.result.worktrees[]? | select(.isMainWorktree | not) | .path' 2> /dev/null); do
    orca terminal close --worktree "path:$w" --all --json > /dev/null 2>&1 || true
    orca worktree rm --worktree "path:$w" --force --run-hooks --json > /dev/null 2>&1 || true
  done
}
cleanup() {   # also on failure: every terminal this script made (the coordinator's, the workers') is closed; the repo stays registered
  local d
  for d in $([ -z "$run" ] || orca_json orchestration worker-list --run "$run" 2> /dev/null | jq -r '.result.workers[]?.dispatchId' 2> /dev/null); do
    orca orchestration worker-stop --dispatch "$d" --json > /dev/null 2>&1 || true   # only this script's own Run
    orca orchestration worker-release --dispatch "$d" --json > /dev/null 2>&1 || true
  done
  [ -z "$coord" ] || orca terminal close --terminal "$coord" --json > /dev/null 2>&1 || true
  drop_worktrees
  [ -z "$repo_id" ] || orca terminal close --worktree "path:$S" --all --json > /dev/null 2>&1 || true
  [ -z "$run" ] || rm -rf "$HOME/.ai-toolkit/coordinator/$run"
  rm -rf "$E"
}
trap cleanup EXIT
step=1; say "preflight: orca ready, install.sh, scratch repo registered once and reset to its root commit"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
bash "$V2/scripts/install.sh" > /dev/null 2>&1 || fail "install.sh failed"
repo_id="$(orca_json repo list | jq -r --arg p "$S" '[.result.repos[] | select(.path == $p)][0].id // empty')"
if [ -n "$repo_id" ] && [ -d "$S.git" ] && git -C "$S" rev-parse -q --verify main > /dev/null; then   # reuse: back to the root commit, no branches
  drop_worktrees; orca terminal close --worktree "path:$S" --all --json > /dev/null 2>&1 || true
  git -C "$S" checkout -q -f main && git -C "$S" reset -q --hard "$(git -C "$S" rev-list --max-parents=0 main)" && git -C "$S" clean -fdxq
  git -C "$S" push -q -f origin main
  for b in $(git -C "$S" for-each-ref --format='%(refname:short)' refs/heads | grep -vx main); do git -C "$S" branch -q -D "$b"; done
  for b in $(git -C "$S.git" for-each-ref --format='%(refname:short)' refs/heads | grep -vx main); do git -C "$S.git" update-ref -d "refs/heads/$b"; done
else
  rm -rf "$S" "$S.git"; mkdir -p "$S"
  git init -q -b main "$S" && git init -q --bare "$S.git" && git -C "$S" remote add origin "$S.git"
  # A target's .ai-toolkit/ is untracked (the user's global gitignore lists it), so a new worktree has no copy: the hooks run
  # from the main checkout. Orca's runner is a bash script, so $ORCA_ROOT_PATH expands.
  # shellcheck disable=SC2016
  printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: $ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh\n  archive: $ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh\n' > "$S/orca.yaml"
  printf '.claude/\n.ai-toolkit/\n' > "$S/.gitignore"   # explicit: never rely on a global gitignore
  git -C "$S" add -A && git -C "$S" commit -q -m "chore: scratch repo" && git -C "$S" push -q -u origin main
  [ -n "$repo_id" ] || repo_id="$(orca_json repo add --path "$S" | jq -r '.result.repo.id')"
fi
[ -n "$repo_id" ] && [ "$repo_id" != null ] || fail "orca repo add"
mkdir -p "$S/.ai-toolkit/bin" "$S/.ai-toolkit/scripts" "$S/.claude/hooks"   # the layout sync.sh will produce (06 section 6)
cp "$V2"/scripts/*.sh "$S/.ai-toolkit/scripts/"; cp "$V2/bin/claude-spoke" "$S/.ai-toolkit/bin/"
cp "$V2/settings/ai-toolkit.env" "$S/.ai-toolkit/ai-toolkit.env"
echo '{}' > "$S/.ai-toolkit/claude-settings.json"   # a worker starts through the launcher only in a project that has the toolkit's guards file
echo '#!/bin/sh' > "$S/.claude/hooks/guard.sh"
mkdir -p "$S/.claude/agents" "$S/.ai-toolkit/rules"   # what sync.sh will ship: the review agent and the answering rule
SH="$V2/shared"; [ -d "$SH" ] || SH="$V2/../shared"   # shared/ sits beside v2/ until the cutover, inside the root after it
cp "$SH/agents/code-review.md" "$S/.claude/agents/"; cp "$SH/rules/on-demand/afk-answering.md" "$S/.ai-toolkit/rules/"
# Stub GitHub for dispatch/land/setup: AI_TOOLKIT_GH in the local env (setup runs in Orca's terminal, not this PATH).
body="Create hello.txt in the repo root containing exactly the word hello, then commit it.\n\nScope: hello.txt\nGate: plan\nModel: $SPOKE_MODEL ${E2E_EFFORT:-low}"
jq -n --arg b "$(printf '%b' "$body")" '{number: 1, title: "Add hello.txt", body: $b}' > "$E/issue.json"
jq -c '[{number, body, labels: {nodes: []}, blockedBy: {nodes: []}}]' "$E/issue.json" > "$E/nodes.json"
# shellcheck disable=SC2016
# graphql lists the open issues: empty once `issue close` was logged, like GitHub (else the coordinator would dispatch it again)
printf '#!/bin/sh\necho "$*" >> %s/gh.log\ncase "$1 $2" in "issue view") cat %s/issue.json ;; "api graphql") if grep -q "^issue close" %s/gh.log; then echo "[]"; else cat %s/nodes.json; fi ;; esac\n' "$E" "$E" "$E" "$E" > "$E/gh"
chmod +x "$E/gh"
printf 'AI_TOOLKIT_GH=%s/gh\nCHECK_CMD="test -f hello.txt"\nLOCAL_GATE=1\nANSWER_MODEL=%s\n' "$E" "${E2E_ANSWER_MODEL:-claude-sonnet-5-5}" > "$S/.ai-toolkit/ai-toolkit.local.env"

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
if [ "${E2E_ANSWER:-auto}" = human ]; then   # only the Run's bound terminal can reply: queue it with --reply from THIS (another) terminal
  wait_for 90 grep -q 'gate question waiting' "$E/coord.log" || fail "the coordinator did not hand the question to the human"
  [ ! -e "$wt/hello.txt" ] || fail "the worker is not parked: hello.txt exists before the gate was answered"
  cmd="$(sed -n 's/.*Reply from any terminal: \(bash [^ ]* --run [^ ]* --reply [^ ]* approve\).*/\1/p' "$E/gh.log" | head -n 1)"
  [ -n "$cmd" ] || fail "no --reply command in the issue comment"
  out="$($cmd 2>&1)" || fail "reply failed: $out"
fi
wait_for 300 replied || fail "the question was never replied"
[ -z "$(ls "$HOME/.ai-toolkit/coordinator/$run/replies" 2> /dev/null)" ] || fail "a queued reply was left in the spool"

step=6; say "worker_done succeeded, review APPROVE, coordinator drained (exit 0)"
wait_for 2400 test -s "$E/coord.rc" || fail "the coordinator did not finish within 40 min"
[ "$(cat "$E/coord.rc")" = 0 ] || fail "coordinator exited $(cat "$E/coord.rc")"
inbox | jq -e '[.[] | select(.type == "worker_done" and (.payload | fromjson | .outcome == "succeeded"))] | length > 0' > /dev/null || fail "no worker_done succeeded in the Run"
grep -q '^SUMMARY: ' "$E/coord.log" || fail "no review verdict in the coordinator log"
[ -z "${E2E_NEGATIVE:-}" ] || { grep -q 'round 1' "$E/gh.log" && [ -e "$E/rejected" ] || fail "the rejected review was not re-dispatched"; }

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
