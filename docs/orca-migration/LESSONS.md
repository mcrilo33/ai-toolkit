# Orca lessons — material for the tutor session

> Written by the migration session as the work exercises each Orca feature. The tutor session turns these into
> explanations and read-only exercises for the user. The migration session does not teach in its own chat.

## Lesson 1 — Worktrees, selectors, terminals, agents

- Orca tracks **every** worktree of a registered repo, including ones created outside Orca (tmux spokes `ai-toolkit-362`, …),
  because the repo has `externalWorktreeVisibility: show`. Each one is a card in the sidebar.
- **repo**: a registered repository (`orca repo list`). `ai-toolkit` id `55c46406-b4cf-40fd-be1e-068f4ec6dc1c`.
- **worktree**: checkout + terminals + UI state; id = `<repoId>::<path>` (`orca worktree list`, `orca worktree ps`).
- **selector**: `issue:<n>`, `branch:<b>`, `path:<p>`, `name:<display>`, `active`/`current` (`orca worktree show --worktree issue:361`).
- **terminal**: a tab/pane of a worktree, addressed by a runtime **handle** `term_…` (`orca terminal list --worktree active`).
  Handles change after an Orca restart: re-list.
- **agent**: a Claude/Codex in a terminal; Orca knows its state (`working` +tool, `waiting` +tool on a permission prompt,
  `done` +last message) through its hooks in `~/.claude/settings.json` (observe-only, they never block).
  Visible in `orca worktree ps --json` → `agents`. Terminals still inside tmux (opened before the `.zshrc` fix) are invisible.
- Card status line: `orca worktree set --worktree active --comment "…"` and `--workspace-status todo|in-progress|in-review|completed`.
- Read-only exercises: `orca worktree ps`, `orca worktree show --worktree active`, `orca terminal list --worktree active`.

## Lesson 2 — `orca.yaml` (setup / archive hooks) — pending #362

- Repo-root `orca.yaml`: `setupAgentStartupPolicy: wait-for-setup` + `scripts.setup` / `scripts.archive`.
- Setup runs in its own terminal tab, cwd = new worktree, env `ORCA_ROOT_PATH`, `ORCA_WORKTREE_PATH`, `ORCA_WORKSPACE_NAME`;
  with `wait-for-setup` the agent starts only after setup exits 0. Archive runs before removal, re-runs on each attempt,
  killed at 120 s (removal aborted). Details: `03-spike.md` #1–#3, #10–#13.
- Until #362 lands, the migration session provisions by hand (see lesson 3, step 2).

## Lesson 3 — Orchestration: running #361 as an Orca worker (2026-10-02)

Steps the migration session ran:
1. `orca worktree create --repo name:ai-toolkit --name 361-identity-hub-side --issue 361 --no-parent --setup skip --json`
   → worktree under `~/orca/workspaces/ai-toolkit/`, branch `361-identity-hub-side` (`/` not allowed in `--name`), `linkedIssue: 361`.
2. `git branch -m feature/361-identity-hub-side` (Orca follows the rename), then `scripts/provision-worktree.sh --worktree … --issue 361`
   → `.claude/` hooks, `.ai-toolkit/{identity,task.md,…}`.
3. `orca orchestration run-create --objective "…" --from <coordinator terminal handle>` → Run `run_54f40aea9fdf`
   (a namespace + the coordinator's inbox; it never schedules anything by itself).
4. `orca orchestration worker-start --run <run> --worktree path:<wt> --agent claude --model claude-sonnet-5-5 --effort high
   --task-title … --spec "<kickoff>"` → Task `task_6e473708660e`, Dispatch `ctx_db94df10975e`, receipt `turnStart: observed`.

Vocabulary:
- **Run**: coordination namespace + inbox. **Task**: unit of work. **Dispatch**: one authoritative attempt of a task.
- **Worker**: the agent launched by `worker-start`, with a preamble teaching it `ask` (blocking question) and `worker_done`.
- **ask / reply**: the worker blocks on `orca orchestration ask`; the coordinator reads `orchestration check --wait` and answers
  with `orchestration reply --id <msg> --body …`. Replaces "marker tag + pane scrape + tmux send-keys".
- During `ask`, the agent shows `working`, not `waiting`: a parked gate is read from the inbox, not from agent state.

Pitfall seen live (2026-10-02): a Run has exactly ONE coordinator terminal. Any terminal that runs
`orchestration run-use --id <run> --from <its handle>` takes it over (`consumer_generation` increments) and the previous
coordinator gets `consumer_fenced` on its next `check`. The #363 worker did this by accident while spiking `worker-start`.
Fix: `run-use` again from the right terminal; workers that need to experiment create their own scratch Run.
Also: terminal handles change when a session is restarted, so a coordinator must re-bind its Run after a restart.

Read-only exercises: `orca orchestration run-show --id run_54f40aea9fdf` (see `coordinator_handle`), `orca orchestration worker-list --run run_54f40aea9fdf`, `orca orchestration run-show --run run_54f40aea9fdf`,
`orca worktree ps`. In the UI, the card `361-identity-hub-side` shows the worker live. Do not type into the worker's terminal:
the migration session coordinates it.
