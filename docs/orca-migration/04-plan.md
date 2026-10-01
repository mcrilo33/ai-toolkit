# Orca migration — 04 Plan "Orca-first" (Phase 3b+)

Date: 2026-10-01. Inputs: `01-inventory.md`, `02-orca-capabilities.md`, `03-spike.md`.
Status: **agreed direction** (user, 2026-10-01): base execution on Orca and use every *useful* Orca feature; keep Langfuse.

## 0. Target architecture

| Layer | Owner | Contents |
|---|---|---|
| **Policy** | ai-toolkit | Rules, skills, agents, all hooks/gates, review-stamp, land (merge + test gate + push), `batch-plan` scope packing, afk answerer reasoning, sync pipeline (still the distribution channel: it also serves Cursor/Copilot) |
| **Contract** | ai-toolkit (written), Orca (triggers) | `orca.yaml` → `provision-worktree.sh` / `archive-worktree.sh`; `.ai-toolkit/identity` as the single identity record |
| **Execution** | Orca | Worktrees, agent launch (`worker-start`), terminals, prompt/answer delivery, agent state & liveness, Q&A/gates (`ask`/`reply`), task deps, notifications, diff review, scheduling (automations), remote/mobile, sandboxes, accounts |
| **Measurement** | Langfuse (+ Orca signals) | Causal spoke/cycle trees stay; Orca adds lifecycle spans (setup, dispatch→first turn, gate wait, permission wait, liveness) |

**Not used, deliberately:** Orca AI commit/PR messages (PR-less flow; `commit-quality` owns the format), artifact publishing,
skill sharing as a distribution channel (only for ad-hoc one-off sharing), computer use, emulators.

## 1. Workstreams, in order

Each step is independently shippable, test-first (pytest, AAA), and keeps the full suite green.
**S1–S3 are needed whatever happens next. S4–S6 are the Orca-first core.**

**Unattended-safety rule (S4–S6):** these steps modify scripts the `/afk` drain itself runs (dispatch, land, coordinator).
Every Orca execution path is therefore behind a config switch (`settings/ai-toolkit.yml`, e.g. `execution.host: tmux|orca`), **default
`tmux`** until a human flips it after the drain. A self-update mid-drain then never changes the live execution path.

### S0 — Model routing refresh (priority, independent)
- Same policy shape as #347 (memory: model-routing-preference), moved to the newest models: spoke driver `claude-sonnet-5-5` / high
  (no `[1m]`); reasoning set `claude-opus-5-5` (high; max for debug/security/devops); architect + planner `claude-fable-5-1` / max.
  Check whether any cheap judge path (e.g. the danger-guard tier-3 judge) should use Haiku 4.5.
- Verify IDs and pricing (`measure_context_cost.py` tables) against the `claude-api` skill. Update all four pinning test surfaces,
  then re-sync the hub so `.claude/agents/*.md` and `spoke-model.env` regenerate (the drain's self-update must pick up the new `spoke-model.env`).

### S1 — `provision-worktree.sh` (extract, no behaviour change)
- Extract from `scripts/worktree-new.sh` (`:241-565`): info/exclude, `.testmondata-baseline`, `.ai-toolkit/{spoke-run-id,lane,mode,task.md,ledger-skeleton.md}`,
  rsync of `.claude/`, `settings.local.json` allowlist, afk-mode permissions.
- Inputs: `ORCA_ROOT_PATH` / `ORCA_WORKTREE_PATH` / `ORCA_WORKSPACE_NAME` when run by Orca, explicit args otherwise.
  Idempotent; fails loud (non-zero ⇒ Orca shows the failure, and `wait-for-setup` never starts the agent on a broken tree).
- **New:** writes spoke OTel env into the worktree's `.claude/settings.local.json` `env` block (replaces the env prefix of the tmux
  launch command; `OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=…`).
- **New:** checks that the Orca skills a worker needs (`orca-cli`, `orchestration`) are installed and current (`orca skills installed` / `update`).
- `worktree-new.sh` calls it, so the tmux path and the Orca path share one code path.

### S2 — Identity record
- `.ai-toolkit/identity` (key=value): `issue`, `type`, `slug`, `mode`, `lane`, `spoke_run_id`, `orca_worktree_id`, `orca_dispatch_id`, `run_id`.
- `shared/hooks/lib/identity.sh` is the only reader. Migrated guards: plan-gate, push-scope, ledger-schema, cycle-step, afk permission/danger
  hooks, commit-quality anchor, `hub-status` / `batch-plan` in-flight detection.
- **Fallback** to today's inference (`WT_SPOKE`, git-dir, branch slug) when the file is absent, so nothing regresses during the transition.

### S3 — `orca.yaml` + archive
```yaml
setupAgentStartupPolicy: wait-for-setup
scripts:
  setup: ./scripts/provision-worktree.sh
  archive: ./scripts/archive-worktree.sh
issueCommand: …            # S3 spike: seed `/source-task #<n>` when a worktree is created from the Orca Tasks panel
defaultTabs: …             # optional: e.g. a test watcher tab
worktree:
  sharedDirectories: […]   # S3 spike: share the test venv across worktrees if safe
```
- `archive-worktree.sh`: idempotent (03 #11), finishes in seconds (03 #12). It spools raw bodies + identity to `GCD/ai-toolkit-afk/ingest-spool/`
  for removals *outside* land. Land keeps the synchronous ingest before `orca worktree rm` (03 #16).
- `sync-to-repo.sh` generates `orca.yaml` for target repos. The local Orca `hookSettings.scripts` stay empty (02 §3.3).

### S4 — Dispatch = `orca orchestration worker-start`
- `start-task` / `next-batch` / afk dispatch call one wrapper:
  `worker-start --run <drain-run> --repo … --worktree new-top-level --name <n>-<slug> --agent claude --model <m> --effort <e>
  --task-title … --spec "<seed: /source-task #n + contract>" [--deps <task ids>]`,
  then `worktree set --issue <n> --comment … --workspace-status in-progress`, then `git branch -m <type>/<n>-<slug>` (03 Q2).
- Model routing comes from `spoke-model.env` into `--model/--effort` (Claude 5 policy unchanged).
- `Blocked-by` edges become `--deps`; `batch-plan` still decides *which* issues form a disjoint batch.
- Attended (non-afk) spokes use the same path, with the Run bound to the hub terminal.

### S5 — Teardown & lookup
- `worktree-land.sh`: merge → gate → push → **ingest** → `worker-release` → `orca worktree rm` (no more `rm -rf`, 01 #1).
- `worktree-done.sh` (abandon path): `orca worktree rm --run-hooks` (archive spools).
- `wt_resolve` / in-flight discovery read `orca worktree list --json` (+ identity) instead of `<repo>-` path prefixes.
- Delete the VS Code review window (`--code`) in favour of Orca's diff view.

### S6 — `/afk` as an orchestration coordinator
- One Run per drain. The supervisor tick stays a **script** (scripted control plane), but reads `orchestration check --wait`,
  `worker-list` (liveness / attention / nextAction) and `worktree ps .agents[].state`, instead of tmux panes.
- `slot_state` mapping (03 #8): `working` → working; `waiting` + `toolName` → parked on permission; inbox `question` → parked on a gate
  (03 #17: the agent shows `working` during `ask`); `done` → idle; liveness `exited` → dead.
- PLAN gate & park answers: the spoke calls `orca orchestration ask`; the answerer (unchanged reasoning, `afk-answering` rule) replies
  via `orchestration reply`. Permission dialogs: `terminal send --wait-submit`. The marker tags (`gate/ready/accept/blocked`) stay as the
  durable git-side record.
- `spoke-ready.sh ready` also emits `worker_done --outcome succeeded` (`failed` on blocked).
- Recovery: `worker-stop` / `worker-abandon` + `worker-start --retry-of`. Deleted: `hub-inject.sh` tmux lanes, pane scrape, pane-pid walk,
  `recover_dead_panes`, `capture-pane` forensics.
- Sleep inhibit: Orca's `keepComputerAwakeWhileAgentsRun` replaces `caffeinate`.

### S7 — Measurement (Langfuse stays the evaluation system)
- The coordinator emits Langfuse spans for Orca lifecycle events: setup duration, dispatch→`turn_started`, gate wait (`ask`→`reply`),
  permission wait (`waiting`→`working`), liveness transitions, release. All are linked by `spoke_run_id` to the existing trees.
- `orca-stats.json` (5 totals + 10 events) is **not** a substitute (checked 2026-10-01).

### S8 — Surfaces
- Spoke notifications: Orca native (drop the global osascript Notification hook for spokes; keep `hub-notify.sh` for drain-level events).
- Status: `worktree set --comment/--workspace-status` at each cycle step (mirrors the GitHub lifecycle labels, which stay).
- Enable agent-session search (Settings) for transcript digging.

### S9 — Automations (agentic jobs only)
- Weekly **workflow evaluation**: an agent reads Langfuse (via the `langfuse` skill) and proposes improvements as issues
  (scoped through followup-scoper).
- Morning backlog triage / bug-scoper sweep.

### S10 — Remote, mobile, sandboxes, accounts (spike, then adopt)
- `orca serve` + mobile pairing to follow the drain and answer gates from the phone ⇒ retire `afk-travel` / `--remote` ssh+tmux.
- Environment recipes (VM/container) for afk spokes running with `--dangerously-skip-permissions` (an extra layer on top of the danger wall).
- Managed Claude accounts to spread concurrency; agent hibernation for parked spokes.

### S11 — Retire the tmux path
- Only after S4–S6 have run a full drain on Orca **and** the downstream synced repos (hex, …) have migrated or opted out.

## 2. Spikes still needed (before the step that depends on them)

| Spike | Blocks |
|---|---|
| `issueCommand` semantics (when it runs, which variables) | S3 |
| `worktree.sharedDirectories` with a venv (isolation, testmon) | S3 |
| Claude honours `env` in `.claude/settings.local.json` for OTel resource attrs | S1 / S7 |
| Orchestration preamble + `/source-task` seed in one prompt; `worker-start` + `--model/--effort` | S4 |
| `worker-start` without `--issue` → `worktree set --issue` race-free | S4 |
| Answering a permission dialog of a **dispatched** worker (`terminal send` vs orchestration) | S6 |
| Mobile pairing / `orca serve` / environment recipes / hibernation / managed accounts | S10 |

## 3. Risks

- **Orca churn:** the CLI moves fast (retired commands, legacy contracts). Mitigation: one wrapper module for all `orca` calls,
  `--json` everywhere, version check at arm time, and policy kept in ai-toolkit.
- **Identity migration touches many guards:** the S2 fallback means no regression.
- **Self-modification:** never land `/afk` scripts during a live drain (memory: afk-supervisor-crash-and-selfmodify).
- **Downstream repos** still depend on the tmux path until S11.
- **The CLI drops at ~30 s on long calls** (03 #12): wrappers re-check state instead of trusting a `runtime_unavailable` error.

## 4. Open decision — how the code lands

This migration session runs without the ai-toolkit framework, with a "no commit / no push" rule. Options:
(a) commit S1… on this branch (`mcrilo33/orca-migration`), no push; the user lands;
(b) file one issue per step and run them through the normal issue → spoke → land circuit (dogfoods the gates; S1–S3 first).
