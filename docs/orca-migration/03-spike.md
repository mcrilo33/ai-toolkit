# Orca migration — 03 Spike results (Phase 3a)

Date: 2026-10-01, Orca 1.4.218, Claude Code 2.1.286. Resolves the **[unverified]** items of `02-orca-capabilities.md` §4.

**Setup:** a disposable repo `orca-spike` in the session scratchpad (own `.git`, a local bare `origin`, no ai-toolkit hooks), registered
with `orca repo add`. Its `orca.yaml`:

```yaml
setupAgentStartupPolicy: wait-for-setup
scripts:
  setup: ./spike-setup.sh     # logs cwd/env/branch, sleeps 10 s, writes .setup-done
  archive: ./spike-archive.sh # logs cwd/env/existence, sleeps $(cat .archive-sleep)
```

Three worktrees were created and removed (A: agent + prompt; B: pushed branch + slow archive; C: branch rename). The repo was then
unregistered (`project setup-delete`), and `~/orca/workspaces/orca-spike` (plus Orca's empty `.orca-worktree-trash`) was removed.
ai-toolkit, `main` and the main checkout were not touched.

## Results

| # | Question | Result | Consequence |
|---|---|---|---|
| 1 | Is `orca.yaml` honoured over local `hookSettings`? | **Yes.** The local repo setting said `start-immediately`, but `orca.yaml`'s `wait-for-setup` applied. No trust prompt appeared for the `orca.yaml` commands. | Decision §3.3 holds (local scripts stay empty). |
| 2 | Does `wait-for-setup` hold the agent? | **Yes.** The agent tab printed `Waiting for setup to finish before starting agent...` then `Setup finished; starting agent.` once the 10 s setup exited. | Provisioning (`.claude/`, `.ai-toolkit/`) is guaranteed before Claude's first tool call. |
| 3 | Setup context | Runs **in its own Orca terminal tab** (has a tty, `TERM_PROGRAM=Orca`), **cwd = the new worktree**. Env: `ORCA_ROOT_PATH` (main checkout), `ORCA_WORKTREE_PATH`, `ORCA_WORKSPACE_NAME`, `ORCA_WORKTREE_ID`, `ORCA_TERMINAL_HANDLE`, `ORCA_PANE_KEY`, … `CreateWorktree` returns in ~1 s; setup continues asynchronously. | `provision-worktree.sh` can derive everything from env; no args needed. |
| 4 | `--name feature/360-spike-a` | **The `/` is sanitized:** branch `feature-360-spike-a`, dir `feature-360-spike-a`; the display name keeps `feature/360-spike-a`. | `<type>/<n>-<slug>` can't be passed via `--name`. See #5. |
| 5 | Rename the branch after create (`git branch -m feature/362-spike-c`) | **Works.** `worktree show` reports `refs/heads/feature/362-spike-c`, and the selector `branch:feature/362-spike-c` resolves. | Create with `--name <n>-<slug>`, then rename to `<type>/<n>-<slug>` (in dispatch or in setup). Branch prefix `none` + auto-rename off keep it stable. |
| 6 | Claude workspace-trust dialog | **Blocks every agent in a new worktree path.** Orca reports `terminal wait … blockedReason: "agent-trust-workspace"` but doesn't answer it. In this Claude version the **default choice is "No, exit"**, so a blind Enter kills the agent (observed). Trust of `~` does not cascade. | **New blocker for unattended dispatch.** See open question Q1. |
| 7 | Prompt delivery | `terminal send --enter --wait-submit 15` → `stages: ["input_accepted","turn_started"]` with a durable `requestId` (bypass-mode agent). In default mode only `input_accepted` was proven within 15 s, although the turn did start. | Submission proof is real but not guaranteed within the window. Keep "never resend on silence"; confirm via agent state instead. |
| 8 | Agent states (`worktree ps .agents[]`) | Observed: `working` (+`toolName`, `toolInput`) → `done` (+`lastAssistantMessage`); **`waiting`** with `toolName: WebFetch`, `toolInput: https://example.com` on a permission prompt; after answering `1` via `terminal send` → `working` → `done`. A fresh, never-prompted Claude reports `done`. | Maps cleanly onto `slot_state` (working / parked-on-permission / idle) **without pane scraping**. `tui-idle` is also satisfied while `waiting`, so use the agent state, not `tui-idle`, to detect "parked". |
| 9 | Answer injection | `terminal send --text '1'` on the permission dialog was accepted and resolved it. Arrow keys work as raw escape text (`$'\e[B'`). | The tmux `send-keys` lane can be replaced 1:1 by `terminal send`. |
| 10 | Archive hook ordering | Runs **before** removal, cwd = the worktree, which still exists and is still listed in `git worktree list`. | Telemetry ingest can live in the archive hook (#8 solved in principle). |
| 11 | Removal with untracked files | Without `--force`, removal is **refused** after the archive hook ran (`?? .setup-done`). With `--force` it succeeds, and **the archive hook runs again** (once per attempt). | The archive script must be idempotent. |
| 12 | Archive hook timeout | Hook killed at **~120 s**. Removal **aborted** and the worktree kept: fail-safe. The CLI call itself dropped at ~30 s with `runtime_unavailable` ("closed the connection") while the runtime kept going; Orca stayed healthy. | Ingest must finish in < 120 s or be detached (spool, then ingest asynchronously). CLI callers must re-check state after a ~30 s disconnect instead of trusting the error. |
| 13 | Branch after removal | **The local branch is deleted even when pushed.** The remote branch is kept. | Fine for land (merge first, then remove). A removal before landing loses the local ref, so the push gate must have run. |
| 14 | git/gh attribution shim | Not active: `type -a git` → `/usr/bin/git`, no `ORCA_ENABLE_GIT_ATTRIBUTION` in the env. | No interference with `commit-quality`. |

## Decisions (2026-10-01)

- **Q1 → parent trust, resolved.** `~/.claude.json` `projects["/Users/mathieucrilout/orca/workspaces"].hasTrustDialogAccepted = true`
  (atomic write, backup `~/.claude.json.bak-orca-20261001`). Re-tested with a fresh `orca-spike/spike-trust` worktree + `--agent claude`:
  no dialog, `tui-idle` satisfied with no `blockedReason`, and agent state `done`. There is no per-worktree entry, so **trust
  cascades from a non-home parent** (it does not from `~`). One-time machine setup, so no per-worktree writes are needed. Keep the
  `terminal send` (Down + Enter) answer as a dispatcher fallback when `blockedReason == agent-trust-workspace`, for other machines or
  a reset config. Cleaned up afterwards (worktree removed, repo unregistered, dir removed).
- **Q2 → the dispatcher renames the branch.** It runs `orca worktree create --name <n>-<slug> …` and then
  `git -C <wt> branch -m <type>/<n>-<slug>`. `provision-worktree.sh` only records identity (from `ORCA_*` env + Orca metadata) and
  does not need the type.

## Open questions (as asked, now decided above)

- **Q1 — Claude trust dialog (#6).** Options:
  (a) the setup script pre-trusts the worktree path in `~/.claude.json` (`projects[<path>].hasTrustDialogAccepted = true`). This
  works but writes a file Claude rewrites concurrently, so it needs an atomic, locked update.
  (b) trust a parent (`~/orca/workspaces`) once, if Claude cascades trust from non-home parents (untested; `~` does not cascade).
  (c) the dispatcher answers the dialog through `terminal send` (Down + Enter) when `blockedReason == agent-trust-workspace`.
  Recommendation: test (b) first (one-time, no per-worktree writes), with (c) as the fallback in the dispatcher.
- **Q2 — where to rename the branch (#5):** in the dispatcher right after `worktree create` (it knows `<type>` and `<n>`), or in
  `provision-worktree.sh` (it would need the type passed in). Recommendation: the dispatcher; setup only records identity.

## Round 2 — the three remaining items (2026-10-01)

| # | Question | Result | Consequence |
|---|---|---|---|
| 15 | `--issue` on the real repo | Tested on ai-toolkit with **closed** #356 and name `spike-issue-link` (no leading digits, so no ai-toolkit script would read it as a spoke). The `/afk` drain was checked first: `.afk-state=drain` is stale since 2026-08-25 and the heartbeat PID is dead. Result: `linkedIssue: 356`, and the selector `issue:356` resolves. **Nothing written to GitHub**: comments 3→3, labels and state unchanged, no assignee. Removed with `orca worktree rm --worktree issue:356`; no leftover worktree or branch. | `--issue` is pure Orca metadata, safe to use at dispatch. `issue:<n>` is a reliable lookup key for the identity mirror. |
| 16 | Ingest duration vs the 120 s archive limit | **Not measurable from history:** land logs have no timestamps, local telemetry has no ingest span (only whole-land spans: p50 9 s, p90 ~5.5 min, max ~32 min, dominated by the test gate), and Langfuse keeps no per-step span. **From the code:** the Python view-builder step has **no timeout** (the script's own comment: macOS has no `timeout`), with 3 s flush + up to 3 attempts + 5 s/10 s backoff. So it is unbounded. **But** `worktree-land.sh:1035` already runs the ingest synchronously **before** teardown. | **Design: keep the ingest in land, not in the archive hook.** Land ingests, then calls `orca worktree rm`. The archive hook only covers removals *outside* land (abandon/manual): it copies raw bodies + identity to a spool under `GCD/ai-toolkit-afk/` in a few seconds (well under 120 s); a later re-run (`--spoke-run-id`) or a detached job ingests from the spool. |
| 17 | Orchestration as a PLAN-gate transport | Full cycle run on the scratch repo with a real Claude worker. `run-create --from <my terminal>` → `worker-start --spec … --worktree new-top-level --agent claude` (it **also ran `orca.yaml` setup with `wait-for-setup`**). The worker wrote `plan.md`, then called `orca orchestration ask` with options `approve,revise` and **blocked**: `hello.txt` was absent and its agent state read `working / Bash: orca orchestration ask …`. The coordinator got a structured `question` message (`check --wait`) and `reply --body approve`. The worker then created `hello.txt` and sent `worker_done` with `outcome: succeeded` and `filesModified: [plan.md, hello.txt]`. Separately, `gate-create` on a task → task `blocked`; `gate-resolve approve` → task `ready`. Cleanup via `worker-release` (transcript archived, terminal closed), worktree rm, repo unregistered. Run `run_1c4ba3d70f6b` left inert in `orchestration.db` (`orchestration reset` would wipe all orchestration state). | **Viable, and better than today's channel:** a structured, durable, acknowledged question/answer instead of a marker tag + pane scrape + keystroke injection. Caveats: (a) during `ask` the agent shows `working`, not `waiting`, so "parked on a gate" must be read from the Run inbox, not agent state; (b) workers need the preamble (worker-start), so plain `worktree create --agent` spokes would have to be dispatched through `worker-start` instead; (c) `nestedWorkerMaxDepth: 1`. Recommendation: **later phase**, after the transport adapter (02 §2). The `ready/gate/accept/blocked` marker tags stay the source of truth until then. |

All Phase 3a questions are now answered except the PLAN-gate migration itself (deferred by design).

## Round 3 — S3 spikes (#362, 2026-10-02)

Scratch repo (git, `orca.yaml` with `setup`, `issueCommand`, `worktree.sharedDirectories: [.venv]`) registered with
`orca repo add`; worktrees created with `--run-hooks`. Orca 1.4.218.

| Spike | Outcome | Evidence |
|---|---|---|
| (a) `issueCommand` | **Not adopted.** | `orca worktree create --issue 7 --run-hooks` linked the issue (`linkedIssue: 7`) and ran `scripts.setup` (`setup-ran.txt` written, `ORCA_*` env present), but the `issueCommand` script never ran (no output file after 8 s). In the Orca bundle `issueCommand` is read from `orca.yaml` or `.orca/issue-command` and executed by an `issue-command-runner` only on the UI worktree-activation path (Tasks panel / new-workspace dialog), not on CLI creation. Dispatch moves to the CLI (`worker-start`, S4), so it would never fire. **Not verified:** the template variables it expands (needs the GUI Tasks flow). It also runs a shell script, not an agent prompt, so seeding `/source-task #<n>` would still go through S4's `--spec`. |
| (b) `worktree.sharedDirectories` for `.venv` | **Not adopted.** | Criterion 1 fails: console-script shebangs are absolute paths into the venv that created them (`.venv/bin/pip` starts `#!/<abs>/.venv/bin/python3.14`), so a shared `.venv` runs every worktree's `pytest` against the SOURCE checkout's interpreter and site-packages; any editable install would import the primary checkout's code, not the worktree's, silently testing the wrong tree. Criterion 3 is not guaranteed: concurrent provisioning (`ensure-test-venv.sh` pip-installing into one shared site-packages) has no lock. Criterion 2 holds by construction (`.testmondata*` live at the worktree root, outside `.venv`). A worktree created after the venv existed was **not** observed (see the note below), so link behaviour itself is untested; the decision rests on the shebang evidence. |

`provision-worktree.sh` therefore keeps creating a per-worktree `.venv` (unchanged); `orca.yaml` carries no `worktree:` block.

> [!WARNING]
> **Deviation from the issue text (found in review, hub to confirm):** the issue names `./.ai-toolkit/scripts/{provision,archive}-worktree.sh`
> for the generated host `orca.yaml`, but spikes #3/#10 run the hooks with cwd = the worktree and a host's `.ai-toolkit/` is
> git-excluded, so those relative paths would not exist in a host worktree (setup would fail, archive would abort removal).
> Sync therefore generates `"${ORCA_ROOT_PATH:-.}/.ai-toolkit/scripts/…"` (pinned by a linked-worktree test; the toolkit's own
> committed `./scripts/` form is unchanged). **Unverified:** that Orca runs the hook through a shell (so the variable expands) and
> reads `orca.yaml` from the main checkout. Confirm with one host-repo spike; revert is one `sed` line in `generate_orca_yaml`.

> [!NOTE]
> The scratch-repo cleanup (`orca worktree rm` for `spike-issue`, `orca project setup-delete`, removing `.ai-toolkit/spike-scratch`) and the
> post-venv worktree creation were blocked by the `afk-danger-guard` judge (verdict `dangerous` for writes under `~/orca/`), so
> the scratch repo `e6dd1f84-f82d-4420-92be-5968ef55ddcd` and worktree `spike-issue` still need removing by hand.
