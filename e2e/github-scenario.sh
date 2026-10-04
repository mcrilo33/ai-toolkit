#!/usr/bin/env bash
# Real end-to-end (06 section 7 steps 1-10) on the throwaway PUBLIC repo mcrilo33/ai-toolkit-e2e: real gh issues, real GitHub CI on the exact
# SHA, real Orca, real claude. The only GitHub repo this script writes to: it refuses any other origin. A stable clone ($G) is registered in
# Orca ONCE (never unregistered), reset per run to the `e2e-base-3` tag (a force-push of the e2e main) and synced from this checkout (--local-only).
# Phases (E2E_PHASES, default "next happy negative"): next = `dispatch.sh --next` order on 5 real issues; happy = issue A (Gate: plan) through
# gate, review, CI, FF land; negative = issue B, a REVIEW_CMD wrapper rejects it every time with a new, actionable blocker -> 2 rounds -> `blocked`. E2E_ANSWER=human: the gate
# is answered with `coordinator.sh --reply` (scripted, or E2E_HUMAN_WAIT=1: it prints the line and waits for a REAL human). Run from an Orca terminal.
# shellcheck source=github-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/github-lib.sh"
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
renv=""
if [ -n "$B" ]; then   # the real reviewer for A, a stub that always rejects B (a real one would accept the fix in round 1)
  # shellcheck disable=SC2016
  printf '#!/bin/sh\n[ "$1" = %s ] && { n=$(($(cat %s/n 2>/dev/null || echo 0) + 1)); echo $n > %s/n; echo "BLOCKER: calc.py:1 - add the comment line # review round $n at the top of calc.py"; exit 3; }\nexec bash %s/.ai-toolkit/scripts/review.sh "$@"\n' "$B" "$E" "$E" "$G" > "$E/review.sh"
  chmod +x "$E/review.sh"; renv="$renv REVIEW_CMD=$E/review.sh"
fi
# The coordinator's own terminal shows its output (a human reads the gate question there) and tees it to the log.
# shellcheck disable=SC2016
printf '#!/usr/bin/env bash\n%s bash %s/.ai-toolkit/scripts/coordinator.sh --answer %s --cap 1 --drain 2>&1 | tee %s/coord.log\necho "${PIPESTATUS[0]}" > %s/coord.rc\n' "$renv" "$G" "$ANS" "$E" "$E" > "$E/run.sh"
coord="$(orca_json terminal create --worktree "path:$G" --title coordinator --command "bash $E/run.sh" | jq -r '.result.terminal.handle // empty')"
[ -n "$coord" ] || fail "terminal create"
wait_for 90 grep -q ' coordinator: run run_' "$E/coord.log" 2> /dev/null || fail "the coordinator did not start"
run="$(sed -n 's/.* coordinator: run \(run_[0-9a-f]*\).*/\1/p' "$E/coord.log" | head -n 1)"
if [ -n "$A" ]; then
  step=4; say "issue #$A: worktree linked in-progress, setup ran (.claude/hooks, spoke-run-id, task.md), agent working"
  has_wt() { wt="$(orca_json worktree show --worktree "issue:$A" 2> /dev/null | jq -r '.result.worktree.path // empty')"; [ -n "$wt" ]; }
  wait_for 300 has_wt || fail "no worktree for issue $A within 5 min"
  wait_for 120 test -f "$wt/.ai-toolkit/setup-done" || fail "setup did not finish (Orca runs the TRACKED orca.yaml of the new worktree)"
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
    gc() { orca_json worktree show --worktree "issue:$A" | jq -r '.result.worktree.comment // ""'; }; answered_c() { [[ "$(gc)" == "gate answered"* ]]; }
    wait_for 60 grep -q "^  | " "$E/coord.log" && [[ "$(gc)" == "GATE waiting: "* ]] || fail "the question text is not in the coordinator log or the worktree comment is not set"
    [ ! -e "$wt/hello.py" ] || fail "hello.py exists before the gate was answered"
    if [ -n "${E2E_HUMAN_WAIT:-}" ]; then printf '\n>>> QUESTION of issue #%s (%s/issues/%s):\n%s\n>>> A HUMAN must answer it. From any terminal:\n>>>   %s\n\n' "$A" "https://github.com/$R" "$A" "$(grep '^  | ' "$E/coord.log" | head -n 25)" "$cmd"
      wait_for 1800 replied || fail "no human reply within 30 min"
    else out="$($cmd 2>&1)" || fail "reply failed: $out"; fi
  fi
  wait_for 300 replied || fail "the question was never replied"
  [ "$ANS" != human ] || wait_for 60 answered_c || fail "the worktree comment was not updated after the reply"
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
