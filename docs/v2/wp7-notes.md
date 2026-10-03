# v2 WP7 notes: the coordinator as a Claude session, explicit auto switch
Branch `v2-wp7`. Budgets (`wc -l`): coordinator.sh 248/250 (budget raised) · reply.sh 19 (new, extracted) · lib.sh 98/100 · coordinate SKILL.md 118/120 · afk 21/40 · hub 56/60 ·
switch-scenario.sh 52/100 · github-lib.sh 63 (factored out) + github-scenario.sh 94 (was 150) · test_handover.py 146 + test_coordinate_skill.py 67.

## Live probes (scratch Run, then the real e2e) that decided the design
- `run-use` from another terminal ALWAYS succeeds: it takes the Run over (`consumer_generation` +1), no consent, no `consumer_fenced` on the caller. The old
  holder's next `check` returns `consumer_fenced`, and a `check --wait` already blocked returns it in the same second. `run-show` gives `coordinator_handle`.
- So the mechanism is RE-BIND: no signal, no stop file. The coordinator's start no longer dies on "held by another terminal" (that branch could not happen).
- `switch-scenario.sh` PASS 122 s on `mcrilo33/ai-toolkit-e2e` (a bash terminal is the stand-in session): gate replied from it, `auto` took the Run (log "was held by <session>",
  `--status` = `coordinator.sh (auto)`, the session fenced), #A landed under auto, #B's gate answered by `answer.sh`, `--stop` returned, loop exit 0 "taken back by <session>".

## Decisions
- Loop: `yield` = exit 0 when `run-show` names another holder (an unreadable holder = still mine; on a fence = "another terminal"). Called at the tick top, before each message, before the ack,
  before `block` (so a fenced loop never labels an issue) and on a failed dispatch. It never acks: the batch replays to the new holder, and the skill ignores replays for closed issues.
- `--stop --run R` = the caller's `run-use`, then a bounded wait (`COORD_STOP_TRIES` x `COORD_STOP_POLL`, 900 x 1 s) until no live `coordinator.sh` holds the old handle; exit 1 =
  still finishing a step (a land is not interrupted). `/coordinate attended` is exactly this command.
- `holder.<pid>` (`<handle> <mode>`) in the spool dir, removed on exit, per-pid so a restarted loop never deletes its successor's file. It is the `--stop` wait signal: wait until no live file
  names a handle != the caller (review fix: reading the old holder from `run-show` made a second `--stop` return 0 mid-land); the pid's `ps` command must still be coordinator.sh (recycled pids).
  `--status` shows `held by: coordinator.sh (mode)`, `a session (<handle>)` or `nobody`. The attended summary never reads it (Orca, git, GitHub only).
- `--reply` writer moved to `scripts/reply.sh` (coordinator.sh would have been 258 lines); `coordinator.sh --reply` is a one-line `exec`. `spool_dir` lives in lib.sh.
- Skill follows the user's note: react to the pushed `You have N orchestration message(s)` line, `check`, handle each message, always `check --ack`, heartbeat-only batches are ack-and-ignore.
- `afk` is now a 21-line alias table of `/coordinate`; `hub` points at it. `--until` takes `HH:MM` only (the old afk text said `+40m` and "finish what is live"; the loop just exits and live workers keep running).

## Findings / open points
- A takeover while `answer.sh` runs: the failed reply now yields (exit 0) instead of the human branch. A replayed `worker_done` for a CLOSED issue is acked as already landed.
- An in-flight `land.sh` is not fenced (no `--from`): it finishes and the loop exits afterwards. The worker_done then replays to the session: land is idempotent-refused, hence the closed-issue check in the skill.
- Not verified live: Orca's push into a real Claude terminal (taken from the coordinator's own experience), and the skill driven end to end by a real Claude session (the e2e uses a bash stand-in running the same commands).
- Probe Runs `run_efa03952d2d0` ("wp7 fence probe") and the e2e Runs stay in Orca (no delete verb; `reset` was not run, it needs an explicit scope).
- Run tests with the hub venv (`~/Repos/ai-toolkit/.venv/bin/python -m pytest -n auto tests_v2`); shellcheck from a `shellcheck-py` venv.
