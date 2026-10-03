# v2 WP0 notes (skeleton): Q1 result and decisions

Branch `v2-wp0`. All v2 files live under `v2/` mirroring the 06 section 3 tree; `tests_v2/` and
`docs/v2/` are top-level. At cutover `git mv v2/* .` (scripts find siblings via their own dir).
Only `orca.yaml` and `scripts/install.sh` collide with old names, but one rule beats a mixed tree, and
the old root `orca.yaml` (used by real worktrees) stays untouched.

## Q1: OTel env injection (decision: two-step launch; the override is verified in WP1)

| Option | Result |
|---|---|
| `agentCmdOverrides.claude` -> `v2/bin/claude-spoke` | **Static evidence only.** The Orca bundle reads `agentCmdOverrides[tuiAgent]` when it builds the terminal-agent launch. No CLI verb writes it (settings live in `profile-state.db`), so it needs the UI. Not live-tested. WP1: user sets the field once to `/private/tmp/aitk-spike/override.sh` (a wrapper that runs `claude-spoke` with `CLAUDE_REAL=logclaude.sh`), run `v2/e2e/spoke-scenario.sh`, read `/private/tmp/aitk-spike/log`, clear the field. |
| `agentDefaultEnv.claude` | Exists, global and static (only `goose` set today). Cannot carry a per-spoke `spoke_run_id`. |
| PATH shim | Not tested and rejected: `~/.local/bin/claude` is the real, updater-managed symlink and comes first on PATH. |
| Two-step `terminal create --command <claude-spoke ...>` + `worker-start --terminal` | **Verified live** (e2e): `ps eww` of the worker's claude shows `OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=<id>`. This is the WP0 launch path. |

Switching later is one block in `v2/e2e/spoke-scenario.sh` (marked `--- launch path`): replace it with
`worker-start --agent claude --model .. --effort ..`. `claude-spoke` is pass-through outside a spoke
(no `.ai-toolkit/spoke-run-id`), so it is safe as a global override. D1: native OTel only, no scores.

## Findings that change later WPs

- **Claude trust dialog hits git worktrees whose git root has no exact `~/.claude.json` entry.** A trusted
  parent (`~/orca/workspaces`) does not cascade (it does for a non-git cwd). The 03-spike "parent trust"
  result was confounded: `--agent` launches are pre-trusted by Orca itself. Two-step launches in a repo
  whose root is trusted (any real repo) are fine; a scratch root needs `Down`+`Enter` (default is "No, exit"),
  which the e2e does, detecting it from `terminal read` (`No, exit`). WP1 dispatch must handle it too.
- A target's `.ai-toolkit/` and `.claude/` are in the user's global gitignore, so a new worktree has no
  `.ai-toolkit/`. A target's `orca.yaml` must call `$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh` (Orca's
  runner is bash, it expands); `v2/orca.yaml` (this repo, tracked scripts) uses `./scripts/...`. WP4 sync.
- `worker-start --from` is fenced to the caller's own terminal (`consumer_fenced`): a coordinator must run
  in an Orca terminal. The e2e is meant to be started in a dedicated one (see its header).
- A failing setup under `wait-for-setup` makes `worker-start` return `ok:true, state:failed` after its timeout.
- Setup runs before `worktree set --issue`, so `setup.sh` takes the issue from Orca's link if any, else from the
  `<n>-<slug>` branch name. `setup.sh` ends by touching `.ai-toolkit/setup-done`: the two-step launcher waits on it.

## Deviations and leftovers

- e2e step 2 has no `sync.sh` (WP4) and no GitHub issue (not created without asking): the scratch repo is
  assembled in the script, the worker gets an inline spec, `task.md` is covered by unit tests with a `gh` stub.
  Step 8 (Langfuse) is out of scope (D1). The e2e scratch root is the fixed `/private/tmp/aitk-e2e-scratch`.
- `~/.claude.json` keeps trust entries I did not remove (live file, concurrent writers): `/private/tmp/aitk-e2e-scratch`
  (reused by every e2e run), and stale ones `/private/tmp/aitk-e2e.PyHSOW`, `/private/tmp/aitk-trust-root`,
  `~/orca/workspaces/aitk-e2e.{NTqCYq,PyHSOW}/wp0-hello`.
- `shellcheck` is not installed: used `shellcheck-py` in a scratch venv (`v2/.shellcheckrc` sets `source-path`).
  CI at cutover needs shellcheck; the default `python3` has no `xdist` (the hub venv does).
- Old pytest runs left orphan `zsh -c ... claude` processes (not mine): kill them if they matter.

## Budgets (lines, `wc -l`)

lib.sh 59/100 · setup.sh 43/80 · archive.sh 6/20 · claude-spoke 26/30 · install.sh 21/40 · ai-toolkit.env 18/25 ·
orca.yaml 5/6 · e2e 91/150 · tests_v2 (conftest + 3 files) 200/200. Run: `pytest -n auto tests_v2` (15 tests, ~2 s).
