# v2 WP1 notes: dispatch + land

Branch `v2-wp1` (merged with `origin/v2` @ WP5). Budgets: dispatch.sh 117/120, land.sh 90/150, lib.sh 74/100,
tests (dispatch 215 + land 237)/600, e2e 122/150. 47 new tests (45 in the two files, +1 lib, +1 setup).
`pytest -n auto tests_v2`: 73 pass; ~37 CPU-s, 15 s wall at load ~7 with an e2e running (see Findings).

## Decisions (coordinator-approved plan + 3 changes)
- `dispatch.sh [--run R] <n> | --next [--dry-run]`. The Run is explicit (`--run`/`$RUN`, exit 2 otherwise): never inferred.
  `--base-branch origin/$BASE_BRANCH` is always passed. `Model:` footer = `<model> [effort]`, validated against
  `[A-Za-z0-9._-]` because it is typed into a terminal. `--dry-run` prints the pick only (cheap tests, useful to the coordinator).
- Launch is `launch_${DISPATCH_LAUNCH:-twostep}`; `override` is one `worker-start --agent claude` (tested, not live-verified).
  Flip the default in `dispatch.sh` once Orca's `agentCmdOverrides.claude` points at `bin/claude-spoke`.
- `--next`: ready = not `hold`/`blocked` (the label the coordinator sets on failures), blockedBy all CLOSED, not already linked in
  `orca worktree list`, Scope disjoint from every in-flight issue's Scope (missing or `*` = exclusive, both sides), `priority` first,
  then number. Nothing ready: exit 3. Exits: dispatch 0/1/2/3; land 0 landed, 1 error, 2 refused, 3 review no, 4 gate red/timeout,
  5 merge conflict, 6 landed but cleanup incomplete (main is pushed, do not re-land).
- `land.sh`: refuses (2) outside the main checkout / off `BASE_BRANCH` / dirty tracked tree / spoke dirty or unpushed. Gate runs on the
  exact tip; if main moved (before or during the gate) it merges main on the spoke, pushes, re-gates (3 rounds). A rejected main push
  resets local main to where it was. `--review` calls `${REVIEW_CMD:-review.sh} <n>` (WP2 hook; default is skip).
  `worker-release` takes `--dispatch D`, else matches `worker-list` rows by `resource.worktreeId` ending `::<worktree path>`.

## Deviations
- lib.sh `gh()` wraps `command ${AI_TOOLKIT_GH:-gh}` and `setup.sh` now calls `load_env`: Orca's setup terminal does not inherit the
  caller's PATH, so the e2e stubs GitHub through the local env file (`AI_TOOLKIT_GH=`), also for `CHECK_CMD`. `usage_exit` (exit 2) and
  `wait_until <tries> <sleep> cmd...` added to lib.sh.
- e2e numbering follows 06 s7: 4 (provisioned), 5 (gate answered by the script via `check --wait` / `reply` / `check --ack`), 6 (worker_done,
  branch pushed), 7 (land). Own scratch Run only; cleanup stops/releases that Run's workers. Two consecutive live runs PASS (~45 s each).

## Findings for later WPs
- Two-step launch: the terminal comes from `terminal create`, so Orca does not own it; `worker-release` reports `retained` (no-op) and
  `worktree rm` is what closes it. `worker-start --terminal` needs an existing worktree (`--worktree path:`).
- A `dispatch.sh` failure after `worktree create` leaves the worktree (and maybe a terminal): the coordinator must `worktree rm` it and
  label `blocked`. `check` hands back whole batches with `deliveryId`: ack after replying; the reply shows up as a `status` message.
  Message `payload` is a JSON string (`fromjson`).
- [verify] against real GitHub (not exercised, ask first): `gh api graphql -F owner='{owner}' -F name='{repo}'` placeholders in
  `--next`, and `gh run list --commit <sha> --json status,conclusion,url` in the CI path of `land.sh` (stub-tested only).
- Test time: ~0.5 CPU-s per test (Apple git shim ~13 ms/call, mostly forks); the `<10 s` cap needs a quiet machine. A builtins-only conftest
  stub gained nothing (measured, reverted). Mutation check: 16 mutants of the two scripts, all killed after one test was tightened.
- `test_otel.py` (WP5) needs PyYAML; the default `python3` lacks it (design lists `pyyaml` as a dev dependency).
