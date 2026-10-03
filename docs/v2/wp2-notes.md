# v2 WP2 notes: coordinator, answerer, reviewer
Branch `v2-wp2`. Budgets (`wc -l`): coordinator.sh 213/230 · answer.sh 28/50 · review.sh 33/60 · land.sh 123/150 · dispatch.sh 119/120 (+4) ·
lib.sh 95/100 (+21: `spawn_claude`) · e2e 148/150 · tests: coordinator 280 + answer/review 113 = 393/400 (+10 land/dispatch tests in WP1's
files). Live e2e PASS: auto; human (`--reply` from another terminal); review round.

## Decisions and deviations (each proven live in the scratch e2e unless marked)
- **Rounds are re-derived, no state**: dispatches on the worker's worktree (`worker-list`) minus 1. Review rejects <= 2, CI red / conflict <= 1,
  liveness relaunch <= 1, counted together (stricter than per kind). CI timeout / "main keeps moving" / refused / error, or a rejection with no
  `BLOCKER:` line (unparseable verdict) block at once: nothing the worker could address. `blocked` = label + comment + notify + release, worktree kept.
- **06 s4 step 11 is wrong**: `worker-start` rejects `--task` with `--spec`, and a NEW task on the idle two-step terminal fails with
  `agent_unconfigured` (4 of 5 live runs, the error captured twice, although `terminal show` said claude). A review/CI round is `dispatch.sh --address "<spec>"`: fresh
  `claude-spoke` terminal (shim env kept), new Task, spec "address: ..."; the old terminal is closed. The worker loses its chat context
  (it re-reads `task.md` and the branch). The liveness relaunch is `dispatch.sh --retry-of D --task T` (original spec re-seeded, spike #19).
- **Human answers (D5) never pause the loop, and need a spool**: `orca orchestration reply` from another terminal is refused (Orca attests
  terminal identity: "no longer bound to Run", "attested as X and cannot act as Y"). So `bash coordinator.sh --run R --reply <msg-id>
  approve|'revise: ...'` from ANY terminal writes one file (name = message id, content = the reply line) into the spool; the loop drains it on
  every wake and sends `reply --id` as the bound consumer, deletes the file and comments on the issue. An id that is unknown or already
  answered is logged and dropped; a failed send stays queued. While a question is unanswered `check --wait` is 30 s (`COORD_WAIT_PENDING_MS`),
  else 300 s. A question for the human (`--answer human`, or an unusable `answer.sh` result) is acked at once, with the exact `--reply` line
  in the notification, the issue comment and `--status`; dispatch and lands go on meanwhile. Pending = unanswered questions in the inbox.
- **The only state v2 keeps**: that spool, `$AITK_STATE_DIR` or `~/.ai-toolkit/coordinator/<run-id>/replies/` (0700, outside every worktree,
  empty when nothing waits). Everything else is re-derived from Orca, git and labels.
- `answer.sh`/`review.sh` are strict: the last non-empty line must be exactly `ANSWER: approve|revise: x` (a real answerer wrote
  `ANSWER: approve. Make no code changes...` -> escalated; rule line added); the verdict must match the JSON contract, retried once.
  `tests_weakened`, `tdd_followed: false` or any blocker beats a verdict that says APPROVE. `--allowedTools` is variadic: prompts go on stdin.
- `land.sh`: `--cleanup-only --branch b [--tip sha]` works with the worktree gone (landed check, base-branch refusal kept); `LOCAL_GATE=1` =
  `--local-gate` (the coordinator passes no flags; `ai-toolkit.env` documents it). A still-failing cleanup (exit 6) labels the open issue
  `blocked`, else `--next` would dispatch it again. `dispatch.sh` fills slots through `--next --dry-run` then `<n>`, so a failed dispatch
  can be cleaned (`worktree rm --force`) and labelled.
- e2e (lead's request): scratch repo registered once at `/private/tmp/aitk-e2e-scratch`, reset to its root commit per run (worktrees removed,
  branches deleted, origin force-reset), never unregistered; the trap closes the coordinator terminal and every scratch terminal; a failure
  keeps `/private/tmp/aitk-e2e-scratch.last-coordinator.log`. `E2E_ANSWER=human`, `E2E_NEGATIVE=1` (REVIEW_CMD stub rejects once).

## Findings for later WPs
- A `while read` over messages must read fd 3: handlers that read stdin (`claude -p`) swallowed the rest of the batch (unit-pinned).
- The gh stub must stop listing an issue once closed, else `--next` re-dispatches it (what the exit-6 `blocked` rule guards on real GitHub).
- Not verified: real GitHub (`--next` graphql placeholders, `gh run list --commit` in the coordinator's CI path); WP6 negative path (2 rounds
  then `blocked`, worktree kept) is only unit-tested; `worker-release` on an unsettled (escalation) worker falls back to `worker-stop`, untested live.
- `--until` can overshoot by one wait (5 min). `tests_v2`: ~250 tests, ~34 s wall on a loaded machine (stub forks): the 10 s cap needs a quiet one.
- Start it: `orca terminal create --worktree path:$MAIN --title coordinator --command "bash .ai-toolkit/scripts/coordinator.sh --answer auto --cap 1 --drain"`.
