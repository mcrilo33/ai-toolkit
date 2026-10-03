# v2 WP2 notes: coordinator, answerer, reviewer

Branch `v2-wp2`. Budgets (`wc -l`): coordinator.sh 198/200 · answer.sh 28/50 · review.sh 33/60 · land.sh 123/150 · dispatch.sh 119/120
(+4) · lib.sh 95/100 (+21: `spawn_claude`) · e2e 144/150 · tests: coordinator 240 + answer/review 113 = **353/300** (+53; the human prompt,
`--address` and gone-worktree cases are the extra; WP1's files gained 8 land + 2 dispatch tests). Live e2e PASS: auto; human+negative.

## Decisions and deviations (each proven live in the scratch e2e unless marked)
- **Rounds are re-derived, no state**: dispatches on the worker's worktree (`worker-list`) minus 1. Review rejects <= 2, CI red / conflict <= 1,
  liveness relaunch <= 1, counted together (stricter than per kind). CI timeout / "main keeps moving" / refused / error, or a rejection with no
  `BLOCKER:` line (unparseable verdict) block at once: nothing the worker could address. `blocked` = label + comment + notify + release, worktree kept.
- **06 s4 step 11 is wrong**: `worker-start` rejects `--task` with `--spec`, and a NEW task on the idle two-step terminal fails with
  `agent_unconfigured` (4 of 5 live runs, the error captured twice, although `terminal show` said claude). A review/CI round is `dispatch.sh --address "<spec>"`: fresh
  `claude-spoke` terminal (shim env kept), new Task, spec "address: ..."; the old terminal is closed. The worker loses its chat context
  (it re-reads `task.md` and the branch). The liveness relaunch is `dispatch.sh --retry-of D --task T` (original spec re-seeded, spike #19).
- **Human answers (D5) cannot go through `orca orchestration reply` from another terminal**: the CLI attests terminal identity ("no longer
  bound to Run", "attested as X and cannot act as Y"). So `--answer human` prompts on the coordinator's own terminal; type `approve` or
  `revise: ...`, or `orca terminal send --terminal <coordinator> --text approve --enter` from anywhere (e2e does this). The loop pauses while
  it waits. Auto mode with an unusable answer: same prompt, 600 s, then acked and left parked; takeover = `run-use` + `reply` + restart.
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
- `--until` can overshoot by `COORD_WAIT_MS` (5 min). `tests_v2` is 244 tests, ~34 s wall on a loaded machine (stub forks): the 10 s cap needs a quiet one.
- Start it: `orca terminal create --worktree path:$MAIN --title coordinator --command "bash .ai-toolkit/scripts/coordinator.sh --answer auto --cap 1 --drain"`.
