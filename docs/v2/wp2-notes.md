# v2 WP2 notes: coordinator, answerer, reviewer
Branch `v2-wp2`. Budgets (`wc -l`): coordinator.sh 229/240 · answer.sh 28/50 · review.sh 33/60 · land.sh 123/150 · dispatch.sh 119/120 (+4) ·
lib.sh 95/100 (+21: `spawn_claude`) · e2e 148/150 · tests 425/400 (coordinator 312 + answer/review 113; +12 land/dispatch tests in WP1's files).
Live e2e PASS: auto; human (`--reply` from another terminal); review round.

## Decisions and deviations (each proven live in the scratch e2e unless marked)
- **Rounds are re-derived, no state**: dispatches on the worker's worktree (`worker-list`, read page by page) minus 1. Review rejects <= 2, CI red /
  conflict <= 1, liveness relaunch <= 1, counted together. CI timeout / "main keeps moving" / refused / error, or a rejection with no `BLOCKER:`
  line (unparseable verdict) block at once. `blocked` = label + comment + notify + release, worktree kept.
- **06 s4 step 11 is wrong**: `worker-start` rejects `--task` with `--spec`, and a NEW task on the idle two-step terminal fails with
  `agent_unconfigured` (4 of 5 live runs, although `terminal show` said claude). A review/CI round is `dispatch.sh --address "<spec>"`: fresh
  `claude-spoke` terminal (shim env kept), new Task, spec "address: ..."; the old terminal is closed; the worker loses its chat context. The liveness
  relaunch is `dispatch.sh --retry-of D --task T` (original spec re-seeded, spike #19).
- **Human answers (D5) never pause the loop**: `orca orchestration reply` from another terminal is refused (Orca attests terminal identity). So
  `bash coordinator.sh --run R --reply <msg-id> approve|'revise: ...'` from ANY terminal queues one file (name = message id, content = the reply
  line); the loop drains it every wake, sends `reply --id` as the bound consumer, deletes it, comments on the issue. Dropped: a malformed body, a
  question seen answered, an unknown id when the 200-row inbox page (all Runs) was not full; otherwise kept and retried. `check --wait` is 30 s while a
  question is unanswered (`COORD_WAIT_PENDING_MS`), else 300 s. A question for the human (`--answer human`, or an unusable `answer.sh`) is acked at
  once; the notification, issue comment and `--status` show the exact `--reply` line; dispatch and lands go on.
- **The only state v2 keeps**: that spool, `$AITK_STATE_DIR` (default `~/.ai-toolkit/coordinator`) + `/<run-id>/replies/` (0700, outside every worktree).
- **Live check, standalone `check --ack D`** (scratch Run): also a consuming check, but nothing is lost: the next batch returns with a NEW delivery id,
  stays unacked, and the following `check --wait` replays it. At-least-once; the plain ack was kept.
- `answer.sh`/`review.sh` are strict: the last non-empty line must be exactly `ANSWER: approve|revise: x`; the verdict must match the JSON contract
  (retried once); `tests_weakened`, `tdd_followed: false` or a blocker beat an APPROVE. `--allowedTools` is variadic: prompts go on stdin.
- `land.sh`: `--cleanup-only --branch b [--tip sha]` works with the worktree gone; `LOCAL_GATE=1` = `--local-gate` (the coordinator passes no flags).
  The coordinator calls `land.sh --review <n>` WITHOUT `--dispatch`, so land releases every dispatch of the worktree (its own lookup is one
  100-row page). A still-failing cleanup (exit 6) labels the open issue `blocked`, else `--next` would dispatch it again.
- e2e: scratch repo registered once at `/private/tmp/aitk-e2e-scratch`, reset per run, never unregistered; the trap closes every scratch terminal;
  a failure keeps `/private/tmp/aitk-e2e-scratch.last-coordinator.log`. `E2E_ANSWER=human`, `E2E_NEGATIVE=1` (REVIEW_CMD stub rejects once).

## Findings for later WPs
- A `while read` over messages must read fd 3: handlers that read stdin (`claude -p`) swallowed the batch (unit-pinned). The gh stub must stop
  listing a closed issue, else `--next` re-dispatches it.
- Not verified: real GitHub (`--next` graphql placeholders, `gh run list --commit`); the 2-rounds-then-`blocked` path is unit-tested only;
  `worker-release` on an unsettled worker falls back to `worker-stop`, untested live.
- `--until` can overshoot by one wait (5 min). `tests_v2` ~255 tests, ~34 s wall on a loaded machine: the 10 s cap needs a quiet one.
- Start: `orca terminal create --worktree path:$MAIN --title coordinator --command "bash .ai-toolkit/scripts/coordinator.sh --answer auto --cap 1 --drain"`.
