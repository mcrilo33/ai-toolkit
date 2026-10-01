# Orca migration — 02 Orca capabilities & best-of-both split (Phase 2a, read-only)

Date: 2026-10-01. Orca 1.4.218 (`orca` on PATH works again, see 01 §0). Read-only probe: nothing was
created, removed, or reconfigured in Orca, git, or `~/`. Collision numbers (#1–#9) refer to `01-inventory.md` §4.

**Sources:** `orca --help` (239 commands); the bundled skill guides `orca skills get orca-cli|orchestration|orca-per-workspace-env`
and the `automations` reference; live `orca repo list / worktree current / worktree ps / terminal list --json`; the Orca profile
settings (live store: the `settings` row of `~/Library/Application Support/Orca/profiles/local-default/profile-state.db`;
the sibling `orca-data.json` is a stale pre-migration copy, last written 13:49); the Orca-installed hooks in
`~/.claude/settings.json` and `~/.orca/agent-hooks/claude-hook.sh`; and code excerpts from `app.asar`.
**Confidence:** **[L]** observed live · **[D]** stated in Orca's bundled docs · **[C]** read from the app code, not exercised.

## 0. Headline findings

1. **Orca enforces nothing on agent behaviour.** Its Claude hooks (installed globally in `~/.claude/settings.json` for
   PreToolUse, PostToolUse, PermissionRequest, Stop, SessionStart, …) always print `{}` and only POST status to
   `127.0.0.1:$ORCA_AGENT_HOOK_PORT` **[L]**. It launches Claude with `--dangerously-skip-permissions` by default
   (`agentDefaultArgs.claude`) **[L]**. ⇒ All policy (gates, guards, review, scope) must stay ai-toolkit's. There is no competition here.
2. **Orca has the provisioning seam ai-toolkit lacks** (collision #2). `orca.yaml` at the repo root, or per-repo `hookSettings`,
   defines `scripts.setup` and `scripts.archive`. They run in `/bin/bash -lc` with `ORCA_ROOT_PATH` (main checkout), `ORCA_WORKTREE_PATH`
   and `ORCA_WORKSPACE_NAME` **[C]**. Setting **`setupAgentStartupPolicy: wait-for-setup`** holds the agent until setup finishes **[C]**,
   so hooks can be in place before the first tool call. The repo is currently set to `start-immediately` with empty scripts **[L]**.
3. **Orca records identity explicitly** (collision #3). Each worktree has a stable `identity.key`, `id = <repoId>::<path>`,
   `linkedIssue` (`worktree create --issue N`, selector `issue:N`), `workspaceStatus`, `comment`, and lineage **[L]**. Terminals get
   `ORCA_WORKTREE_ID`, `ORCA_PANE_KEY`, `ORCA_TAB_ID` and `ORCA_TERMINAL_HANDLE` **[C]**. That is the "one explicit record"
   ai-toolkit's afk-design principle #1 wants, so we don't need to invent it, only mirror it.
4. **Orca's terminal API is a better transport than tmux** (collision #4). `terminal send --enter --wait-submit N` returns a
   durable request id plus `input_accepted` → `turn_started` stages and supports `--retry-request` **[D]**. `terminal wait --for tui-idle|exit`
   **[D]**. Bounded cursor reads **[D]**. Fleet-level `worktree ps` reports `agents` and `status` **[L]**. These directly address the
   recurring /afk failure memories: unsubmitted answers, multiline Enter, answers that couldn't be proven consumed, pane-scrape flakes.
5. **Your shell currently blinds Orca.** `~/.zshrc:1-10` auto-starts tmux (`ZSH_TMUX_AUTOSTART=true` unless `TERM_PROGRAM=vscode`).
   Every Orca terminal therefore re-execs into a tmux server spawned by launchd, which drops all `ORCA_*` variables. In this
   session `env` has no `ORCA_*` and `TERM_PROGRAM=tmux`, and `orca worktree ps` shows `agents: []` for every worktree **[L]**.
   Orca's agent status, notifications and hook-based liveness are therefore off today. **Fixed 2026-10-01** for new terminals (§3.1).
   (Side effect worth knowing: it is also why ai-toolkit's tmux pane lookup would accidentally "work" inside Orca right now.)
6. **Two Orca defaults are hostile to ai-toolkit identity:**
   - `autoRenameBranchFromWork: true` **[L]** renames branches from the work done. Whether it touches explicitly named branches is
     **[unverified]**.
   - `branchPrefix: git-username` produces `mcrilo33/<name>` **[L]** instead of `<type>/<n>-<slug>`. That silently disables
     plan-gate, the afk hooks, in-flight detection and the commit-quality anchor (#3).

## 1. What Orca can do (relevant surface)

| Area | Capability | Key commands / settings | Conf. |
|---|---|---|---|
| Worktrees | Create with name, base, issue link, parent lineage, agent + prompt, setup run/skip/inherit; remove with archive hook | `worktree create --name --issue --base-branch --agent --prompt --setup`; `worktree rm --run-hooks [--allow-failed-archive-hook]`; `worktree set --comment --workspace-status`; `worktree ps` | L/D |
| Location | `workspaceDir/<repo>/<name>` = `~/orca/workspaces/ai-toolkit/<name>`; `isMainWorktree` flags the hub | settings `workspaceDir` | L |
| Repo hooks | `scripts.setup` (async, new tab by default, `setupScriptLaunchMode`), `scripts.archive` (sync, 120 s timeout; a failure aborts removal unless allowed); local vs `orca.yaml` source via `commandSourcePolicy` (`local-only`/`shared-only`/`run-both`) | `orca.yaml` keys: `scripts`, `setupAgentStartupPolicy`, `issueCommand`, `defaultTabs`, `environmentRecipes`, `worktree.sharedDirectories` | C |
| Terminals | list/show/read (cursor), send (durable, submit-proof), wait (`tui-idle`, `exit`), create with `--command`, split, close; handles are runtime-scoped (re-list after restart) | `terminal *` | D/L |
| Agent status | Hook-fed status per pane (Stop, PermissionRequest, Notification, Subagent*), hibernation (experimental, off) | `agentStatusHooksEnabled: true`, `worktree ps .agents` | L (blocked by #5) |
| Orchestration | Runs, Task DAGs, supervised workers (`worker-start` = placement + readiness + prompt injection), inbox/ask/reply, **decision gates**, `worker_done` with outcome, layered liveness (`live`/`unverifiable`/`exited`), `nestedWorkerMaxDepth: 1`; state in `orchestration.db` | `orchestration *` | D |
| Automations | Cron/RRULE-scheduled **agent prompts** in a new worktree per run or an existing workspace | `automations create --trigger --prompt --provider` | D |
| Notifications | Desktop notifications for agent-task-complete and terminal bell, suppressed when focused | settings `notifications` | L |
| Review/SCM | Diff view, open changed files, AI commit-message and PR generation (`commitMessageAi`, `sourceControlAi` enabled) | `file diff`, `file open-changed` | L |
| Other | Full-text agent-session search (needs a GUI toggle), managed Claude/Codex accounts, embedded browser, remote runtimes/VMs, Linear/GitHub task sources, git/gh attribution shim (`ORCA_GIT_COMMIT_TRAILER`, default state unverified) | `search`, `account`, `tab`, `environment` | D/C |

**What Orca does *not* have:** no merge/land concept, no test gate, no review authority (its diff view approves nothing), no
scope model, no backlog planner, no headless control-plane script loop (automations run an *agent*, not a script), and no notify CLI.

## 2. Best-of-both: who should own what

| Concern | Today (ai-toolkit) | Orca | **Keep** | Why |
|---|---|---|---|---|
| Rules, skills, agents, prompts | `shared/` + sync | — | **ai-toolkit** | Pure policy; Orca has only skill sharing |
| Agent-side gates (hub/spoke guards, plan-gate, scope, commit-quality, red-proof, secrets, danger wall) | Claude hooks + native git hooks | Observe-only hooks | **ai-toolkit** | Orca enforces nothing (finding 1) |
| Worktree create / location / remove | `worktree-new.sh`, `worktree-done.sh` | `worktree create/rm` + UI | **Orca** | Removes the two-owner hazard (#1); scripts call `orca worktree create/rm` instead of `git worktree add/remove` and `rm -rf` |
| Provisioning (`.claude/` copy, `.ai-toolkit/` state, info/exclude, testmon baseline, venv) | inline in `worktree-new.sh:241-565` | `scripts.setup` + `wait-for-setup` | **ai-toolkit content, Orca trigger** | Extract a `provision-worktree.sh` callable from `orca.yaml`; idempotent; fail loud (#2) |
| Identity (issue, mode, lane, run id) | inferred: `WT_SPOKE` + git-dir + branch slug | `linkedIssue`, `identity.key`, `ORCA_WORKTREE_ID` | **Orca as source, mirrored by ai-toolkit** | Setup writes `<wt>/.ai-toolkit/identity` from Orca metadata; guards read only that file; branch naming stops being load-bearing (#3) |
| Branch naming | `<type>/<n>-<slug>` | `<user>/<name>` + auto-rename | **ai-toolkit convention, not load-bearing** | **Decided 2026-10-01**, see §2.1 (#3) |
| Agent launch, model/effort, OTel env | tmux `zsh -c "WT_SPOKE=… claude --model …"` | `worktree create --agent --prompt`, `terminal create --command` | **Orca** | ai-toolkit supplies the argv/env via `spoke-model.env`; `WT_SPOKE` becomes redundant once identity is a file |
| Terminal read / answer delivery / liveness | `capture-pane`, `send-keys`, pane-pid walk | `terminal read/send --wait-submit/wait tui-idle`, status hooks | **Orca transport, ai-toolkit logic** | Put an adapter under `hub-inject.sh` (tmux backend and orca backend); the answerer and gate-broker decisions don't change (#4) |
| Gate decisions (PLAN gate, park answers, permission auto-approve) | marker tags + gate broker + afk-answering rule | orchestration decision gates / ask-reply | **ai-toolkit** (for now) | Marker tags are Orca-agnostic and already the explicit-state channel; Orca gates are a candidate transport later |
| Backlog planning, dispatch, scope packing | `batch-plan.sh`, `hub-afk-dispatch.sh` | orchestration DAG (no scope or critical-path model) | **ai-toolkit planning, Orca placement** | Dispatch ends in `orca worktree create` instead of `worktree-new.sh`'s `git worktree add` |
| Supervisor tick / watchdogs | nohup self-copy loops | automations (agent prompts) | **ai-toolkit** | A scripted control plane, not an LLM (memory: scripted-control-plane); automations don't fit a 300 s tick |
| Land (merge, gate, push, close) | `worktree-land.sh` on the hub | none | **ai-toolkit** | Orca has no land; the hub = `isMainWorktree` maps cleanly (#5) |
| Teardown + telemetry ingest | `worktree-done.sh`; ingest before teardown | `worktree rm --run-hooks` → `scripts.archive` | **Orca trigger, ai-toolkit ingest in archive hook** | The archive hook runs before removal and its failure blocks removal ⇒ fixes #8. **Risk:** 120 s timeout vs ingest duration [unverified] |
| Review authority | review-stamp `.review/<hash>.json` | diff view | **ai-toolkit authority, Orca surface** | Drop the VS Code review window for Orca's diff view; keep the stamp. Remove `git add -A` from `approve_review` so Orca's staged view stays truthful (#6) |
| Notifications | `hub-notify.sh` / osascript; global Notification hook | per-agent desktop notifications | **Orca for spokes, ai-toolkit for drain events** | Orca has no notify CLI, so drain-complete/blocked stays `hub-notify.sh`. Dedupe the global Notification hook against Orca's (#7) |
| Usage / cost | Langfuse spoke tree (causal, per-cycle) | usage/stats view | **both** | Different questions: Orca = quick per-agent usage; Langfuse = causal per-spoke telemetry (#7) |
| Session search, managed accounts, browser | — | yes | **Orca** | Free additions |

**Net:** ai-toolkit keeps everything that *decides* (policy, gates, identity contract, planning, landing, review authority).
Orca takes everything that *hosts* (worktrees, terminals, launch, liveness, transport, notifications, UI). The glue is a
provisioning script plus an identity file plus a terminal-transport adapter. That confirms the three-part seam from 01 §1.

### 2.1 Decision: branch naming vs identity (2026-10-01)

**Keep ai-toolkit's `<type>/<n>-<slug>` as the branch convention; make identity independent of it.**

- **Why not Orca's renaming:** `<user>/<generated-name>` is opaque to GitHub, CI, `gh` scripts and commit-quality's issue anchor,
  and the name only settles once an agent starts. *Correction:* Orca's own settings text says the auto-rename touches **only branches
  Orca named itself, and never after a push** **[C]**. So it does not break `@{upstream}`, marker tags or PRs on explicitly named
  branches; the risk is lower than first stated. Whether a name passed via `--name` counts as "Orca-named" is **[unverified]**.
- **Why not the branch as identity either:** today a non-conforming branch name silently disables the plan gate, the afk hooks
  and in-flight detection. Orca's model is right: identity is recorded data (`linkedIssue`, `identity.key`), not a parsed name.
- **Resulting rules:**
  1. Dispatch creates worktrees with `orca worktree create --issue <n> --name <n>-<slug>` (Orca turns `/` into `-` in `--name`),
     then renames the branch with `git branch -m <type>/<n>-<slug>`. Orca follows the rename (03 #4–#5, Q2).
  2. `autoRenameBranchFromWork` is **off** globally (its value is for throwaway exploration worktrees, which need no gate identity).
  3. The setup script writes `<wt>/.ai-toolkit/identity` from Orca metadata. Guards read that file first and fall back to the branch
     slug. A no-issue worktree (like `mcrilo33/orca-migration`) then gets "no issue" gate behaviour deliberately, not by accident.
  4. `branchPrefix` is **`none`** (values: `git-username` | `custom` | `none`). `custom` can't express a per-change `<type>/`, and
     `git-username` would yield `mcrilo33/feature/<n>-…`, breaking the `<type>/<n>-<slug>` patterns. Trade-off accepted: worktrees
     created by hand in the Orca UI get a bare branch name (e.g. `orca-migration`), which is fine for unpushed, no-issue work.
- **Resolved by the spike:** `--name feature/360-x` yields branch `feature-360-x` (`/` sanitized), hence the rename in rule 1.

## 3. Prerequisites before any Phase 3 work

1. ~~Fix the tmux autostart in Orca terminals (finding 5).~~ **Done 2026-10-01:** `~/.zshrc` skips autostart when
   `TERM_PROGRAM=Orca` or `ORCA_PANE_KEY` is set (backup `~/.zshrc.bak-orca-20261001`). **Verified live 17:37** in a throwaway
   terminal (`terminal create` → checks → `/exit` → `terminal close`): `TERM_PROGRAM=Orca`, no `$TMUX`, 19 `ORCA_*` vars present
   (incl. `ORCA_PANE_KEY`, `ORCA_WORKTREE_ID`, `ORCA_TERMINAL_HANDLE`, `ORCA_AGENT_HOOK_*`). `terminal wait --for tui-idle` was satisfied
   after `claude` started, and `worktree ps` listed it: `agents: [{agentType: "claude", state: "done", paneKey: …}]`.
   Note: an idle, never-prompted Claude reports `state: "done"`. Mapping these states onto `slot_state` stays a §4 item.
   Terminals opened before the fix (including the session that wrote this doc) remain in tmux and invisible to Orca.
2. ~~Turn off `autoRenameBranchFromWork`~~ **Done 2026-10-01** (Settings → « Renommage auto de la branche »; verified `false` in
   `profile-state.db`). `branchPrefix` set to `none` the same day (verified 17:33, decision §2.1 rule 4).
3. ~~Decide the `orca.yaml` vs local `hookSettings` source.~~ **Decided 2026-10-01: `orca.yaml`** (versioned, reviewed, effective
   only once landed on `main` because Orca reads it from the main checkout; `sync-to-repo.sh` can generate it for target repos).
   It stays thin: `setupAgentStartupPolicy: wait-for-setup` plus `scripts.setup` / `scripts.archive` calling versioned ai-toolkit
   scripts (`provision-worktree.sh`, `archive-worktree.sh`, to be written in Phase 3). All logic lives in ai-toolkit; Orca only triggers.
   **Constraint:** keep the local `hookSettings.scripts` empty. With `commandSourcePolicy` unset, a non-empty local script
   overrides `orca.yaml` (`local-only`) **[C]**. Phase 3 also checks whether Orca asks for trust before running `orca.yaml`
   commands (`setupTrust` in the code) **[unverified]**.

## 4. Unverified — needs a disposable-worktree spike (Phase 3, with approval)

> **Spike done 2026-10-01; results in `03-spike.md`.** Resolved: setup cwd/env, `wait-for-setup`, archive ordering and timeout,
> `/` in branch names, agent states, answer delivery, attribution shim, Claude trust (03 Q1), `--issue` (03 #15), ingest placement
> (03 #16: stays in land; archive only spools), orchestration gates (03 #17: viable, deferred).

- Setup hook: the exact cwd and env it runs with, and whether `wait-for-setup` really holds `--agent` launch until exit 0.
- Archive hook: ordering vs `git worktree remove`; whether 120 s is enough for `telemetry-ingest-spoke.sh`; behaviour on timeout.
- Whether `autoRenameBranchFromWork` applies to explicitly named or issue-linked worktrees.
- Agent status quality once the tmux fix is in: does PermissionRequest/Stop give the "parked vs working" signal `slot_state` needs?
- `terminal send --wait-submit` against a parked Claude permission dialog (Esc/arrow semantics vs `send-keys`).
- Whether the git/gh attribution shim is active, and whether its commit trailer passes `commit-quality`.
- Orchestration decision gates as a PLAN-gate transport (deferred; not needed for the split above).
