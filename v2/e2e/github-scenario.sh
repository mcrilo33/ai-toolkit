#!/usr/bin/env bash
# Real end-to-end (06 section 7 steps 1-10) on the throwaway PUBLIC repo mcrilo33/ai-toolkit-e2e: real gh issues, real GitHub CI on the exact
# SHA, real Orca, real claude. The only GitHub repo this script writes to: it refuses any other origin. A stable clone ($G) is registered in
# Orca ONCE (never unregistered), reset per run to the `e2e-base` tag (a force-push of the e2e main) and synced from this checkout (--local-only).
# Phases (E2E_PHASES, default "next happy negative"): next = `dispatch.sh --next` order on 5 real issues; happy = issue A (Gate: plan) through
# gate, review, CI, FF land; negative = issue B, a REVIEW_CMD wrapper rejects it every time -> 2 rounds -> `blocked`. E2E_ANSWER=human: the gate
# is answered with `coordinator.sh --reply` (scripted, or E2E_HUMAN_WAIT=1: it prints the line and waits for a REAL human). Run from an Orca terminal.
set -euo pipefail
V2="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=../scripts/lib.sh
. "$V2/scripts/lib.sh"
load_env
: "${ORCA_TERMINAL_HANDLE:?run me from an Orca terminal}"
R=mcrilo33/ai-toolkit-e2e; G=/private/tmp/aitk-e2e-gh; E="$G.e2e"; ANS="${E2E_ANSWER:-auto}"; PH=" ${E2E_PHASES:-next happy negative} "
repo_id=""; run=""; coord=""; step=0; A=""; B=""; mine=""; wt=""; sha=""
rm -rf "$E"; mkdir -p "$E"
say() { printf '== [%ss] step %s: %s\n' "$SECONDS" "$step" "$*"; }
fail() { printf 'FAIL step %s: %s\n  ids: repo=%s run=%s coordinator=%s issues=%s\n  coordinator log: %s\n' "$step" "$*" "$repo_id" "$run" "$coord" "$mine" "$(tail -n 5 "$E/coord.log" 2> /dev/null | tr '\n' '|')"
  cp "$E/coord.log" "$G.last-coordinator.log" 2> /dev/null && echo "  full log: $G.last-coordinator.log"; exit 1; }
phase() { case "$PH" in *" $1 "*) ;; *) return 1 ;; esac; }
wait_for() { local t="$1" i; shift; for ((i = 0; i < t; i += 3)); do "$@" && return 0; sleep 3; done; return 1; }
ghe() { gh -R "$R" "$@"; }
mkissue() { ghe issue create -t "$1" -b "$2" ${3:+-l "$3"} | sed 's#.*/##'; }   # title body [label] -> number
drop_worktrees() {   # every non-main worktree of the clone, terminals first
  local w
  for w in $([ -z "$repo_id" ] || orca_json worktree list --repo "path:$G" 2> /dev/null | jq -r '.result.worktrees[]? | select(.isMainWorktree | not) | .path' 2> /dev/null); do
    orca terminal close --worktree "path:$w" --all --json > /dev/null 2>&1 || true
    orca worktree rm --worktree "path:$w" --force --run-hooks --json > /dev/null 2>&1 || true
  done
}
cleanup() {   # also on failure: every terminal this script made is closed, the issues it filed are closed; the repo stays registered
  local d i
  for d in $([ -z "$run" ] || orca_json orchestration worker-list --run "$run" 2> /dev/null | jq -r '.result.workers[]?.dispatchId' 2> /dev/null); do
    orca orchestration worker-stop --dispatch "$d" --json > /dev/null 2>&1 || true; orca orchestration worker-release --dispatch "$d" --json > /dev/null 2>&1 || true
  done
  [ -z "$coord" ] || orca terminal close --terminal "$coord" --json > /dev/null 2>&1 || true
  drop_worktrees; [ -z "$repo_id" ] || orca terminal close --worktree "path:$G" --all --json > /dev/null 2>&1 || true
  for i in $mine; do ghe issue close "$i" > /dev/null 2>&1 || true; done
  [ -z "$run" ] || rm -rf "${AITK_STATE_DIR:-$HOME/.ai-toolkit/coordinator}/$run"; rm -rf "$E"
}
trap cleanup EXIT
step=1; say "preflight: orca ready, clone of $R registered once and reset to e2e-base, synced, hooks installed"
[ "$(orca_json status | jq -r '.result.runtime.state')" = ready ] || fail "orca status is not ready"
[ -d "$G/.git" ] || git clone -q "git@github.com:$R.git" "$G"
[ "$(git -C "$G" remote get-url origin | sed -E 's#.*[:/]([^/]+/[^/]+)$#\1#; s#\.git$##')" = "$R" ] || fail "origin of $G is not $R: refusing to touch it"
repo_id="$(orca_json repo list | jq -r --arg p "$G" '[.result.repos[] | select(.path == $p)][0].id // empty')"
[ -n "$repo_id" ] || repo_id="$(orca_json repo add --path "$G" | jq -r '.result.repo.id')"
drop_worktrees; orca terminal close --worktree "path:$G" --all --json > /dev/null 2>&1 || true
git -C "$G" fetch -q --prune --tags origin
if ! git -C "$G" rev-parse -q --verify refs/tags/e2e-base > /dev/null; then   # once: the base = README + orca.yaml + .gitignore + one pytest job + one test
  git -C "$G" checkout -q -f main; git -C "$G" reset -q --hard origin/main; mkdir -p "$G/tests" "$G/.github/workflows"
  # shellcheck disable=SC2016
  printf 'setupAgentStartupPolicy: wait-for-setup\nscripts:\n  setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"\n  archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"\n' > "$G/orca.yaml"
  printf '.claude/\n.ai-toolkit/\n/CLAUDE.md\n__pycache__/\n.pytest_cache/\n' > "$G/.gitignore"; printf 'def test_smoke():\n    assert True\n' > "$G/tests/test_smoke.py"
  printf 'name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: pip install pytest && pytest -q\n' > "$G/.github/workflows/ci.yml"
  git -C "$G" add -A; git -C "$G" -c core.hooksPath=/dev/null commit -q -m "chore: e2e base"; git -C "$G" tag e2e-base; git -C "$G" push -q -f origin main refs/tags/e2e-base
fi
git -C "$G" checkout -q -f main && git -C "$G" reset -q --hard e2e-base && git -C "$G" clean -fdq
for b in $(git -C "$G" for-each-ref --format='%(refname:short)' refs/heads | grep -vx main); do git -C "$G" branch -q -D "$b"; done
for b in $(git -C "$G" ls-remote --heads origin | sed 's#.*refs/heads/##' | grep -vx main); do git -C "$G" push -q origin --delete "$b"; done
git -C "$G" push -q -f origin main
for i in $(ghe issue list --state open --limit 200 --json number --jq '.[].number'); do ghe issue close "$i" > /dev/null; done
for l in priority hold blocked; do ghe label create "$l" > /dev/null 2>&1 || true; done
bash "$V2/scripts/sync.sh" "$G" --local-only > /dev/null || fail "sync.sh failed"
printf 'CHECK_CMD="pytest -q"\nLOCAL_GATE=0\nANSWER_MODEL=%s\n' "${E2E_ANSWER_MODEL:-claude-sonnet-5-5}" > "$G/.ai-toolkit/ai-toolkit.local.env"
bash "$V2/scripts/install.sh" "$G" > /dev/null 2>&1 || fail "install.sh failed"
cd "$G"
if phase next; then
  step=2; say "--next with real GraphQL: pick order priority, then number; hold and blocked-by are skipped until the blocker closes"
  K=$(mkissue "e2e next K" "blocked by X"); X=$(mkissue "e2e next X" "plain"); H=$(mkissue "e2e next H" "on hold" hold)   # K < X: only the blocked-by keeps K behind X
  Rr=$(mkissue "e2e next R" "plain"); P=$(mkissue "e2e next P" "urgent" priority); mine="$mine $X $K $H $Rr $P"
  gh api -X POST "repos/$R/issues/$K/dependencies/blocked_by" -F "issue_id=$(gh api "repos/$R/issues/$X" --jq .id)" --silent || fail "cannot set blocked-by"
  pick() { RUN=run_pick bash "$V2/scripts/dispatch.sh" --next --dry-run 2> /dev/null || true; }
  for want in $P $X $K $Rr ""; do
    got="$(pick)"; [ "$got" = "$want" ] || fail "--next picked '$got', expected '$want' (order P=$P X=$X R=$Rr K=$K, hold H=$H)"
    [ -z "$want" ] || ghe issue close "$want" > /dev/null
  done
  ghe issue close "$H" > /dev/null
fi
step=3; say "coordinator.sh --answer $ANS --cap 1 --drain in an Orca terminal on the clone's main (own Run)"
body="Create hello.py with a function greet(name) returning 'hello ' + name, and tests/test_hello.py testing it. Run pytest before pushing.\n\nScope: hello.py tests/test_hello.py\nGate: plan\nModel: ${SPOKE_MODEL} low"
phase happy && { A=$(mkissue "Add hello.py with a tested greet function" "$(printf '%b' "$body")"); mine="$mine $A"; }
body="Create calc.py with a function double(x) returning 2*x, and tests/test_calc.py testing it. Run pytest before pushing.\n\nScope: calc.py tests/test_calc.py\nGate: none\nModel: ${SPOKE_MODEL} low"
phase negative && { B=$(mkissue "Add calc.py with a tested double function" "$(printf '%b' "$body")"); mine="$mine $B"; }
renv="NOTIFY_CMD=true"; [ -z "${E2E_HUMAN_WAIT:-}" ] || renv=""
if [ -n "$B" ]; then   # the real reviewer for A, a stub that always rejects B (a real one would accept the fix in round 1)
  # shellcheck disable=SC2016
  printf '#!/bin/sh\n[ "$1" = %s ] && { echo "BLOCKER: stub reviewer rejects issue %s on purpose"; exit 3; }\nexec bash %s/.ai-toolkit/scripts/review.sh "$@"\n' "$B" "$B" "$G" > "$E/review.sh"
  chmod +x "$E/review.sh"; renv="$renv REVIEW_CMD=$E/review.sh"
fi
: > "$E/coord.log"
coord="$(orca_json terminal create --worktree "path:$G" --title coordinator --command \
  "$renv bash $G/.ai-toolkit/scripts/coordinator.sh --answer $ANS --cap 1 --drain > $E/coord.log 2>&1; echo \$? > $E/coord.rc" | jq -r '.result.terminal.handle // empty')"
[ -n "$coord" ] || fail "terminal create"
wait_for 90 grep -q ' coordinator: run run_' "$E/coord.log" || fail "the coordinator did not start"
run="$(sed -n 's/.* coordinator: run \(run_[0-9a-f]*\).*/\1/p' "$E/coord.log" | head -n 1)"
if [ -n "$A" ]; then
  step=4; say "issue #$A: worktree linked in-progress, setup ran (.claude/hooks, spoke-run-id, task.md), agent working"
  has_wt() { wt="$(orca_json worktree show --worktree "issue:$A" 2> /dev/null | jq -r '.result.worktree.path // empty')"; [ -n "$wt" ]; }
  wait_for 300 has_wt || fail "no worktree for issue $A within 5 min"
  wait_for 120 test -f "$wt/.ai-toolkit/setup-done" || fail "setup did not finish"
  { [ -d "$wt/.claude/hooks" ] && [ -s "$wt/.ai-toolkit/spoke-run-id" ] && grep -q "#$A" "$wt/.ai-toolkit/task.md"; } || fail "worktree not provisioned"
  br="$(git -C "$wt" branch --show-current)"; sid="$(cat "$wt/.ai-toolkit/spoke-run-id")"
  working() { orca_json worktree ps | jq -e --arg p "$wt" '[.. | objects | select(.path? == $p) | .agents[]? | select(.state == "working" or .state == "waiting")] | length > 0' > /dev/null; }
  wait_for 180 working || fail "the agent never showed up in worktree ps"
  orca_json worktree show --worktree "issue:$A" | jq -e '.result.worktree | .linkedIssue == '"$A"' and .workspaceStatus == "in-progress"' > /dev/null || fail "issue $A not linked in-progress"
  step=5; say "PLAN gate: one question, $ANS answer"
  inbox() { orca_json orchestration inbox --limit 200 --full | jq -c --arg r "$run" '[.result.messages[] | select(.run_id == $r)]'; }
  asked() { inbox | jq -e '[.[] | select(.type == "question")] | length > 0' > /dev/null; }
  replied() { inbox | jq -e '[.[] | select(.type == "status" and (.subject | startswith("Re:")))] | length > 0' > /dev/null; }
  wait_for 900 asked || fail "no question within 15 min"
  if [ "$ANS" = human ]; then   # only the Run's bound terminal can reply: --reply queues it from THIS terminal
    cmd=""; getcmd() { cmd="$(ghe issue view "$A" --json comments --jq '.comments[].body' | sed -n 's/.*Reply from any terminal: \(bash [^ ]* --run [^ ]* --reply [^ ]* approve\).*/\1/p' | head -n 1)"; [ -n "$cmd" ]; }
    wait_for 120 getcmd || fail "no --reply command in a comment on issue $A"
    [ ! -e "$wt/hello.py" ] || fail "hello.py exists before the gate was answered"
    if [ -n "${E2E_HUMAN_WAIT:-}" ]; then printf '\n>>> A HUMAN must answer the gate of issue #%s (%s/issues/%s). From any terminal:\n>>>   %s\n\n' "$A" "https://github.com/$R" "$A" "$cmd"
      wait_for 1800 replied || fail "no human reply within 30 min"
    else out="$($cmd 2>&1)" || fail "reply failed: $out"; fi
  fi
  wait_for 300 replied || fail "the question was never replied"
fi
step=6; say "worker_done, review, CI on the exact SHA, FF land; then the negative path; coordinator drained (exit 0)"
wait_for 2400 test -s "$E/coord.rc" || fail "the coordinator did not finish within 40 min"
[ "$(cat "$E/coord.rc")" = 0 ] || fail "coordinator exited $(cat "$E/coord.rc")"
if [ -n "$A" ]; then
  inbox | jq -e '[.[] | select(.type == "worker_done" and (.payload | fromjson | .outcome == "succeeded"))] | length > 0' > /dev/null || fail "no worker_done succeeded in the Run"
  grep -q '^SUMMARY: ' "$E/coord.log" || fail "no review verdict (SUMMARY:) in the coordinator log"
  step=7; say "landed: origin/main has the commit, CI green on that SHA, issue closed, branch/worktree/worker gone"
  git fetch -q --prune origin; sha="$(git rev-parse origin/main)"
  { [ "$(git rev-parse HEAD)" = "$sha" ] && git cat-file -e "$sha:hello.py" && git cat-file -e "$sha:tests/test_hello.py"; } || fail "main does not carry hello.py + its test"
  ghe run list --commit "$sha" --json conclusion --jq '.[].conclusion' | grep -qx success || fail "no green CI run for $sha"
  { [ "$(ghe issue view "$A" --json state --jq .state)" = CLOSED ] && ghe issue view "$A" --json comments --jq '.comments[].body' | grep -q "landed in $sha"; } || fail "issue $A not closed with the landed sha"
  [ -z "$(git ls-remote --heads origin "$br")" ] || fail "remote branch $br still exists"
  [ "$(orca_json worktree list --repo "path:$G" | jq "[.result.worktrees[] | select(.linkedIssue == $A)] | length")" = 0 ] && [ ! -d "$wt" ] || fail "worktree for issue $A still there"
fi
if [ -n "$B" ]; then
  step=9; say "negative path: issue #$B rejected by the review twice -> blocked, worker released, worktree kept"
  ghe issue view "$B" --json state,labels --jq '.state + " " + ([.labels[].name] | join(","))' | grep -q '^OPEN .*blocked' || fail "issue $B is not open + blocked"
  [ "$(ghe issue view "$B" --json comments --jq '[.comments[].body | select(startswith("round "))] | length')" = 2 ] || fail "expected exactly 2 review rounds on issue $B"
  orca_json worktree show --worktree "issue:$B" > /dev/null || fail "the worktree of blocked issue $B was not kept"
  [ "$(orca_json orchestration worker-list --run "$run" --terminal-state active | jq '.result.workers | length')" = 0 ] || fail "a worker is still active"
  [ "$(git ls-tree -r --name-only origin/main | grep -c calc.py || true)" = 0 ] || fail "calc.py landed although the review rejected it"
fi
if [ -n "${LANGFUSE_SECRET_KEY:-}" ] && [ -n "$A" ]; then   # optional (D1): informational, never fails the run
  curl -fsS -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" "$LANGFUSE_HOST/api/public/sessions/$sid" > /dev/null 2>&1 && echo "langfuse: session found" || echo "langfuse: no session (optional)"
fi
step=10; [ "$SECONDS" -lt 2700 ] || fail "took $SECONDS s (cap 2700)"
echo "PASS e2e on $R in ${SECONDS}s (phases:$PH answer=$ANS run=$run main=$sha)"
