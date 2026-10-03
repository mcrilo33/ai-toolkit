# Orca migration — live status (written by the migration session)

> The tutor session reads this file before answering. The migration session updates it at every milestone.
> Last update: 2026-10-02 08:15.

## Where we are

- **Plan:** `04-plan.md` ("Orca-first"). Spike results: `03-spike.md`. Capabilities and decisions: `02-orca-capabilities.md`.
- **Method (since 2026-10-02):** interactive. The `/afk` drain is OFF. The migration session lands work itself and runs the
  remaining steps as **Orca orchestration workers** (`orca orchestration worker-start`), acting as coordinator (`ask`/`reply`).
- **Landed on main:** #358 S0 (models → Opus 5.5 / Sonnet 5.5 / Fable 5.1), #359 S1 (`provision-worktree.sh`),
  #360 S2a (`.ai-toolkit/identity` + `identity.sh`), #351 (hub-afk.sh split), #357, #367 (flaky travel test).
- **In flight (old tmux spokes, finishing):** #362 S3 (`orca.yaml` + archive), #369 (native hooks miss `identity.sh`), #370 (afk status).
- **Next, as Orca workers:** #361 S2b (hub-side identity), then #363 S4 (dispatch via worker-start), #364 S5 (teardown),
  #365 S6a / #366 S6b (`/afk` on Orca).

## Decisions 2026-10-02

- **Goal added: minimise ai-toolkit's code.** Orca-only (no tmux/orca dual path, no `execution.host` switch). Each step replaces
  and deletes. Downstream synced repos also move to Orca. Travel mode (`afk-travel`/`travel-local`) is deleted now.
- **Coordination:** a second Claude session on the main checkout (hub) lands work and drives the remaining tmux spokes (#362, #370).
  The migration session does not touch tmux spokes; it rewrites #361–#366 as replace-and-delete issues and runs them as Orca workers.
- Update: #369 landed (native hooks now ship `identity.sh`).

## Machine state the tutor should know

- Orca 1.4.218; `orca` on PATH works. Repo `ai-toolkit` registered (id `55c46406-…`).
- Settings: branch prefix `none`, auto-rename branch OFF, Claude launched with `--dangerously-skip-permissions` by default.
- `~/.zshrc` skips tmux autostart in Orca terminals (`TERM_PROGRAM=Orca`), so Orca sees agents.
- Claude trusts `~/orca/workspaces` (no trust dialog in new Orca worktrees).
- `sudo pmset -a disablesleep 1` is still ON (reset with `0` when done).
- Until `execution.host: orca` is set (after S4–S6), ai-toolkit's own scripts still use tmux. Orca is used directly by the human and
  by the migration session.

## Teaching

All Orca teaching material is in `LESSONS.md` (the tutor teaches; the migration session only writes the material).

## Live Orca objects

- Orchestration Run: `run_54f40aea9fdf` (coordinator = the migration session's terminal).
- Worker #361 S2b: worktree `~/orca/workspaces/ai-toolkit/361-identity-hub-side`, branch `feature/361-identity-hub-side`,
  task `task_6e473708660e`, dispatch `ctx_db94df10975e`. PLAN gate answered `approve` via `orchestration reply` (3 subtasks).
- Worker #371 (delete travel mode + `/afk --remote`, ≈ −1,780 lines): worktree `~/orca/workspaces/ai-toolkit/371-delete-travel-mode`,
  branch `chore/371-delete-travel-mode`, task `task_6241bf321243`, dispatch `ctx_25fc6fb88bdb`, Gate: none.
- Worker #363 S4 (dispatch via worker-start, delete tmux launch): worktree `~/orca/workspaces/ai-toolkit/363-orca-dispatch`,
  branch `feature/363-orca-dispatch`, task `task_1754d3daae2c`, dispatch `ctx_c45407b1c094`, Gate: none.
- #362 S3 landed (`orca.yaml` on main @ 2598b69b). #364 S5 rewritten (Orca-only, ≈ −465 code / −480 test lines).
- #365 rewritten as merged S6+S7 (drive /afk through Orca, delete the tmux transport, ≈ −1,000 code lines); #366 closed as superseded.
- #372 new: delete tier-2 watchdog, caffeinate, hang forensics, spoke-relaunch.sh (≈ −2,520 code / −5,040 test lines); blocks #365.
- #363 decision (spike b): the settings.local.json `env` block does NOT reach Claude's OTel init, so spokes are launched with
  `orca terminal create --command "<OTel prefix> WT_SPOKE=… claude --model … --effort … --dangerously-skip-permissions"` then
  `worker-start --terminal <h>`. `worker-start` needs a live Orca coordinator terminal + Run: dispatch only from an Orca terminal.
- **Finding (2026-10-02 09:47): parallel workers oversubscribe the machine.** Three workers each running the first-push full
  suite (`-n auto`) at once give load ~9–10 and random, changing test failures (#371 failed twice: 1 node, then 16 different nodes;
  all pass in isolation and with the spoke env). Mitigation now: the coordinator serializes pushes (`GO push <n>` message when no
  pytest runs and load < 5). Follow-up for the Orca coordinator (S6): serialize full-suite gates across workers.
- **Finding (2026-10-02 ~12:00): the push gate fails only inside Orca workers.** Same nodes every time
  (`test_drain_simulation[301-dead-agent-not-injected]`, `test_hub_afk::test_arm_inhibitor_*`, sometimes `[299-…]`), on a quiet
  machine. The same gate passes when the coordinator runs it (`git push --dry-run` from the 371 worktree: 5769 + 42 green), main's
  full suite passes in an Orca terminal, and the settings.local.json env block does not trigger it. Env diff worker vs coordinator:
  only Orca's `GIT_CONFIG_*` credential vars (already scrubbed by `tests/conftest.py`), `GIT_EDITOR=true`, `GIT_TERMINAL_PROMPT=0`,
  `AI_TOOLKIT_OTEL_*`, `ORCA_AGENT_LAUNCH*`. Ulimits identical. Likely cause: the process context of an agent's Bash tool vs the
  detached processes these tests spawn (`caffeinate -w <pid>`, drain revive). **Workaround:** the coordinator runs the pushes
  (`bash scripts/spoke-push.sh` in the worker's worktree); workers then emit `ready`. Follow-up issue to file: reproduce
  inside a worker and fix the tests' process handling.
- **Correction (12:20):** the "worker context" theory above is wrong. The coordinator's own push of #361 failed the same way, and
  **main itself fails 3/3** with `pytest -n auto tests/unit/test_hub_afk.py tests/integration/test_drain_simulation.py`
  (arm_inhibitor x3 + drain-sim 301). This is a pre-existing xdist flake that the mapped gate selection hits almost every time.
  Filed as **#374 (priority)** and dispatched as Orca worker (`374-xdist-flake`, dispatch `ctx_2ca9617237af`). #361/#363 wait for it.
- **#374 landed** (main @ aabe872c, 2026-10-02 ~13:40, by the migration session with the user's go-ahead). Root cause: macOS
  cold first-exec of freshly written stub scripts (p50 1.3 s, max ~3 s) vs 3–4 s bounded waits; tests-only fix warms each stub.
- The user authorised the migration session to land the Orca branches itself (`worktree-land.sh <n>` from the main checkout,
  never concurrently with the hub session). #371 land in progress.
- The migration session was restarted on 2026-10-02 (now an Orca terminal, handle `term_c25b…`); Run `run_54f40aea9fdf` re-bound
  with `orchestration run-use --id … --from <handle>`.
- **Protection decision (user, 2026-10-02):** no extra permission/danger-wall mechanism for Orca spokes (they run with
  `--dangerously-skip-permissions`). Existing hooks stay. Re-enable the danger wall if anything abnormal is observed.

## 2026-10-02 ~14:00 — parallel push
- Workers #361 and #363 told to merge origin/main (aabe872c) now, then ask "ready to push"; the coordinator pushes and lands.
- New Orca workers: **#372** (delete tier-2 watchdog etc., base origin/main, dispatch `ctx_3767772569a4`) and **#364 S5**
  (teardown/lookup, STACKED on local `feature/363-orca-dispatch`, dispatch `ctx_105c39a24597`, plan decisions pre-approved:
  delete --keep-branch, scan-based wt_resolve, land via `worktree-done.sh --no-hooks`, no list cache).
- Only push/land gates stay serialized (coordinator). #365 S6+S7 starts once #363/#364/#372 are in.
- 14:12 — **Hub venv had no pytest-xdist**, so every land ran the full suite serially (~30 min; #371's land). Installed
  `pytest-xdist==3.8.0` into `~/Repos/ai-toolkit/.venv` with `uv pip install` (the uv venv has no pip, which is why
  `ensure-test-venv.sh` could not do it). Follow-up: make `ensure-test-venv.sh` use `uv pip` when pip is absent.
- #372 finished (−2,526 code / −5,246 test lines, review APPROVE); queued after #363/#361, must merge main after #371 lands.
- 14:50 — **Test stability is now a tracked goal (user):** epic **#377**. In flight as Orca workers: **#375** (reap_pass flake,
  dispatch `ctx_b0dbf5ba7a36`) and **#376** (one shared warmed-stub helper + governance, dispatch `ctx_0acc4520a6df`).
  The rest of #377 (wall-clock sweep, no real tmux in tests, uv-aware ensure-test-venv, gate serialization, nightly flake
  detection automation, quarantine policy) runs after the migration core.
- #363 first push failed on the #375 flake (1 node); retry queued after #361.

## 2026-10-02 ~17:00 — lands and lessons
- Landed today: #374, #371, #375, **#372** (−2,525 code / −5,233 test lines). #361 merge conflict resolved by the coordinator
  (independent review APPROVE), queued. #376 land refused (conflicts with #372 deletions + #375), worker merging main.
- #379: macOS job now runs; it exposes **10 real macOS-only failures on main** (bootstrap_test_suite ×7, identity_lib `[Issue]`
  likely case-insensitive FS, recover_dead_panes ×2). Worker files one bug; the job becomes required once fixed.
- **Lessons for the S6 coordinator:**
  1. Have the worker merge origin/main **right before** its push (branches waiting in the queue go stale; 2 land conflicts today).
  2. Never release a worker before its land is confirmed (the 361 worker was released after a failed land).
  3. The local `--testmon` tier runs ~5800 tests serially in a fresh worktree (31–34 min pushes for #375/#376); #378 fixes it.
  4. Gate detection must ignore test processes that invoke `worktree-land.sh` in fixtures.
- 17:35 — **Landed: #379** (macOS CI job runs; 10 macOS-only failures → #384), **#361 S2b** (hub-side identity, after two
  coordinator-resolved merge conflicts, each independently reviewed). main @ 0905b105.
- `ship.sh` now refuses to push a branch that does not contain origin/main and asks the worker to merge first (stale guard).
- Waiting on workers: #363 (merging main again: same sync-list line as #361), #376 (merging main after land conflicts),
  #378 (final merge + re-mint at its turn), #364 (after #363).
- 18:00 — **Landed #363 S4** (dispatch via Orca: orca-lib.sh, worktree-new → worktree create + rename + provision +
  `terminal create` with OTel prefix + `worker-start --terminal`; net −28 code / −612 test lines by design of decision B).
  main @ 5c1eea99. First land attempt failed: `worktree-land.sh 363` was ambiguous because the worker left a spike worktree
  (`wip/363-st2`, registered in git) — landed by exact path; spike worktree removed. Lesson: workers must clean their spike
  worktrees; S5's identity-based lookup should also disambiguate.
- #364 S5 told to merge main (its base #363 is now in main).
- 18:45 — **Landed #364 S5** (Orca teardown/lookup; −448 code lines). main @ accd3f18.
- **#365 S6+S7 dispatched by the new Orca `worktree-new.sh` (S4) end to end**: Orca worktree + branch rename + provision +
  identity record with `orca_worktree_id`/`orca_dispatch_id` + `terminal create` with OTel prefix + `worker-start`
  (dispatch `ctx_d74be994b12a`, run `run_54f40aea9fdf`). First real use of the migrated dispatch.
- #378 (option B) told to do its final merge (expect conflicts with #364 in worktree-land.sh).
- 21:23 — **Landed #378 (option B)**: local pre-push = fast tier only; `ready` and land wait for CI green (run-level
  conclusion) on the exact SHA; land runs no local suite. CI Linux suite runs **serially** (20-min timeout) until epic #377
  makes it xdist-safe (3 parallel runs each hit a different timing flake). Hub fast-forwarded, git hooks re-installed
  (= main), hub re-synced. Follow-ups #381/#382/#383 + items on #377.
- #365 S6+S7: two real gate regressions fixed (spoke-ready --gate span), round 2 ready; merging main (378 also edits
  spoke-ready.sh), then pushed with the NEW fast tier + CI.
- 21:58 — **Landed #365 (S6+S7)**: /afk now drives Orca (slot_state from inbox/agent state/liveness, reply-by-id or
  terminal send with proof, in-place `--retry-of` recovery, `spoke-ready --gate` blocks in `ask`, worker_done on ready);
  tmux transport deleted. Net non-test LOC −874, tests −2859. First ship under option B: push 2m16s (fast tier, 3458
  tests), ready waited for CI, land ran no local suite (FF of CI-green 8c59af6f). Live proof: the worker's own
  spoke-ready sent `worker_done "ready/365"` (a 2nd manual worker_done was rejected: dispatch capability revoked once done).
  Hub hooks re-installed + re-synced; worker released. Remaining tmux: hub-status/hub-agent (S8).
- 2026-10-03 00:55 — **#376 lost ~3 h waiting on an unanswered question.** Lessons:
  (1) once a worker sends worker_done, its dispatch capability is revoked, so a later `ask` can never reach the inbox; the
  worker falls back to asking in its terminal, which the coordinator doesn't watch. Fix: re-dispatch with `--retry-of`
  for follow-up rounds, or watch `worktree ps .agents[].lastAssistantMessage` (not the terminal tail, which the status
  line scrolls away).
  (2) Claude Code's ghost prompt suggestion ("A, fix the 14 …") in the input box looks like typed input in `terminal read`;
  it is NOT an answer.
  (3) main carries 14 pyright errors in tests (test_hub_afk.py, test_gate_broker_detect.py, dup `_tlog`) that the land
  path never checked: under option B neither CI nor the land runs the pyright gauntlet. To add to epic #377.
- 09:33 — #376 pushed (fast tier 4506 passed in 5m51s), but the land was **refused: stale `ready/376`** (old round's tag at
  55349ded, tip 3f41550b). ship.sh accepted any existing tag; `worktree-land.sh` caught it (good fail-safe). ship.sh now
  waits for `ready/<n>^{commit} == branch tip`. Lesson: a marker check must compare the SHA, not just existence.
- 2026-10-03 ~10:00 — **Wave 2 dispatched** (user: "dispatch, don't change the order"; goals = use Orca's features fully +
  a fast, reliable, useful test suite). Via the migrated `worktree-new.sh` (Orca worktree + worker-start):
  #385 parallel-safe suite + CI `-n auto` (<5 min, 10/10) `ctx_fbd9b9fbedca` [Gate: plan];
  #386 test usefulness audit `ctx_bedc45b0fe99` [Gate: plan];
  #387 macOS job (closes #384) + whole-repo pyright in CI `ctx_467b2ec5e341`;
  #388 Orca S8 surfaces (native notifications, per-step `worktree set --comment`, keep-awake) `ctx_a2f7a5f32472`.
  New flow under option B: **workers push and run `spoke-ready.sh` themselves** (fast tier + CI wait; no coordinator push
  serialization); questions only via `orchestration ask`; PLAN gates via `spoke-ready.sh --gate` (S6). Coordinator only lands.
  Next: S9 automations (nightly flake detector, weekly Langfuse evaluation), then S10 `orca serve`/mobile spike.
- 09:51 — **Landed #376** (warmed-stub helper, ~250 stub sites; CI green run 37106683889; land closed the issue). main @
  3f41550b. Hub hooks re-installed + re-synced, worker released. Issue closing stays in `worktree-land.sh` (Orca's
  `--issue` link is metadata only and never writes to GitHub).
- ~10:20 — **Backlog triage with the user**, issue by issue: #381 dispatch (`ctx_85d27deaa03b`, Gate: plan); #382 dispatch
  (`ctx_0a6e2775a424`, Gate: plan; #386 told to leave those tests alone); #383 **deferred until #382 + #387 land**
  (conflicts on spoke-ready.sh/test-select.sh); #370 and #373 **taken over from the hub session**: both defects still on
  main, so their tmux claude sessions were stopped, the old worktrees removed (branches kept), and they were re-dispatched
  as Orca workers that port the existing fix (`ctx_5d9d12755fa7`, `ctx_f4bc209e8dd4`). 8 Orca workers running.
- **Fable for judgment (user decision, 2026-10-03):** the coordinator stays on Opus. Fable (`model: fable` on the Agent tool)
  is used for (1) an independent pre-land code review of every wave-2/3 branch (full diff vs origin/main), on a model
  different from the Sonnet worker, and (2) a critique of the judgment-heavy plan gates (#385 flake classes, #386 test
  audit) before approval. Compare against earlier reviews via the Langfuse `agent-verdict` scores.
- ~10:45 — Plan gates approved: #373 (effective-attendance predicate; 3 attended pins get AFK_STATE isolation), #381
  (red-CI exit 6 + lane, lock touch, explicit land-progress records), #382 (drop the testmon baseline copy; check that
  the fast tier still runs mapped tests with no DB).
  **Lesson: `spoke-ready.sh --gate` failed with `dispatch_capability_invalid` for every worker.** Orca hands the
  `dcap_…` token only in the worker preamble; spoke-ready needs `--dispatch-capability <token>` once (then remembers it).
  My rules omitted it. Workers fell back to direct `orchestration ask`. Corrected for all 8. Follow-up: have
  `worktree-new.sh` write `.ai-toolkit/dispatch-capability` from the worker-start result, so no human or agent has to
  pass it.
  **Not a leak:** the pre-push tripwire "breach on refs/tags/gate/382" seen by #373 = #382 creating its real gate tag
  during #373's suite (concurrent workers vs snapshot-based tripwire). Expect more of these with 8 workers.

## 2026-10-03 ~11:00 — STRATEGY CHANGE: v2 rewrite (user decision)
Evidence: after two days, code went ~47k → ~45k lines (tests 91k → 86k). The migration was translating ai-toolkit onto Orca,
not simplifying it, and most friction came from ai-toolkit's own machinery. User: "the project should be very simple with
a short codebase"; "stop using ai-toolkit for the migration".
- **Plan:** Fable architect designs v2 (`06-target-architecture.md`: per-feature verdict ORCA / POLICY / GLUE / DROP, file
  tree + line budgets, lifecycle on Orca primitives, Langfuse, sync, tests, work packages) → user validates → Orca
  workers build on a `v2` branch **without ai-toolkit machinery and without GitHub CI** → E2E spoke scenario defined
  upfront, run from the skeleton onwards → single cutover to main, delete the old code, re-sync downstream.
- **All 8 wave workers stopped** (#370 #373 #381 #382 #385 #386 #387 #388): dispatches stopped, Claude terminals
  closed, one orphaned pytest killed. Worktrees, branches and gate tags kept for reference. Their issues stay open until
  the design says what is obsolete.
- Lesson: `worker-stop` doesn't kill an agent in an *external* terminal (worktree-new.sh creates them); `orca terminal
  close` does, but child pytest runs survive it (kill the process tree).
- Closed the 8 stopped workers' Orca cards (`orca worktree rm --force`). Kept: local tags `archive/<branch>` for the 4
  branches with commits (381, 385, 387, 388; 385/387 also on origin), WIP patches for 373/381 in the coordinator
  scratchpad `archive/`. Remaining ai-toolkit cards: the main checkout and `orca-migration`.
- **v2 design validated** with amendments (06 §0: D1 Langfuse later batch, raw native OTel only; D2 no TDD/plan hooks;
  D3 no `<type>/` prefix; D4 Claude-only, Cursor/Copilot removed; D5 auto-or-human gate answers; D6 Opus review on
  every land; D7 v2 branch without machinery or CI; D8 yolo + deny-list hooks, **something stronger needed for
  sensitive projects**; D9 repo-scoped token later).
- Branch **`v2`** created from main (a93aa99e = main + ci.yml excluding `v2`/`v2-*` from CI; the first v2 push had
  triggered a run, cancelled). `extensions.worktreeConfig=true` on the repo; v2 worktrees get
  `core.hooksPath=~/orca/workspaces/ai-toolkit/.v2-nohooks` (no ai-toolkit git hooks); created with `--setup skip`
  (no provision-worktree). **WP0 dispatched** (`ctx_5b277d1ff006`, worktree `v2-wp0`, Sonnet 5.5 high); new tests
  live in `tests_v2/` until cutover.
- Stale inbox from stopped #373: its RED commit was blocked by sibling spokes' tripwires restoring/blaming shared tags (gate/373, gate/381, gate/382). Evidence for v2 dropping tripwires + marker tags.
- **WP0 merged into v2** (a7a41ca8): v2/ skeleton, 246 code lines + 200 test lines (14 tests, 2.5 s), e2e steps 1-4 pass
  on a local scratch repo. Fable review APPROVE (0 blockers); its 4 warnings fixed (local-env secrets no longer exported
  into claude's env; test isolation; explicit scratch .gitignore; setup marker order). Q1: two-step launch (`terminal
  create --command v2/bin/claude-spoke` + `worker-start --terminal`); Orca `agentCmdOverrides.claude` is GUI-only:
  to verify in WP1 with the user setting it once (/private/tmp/aitk-spike/override.sh kept).
- **WP1 (dispatch+land) `ctx_bfd51674909d`, WP3 (hooks) `ctx_38ddb155dd32`, WP4 (Claude-only sync + policy)
  `ctx_79d9fe8ab008`, WP5 (native OTel collector, reduced) `ctx_02e161c55147`** dispatched in parallel (worktrees
  v2-wp1/3/4/5, setup skipped, git hooks off). Each sends a plan via ask first. WP2 (coordinator) waits for WP1.
- WP5 merged into v2 (48023a3c): otelcol 48/80, compose 14/20, otel.sh 32/40, live Langfuse session + raw JSONL verified; Fable APPROVE, warnings fixed (GIT_DIR-proof raw-dir guard, 0700). At cutover: docker rm -f lf-collector, then otel.sh up.
- **WP4 merged into v2** (027a6274): Claude-only sync.sh 114/200, frontmatter in source files (metadata.yml gone), 6
  skills pruned, core skills/rules rewritten to v2 verbs, code-review JSON verdict contract; +1.4k / −16.2k lines.
  Fable APPROVE; fixes: UPSTREAM_REPO env key, v1 wording, broader lint, otel.sh not synced, per-repo hooksPath.
  **Cutover constraint:** run `sync.sh --migrate-v1` on hex only after WP3 is merged (else v1 settings.json points
  at deleted hooks → exit 127 on every tool call). Open: shared/ policy 11.2k vs 8k target (generic skills untouched).
- WP1 review: REQUEST_CHANGES (blocker: land.sh could push ungated commits when local main ≠ origin/main). WP3
  security review: REQUEST_CHANGES (rm -rf build/ false positive, git clean -x bypass, sk-ant keys). Both fixing.
- **WP1 merged into v2** (0bb222f7): dispatch.sh 117/120, land.sh 111/150; e2e steps 1-7 live on a scratch repo.
  Fable round 1 found a real BLOCKER (ungated push when local main ≠ origin/main), fixed and re-verified by a Fable delta
  review (old bug reproduced → now exit 2). The worker's own 16/16 mutation-killed tests had missed it: evidence for D6.
- **WP2 (coordinator + answer.sh + review.sh) dispatched** `ctx_92022f030810`, also carrying the `--cleanup-only`
  idempotency fix for land.sh.
- Cleanup: closed 4 orphaned Orca terminals (2 from the WP0 e2e scratch repo, 2 from the 10-01 orca-spike); they showed as 'Unknown' worktrees in the UI. Lesson for e2e scripts: close every terminal they create, even on failure (trap).
- Orca UI keeps a stale 'Unknown' card (with its main worktree) for every repo added then removed via 'project setup-delete' (~10 from e2e runs); the runtime/CLI no longer lists them. User removes them from the UI. e2e now reuses ONE registered scratch repo. Candidate Orca bug report.
- **Orca constraint (proved live by WP2):** only the terminal bound to a Run as consumer can `orchestration reply`;
  another terminal is refused ("no longer bound to Run"), and `--from <coordinator handle>` is refused ("attested as X").
  ⇒ human gate answers go through the coordinator: `coordinator.sh --reply <msg> approve` from any terminal writes a
  0700 spool file the loop drains on each wake (30 s waits while a human question is pending); the loop never pauses.
- WP3 delta review: all 9 fixes verified; 3 more one-liners requested ($TMPDIR quote FP, git stash -a, $PWD-prefixed
  root paths). **Coordinator decision: stop the review loop on the deny-list after this round.** Each round finds new
  edge cases; that's the nature of a pattern deny-list (D8 "clumsiness, not a sandbox"). Fix these 3 (budget 150), log
  the rest as known limits, merge after a local test check. Strengthens the D8 caveat: sensitive projects need a real
  sandbox (Claude native sandbox / Orca VM), not more patterns.
- **WP3 merged into v2** (5ae1c19e): Claude hooks 140/140 (push-guard, danger-guard, secrets-scan), git hooks
  commit-msg + pre-commit, settings.json; 463 tests with all WPs merged. Known D8 limits listed in docs/v2/wp3-notes.md.
  hex `--migrate-v1` is now unblocked (hooks exist in v2).
- User decisions for WP6: (1) **real-GitHub e2e on a throwaway public repo `mcrilo33/ai-toolkit-e2e`** (created
  2026-10-03); (2) **cutover rehearsal on a throwaway clone of ai-toolkit itself, not on hex** (git mv v2/* ., deletion
  list, sync into itself, register in Orca, run a real spoke). hex migrates separately later (`sync.sh --migrate-v1`).
- **WP2 merged into v2**: coordinator.sh 229/240, answer.sh 28/50, review.sh 33/60, `--reply` spool (human answers from
  any terminal, loop never pauses), live e2e auto + human + one review round; Fable APPROVE, its 4 warnings fixed
  (spool kept on a truncated inbox page, paged worker-list, land releases every dispatch). 545 tests with all WPs.
  `check --ack` verified at-least-once (nothing lost). Design change: fix-up rounds relaunch via `dispatch.sh --address`
  (new task, fresh two-step terminal); Orca refuses a new task on an idle two-step terminal.
- **WP6 dispatched** (real-GitHub e2e on mcrilo33/ai-toolkit-e2e, v2 ci.yml, cutover.sh + rehearsal on a throwaway
  ai-toolkit clone with a local bare origin, docs/v2/architecture.md).
- WP6: real-GitHub e2e green in auto (206 s), scripted-human and simulated-late-human modes. Handed the attended human run to the user (msg_23d50012752f, reply 'go' or 'skip' after).
- **UX finding (user's attended e2e run):** the human sees the --reply command in the terminal but NOT the question (the plan text is only in the issue comment). Fix: print the question body (wrapped) above the --reply line in the e2e output, in coordinator.sh --status, and a short excerpt in the notification.
- User decision: no experimental Orca features (custom board statuses rejected). Human-gate alerts = worktree --comment on the spoke card + terminal bell in the coordinator's Orca terminal (terminalBell setting) + question text in terminal/--status/issue; osascript best-effort with a WARN. First attended run timed out (no reply); to redo after the fixes.
- WP6 ready for attended run #2: question text shown in coordinator terminal/--status/e2e, GATE comment on the spoke card (live-verified; --comment "" doesn't clear, overwritten after reply), terminal bell → Orca's own notification only when the Orca window is NOT focused (suppressWhenFocused), unverifiable from CLI. osascript exits 0 yet shows nothing. Cutover rehearsal green. 560 tests.
- **WP6 done** (v2-wp6, 26 files): real-GitHub e2e PASS (auto 197 s: --next order, happy path with real review + CI on
  the exact SHA + FF land, negative path → blocked; scripted human PASS; simulated human-wait PASS); cutover rehearsal
  PASS 103 s on a throwaway clone (local bare origin); ci.yml 33/60; cutover.sh 48/80 + docs/v2/cutover.md (checklist
  + rollback to v1-final); docs/v2/architecture.md 104/300. **Measured after cutover: code 1,100 lines (was 43.5k),
  config 140, tests 2,341 (was 84.9k), docs 149, policy 11.2k (target 8k: still to prune).** Findings: Orca runs the
  TRACKED orca.yaml of a new worktree; issue:N selectors are global across repos (use path:); rehearsal caught
  cutover.sh dropping settings/* (fixed). Fable review of WP6 running. Attended human run still pending (user).
- **WP6 merged into v2** (b7d2b5fe): checklist fixes from the Fable review (orca-migration docs precondition, CI on a scratch branch on ubuntu+macos BEFORE pushing main, hub sync with --migrate-v1, rollback restores the local hooksPath, branch-protection check). 560 tests. Coordinator committed docs/orca-migration on its branch (39e0c647) to land on main before cutover.
- **User decision: hybrid coordinator with an explicit switch.** Attended = a Claude Code session holds the Run (Orca pushes messages into it; discuss, then reply); '/coordinate auto' hands the Run to coordinator.sh --answer auto; '/coordinate attended' takes it back (+ summary of what happened). Never inferred from presence. --reply stays as fallback. **WP7 dispatched** (ctx_3392d16e464f) before cutover; the attended e2e run will test this mode.
- **WP7 merged into v2** (19c58cc6): /coordinate skill (attended Claude session = coordinator), explicit
  `/coordinate auto|attended` switch via run-use fencing, `coordinator.sh --stop` waits on any live foreign holder,
  reply.sh extracted. Fable round 1 found a double-land BLOCKER (second --stop returned 0 mid-land), fixed and
  re-verified. 620 tests; switch e2e PASS live (stand-in session).
- **Post-cutover v2 follow-ups (from reviews):** --stop `prev=$(holder)` under set -e fails silently on a run-show
  error; wait on ANY live holder (incl. own handle); on_question yield without fenced on run-show hiccup; land.sh
  launch_override links the issue late; first:100 GraphQL window; policy 11.2k → 8k prune; tests_v2 10 s cap on a
  quiet machine; ci.yml first real run.
- Attended test prepared: clone /private/tmp/aitk-e2e-gh reset + synced from v2 19c58cc6 (export /private/tmp/aitk-v2-src), issue #111 (slugify, Gate: plan).
- **Attended run with the user: PASS.** A real Claude session in Orca ran `/coordinate attended`, dispatched issue #111
  (slugify, Gate: plan); Orca pushed the gate question into the session, the user discussed and approved through it,
  the worker coded test-first (2 commits), review + CI green on 37ceb79e, FF land, issue closed "landed in 37ceb79e".
  v2 is validated end to end, attended and unattended. Next: cutover (needs the user's explicit go).

## RESUME POINT (2026-10-03, before the user restarts Orca)
- **v2 is complete and validated**: origin/v2 = 19c58cc6 (WP0-WP7 merged, each Fable-reviewed), 620 tests, real-GitHub
  e2e green (auto, human --reply, switch), **attended run with the user PASS** (issue #111 on mcrilo33/ai-toolkit-e2e).
- **Next = the cutover**, user said GO after restarting/cleaning Orca (keep only the ai-toolkit project). Follow
  `docs/v2/cutover.md` on origin/v2 exactly:
  0. Commit STATUS/LESSONS on mcrilo33/orca-migration, then merge this branch into main (push) so v1-final keeps
     docs/orca-migration; check no v1 spoke / afk drain; branch protection check (gh api …/branches/main/protection).
  1-5. Fresh clone (NOT the hub), merge origin/main into v2, cutover.sh --dry-run then real, tests + shellcheck,
     merge --no-ff into main, sync + install into the clone.
  6. Push to scratch branch v2-cutover-ci; wait for CI green on ubuntu AND macos; delete the scratch branch.
  7. Push v1-final + main; CI green on main.
  8. Hub ~/Repos/ai-toolkit: pull --ff-only, `sync.sh . --migrate-v1`, `install.sh .`, verify skills/hooks;
     `docker rm -f lf-collector` then `otel.sh up`. Untracked pr6* files must survive.
  10. Update memory notes naming deleted mechanisms.
- Rollback: docs/v2/cutover.md "Rollback" (v1-final tag).
- After cutover: remove the aitk-e2e-gh registration and /private/tmp clones, the scratch export /private/tmp/aitk-v2-src;
  post-cutover follow-ups are listed above in this file.
- The coordinator Run of this session was run_54f40aea9fdf; after an Orca restart the terminal handle changes:
  re-bind with `orca orchestration run-use --id run_54f40aea9fdf --from $ORCA_TERMINAL_HANDLE` if needed (or a new Run).
