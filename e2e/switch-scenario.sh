#!/usr/bin/env bash
# Live check of the attended <-> auto switch (WP7) on the real GitHub e2e repo (preflight shared with github-scenario.sh). THIS terminal is the
# stand-in "session": it holds the Run, gets issue A's PLAN gate and replies approve (attended); `coordinator.sh --answer auto` then takes the Run
# over (the session is fenced), lands A and answers issue B's gate with answer.sh (auto); `coordinator.sh --stop` takes the Run back and the loop
# exits 0 "taken back by" (attended). Real Claude workers, answerer and reviewer: ~10 min. Run from a dedicated Orca terminal.
set -euo pipefail
# shellcheck source=github-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/github-lib.sh"
S="$G/.ai-toolkit/scripts"; H="$ORCA_TERMINAL_HANDLE"
mk() { mkissue "$1" "$(printf '%s Run pytest before pushing.\n\nScope: %s\nGate: plan\nModel: %s low' "$2" "$3" "$SPOKE_MODEL")"; }
held() { bash "$S/coordinator.sh" --status --run "$run" | sed -n 's/^held by: //p'; }
inbox() { orca_json orchestration inbox --limit 200 --full | jq -c --arg r "$run" '[.result.messages[] | select(.run_id == $r)]'; }
replies() { inbox | jq '[.[] | select(.type == "status" and (.subject | startswith("Re:")))] | length'; }
step=2; say "the session creates the Run, files issues A and B (Gate: plan) and dispatches A"
A=$(mk "Add hello.py with a tested greet function" "Create hello.py with a function greet(name) returning 'hello ' + name, and tests/test_hello.py testing it." "hello.py tests/test_hello.py")
B=$(mk "Add calc.py with a tested double function" "Create calc.py with a function double(x) returning 2*x, and tests/test_calc.py testing it." "calc.py tests/test_calc.py"); mine="$A $B"
run="$(orca_json orchestration run-create --objective "e2e switch" --from "$H" | jq -r '.result.run.id // empty')"; [ -n "$run" ] || fail "run-create"
[ "$(held)" = "a session ($H)" ] || fail "status should say the session holds the Run, got '$(held)'"
RUN="$run" bash "$S/dispatch.sh" "$A" > /dev/null || fail "dispatch #$A failed"
step=3; say "attended: the PLAN gate of #$A reaches the session, which replies approve and acks"
got=""; ask_q() { got="$(orca_json orchestration check --run "$run" --terminal "$H" --wait --types question --timeout-ms 15000)" && jq -e '[.result.messages[] | select(.type == "question")] | length > 0' <<< "$got" > /dev/null; }
wait_until 40 0 ask_q || fail "no gate question reached the session within 10 min"
qid="$(jq -r '[.result.messages[] | select(.type == "question")][0].id' <<< "$got")"
orca_json orchestration reply --run "$run" --from "$H" --id "$qid" --body approve > /dev/null || fail "the reply from the session failed"
orca_json orchestration check --run "$run" --terminal "$H" --ack "$(jq -r '.result.deliveryId' <<< "$got")" > /dev/null || fail "ack failed"
[ "$(replies)" = 1 ] || fail "the Run does not show the session's reply"
step=4; say "/coordinate auto: coordinator.sh --answer auto takes the same Run, the session is fenced"
# shellcheck disable=SC2016
printf '#!/usr/bin/env bash\nbash %s/coordinator.sh --run %s --answer auto --cap 1 2>&1 | tee %s/coord.log\necho "${PIPESTATUS[0]}" > %s/coord.rc\n' "$S" "$run" "$E" "$E" > "$E/run.sh"
coord="$(orca_json terminal create --worktree "path:$G" --title coordinator --command "bash $E/run.sh" | jq -r '.result.terminal.handle // empty')"; [ -n "$coord" ] || fail "terminal create"
wait_for 90 grep -q "(was held by $H)" "$E/coord.log" 2> /dev/null || fail "the coordinator did not take the Run over from the session"
case "$(held)" in "coordinator.sh (auto"*) ;; *) fail "status after the switch says '$(held)'" ;; esac
out="$(orca_json orchestration check --run "$run" --terminal "$H" --timeout-ms 1000 2>&1 || true)"; grep -q consumer_fenced <<< "$out" || fail "the session was not fenced"
step=5; say "while away: #$A lands under auto, then #$B is dispatched and answer.sh answers its gate"
closed() { [ "$(ghe issue view "$A" --json state --jq .state)" = CLOSED ]; }
wait_for 1500 closed || fail "#$A was not landed by the coordinator"
git fetch -q origin; sha="$(git rev-parse origin/main)"; git cat-file -e "$sha:hello.py" || fail "origin/main lacks hello.py"
ghe issue view "$A" --json comments --jq '.comments[].body' | grep -q "landed in $sha" || fail "#$A has no 'landed in $sha' comment"
wait_for 900 grep -q "#$B gate answered: " "$E/coord.log" 2> /dev/null || fail "answer.sh did not answer the gate of #$B"
step=6; say "/coordinate attended: coordinator.sh --stop takes the Run back, the loop exits 0 'taken back by'"
out="$(bash "$S/coordinator.sh" --stop --run "$run" 2>&1)" || fail "--stop: $out"
wait_for 30 test -s "$E/coord.rc" || fail "the coordinator never exited"
[ "$(cat "$E/coord.rc")" = 0 ] || fail "the coordinator exited $(cat "$E/coord.rc"), not 0"
grep -q "Run $run taken back by $H" "$E/coord.log" || fail "no 'taken back by $H' line in the coordinator log"
[ "$(held)" = "a session ($H)" ] || fail "status after --stop says '$(held)'"
orca_json orchestration check --run "$run" --terminal "$H" --timeout-ms 1000 > /dev/null || fail "the session cannot consume the Run it took back"
step=7; say "the attended summary is derivable from Orca, git and GitHub"
[ "$(replies)" = 2 ] || fail "expected 2 gate replies in the Run (session + answer.sh), got $(replies)"
orca_json worktree show --worktree "issue:$B" > /dev/null || fail "#$B's worktree is not there for the session to continue"
[ "$(ghe issue view "$B" --json state --jq .state)" = OPEN ] || fail "#$B should still be open"
[ "$SECONDS" -lt 2700 ] || fail "took $SECONDS s (cap 2700)"
echo "PASS switch e2e on $R in ${SECONDS}s (run=$run session=$H landed=#$A main=$sha)"
