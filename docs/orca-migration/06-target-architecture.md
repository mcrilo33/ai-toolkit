# Orca migration — 06 Target architecture: ai-toolkit v2

Date: 2026-10-03. Baseline: `main` @ `3f41550b`. Orca 1.4.218, Claude Code 2.1.288. Status: **validated with amendments (§0), 2026-10-03**.
Evidence tags: **[L]** observed live today (`--help`, `orca status`, the Orca settings document, Langfuse OpenAPI) ·
**[S]** verified in `03-spike.md` / `STATUS.md` · **[verify]** not yet exercised.

## 0. User decisions (2026-10-03) — these override the sections below

| # | Decision | Effect on this design |
|---|---|---|
| D1 | **Langfuse moves to a separate batch after cutover.** v2 keeps only the minimal native OTel export (`bin/claude-spoke` env + the existing collector config) so raw data is collected from day one. | WP5 shrinks to the shim + collector config; `scores.py`, dashboards and any fine-grained layer (step attribution, context itemization) are designed **after cutover, from a list of concrete analysis questions** the user writes. Scores in §4/§5 are deferred. |
| D2 | **No hooks for TDD or the PLAN gate**: policy + measurement; add a hook only if data shows skips (Q2 accepted). | As proposed. |
| D3 | **Drop the `<type>/` branch prefix**: Orca's `<n>-<slug>` branches (Q3 accepted). | As proposed. |
| D4 | **Claude Code only: remove Cursor and Copilot compatibility** (stronger than Q4). | `sync.sh` becomes a copy to `.claude/` + `CLAUDE.md`; `frontmatter.py` (per-platform projection) and the 5 `metadata.yml` are deleted, with frontmatter living in each source file. No `.cursor/`/`.github/` output. Check that `hex` does not rely on those files before cutover. |
| D5 | **Gate answers: `answer.sh` (auto) or the human (`--answer human`)**, chosen per coordinator run. | As proposed (§4 step 7). |
| D6 | **Independent Opus review (`review.sh`) on every land.** | As proposed (§4 step 11, Q6). |
| D7 | Build on a `v2` branch **without ai-toolkit machinery and without GitHub CI until cutover** (`land.sh --local-gate` meanwhile); E2E scenario from WP0 onwards; single cutover. | As §8. |
| D8 | **Security: yolo (`--dangerously-skip-permissions`, Orca's default) + the deny-list hooks** (`push-guard`, `danger-guard`, `secrets-scan`) + native git hooks. No Claude sandbox for now. **Explicit caveat (user): something stronger is needed before using the framework on more sensitive projects** (candidates: Claude Code native sandbox, Orca VM/container environments, scoped tokens). | As proposed (§2.3, WP3). Recorded as a known limitation. |
| D9 | **Spokes keep the user's `gh` login**; a fine-grained, repo-scoped token comes in a later batch (with the sandboxes work). | No change in v2. |

## 1. Summary

v2 keeps **policy** (rules, skills, agents, prompts) and a **thin scripted lifecycle** (dispatch → gate → land) on top of
Orca. Orca owns worktrees, agent launch, prompts, agent state, liveness, the question/answer channel, completion signals,
retries, notifications, keep-awake, and the UI. GitHub owns the backlog and the test gate (CI). Langfuse owns measurement,
fed by Claude Code's native OTel export plus ~10 lifecycle scores posted by the coordinator. Everything that existed to
drive tmux, to replace a missing provisioning seam, to prove things locally (gauntlets, tripwires, marker tags, HMAC
review stamps, testmon tiers) or to rebuild traces from transcripts is deleted. One file talks to `orca`, one talks to
Langfuse. There is no self-copying supervisor, no watchdog tier, no state directory of marker files: state lives in
Orca's orchestration DB, Orca worktree metadata, git, and GitHub labels.

| Layer | Today (`main` @ 3f41550b) | v2 target (hard cap) |
|---|---|---|
| Glue code (`.sh`/`.py` in `scripts/`, `shared/hooks/`, `shared/skills/hub/scripts/`, `mcp/`) | **43.5k** lines (telemetry 12.3k, hub drain 12.7k, hooks 7.5k, scripts 9.9k, mcp 0.3k) | **≤ 2.5k** (planned ≈ 1.6k, §3) |
| Config (yml/json/yaml) | 1.6k (`ai-toolkit.yml` 283, otelcol 219, dashboards 442, settings 646) | ≤ 0.4k |
| Policy content (rules, skills, agents, prompts, metadata) | ≈ 13.3k | ≤ 8k after pruning (§2) |
| Tests | **84.9k** lines, 147 files, 10–33 min gates, xdist-unsafe | **≤ 3k**, ~12 files, < 60 s, `-n auto`-safe, plus one e2e script |
| Docs | 4.5k | ≤ 0.8k (one architecture/runbook doc + frontmatter reference) |

## 2. Feature inventory and verdict

Verdicts: **ORCA** (Orca owns it; primitive cited) · **POLICY** (kept as content) · **GLUE** (new minimal v2 code) · **DROP**.

### 2.1 Execution: worktrees, dispatch, launch, teardown (`scripts/`)

| Feature | Current files (~lines) | Verdict | Justification |
|---|---|---|---|
| Worktree create / path / branch | `worktree-new.sh` (469), `worktree-lib.sh` (727) | **ORCA** | `orca orchestration worker-start --worktree new-top-level --name --repo --base-branch` creates it agent-first and runs setup [L help]; `worktree list/show/rm` own the rest [L]. |
| Branch rename to `<type>/<n>-<slug>` | `worktree-new.sh:229` | **DROP** | Nothing in v2 reads the type; Orca's `<n>-<slug>` branch (branchPrefix `none` [L]) carries the issue number. Open question Q3. |
| Provisioning (`.claude/` copy, excludes, identity, task.md, ledger skeleton, allowlist, venv, testmon baseline) | `provision-worktree.sh` (612) | **GLUE** `setup.sh` (80) | Orca triggers it (`orca.yaml scripts.setup`, `wait-for-setup` holds the agent [S #1–3]). Keeps: `.claude/` copy, excludes, `spoke-run-id`, `task.md`, host hook. Drops: allowlist (spokes run `--dangerously-skip-permissions`, Orca default [L]), ledger skeleton, testmon, identity record (Orca `linkedIssue` + `issue:N` selector [S #15]). |
| Identity record + reader | `.ai-toolkit/identity`, `hooks/lib/identity.sh` (110) | **ORCA** | `worktree set --issue N`, selector `issue:N`, `ORCA_WORKTREE_ID` [S #15, L]. |
| Agent launch, model/effort, seed prompt | `worktree-new.sh:353–383`, `spoke-model.env` | **ORCA** | `worker-start --agent claude --model <id> --effort <level> --spec "<seed>"` [L help; "exits 0 only for ready"]. |
| OTel env on the launch command | `worktree-new.sh:285–293`, `worktree-lib.sh` otel pairs | **GLUE** `bin/claude-spoke` (30) | A PATH shim exports the pairs + `OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=…` from `.ai-toolkit/spoke-run-id` and `exec`s claude. Static pairs may also go in Orca `agentDefaultEnv.claude.*` (key exists for goose [L]) [verify]. Fallback: the verified two-step `terminal create --command` + `worker-start --terminal` [S #363]. Q1. |
| Orca CLI wrapper (settled calls, 30-s drop, tick cache, worker/inbox readers) | `orca-lib.sh` (276) | **GLUE** `lib.sh` (≤100) | Keep only `orca_json` + the settled-mutation retry (`--retry-request`, 03 #12). The drain's cached readers go with the drain. |
| Teardown + branch prune | `worktree-done.sh` (195) | **ORCA** | `orca worktree rm --worktree issue:N --run-hooks` deletes the checkout and local branch [L, S #13]; `land.sh` deletes the remote branch after FF. |
| Archive hook (raw-bodies spool) | `archive-worktree.sh` (111) | **GLUE** `archive.sh` (20) | No raw bodies in v2 (§5); the hook only posts `outcome=abandoned` when a worktree dies un-landed. |
| `/quick` worktree + hub-guard-allow marker | `worktree-quick.sh` (125) | **ORCA** | `orca worktree create --name quick-<slug> --agent claude --prompt` [L]; the Orca UI does the same. |
| Spoke push wrapper | `spoke-push.sh` (186) | **DROP** | Existed for the Bash allowlist (#37/#45); bypass mode has no prompts. Workers run `git push -u origin <branch>`. |
| Marker tags `ready/gate/accept/blocked`, PLAN-gate ask glue, queued subtasks, wake spool | `spoke-ready.sh` (688) | **ORCA** | `orchestration ask/reply` for the gate [S #17], `send --type worker_done --outcome` for completion [S #365 live], `check --wait` for wake [L]. STATUS: the stale `ready/376` tag and the `dispatch_capability_invalid` failures of all 8 workers were costs of wrapping Orca's verbs; v2 workers run the preamble's `ask`/`worker_done` verbatim. |
| Land (lock, guards, CI wait, sync-with-main, FF, push, ingest, release, close, labels) | `worktree-land.sh` (937) | **GLUE** `land.sh` (150) | Orca has no land [02 §1]. Keep: CI-green check, merge-main-on-spoke-then-FF, close, `worker-release`, `worktree rm`. Drop: 120-line mkdir lock (one coordinator lands), marker consumption, packed-issue scanning, lifecycle labels, telemetry ingest. |
| GitHub lifecycle labels / dispatch comment | `worktree-gh-lib.sh` (234) | **ORCA** | `worktree set --workspace-status in-progress\|in-review\|completed --comment` is the status surface [L]. Keep only `blocked`/`hold`/`priority` labels. |
| Post-land sweep, test budgets, testmon venv | `gate-sweep.sh` (489), `test-budget-watch.sh` (231), `ensure-test-venv.sh` (91), `test-run.sh` (69) | **DROP** | CI is the gate (05 option B, decided); no pruned local tiers to backstop. |
| Native git hook installer | `install-git-hooks.sh` (355) | **GLUE** `install.sh` (40) | Installs 2 tiny native hooks (§2.3), parent trust (03 Q1), `orca skills install`, zshrc tmux check. |
| OTel collector/bridge lifecycle + watchdog | `worktree-otel-lib.sh` (356), `hub-otel-watch.sh` (275) | **GLUE** `otel.sh` (40) | `docker compose up -d` for the collector; the coordinator checks it at start. No bridge, no watchdog. |
| Land-time Langfuse ingest | `telemetry-ingest-spoke.sh` (353) | **GLUE** `scores.py` (120) | See §5. |
| Global on/off switch | `ai-toolkit` (24), `lib/enabled.sh` (247) | **DROP** | No local gates to switch off. |
| Cursor plugin build, rule listing | `build-cursor-plugin.sh` (240), `list-cursor-rules.sh` (115), `dist/` | **DROP** | Q4. |
| User install | `install.sh` (81, broken path bug) | **GLUE** `install.sh` | Merged with the hook installer above. |

### 2.2 Unattended control plane (`shared/skills/hub/scripts/`, 12.7k lines)

| Feature | Current files (~lines) | Verdict | Justification |
|---|---|---|---|
| Supervisor tick, arm/off/status, state dir, heartbeat, self-copy re-exec, self-update | `hub-afk.sh` (891), `hub-afk-arm.sh` (419), `hub-afk-supervise.sh` (581), `hub-afk-state.sh` (276) | **GLUE** `coordinator.sh` (200) | One foreground loop in an Orca terminal on the main worktree, bound to one Run (`run-create`/`run-use` [L]); `check --wait` is the tick [L]. STATUS lessons kept: never release a settled worker without a next owner; re-bind the Run after a restart (`consumer_fenced`, LESSONS 3). |
| Batch planning (graphql, critical path, scope packing, merge lint) | `batch-plan.sh` (1033) | **GLUE** `dispatch.sh --next` (≤60 of its 120) | Ready = open, not `hold`, all blocked-by closed, `Scope:` disjoint from in-flight (`orca worktree list` linkedIssues [L]); `priority` label first, then number. No makespan ranking, no packed chains. |
| Dispatch + failure counts | `hub-afk-dispatch.sh` (513) | **GLUE** `dispatch.sh` | `worker-start` exits non-zero with `failedStage`/`residualResources` [L help]; one retry, then `blocked`. |
| State classification (`slot_state`), pane scrape, transcript parsing | `gate-broker-detect.sh` (465), `gate-broker-classify.sh` (710), `hub-inject.sh` (246) | **ORCA** | `worktree ps .agents[].state` = working/waiting/done [S #8]; `worker-list projection.liveness` = live/unverifiable/exited [L]; inbox `question` = parked on a gate [S #17]. |
| Answer injection, nudges, conflict lane | `hub-inject.sh`, `gate-broker-markers.sh` (756) | **ORCA** | `orchestration reply --id` (ack is the proof [S #20]); `terminal send --wait-submit` for a permission dialog [S #7, #9, #21]. |
| Answerer (headless reasoner, read-only fingerprint, decision journal, QCM, fast-path) | `gate-broker.sh` (791), `gate-broker-answerer.sh` (1057) | **GLUE** `answer.sh` (50) + **POLICY** `afk-answering` rule | `claude -p --model $ANSWER_MODEL --append-system-prompt-file <rule> --allowedTools Read,Grep,Glob` in the worktree; the `ANSWER:` line is replied. Journal = Langfuse score `answer_class`. Fast-path, QCM, permission-dialog branches dropped (bypass mode; deny `AskUserQuestion` in spoke settings). |
| Permission auto-approve + danger wall (tier-3 judge, cache, poison) | `gate-broker-permission.sh` (517), `gate-broker-danger.sh` (722), `afk-permission-hook.sh` (59), `afk-danger-guard.sh` (50) | **DROP** (+ `danger-guard.sh` 60) | User decision 2026-10-02: no extra permission wall for Orca spokes. A deterministic deny-list hook replaces the LLM judge (memory: judge outage denied the whole control plane). |
| Recovery: respawn, revive, reap, dead-pane | `hub-afk-recover.sh` (1055) | **ORCA** | `worker-start --task T --retry-of D` re-seeds the spec [S #19]; `worker-stop/abandon/release` [L]. Dead-pane false fires (#290) have no equivalent: liveness is Orca's verdict. |
| Auto-land | `hub-afk-land.sh` (788) | **GLUE** | Folded into `coordinator.sh` → `land.sh` on `worker_done succeeded`. |
| Watchdog tier 2, forensics, caffeinate | deleted in #372; `caffeinate` remnants | **ORCA** | `keepComputerAwakeWhileAgentsRun=true` [L settings]. |
| Hub status survey, tmux jump, ledger probe | `hub-status.sh` (558) | **ORCA** | `orca worktree ps` [L] + `gh issue list`; the `/hub` skill prints both (≤60 lines of policy). |
| Hub-side agent runs | `hub-agent.sh` (255) | **ORCA** | `worker-start --worktree current --agent claude --spec` [L help: "a fresh agent terminal is created"]. |
| Notifications, ready-watch loop | `hub-notify.sh` (352), `hub-ready-watch.sh` (153) | **ORCA** | `notifications.agentTaskComplete=true`, `terminalBell` [L settings]; the coordinator adds one `osascript` line for a blocked issue. |
| Transition log | `transition-log.sh` (290) | **DROP** | Orca's orchestration DB records dispatch/ask/reply/worker_done; Langfuse scores record outcomes. |
| Travel / remote | deleted (#371) | **ORCA** | `orca serve --mobile-pairing` [L help] [verify the reply-from-phone UX]. |

### 2.3 Hooks (`shared/hooks/`, 31 hooks + lib, 7.5k lines)

| Feature | Current files (~lines) | Verdict | Justification |
|---|---|---|---|
| hub-guard | 158 | **GLUE** native `pre-commit` (15) | "No commits on the base branch in the main checkout" is a 10-line git hook; works for humans and every tool. |
| spoke-main-guard, push-scope-guard, git-push-review, block-no-verify | 295 + 513 + 60 + 84 | **GLUE** `push-guard.sh` (40) | One PreToolUse hook: deny `git push` to the base branch, `--force*`, `--no-verify`, `git checkout <base>` from a non-main worktree. Scope is enforced at dispatch only (memory #356). |
| rm/chmod scope guards, scope-guard lib, config-protection, afk-danger-guard | 248 + 315 + 152 + 105 + 50 | **GLUE** `danger-guard.sh` (60) | Deny-list: `rm -rf` outside the worktree, `git reset --hard` in the main checkout, writes to `.github/workflows/`, `~/.claude/settings.json`, `orca.yaml`. No auto-allow logic (no prompts in bypass mode). |
| secrets-scan, secrets-scan-revert | 82 + 101 | **GLUE** `secrets-scan.sh` (40) | Keep the pre-write block; drop the post-edit revert. |
| commit-quality, commit-gauntlet | 268 + 291 | **GLUE** native `commit-msg` (25) | Conventional type + `#N` anchor regex. Lint/typecheck move to CI. |
| red-proof-verify/warn, reviewer-sep-warn, review-stamp-guard, review-window-open/close | 112 + 148 + 135 + 95 + 55 + 76 | **DROP** | TDD and review become policy + coordinator-side independent review + CI (§4). HMAC stamps never escaped the same-user ceiling (`code-review.md:135`). |
| plan-gate-guard | 107 | **DROP** | The gate is `orchestration ask`; measured by Langfuse `gate_wait_s`. Q2. |
| ledger-schema-guard, todo-ledger-warn/nudge, cycle-step-mark, delegation-gate-warn | 71 + 96 + 36 + 153 + 183 | **DROP** | Ledger existed to stamp cycle steps for the custom trace builder, which is deleted. |
| post-edit-format, quality-gate, console-log-warn | 93 + 114 + 79 | **DROP** | CI lint (`ruff`, `shellcheck`); policy says format before commit. |
| afk-permission-hook, afk-notify-wake, parent-span-export | 59 + 93 + 75 | **DROP** | Bypass mode; Orca inbox wake; native OTel parents spans itself. |
| test-select, test-reverse-index, anti-gutting-scan, gate-stamp/tripwire | 418 + 229 + 189 + 50 (+ ~300 in utils) | **DROP** | CI gate. Test gutting is a named check in the reviewer brief. |
| utils.sh, telemetry.sh (hook spans), base-branch.sh | 1110 + 637 + 57 | **DROP** | v2 hooks are self-contained; native OTel already emits `claude_code.hook` spans. |
| Hook config generation + reconciliation, per-platform hook metadata | `hooks_generator.py` (411), `hooks_reconciler.py` (219), `hooks/metadata.yml` (440) | **DROP** | `settings/claude/settings.json` is a static file the sync copies. Hooks are Claude-only in v2 (Q4). |

### 2.4 Policy content, sync, config, telemetry, tests

| Feature | Current (~lines) | Verdict | Justification |
|---|---|---|---|
| Rules (19) | 2481 | **POLICY** | Keep as-is: guidelines, security, code-quality, python-style, pytest-conventions, markdown, mermaid, gitignore, github-actions, library-research, bug-triage, issue-hygiene (Scope:/Gate:/Model: footers stay the dispatch contract), agent-orchestration, scientific-integrity. Rewrite: `workflow` (224→≤100), `planning-hub` (94→≤50), `afk-answering` (182→≤120: drop fast-path/QCM/reaper sections), `operational-gotchas` (prune tmux/gate items). Fold `afk-design-principles` (80) into a 30-line "coordinator principles" section of `workflow`. |
| Skills (34) | SKILL.md 5.7k + refs 3.5k | **POLICY** | Keep generic skills unchanged. Rewrite to Orca verbs: `solo-cycle` (380→≤120), `hub` (208→≤60), `afk` (171→≤40), `land` (116→≤30), `start-task` (215→≤60), `quick` (97→≤30), `tdd-workflow`, `git-commit`. **DROP**: `source-task` (task.md is on disk), `next-batch` (coordinator), `verify-agents/rules/skills` (replaced by the policy-lint test), `bootstrap-test-suite` (testmon gone), the `hub/scripts/` dir. Keep `langfuse` + `github-issues` refs. |
| Agents (13) | 1542 | **POLICY** | Keep all. `code-review` (159→≤110): replace the `approve_review` MCP section with the JSON verdict contract (§4). Models declared in each agent's frontmatter (Claude 5 family, pinned by one test). |
| Prompts | 43 | **POLICY** | Keep. |
| Review-stamp MCP server | `mcp/review-stamp/` (269), `.review/` | **DROP** | Review authority moves to the coordinator (§4). |
| Sync pipeline | `sync-to-repo.sh` (1029), `metadata_parser.py` (330), `sync_manifest.py` (157), `metadata.yml` ×5 (995), docs | **GLUE** `sync.sh` (200) + `frontmatter.py` (120) | §6. |
| Declarative config + accessor | `settings/ai-toolkit.yml` (283), `ai_toolkit_config.py` (741) | **GLUE** `settings/ai-toolkit.env` (25) | Flat `KEY=value`, sourced by bash, read by Python with 5 lines. Models, base branch, check command, cap, Langfuse public settings. |
| IDE settings (Cursor/VS Code MCP lists, copilot settings) | `settings/{cursor,vscode}/` (359) | **DROP** from this repo | Personal machine config; move to dotfiles. Q8. |
| Telemetry package, bridge, context measurement, dashboards | `scripts/telemetry/` (12.3k), `dashboard/langfuse/` (661) | **GLUE** `langfuse/otelcol.yaml` (80) + `scores.py` (120) + 2 dashboards | §5. |
| CI | `ci.yml` (~220) | **GLUE** (60) | pytest `-n auto`, shellcheck, sync idempotency, macOS job on the same tiny suite; drop the red-issue filer and the fr_FR subset. |
| Tests | 84.9k, 147 files | **GLUE** ≤ 3k | §7. |

## 3. v2 architecture

```text
ai-toolkit/                                     budget (lines)   role
├── orca.yaml                                        6            wait-for-setup; setup → scripts/setup.sh; archive → scripts/archive.sh
├── settings/ai-toolkit.env                         25            SPOKE_MODEL/EFFORT, REVIEW_MODEL, ANSWER_MODEL, BASE_BRANCH, CHECK_CMD,
│                                                                 CONCURRENCY_CAP, GATE_DEFAULT, LANGFUSE_HOST/PUBLIC_KEY, OTEL_ENDPOINT
├── settings/claude/settings.json                   40            hook registrations, deny AskUserQuestion, model/effort defaults (synced as .claude/settings.json)
├── bin/claude-spoke                                30            PATH shim: OTel env + spoke_run_id resource attr, then exec claude
├── scripts/
│   ├── lib.sh                                     100            die/warn, load env, orca_json + settled retry, gh helpers, issue footer parsing
│   ├── setup.sh                                    80            Orca setup hook: copy .claude/ from ORCA_ROOT_PATH, excludes, spoke-run-id, task.md, host hook
│   ├── archive.sh                                  20            Orca archive hook: outcome=abandoned score when removed un-landed
│   ├── dispatch.sh                                120            issue → worker-start (+ worktree set --issue/status); --next picks the next ready issue
│   ├── land.sh                                    150            review → CI green → merge main on spoke → FF → push → close → release → worktree rm
│   ├── review.sh                                   60            independent pre-land review: claude -p --agent code-review → JSON verdict
│   ├── answer.sh                                   50            afk answerer: claude -p + afk-answering rule, read-only → ANSWER line
│   ├── coordinator.sh                             200            the Run loop (§4); --answer auto|human, --until, --drain, --cap
│   ├── sync.sh                                    200            shared/ → .claude/.cursor/.github + .ai-toolkit/{scripts,bin,hooks} + orca.yaml + manifest GC
│   ├── frontmatter.py                             120            per-platform frontmatter projection (stdlib)
│   ├── scores.py                                  120            Langfuse session scores + one lifecycle span per event (stdlib urllib)
│   ├── otel.sh                                     40            collector up/down/status (docker compose)
│   └── install.sh                                  40            hooksPath, parent trust, orca skills, shim on PATH, zshrc check
├── hooks/claude/{push-guard,danger-guard,secrets-scan}.sh   140  the three Claude PreToolUse hooks
├── hooks/git/{commit-msg,pre-commit}                40           native hooks (any tool, any human)
├── langfuse/{otelcol.yaml,compose.yaml,dashboards/*.json}  220   config, not code
├── shared/{rules,skills,agents,prompts}/           ≤ 8k          policy content; frontmatter lives in each file (§6)
├── tests/ (≤ 3k) + e2e/spoke-scenario.sh (150)                   §7
├── .github/workflows/ci.yml                        60
└── docs/{v2-architecture.md, frontmatter.md}      ≤ 800
```

**Budget: 2,500 glue lines hard cap (planned ≈ 1,640), config ≤ 400.** Why this number and not 15k: every line of today's
43k exists to (a) drive tmux and prove delivery (gone: Orca `send`/`ask`/`worker_done` are acknowledged), (b) classify
agent state from panes and transcripts (gone: `worktree ps`, `worker-list`), (c) prove tests locally under load (gone: CI
gate, decided), (d) rebuild traces from transcripts and raw bodies (gone: native OTel + scores), (e) sign reviews and
guard the signature (gone: structural separation), or (f) generate hook configs per platform (gone: one static file). The
remaining irreducible work is: compose one `worker-start`, land one branch, run one inbox loop, project frontmatter, post
scores. None of those exceeds 200 lines; the cap leaves ~850 lines for the surprises the e2e scenario will surface.

## 4. The spoke lifecycle in v2

| Step | What happens | Orca primitive / GLUE file | Evidence |
|---|---|---|---|
| 1. Issue | `gh issue create` with `Scope:` / `Gate: plan\|none` / optional `Model:` footer; `priority`, `hold` labels | POLICY `issue-hygiene`, `start-task` | unchanged contract |
| 2. Pick | ready = open, not `hold`, blocked-by all closed, `Scope:` disjoint from `orca worktree list` linked issues; `priority` first | `dispatch.sh --next` | replaces `batch-plan.sh` |
| 3. Dispatch | `worker-start --run $RUN --worktree new-top-level --repo path:$MAIN --name <n>-<slug> --agent claude --model $M --effort $E --task-title "#n title" --spec "$SEED" --json`; then `worktree set --worktree path:<p> --issue n --workspace-status in-progress`; `scores.py dispatched` | `dispatch.sh` | `worker-start` flags [L]; `--issue` only on `worktree set` [L] |
| 4. Setup | Orca runs `scripts/setup.sh` in the new worktree before the agent starts: `.claude/` from `$ORCA_ROOT_PATH`, info/exclude, `.ai-toolkit/spoke-run-id`, `task.md` from `gh issue view`, optional `.ai-toolkit/setup.local.sh` (host venv etc.). Non-zero = agent never starts | `orca.yaml` `wait-for-setup` | [S #1–3] |
| 5. Launch | Orca launches `claude` (first on PATH = `bin/claude-spoke` → OTel env) with `agentDefaultArgs.claude = --dangerously-skip-permissions` [L]; preamble + seed injected by `worker-start` | ORCA | Q1 for the shim; two-step fallback [S #363] |
| 6. Seed | "Read `.ai-toolkit/task.md`. Gate: plan → explore, write the plan, then run your preamble's `ask` with the plan as the question and options `approve,revise`; do not edit code before `approve`. Cycle per subtask: RED → GREEN → REFACTOR → in-spoke `code-review` → `git push -u origin <branch>`. When the acceptance criteria hold: push, then `worker_done --outcome succeeded` with a 3-sentence summary. Never merge or push the base branch." | `dispatch.sh` (string) + POLICY `solo-cycle` | `ask` is the preamble's own command: no capability plumbing (STATUS 10:45 lesson) |
| 7. PLAN gate | Worker blocks in `orchestration ask`; coordinator `check --wait --types question` → `--answer auto`: `answer.sh` (rule-driven, read-only, in the worktree) → `reply --id --body "approve"` or `"revise: …"` → `check --ack`. `--answer human`: desktop notification + issue comment; the human replies with `orca orchestration reply` (or `/reply` in the hub session). `scores.py gate` (wait seconds, answer class) | `coordinator.sh`, `answer.sh` | ask/reply/ack [S #17, #20]; a parked gate reads from the inbox, not agent state |
| 8. TDD cycle | Policy only inside the spoke (skills + rules + in-spoke `code-review` subagent, advisory). Enforced later by (a) CI green on the branch and (b) the independent review's `tdd_followed` / `tests_weakened` fields, which block the land | POLICY | no gauntlet, no ledger, no red-proof hooks |
| 9. Push | Plain `git push -u origin <branch>` per subtask; `push-guard.sh` denies base-branch/force/`--no-verify`; `commit-msg` checks the format | hooks | bypass mode, no allowlist |
| 10. Done | Worker sends `worker_done --outcome succeeded`; Orca settles the Task/Dispatch; `worktree set --workspace-status in-review` | ORCA | [S #365 live proof] |
| 11. Independent review | Coordinator runs `review.sh <n>`: `claude -p --model $REVIEW_MODEL --agent code-review` on `origin/main...<branch>` in the worktree, read-only; the agent's final message is one JSON object `{verdict, blockers, warnings, tdd_followed, tests_weakened, summary}`. REQUEST_CHANGES → `worker-start --task T --terminal <h> --spec "address: …"` (reuse after settlement [L coordinator-loop]), max 2 rounds, then `blocked`. `scores.py review` | `review.sh`, `coordinator.sh` | STATUS 2026-10-03 "Fable for judgment": a different model than the worker, structurally separate; replaces HMAC stamps |
| 12. Tested | CI must be green for the exact tip: `gh run list --commit <sha> --json status,conclusion,url` polled with a bound [L gh flags]. If main moved: `git -C <wt> merge --no-edit origin/main` → push → wait again (conflict → one re-dispatch round "merge main, resolve, push"). **Before cutover** (no CI on `v2`): `land.sh --local-gate` runs `$CHECK_CMD` on the merged tip. **Recommendation: CI returns at cutover** (ci.yml on every branch; the e2e repo is public, minutes are free) | `land.sh` | 05 option B decided; STATUS: local full suites cost 5–33 min and flaked under load |
| 13. Land | Hub: `git merge --ff-only <branch>` → `git push origin main` → `gh issue close -c "landed in <sha>"` → `git push origin --delete <branch>` → `worker-release --dispatch` → `orca worktree rm --worktree issue:n --run-hooks --json` → `scores.py landed` | `land.sh` | Orca never closes issues (STATUS 09:51); `worktree rm` deletes the local branch [S #13] |
| 14. Teardown | Orca removes the checkout and closes terminals; archive hook posts `abandoned` only when removal happens without a land | ORCA + `archive.sh` | [S #10–13] |
| 15. Trace | Native OTel traces grouped by `spoke_run_id` session + ~10 session scores | `claude-spoke`, `otelcol.yaml`, `scores.py` | §5 |

**The coordinator (replaces the 4k-line `/afk` drain + 7.5k of gate-broker).** `coordinator.sh` runs as a foreground
process in an Orca terminal on the main worktree (`orca terminal create --worktree path:$MAIN --title coordinator
--command "scripts/coordinator.sh --answer auto --until 23:00"`), so it is visible in the Orca UI, has
`ORCA_TERMINAL_HANDLE`, and is the Run's single consumer. Loop: (1) fill slots up to `CONCURRENCY_CAP` with
`dispatch.sh --next`; (2) `check --wait --types question,worker_done,escalation --timeout-ms 300000`; (3) per message:
`question` → answer/notify; `worker_done succeeded` → `land.sh n` (inline, so lands are serialized by construction);
`worker_done failed`/`escalation` → label `blocked`, comment, notify, `worker-release`; (4) `check --ack`; (5) every
10th wait with no mail: `worker-list` sweep — `exited` without `worker_done` → one `worker-start --retry-of`, then
`blocked`; (6) stop at `--until`, or in `--drain` when the backlog is empty and no worker is live. No state files: a
restart re-binds the Run (`run-use`) and re-derives everything from `worker-list`, `worktree list`, and labels.
Two optional Orca automations [L CLI]: weekly `--prompt "evaluate last week's Langfuse scores and file ≤3 workflow
issues" --workspace <main> --provider claude`, and hourly `--precheck "scripts/coordinator.sh --status"` to restart a dead
coordinator while the backlog is non-empty. Neither is required for the e2e scenario.

## 5. Langfuse in v2

| Question | Custom parser (today, 12.3k lines) | Native OTel + scores (v2, ~220 lines + 80 config) |
|---|---|---|
| Per-turn / tool / sub-agent / hook spans, tokens, cost | rebuilt from Langfuse observations + transcripts | native export (`claude_code.*` spans); collector renames to `tool:X`, `hook:X`, `sub-agent:X`, remaps tokens so Langfuse prices them (kept OTTL, ~60 lines) |
| One session per spoke across resumes | `spoke_run_id` minted at spawn | same id, minted by `setup.sh`, set as `langfuse.session.id` by the collector (kept) |
| Cycle-step spans (`step:RED`…), ledger windows, per-step cost | 1,800-line `scores.py` + ledger hooks | **dropped**: steps are not re-derived; per-step cost was the main consumer of the ledger machinery |
| Loaded-context itemization, raw request bodies, message bridge | 745 + 949 + 555 lines, docker sidecar | **dropped**: a one-off measurement; re-run ad hoc if a question needs it |
| Lifecycle timeline + outcome | lifecycle.json gathered at land | `scores.py` posts **session scores** (`POST /api/public/scores` with `sessionId` [L OpenAPI `ScoreBody`]) from the coordinator at each event |
| Dashboards | 4 JSON files keyed on deleted span names | 2 files: "spoke outcomes" (scores) and "spoke cost/latency" (native spans by `name`, `providedModelName`) |

**Scores that matter for "measure and improve the workflow"** (all numeric, session-scoped, idempotent ids =
`<spoke_run_id>:<name>`): `outcome` (1 landed / 0 blocked / -1 abandoned), `dispatch_to_done_s`, `gate_wait_s`,
`gate_answer_class` (0 human / 1 auto-approve / 2 revise), `review_verdict` (1/0), `review_rounds`, `tdd_followed`,
`tests_weakened`, `ci_green_first_try`, `land_s`, `lines_changed` (from `git diff --shortstat`), and `cost_per_line`
derived in a dashboard from Langfuse's own session cost. Spans: one `lifecycle:<event>` span per event (same OTLP
endpoint, 20 lines) so the timeline is visible next to the native trace. Budget: `scores.py` 120, `otelcol.yaml` 80,
`compose.yaml` 20, dashboards ~120 (config). Prerequisite [verify]: Langfuse creates the session on the first native span,
so `scores.py dispatched` runs after `worker-start` returns ready (first turn observed).

## 6. Downstream sync (`hex` today; `hex` must keep working after cutover)

| Keep | Shrink / change |
|---|---|
| Projection of rules/skills/agents/prompts to `.claude/`, `.cursor/`, `.github/` with per-platform frontmatter; `CLAUDE.md` / `copilot-instructions.md` from `guidelines.md`; manifest-based GC of files a previous sync wrote; `--local-only` excludes | **Frontmatter moves into each source file** (standard SKILL.md/agent frontmatter, flat keys; a platform override is a prefixed key, e.g. `cursor_description:`). `frontmatter.py` holds one projection table (fields per platform) and a 20-line flat-YAML reader. Deletes `metadata.yml` ×5 and `metadata_parser.py`. |
| Copy of the lifecycle into the target: `.ai-toolkit/{scripts,bin,hooks}`, `orca.yaml` (setup → `.ai-toolkit/scripts/setup.sh`), `.claude/settings.json` | Static `settings/claude/settings.json` copied as-is (hook paths relative to `$CLAUDE_PROJECT_DIR`); no generator/reconciler. Hooks are not synced to Cursor/Copilot (Q4). |
| Idempotency (CI asserts a second sync produces no drift) | No `git config ai-toolkit.*` materialization: `settings/ai-toolkit.env` is copied to `.ai-toolkit/ai-toolkit.env` and the target overrides with `.ai-toolkit/ai-toolkit.local.env` (gitignored). No model stamping: agents carry `model:` themselves. |
| Downstream prerequisites | `orca repo add <path>`, `install.sh` once per machine, `AI_TOOLKIT_BASE_BRANCH`/`CHECK_CMD` in the local env file. |

## 7. Test strategy for v2

Principles (from #374/#375/#377): no wall-clock assertions, no real `orca`/`gh`/`claude`/`docker` in unit tests (PATH stubs
that record argv and replay JSON, reusing the design of today's `tests/unit/_orca_stub.py`), one temp git repo with a bare
`origin` per test, env isolation (`GIT_*`, `OTEL_*`, `ORCA_*` stripped), every test `-n auto`-safe, whole suite < 60 s.

| File (~lines) | What it pins |
|---|---|
| `conftest.py` (120) | stubs on PATH, temp repo + origin fixture, env strip |
| `test_dispatch.py` (250) | slug/model/gate derivation from an issue; exact `worker-start` argv; `--next` selection (hold, priority, blocked-by, Scope overlap) |
| `test_land.py` (350) | FF land order (review → CI → merge → push → close → release → rm); main moved → merge on spoke; conflict → exit code; CI red → nothing merged; `--local-gate` runs `CHECK_CMD`; refuses outside the main worktree |
| `test_coordinator.py` (300) | message routing (question → answer → reply → ack; worker_done → land; failed → blocked), bounded review rounds, liveness retry once, stop conditions; ticks injected, no sleeps |
| `test_setup.py` (150) | idempotent provisioning; fails loud when `.claude/` is missing in the root; task.md from a stubbed `gh` |
| `test_hooks.py` (250) | deny/allow matrix for the 3 Claude hooks (JSON on stdin) and the 2 native hooks |
| `test_sync.py` (300) | per-platform frontmatter projection; run-twice no drift; manifest GC; `orca.yaml` + `.ai-toolkit/` layout in a target |
| `test_scores.py` (120) | payload shape, idempotent score ids, no secret in argv/logs |
| `test_policy_lint.py` (150) | every skill/agent has frontmatter + description; agent `model:` ∈ allowed Claude 5 ids (no `[1m]`); no file references a deleted mechanism (`spoke-ready`, `review-stamp`, `tmux`, `gate/`, `.review/`, `hub-afk`) |

**End-to-end acceptance script `e2e/spoke-scenario.sh`** (real Orca, real Claude, a throwaway public GitHub repo
`ai-toolkit-e2e` so `gh` and CI are real; runnable from the WP0 skeleton, then after every WP):

1. Preflight: `orca status` ready; repo registered; `install.sh` done; Langfuse optional (`LANGFUSE_SECRET_KEY` unset ⇒ score asserts skipped).
2. `sync.sh` into the e2e repo; commit; file issue A "add `hello.py` + test" with `Gate: plan`, `Scope: hello.py tests/`.
3. Start `coordinator.sh --answer auto --cap 1 --until +40m` in an Orca terminal on the e2e main worktree.
4. Within 3 min: `orca worktree show --worktree issue:A` exists, agent `working`, `.claude/hooks` present (setup ran), `spoke-run-id` written.
5. Within 10 min: one `question` arrived and was replied `approve` (`orchestration inbox --json`); `gate_wait_s` posted.
6. Within 25 min: `worker_done succeeded`; branch pushed; a test file exists in the diff; review verdict JSON = APPROVE.
7. Land: `origin/main` contains the commit; issue A closed; remote branch gone; `worktree list` has no `issue:A`; `worker-list --terminal-state active` empty.
8. Langfuse (if configured): session `spoke_run_id` has ≥ 1 trace; scores `outcome=1`, `review_verdict=1`, `dispatch_to_done_s` present.
9. Negative path (WP6): issue B asks for a change whose test must fail → `worker_done` + review REQUEST_CHANGES → 2 rounds → label `blocked`, worker released, worktree kept.
10. Cleanup: coordinator stopped, Run inert, no leftover terminal/worktree; total wall-clock < 45 min. Pass = all asserts; the script exits non-zero on the first failed assert and prints the Orca object ids for inspection.

## 8. Build plan (parallel Orca workers on `v2`, Sonnet 5.5 / high; Opus 5.5 reviews each WP before merge)

| WP | Scope / files | Budget (code + tests) | Depends on | Acceptance |
|---|---|---|---|---|
| **0 Skeleton** | tree, `settings/ai-toolkit.env`, `scripts/lib.sh`, `orca.yaml`, `setup.sh`, `archive.sh`, `bin/claude-spoke`, `install.sh`, `tests/conftest.py` + stubs, `e2e/spoke-scenario.sh` steps 1–4 | 300 + 200 | — | a worktree created by `orca worktree create --setup run` on a scratch repo is provisioned; e2e steps 1–4 pass with a hand-run `worker-start`; Q1 resolved (shim or two-step) |
| **1 Dispatch + land** | `dispatch.sh`, `land.sh`, `test_dispatch.py`, `test_land.py` | 270 + 600 | 0 | e2e steps 4, 6–7 with a human replying to the gate by hand (`orca orchestration reply`) |
| **2 Coordinator** | `coordinator.sh`, `answer.sh`, `review.sh`, `test_coordinator.py`; `code-review` agent JSON contract; `afk-answering` rewrite | 310 + 300 | 1 | full e2e (steps 1–8) green unattended |
| **3 Hooks + settings** | 3 Claude hooks, 2 native hooks, `settings/claude/settings.json`, `test_hooks.py` | 180 + 250 | 0 | matrix green; a spoke cannot push main / force / `--no-verify`; `pre-commit` blocks a commit on main in the main checkout |
| **4 Sync + policy** | `sync.sh`, `frontmatter.py`, manifest GC, `ci.yml`; frontmatter moved into source files; skills/rules pruned per §2.4; `test_sync.py`, `test_policy_lint.py` | 320 + 450 (+ content) | 0 | sync twice into a temp repo = no drift; `hex` synced from `v2` on a branch and a Claude session there loads rules/skills; policy lint green |
| **5 Telemetry** | `langfuse/otelcol.yaml`, `compose.yaml`, `otel.sh`, `scores.py`, 2 dashboards, `test_scores.py` | 180 + 120 | 0 | a shim-launched `claude -p` produces a Langfuse session; a posted session score is visible |
| **6 E2E + cutover** | e2e steps 5–10 incl. negative path; `docs/v2-architecture.md` (runbook); deletion list applied; cutover rehearsal on `hex` | 150 + docs | 1–5 | e2e green 3× in a row (1 attended, 2 unattended); WP review APPROVE on every package |

Order: WP0 (serial) → WP1, WP3, WP4, WP5 in parallel → WP2 → WP6. The coordinator for this build is the user's own Orca
session (`worker-start` per WP, `ask` for decisions), no ai-toolkit machinery, no CI until cutover.

**Cutover checklist.** (1) e2e 3× green on `v2`; (2) `hex` re-synced from `v2` and one real issue dispatched + landed there;
(3) `ci.yml` from `v2` becomes main's workflow; (4) single `--no-ff` merge `v2` → `main` whose deletion commit removes the
list below; (5) re-sync the hub (`sync.sh ~/Repos/ai-toolkit`), `install.sh`, re-import the 2 dashboards; (6) tag
`v1-final` before the merge and keep `docs/orca-migration/` on that tag only; (7) update the memory notes that name deleted
mechanisms; (8) `orca orchestration reset` of the inert spike Runs (optional, explicit scope).

**Deletion list at cutover.** `scripts/*` except the v2 files; `scripts/telemetry/` (all); `shared/hooks/` (all 31 +
`lib/`, replaced by `hooks/`); `shared/skills/hub/scripts/` (22 files); `shared/skills/{source-task,next-batch,
verify-agents,verify-rules,verify-skills,bootstrap-test-suite}`; `shared/*/metadata.yml`; `mcp/`; `dashboard/`; `dist/`;
`settings/ai-toolkit.yml`, `settings/{cursor,vscode}/`; `tests/` (all 147 files); `docs/` except the two v2 docs;
`.github/workflows/ci.yml` replaced; `requirements-dev.txt` → `pytest`, `pytest-xdist`, `pyyaml` only.

## 9. Risks and open questions

| # | Question / risk | Recommendation |
|---|---|---|
| Q1 | **OTel env injection.** Does Orca resolve `claude` through the login shell's PATH (so `bin/claude-spoke` is picked up by `worker-start --agent claude`), and does `agentDefaultEnv.claude.*` exist for Claude as it does for goose [L]? | WP0 spike (30 min). Prefer the shim (one `worker-start`); fall back to the verified `terminal create --command` + `worker-start --terminal` two-step [S #363]. |
| Q2 | **PLAN gate enforcement.** Policy-only (worker must `ask` before coding) vs a 30-line hook denying Edit/Write until a gate file is cleared. | Policy + measurement first (`gate_wait_s` and a `gate_skipped` score when `worker_done` arrives with no prior question on a `Gate: plan` issue); add the hook only if the data shows skips. |
| Q3 | **Branch naming.** Drop the `<type>/` rename and use Orca's `<n>-<slug>` branch. | Drop it: nothing in v2 reads the type, and the rename was one more step between `worker-start` and a live agent. |
| Q4 | **Cursor/Copilot hooks and the Cursor plugin.** Keep syncing rules/skills/agents to `.cursor/` and `.github/`, but no hooks and no plugin build. | Yes. Execution is Orca + Claude Code only; the other platforms get content. |
| Q5 | **No local test tier at all.** Pre-push runs nothing; CI is the only test gate; `land.sh --local-gate` runs `CHECK_CMD` offline. | Accept. STATUS shows local tiers cost 5–33 min and were the flake source; the v2 suite itself runs in < 60 s anyway. |
| Q6 | **Independent review on every land** (Opus 5.5 by default, Fable optional) costs tokens per land. | Accept for now; it is the only enforced review in v2. Revisit with `review_verdict` data (if > 95% APPROVE on `Gate: none` issues, make it optional there). |
| Q7 | **Attended answers.** The human replies with `orca orchestration reply --id` from the hub session (a `/reply` skill wraps it); whether the Orca UI surfaces a worker's pending question is [verify]. | Ship the CLI path + desktop notification in WP2; check the UI during the e2e. Mobile replies via `orca serve --mobile-pairing` are a post-cutover spike. |
| Q8 | **Downstream and personal config.** `hex` needs Orca registration, the shim, and `CHECK_CMD`; the Cursor/VS Code MCP lists leave this repo. | Move IDE settings to dotfiles at cutover; WP6 rehearses the full `hex` path before the merge. Residual risk: Orca CLI churn (pin `ORCA_MIN_VERSION`, all calls in `lib.sh`, `--json` everywhere). |
