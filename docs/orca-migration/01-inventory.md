# Orca migration — 01 Inventory (Phase 1, read-only)

Date: 2026-10-01. Source: `main` @ `c85fca7d`. Nothing outside `docs/orca-migration/` was modified.

Path prefixes: **S/** `scripts/` · **K/** `shared/hooks/` · **H/** `shared/skills/hub/scripts/` ·
**GCD** = `git rev-parse --git-common-dir` (shared by every worktree) · **SD** = `GCD/ai-toolkit-afk/`.
`.claude/` is gitignored (`.gitignore:9`): everything under it is **sync output**, the source of truth is `shared/`.

## 0. Environment findings (premises checked)

| Premise in the brief | Actual state |
|---|---|
| "You are in an Orca-managed git worktree" | **False.** The Phase-1 session ran on the primary checkout `/Users/mathieucrilout/Repos/ai-toolkit` on `main`. `git worktree list` shows only it plus `ai-toolkit-351`, `ai-toolkit-357` (both created by `S/worktree-new.sh`, not Orca). The hub-guard hook blocked writing this file there (see collision #5). |
| `~/Repos/ai-toolkit-strict-causal-tree` is a sibling worktree/variant | **Does not exist.** A branch `feature/strict-causal-tree` exists locally, with **0 commits ahead of `main`** (fully merged, tip `b9412a7d`, #82 telemetry work). No checkout of it anywhere. |
| Other siblings | `~/Repos/ai-toolkit-{132,135,…,331}` and `ai-toolkit-scratch-notgit` are **empty, non-git leftovers** of torn-down spokes. `~/Repos/tech-ai-toolkit` is a **separate repo** (own `.git`, branch `feat/init-structure`). |
| Orca available | `/Applications/Orca.app` is installed, the `orca-cli` skill is installed. `/usr/local/bin/orca` was a root-owned mode-700 symlink (created 2026-10-01 11:45), so the CLI failed with "Unable to determine Orca.app path". **Fixed 2026-10-01 17:07:** now a `755` symlink to `/Applications/Orca.app/Contents/Resources/bin/orca`; `orca --version` → 1.4.218, `orca status` → runtime ready/connected. Plain `orca` works; the bundled path is no longer required. |
| `AGENTS.md` | Does not exist in this repo (only `CLAUDE.md`). |

**Live observation: the first Orca worktree created for this migration** (via the bundled CLI
`/Applications/Orca.app/Contents/Resources/bin/orca`, Orca 1.4.218, `worktree create --no-parent --setup skip`):

| Property | Value | Collision it confirms |
|---|---|---|
| Path | `~/orca/workspaces/ai-toolkit/orca-migration`, not the `~/Repos/ai-toolkit-<tag>` sibling | #1: invisible to `wt_resolve` and the `<repo>-` prefix matching |
| Branch | `mcrilo33/orca-migration` (Orca's `<gitUser>/<name>` scheme), base `origin/main` | #3: no `<type>/<n>-` issue number, so plan-gate, the afk hooks, in-flight detection and commit-quality's anchor are all off |
| `.claude/`, `.ai-toolkit/` | **absent** | #2: an agent here runs with **no Claude Code hooks**; only the native git hooks fire (same GCD `~/Repos/ai-toolkit/.git`) |
| Orca repo hook settings | `setupRunPolicy: run-by-default`, `scripts.setup: ""` | Orca's setup script is the natural slot for the provisioning contract (Phase 2) |

## 1. Hypothesis check

> ai-toolkit = policy/knowledge layer; Orca = execution layer.

**Directionally right, but the seam is not where the hypothesis puts it.** ai-toolkit today *also* is a
full execution layer (worktree creation, tmux terminals, agent launch, liveness, keystroke injection,
notifications, cost dashboards) — sections C and D below — and, critically, **the policy layer is
delivered through the execution layer**:

1. **Hooks only exist in a worktree because `worktree-new.sh` rsyncs `.claude/` into it** (`S/worktree-new.sh:397-406`). `.claude/` is gitignored, so a worktree Orca creates with plain `git worktree add` gets **zero PreToolUse/PostToolUse gates**. Only the native git hooks (common `.git/hooks/`) still fire.
2. **Per-spoke policy state is provisioned at spawn** into `<wt>/.ai-toolkit/` (`task.md` with `Scope:`, `mode`, `lane`, `spoke-run-id`, ledger skeleton, review-stamp MCP launcher). Without it: push-scope advisory, afk danger-wall, telemetry, `approve_review` (relative launch path `./.ai-toolkit/mcp/review-stamp/run.sh`) all break.
3. **Spoke identity is inferred, three different ways** — git-dir path `*/.git/worktrees/*`, env `WT_SPOKE` (set *only* in the tmux launch command, `S/worktree-new.sh:765`), and the issue number parsed from branch `<type>/<n>-<slug>`. Orca-launched agents would be "half spokes" (git-dir yes, `WT_SPOKE` no, issue number only if branch named that way). This is the "inferred state" anti-pattern `afk-design-principles` #1 warns about.
4. **The unattended `/afk` loop (answerer, auto-land, reap, watchdogs) is execution-coupled to tmux**: pane lookup by `pane_current_path`, liveness by pane-pid descendant walk, answers by `send-keys`. There is no seam to plug Orca terminals into yet.

So the realistic target is a three-part split: **policy** (rules, skills, agents, hooks, gates — portable),
**provisioning/identity contract** (what any worktree must contain + how identity is recorded — must become
explicit so Orca can satisfy it), and **execution** (worktrees, terminals, notifications, usage — candidate
to hand to Orca). Whether Orca can *host* the `/afk` autonomy (inject/read terminals, agent status, a
post-create hook) is **unverified** — Orca's docs/CLI were not read in Phase 1.

Layer tags in the tables: **P** policy · **V** provisioning/identity · **X** execution · **O** observability.

## 2. Inventory

### A. Policy & knowledge layer (portable as-is)

| capability | layer | where implemented (file:line) | trigger | state read/written | assumptions about cwd, branch, or worktree location |
|---|---|---|---|---|---|
| Rules (19) | P | `shared/rules/*.md` + `metadata.yml`; synced to `.claude/rules/` (13 — afk-answering, bug-triage, issue-hygiene, library-research, planning-hub, workflow are not synced as always-on for Claude, #320/#321) | loaded per platform frontmatter | read-only | `.claude/rules/` must exist in the session's project root |
| Skills (35) | P | `shared/skills/<name>/SKILL.md` (afk, hub, land, quick, start-task, next-batch, solo-cycle, source-task, …) | `/skill` or model-invoked | read-only | synced into `.claude/skills/`; hub scripts resolved via `$CLAUDE_PROJECT_DIR/.claude/skills/hub/scripts` |
| Agents (13) | P | `shared/agents/*.md` → `.claude/agents/` (model stamped at `S/sync-to-repo.sh:378`) | Agent tool | read-only | `code-review` frontmatter launches review-stamp via relative `./.ai-toolkit/...` path |
| Subagent dispatch from skills | P | solo-cycle → tdd-red/green, code-review (`shared/skills/solo-cycle/SKILL.md:115,202,213,222`); tdd-workflow (`:42,60,69`); hub/start-task lane-1 `isolation: worktree` (`hub/SKILL.md:153,179`, `start-task/SKILL.md:27,201`) | skill steps | lane 1 uses Claude Code's own `.claude/worktrees/` | **third worktree owner** (Claude Code) alongside the scripts and Orca |
| Prompts | P | `shared/prompts/commit-msg.md` | commit flow | — | — |
| Sync pipeline | P | `S/sync-to-repo.sh:20` (entry), `:956` dispatch; `S/metadata_parser.py:107`; `S/ai_toolkit_config.py:729`; `settings/ai-toolkit.yml` | manual `sync-to-repo.sh <target> [tool]` | writes target `.claude/ .cursor/ .github/ .ai-toolkit/`, `git config ai-toolkit.*` (`:695,740,771`), `GCD/info/exclude` (`:896-937`), `.ai-toolkit-manifest.json` (`:825`) | target must be a git repo (linked-worktree `.git` file accepted, `:71-74`); manifest cleanup **deletes previously-written toolkit paths** |
| Hook config generation | P | `S/hooks_generator.py:190-210,311`; `S/hooks_reconciler.py:82,148`; called from `S/sync-to-repo.sh:463-496` | sync | writes `.claude/settings.json`, `.cursor/hooks.json`, `.github/hooks/ai-toolkit.json` | hook command = `"$CLAUDE_PROJECT_DIR"/.claude/hooks/scripts/<name>.sh` |
| Global on/off | P | `S/ai-toolkit:2`; `K/lib/enabled.sh:31-48` | `ai-toolkit off/on/status` | `GCD/ai-toolkit-off`, `git config --local ai-toolkit.enabled` | applies to **all worktrees** of the clone |
| Base-branch resolver | P | `K/lib/base-branch.sh:27-59` (env at `:39`); `S/worktree-lib.sh:44` | sourced by guards + worktree scripts | `git config ai-toolkit.base-branch` > `AI_TOOLKIT_BASE_BRANCH` > `origin/HEAD` > `init.defaultBranch` > main/master | **not used by** `K/test-select.sh:206` `default_branch()` nor `K/anti-gutting-scan.sh:66` (drift if base ≠ default) |

### B. Gates & hooks (policy, but location/identity-sensitive)

Hook registrations live in `.claude/settings.json` (generated). `settings.local.json` registers no hooks (only `AI_TOOLKIT_TELEMETRY=1`).

| capability | layer | where implemented (file:line) | trigger | state read/written | assumptions about cwd, branch, or worktree location |
|---|---|---|---|---|---|
| Hub guard (no edit/commit/branch on main checkout's base) | P/V | `K/hub-guard.sh:87-95,98-101,113,155` | PreToolUse `Edit\|Write\|NotebookEdit\|Bash` | reads `GCD/hub-guard-allow`, merge/rebase markers | **hub = git-dir not under `.git/worktrees/` AND on base branch**; root via `project_root_from_payload` (`K/lib/utils.sh:262`), not `CLAUDE_PROJECT_DIR` |
| Spoke-main guard | P/V | `K/spoke-main-guard.sh:252,255-262,298` | PreToolUse `Bash` | — | spoke = `WT_SPOKE` set **or** git-dir under `.git/worktrees/` |
| Push-scope guard + out-of-`Scope:` advisory | P/V | `K/push-scope-guard.sh:99-106,260,389,404-426,510` | PreToolUse `Bash(git push *)` | reads `<wt>/.ai-toolkit/task.md` `Scope:` | spoke = git-dir pattern; branch convention `feature/<id>-<slug>` (`:52`) |
| PLAN-gate guard | P/V | `K/plan-gate-guard.sh:74,136-145,159` | PreToolUse edit/Bash | tag `refs/tags/gate/<N>` vs HEAD; transcript | **issue number from branch slug** |
| Ledger schema guard | P/V | `K/ledger-schema-guard.sh:36,61` | PreToolUse `TaskCreate\|TaskUpdate` | `<wt>/.ai-toolkit/ledger-skeleton.md` | only when `WT_SPOKE` set |
| Block `--no-verify` | P | `K/block-no-verify.sh:17` | PreToolUse `Bash(git * --no-verify *)` | — | Claude-only (no native equivalent by design, `S/install-git-hooks.sh:26-31`) |
| Commit-quality (conventional + issue anchor) | P | `K/commit-quality.sh:37,113,143-156,266` | PreToolUse `Bash(git commit *)` **+** native commit-msg | `git config ai-toolkit.hook.commit-quality.*` | anchor = branch `feature/142-x` or `Closes #N` |
| Commit gauntlet (lint/types on staged) | P | `K/commit-gauntlet.sh:54,163,277,285` | PreToolUse `git commit` + native commit-msg | staged index | root from payload/cwd |
| Red-proof verify / warn | P | `K/red-proof-verify.sh:60,86-103`; `K/red-proof-warn.sh:43,92-141` | commit (verify) / push (warn, advisory) | runs pytest, prefers `<wt>/.venv/bin/pytest` | `.venv` in worktree root |
| Reviewer-separation push check | P | `K/reviewer-sep-warn.sh:46-54,65,81,97`; base/hash `K/lib/utils.sh:500-525` | PreToolUse `git push` + native pre-push (advisory) | `<root>/.review/<hash>.json`, `REVIEW_STAMP_KEY` (env/Keychain, `utils.sh:680`) | **base = merge-base `@{upstream}`** → origin/main → origin/HEAD |
| review-stamp MCP `approve_review` | P | `mcp/review-stamp/server.py:65-79` (root), `:86-102` (base), `:105-126` (hash), `:162` (`git add -A`), `:181-183` (write); `run.sh:10-16` | code-review agent tool call | **stages the whole tree**; writes HMAC'd `.review/<sha256>.json` | root = `CURSOR_PROJECT_DIR` else walk up from **server process cwd**; `.ai-toolkit/` must exist in worktree root |
| Review-stamp guard / review window | P | `K/review-stamp-guard.sh:36,64-91`; `K/review-window-{open,close}.sh` | PreToolUse `mcp__review-stamp__approve_review`; window is Cursor-only | `.review/.window*` | effectively Cursor-only |
| Todo-ledger warn / nudge | P | `K/todo-ledger-warn.sh:11-15`; `K/todo-ledger-nudge.sh:23-33` | push / SessionStart | transcript | Claude transcript format |
| Delegation hints | P | `K/delegation-gate-warn.sh:22-34,181` | PreToolUse `Bash` | `$GIT_DIR/.ai-toolkit-hooks/delegation-hints` | per-worktree git dir |
| rm/chmod worktree-scoped auto-allow | P | `K/rm-scope-guard.sh:111-123`; `K/lib/scope-guard.sh:10-36` | PreToolUse `Bash(rm *)` / `chmod *` | — | scope = `--show-toplevel` of cwd |
| Secrets scan/revert, config-protection, post-edit format | P | `K/secrets-scan*.sh`; `K/config-protection.sh:28-50`; `K/post-edit-format.sh:27-29`; `K/quality-gate.sh`; `K/console-log-warn.sh` | Pre/PostToolUse `Write\|Edit` | `<file>.secret-revert.<ts>.bak` | path-based |
| Native commit-msg / pre-push | P | `S/install-git-hooks.sh:122-205` (commit-msg), `:207-273` (pre-push), `:301-313` (info/exclude) | git | `.git/hooks/ai-toolkit-scripts/` | **fires in any worktree** (common hooks dir): the only gates an un-provisioned worktree keeps |
| Test selector (pre-push) | P | `K/test-select.sh:139-159,206-240,362,398-400` | native pre-push | `<wt>/.testmondata`; git config skip/cmd | own `default_branch()`; needs `.venv`/pytest or blocks python diffs |
| Green-tree stamps / tripwire | P | `K/lib/gate-stamp.sh:7-10,53-70`; `K/lib/utils.sh:789-1091` | test-select, gate-sweep | `GCD/.gate-stamps/<tree>`; snapshots refs | tripwire treats **registered** sibling worktrees as benign |
| Anti-gutting scan | P | `K/anti-gutting-scan.sh:66-80,165-173` | native pre-push | `SD/unattended` | fails closed only when unattended |
| gate-sweep / test-budget-watch | P | `S/gate-sweep.sh:96-140`; `S/test-budget-watch.sh:75-86,151-169` | tail of `worktree-land.sh` | `GCD/.gate-sweep/`, `.testmondata-baseline` | runs on the hub checkout |
| Ready precondition | P/V | `S/spoke-ready.sh:186-191,209,243-274` | `spoke-ready.sh ready <N>` | needs `@{upstream}`==HEAD, clean tree, APPROVE `.review/*.json` | run inside the spoke |

### C. Execution layer: worktrees, branches, terminals (Orca overlap)

| capability | layer | where implemented (file:line) | trigger | state read/written | assumptions about cwd, branch, or worktree location |
|---|---|---|---|---|---|
| Find hub (main checkout) | X | `S/worktree-lib.sh:413` `wt_main_root` | all worktree/hub scripts | first entry of `git worktree list --porcelain` | main checkout = hub = landing site |
| Create spoke worktree + branch | X | `S/worktree-new.sh:122-130` (branch), **`:190` (path)**, `:193-214` (prune, fetch, `git worktree add -b`) | `start-task`, `next-batch`, `H/hub-afk-dispatch.sh:353-356,385` | branch `<type>/<n>-<slug>`; fails if path/branch exists | **path = `$(dirname ROOT)/$(basename ROOT)-<tag>`**; cds to main checkout first (`:95-96`) |
| Provision spoke (identity + policy delivery) | V | `S/worktree-new.sh:241-248` (info/exclude), `:268-283` (`.testmondata-baseline`), `:289-298` (`spoke-run-id`, `lane`, `mode`), `:320-388` (`task.md`, ledger), **`:397-406` (rsync `.claude/`)**, `:516-565` (`settings.local.json` allowlist), `:777` (bypassPermissions in afk mode) | inside worktree-new | `<wt>/.ai-toolkit/*`, `<wt>/.claude/` | assumes the main checkout's `.claude/` is the source; skips `.review/`, `worktrees/` |
| Launch agent in tmux | X | `S/worktree-new.sh:765` (`WT_SPOKE=… claude --model …`), `:829-858`; session name `S/worktree-lib.sh:579` | worktree-new (unless `--no-terminal`) | tmux session `<parentdir>-<repo>`, window `<n>-<slug>`, `zsh -c "claude…; exec $SHELL"` | window name is the kill/lookup key; `WT_SPOKE` exists **only** here (and in relaunch) |
| Model/effort/OTel for spokes | X/O | `S/sync-to-repo.sh:607-619` → `.ai-toolkit/scripts/spoke-model.env`; sourced at `S/worktree-new.sh:709` | spawn | env | — |
| VS Code review window | X | `S/worktree-new.sh:589-598`; `S/worktree-lib.sh:449-558` | spawn (unless `--no-code`) | `~/.claude/<repo>.code-workspace` | duplicates an Orca diff/editor surface |
| GitHub lifecycle stamping | X | `S/worktree-new.sh:892-900`; `S/worktree-gh-lib.sh:42,198,232` | spawn | labels `status:in-progress`, `mode:*`, `lane:*`, comment | issue-numbered only |
| Queue subtasks onto a spoke | X | `S/worktree-new.sh:806-818` | `--subtasks` | `SD/queued-<n>/<sub>` | shared GCD |
| `/quick` worktree | X | `S/worktree-quick.sh:59-66,89,142,161` | `quick` skill | branch `quick/<slug>`; writes **global** `GCD/hub-guard-allow` | hub session cds in; no tmux |
| Spoke push | X | `S/spoke-push.sh:71,116` (`git push -u origin <current>`) | solo-cycle | refs; `AI_TOOLKIT_PARENT_SPAN` | inside spoke; takes no issue arg |
| Marker tags ready/gate/accept/blocked | V | `S/spoke-ready.sh:388` (`tag -f -a`), `:411-426` (push tag), `:449-462` (wake event), `:494` | spoke-push `--ready`, solo-cycle | `refs/tags/<kind>/<n>`; `SD/events/` | inside spoke. **Explicit-state channel, Orca-agnostic** |
| Land | X | `S/worktree-land.sh:189` (refuse if `WT_SPOKE`), `:221-230` (hub + base guards), `:241` (lock), `:568-574` (`ready/<n>` at tip), `:600-603` merge, `:625` rollback, `:837` push, `:1035` telemetry ingest, `:1054` teardown, `:1074-1087` tags/issue | `land` skill, `H/hub-afk-land.sh:617,725`, quick `--local` | `SD/land.lock`; `LAND_*` env | **must run on the main checkout, on the base branch, clean** |
| Teardown | X | `S/worktree-done.sh:36` (refuse in spoke), `:68`, `:115-120`, `:132-159` (`git worktree remove`, `rm -rf`), `:225,254` (branch -D / push --delete); resolve `S/worktree-lib.sh:633-644` | land, `H/hub-afk-recover.sh:1086`, manual | workspace file, VS Code storage | matches by `<repo>-` path prefix: **would delete dirs behind Orca's back** |
| Kill spoke tmux windows | X | `S/worktree-land.sh:267-283,1125-1144`; `H/hub-afk.sh:552-558` (`-a`, name `<issue>-*`) | land, afk reap | tmux | name or `pane_current_path` match |
| Relaunch / reopen agent | X | `S/spoke-relaunch.sh:183,204-209,238`; `H/hub-afk-recover.sh:420-432` | manual / afk | reuses `.ai-toolkit/spoke-run-id` | refuses without `spoke-run-id` |
| Hub helper agent windows | X | `H/hub-agent.sh:216-227` | land, hub | `.ai-toolkit/hub-agents/<label>.log` | main checkout |
| In-flight discovery / status | X | `H/hub-status.sh:205,233,339-344`; `H/batch-plan.sh:994-996`; `H/gate-broker-markers.sh:786` `inflight_worktrees` | `/hub`, `/next-batch`, afk | `git worktree list` | issue = leading digits of branch slug |
| Legacy single-checkout branch | X | `shared/skills/source-task/SKILL.md:60,89` (`git checkout -b`) | `/source` | refs | no worktree |

### D. Unattended `/afk` control plane (execution-coupled to tmux)

The canonical source is `H/` (`shared/skills/afk/` holds only `SKILL.md`). At runtime it self-copies to `$TMPDIR/hub-afk-self.*`.

| capability | layer | where implemented (file:line) | trigger | state read/written | assumptions about cwd, branch, or worktree location |
|---|---|---|---|---|---|
| Arm / off / status | X | `H/hub-afk.sh:1012` main, `:294,314,983,1032`; clears window markers `:1101-1120` | `/afk <dur>\|drain\|off\|status` | `GCD/.afk-state`, `GCD/.afk-arm-epoch` | MAIN_ROOT = toplevel; one drain per clone |
| Arm preconditions / self-check | X | `H/hub-afk-arm.sh:396`, `:470` (judge `:277`, claude `:312`, gh `:330`, OTel `:434`) | arm, reconcile | heartbeat, git status, gh auth | **main checkout on base, clean tracked tree** |
| Self-copy re-exec | X | `H/hub-afk-supervise.sh:204` | long-running entries | `AFK_RUNNING_COPY`, `AFK_ORIG_SCRIPT` | sibling modules co-located |
| Supervisor tick | X | `H/hub-afk.sh:709` `supervise_tick`, loop `:1131+`; heartbeat `:482-502` | every 300 s or SIGUSR1 | `GCD/.afk-heartbeat`, `GCD/.afk-last-action` | single background process on the hub |
| Event wake spool | X | `H/hub-afk.sh:731-772`; reader `H/gate-broker-markers.sh:119-137`; writers `S/spoke-ready.sh:457-460`, `K/afk-notify-wake.sh:38-93` | SIGUSR1 / Notification hook | `SD/events/<epoch>-<issue>-<type>` | writer derives GCD from spoke cwd. **File + signal, no tmux** |
| Batch planning | X | `H/batch-plan.sh`; `H/hub-afk-dispatch.sh:298` | tick, `/next-batch` | one `gh api graphql`; in-flight scopes | disjoint `Scope:` packing; cap `settings/ai-toolkit.yml` `batch.concurrency_cap: 8` |
| Dispatch | X | `H/hub-afk-dispatch.sh:173,222,256,353-356` | tick | `SD/dispatch-<n>.epoch`, `SD/dispatch-fail-<n>.count`, `SD/queued-*` | via `worktree-new.sh --mode afk` |
| State classification | X | `H/gate-broker-detect.sh:314` `slot_state` | every pass | marker tags at tip, **pane scrape**, transcript, `SD/progress-*`, transitions | pane looked up by cwd |
| **Pane resolution** | X | **`H/hub-inject.sh:190-198`** `_spoke_pane_target` | every pane read/write | `tmux list-panes -a` × `pane_current_path` | pane cwd must equal worktree realpath; **silently empty for non-tmux terminals** |
| **Agent liveness** | X | `H/hub-inject.sh:232,276,298`; `H/hub-afk-recover.sh:40`; `H/hub-afk.sh:259` | before keystrokes; reap | `#{pane_pid}` + descendant walk for `claude\|node` | needs a pane pid (principle #4) |
| Permission-dialog scrape | X | `H/gate-broker-permission.sh:30-117,138`; `H/hub-inject.sh:98,542` | slot_state | `capture-pane`; transcript `~/.claude/projects/<slug-of-wt-path>/` | transcript dir derived from worktree path |
| **Answer injection** | X | `H/hub-inject.sh:316,488,578-605,633` | broker deliver, nudge, conflict lane | `send-keys` Esc/-l/Enter; `SD/answer-attempt-<n>.epoch` | live TUI in a tmux pane |
| Gate broker / answerer | P/X | `H/gate-broker.sh:547,886`; `H/gate-broker-answerer.sh:674,898`; rule `shared/rules/afk-answering.md` | slot_state = waiting | `SD/decision-journal.jsonl`, `warned-*`, `served-*`, `park-sig-*`, `gate-broker/qcm-*` | headless `claude -p` on a snapshot. **Decision logic is portable; delivery is tmux** |
| Permission auto-approve + danger wall | P | `K/afk-permission-hook.sh:39-58`, `K/afk-danger-guard.sh:30-49` → `H/gate-broker-permission.sh:363,422,505-524`; `H/gate-broker-danger.sh:369,630` | spoke PreToolUse `Bash\|Read` | `GCD/.afk-heartbeat` (`kill -0`), `<wt>/.ai-toolkit/mode`, `SD/judge-*` | issue-numbered branch; any worktree of the clone qualifies while a drain is live |
| Wedge respawn / revive / reap | X | `H/hub-afk-recover.sh:420,438,473,568,611,1012,1070,1097` | inject rc 2, idle, dead pane | `SD/resumed-*`, `redispatched-*`, `nudge-*`; new tmux window `claude --continue` | **"no pane" ⇒ treated as dead ⇒ resume/teardown** |
| Auto-land | X | `H/hub-afk-land.sh:337,357,410,426,543,617,725,779` | `ready/<n>` at tip | `SD/land-*`, `GCD/.afk-landed-count` | lands on hub; stashes hub dirt |
| Blocked escalation / reconcile | V | `H/gate-broker-markers.sh:822,835`; `H/hub-afk.sh:623,695` | auth halt, land failure | `blocked/<n>` tag, `SD/blocked-<n>.txt`, gh label | `cd $wt && spoke-ready.sh` |
| Transition log | V | `H/transition-log.sh:48-279`; `H/hub-afk.sh:644` | actors at transition time | `SD/transitions/<n>.jsonl` | keyed by issue, not worktree |
| Keeper watchdog (tier 1) | X | `H/hub-afk-supervise.sh:264,517,560,624,640,666` | each tick / `--watchdog` 60 s / `--reconcile` | `GCD/.afk-watchdog`, `SD/respawn-*` | nohup daemon |
| Hub watchdog (tier 2) | X | `H/hub-watchdog.sh:310,566,598`; `H/hub-watchdog-detect.sh:273,755,767`; `H/hub-watchdog-intervene.sh:69,202,378` | co-armed `H/hub-afk-supervise.sh:612` | `GCD/hub-watchdog.*`, `SD/intervention-ledger.jsonl` | revive is **headless `nohup claude --continue`**: an invisible agent |
| Self-update deploy | X | `H/hub-afk-supervise.sh:341,465` | land touching supervisor scope | `SD/self-update-pending`; re-sync `.ai-toolkit/scripts` | synced layout in MAIN_ROOT |
| Sleep inhibit / hang forensics | X | `H/hub-afk-supervise.sh:49,788` | tick / pre-revive | `SD/sleep-inhibit` (caffeinate); `GCD/hang-forensics/` (`capture-pane -S -`) | macOS + tmux |
| Hub notifications | X | `H/hub-notify.sh:59,74-83,90,103,344`; `H/hub-afk.sh:471` | hub `/loop 2m hub-ready-watch.sh` | `GCD/hub-notify-seen`, `.afk-drain-complete`; osascript | spokes have notifications off; `HUB_NOTIFY_CMD` override |
| Remote / travel | X | `H/hub-afk-arm.sh:53,68`; `S/afk-travel.sh:55,82,93`; `S/travel-local.sh:201,223,250` | `--remote`, `/afk-travel` | `~/.afk-remote`, `pmset`, Keychain hotspot items, `SD/progress-*` | ssh + tmux session `afk` |

### E. Observability, MCP, tests

| capability | layer | where implemented (file:line) | trigger | state read/written | assumptions about cwd, branch, or worktree location |
|---|---|---|---|---|---|
| Spoke native OTel env | O | `S/worktree-lib.sh:111,129-147`; default ON at `S/worktree-new.sh:691` (`docs/downstream-setup.md:56-59` still says opt-in) | spawn / relaunch `S/spoke-relaunch.sh:172` | `OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=<branch>+<epoch>,repo=…`; `<wt>/.ai-toolkit/raw-bodies` | env prefix exists only in the tmux launch command |
| Collector / bridge / otel-watch | O | `S/worktree-otel-lib.sh:134` (bridge :4319), `:198` (docker `lf-collector` 4317/4318/4418/8889), `:291,322` | spawn when `AI_TOOLKIT_OTEL=1` | Docker, nohup bridge, `GCD/hub-otel-watch.pid` | host-wide singletons; Langfuse :3000 |
| Hook spans | O | `K/lib/telemetry.sh:122,190,201,236,271,417` | every hook when enabled | `~/.ai-toolkit/telemetry/events.jsonl`; OTLP :4318 | root/branch derived at runtime |
| Parent-span causality | O | `K/parent-span-export.sh:33,68-73`; `S/spoke-push.sh:116` | PreToolUse `Bash` | `<wt>/.ai-toolkit/parent-span` | one per worktree |
| Cycle-step markers | O/V | `K/cycle-step-mark.sh:9-12,64,123-136` | PostToolUse `Bash\|Write` | witnesses: `@{upstream}`==HEAD, `.review/*.json` | **needs `WT_SPOKE`** |
| Land-time ingest + spoke tree | O | `S/telemetry-ingest-spoke.sh:76,86,200-220,277,332`; `S/telemetry/langfuse_spoke_tree.py:306,311,899`; `summarizer.py` | `worktree-land.sh:1035`, before teardown | raw-bodies, `~/.claude/projects/<slug>`; Langfuse; Keychain `DEEPSEEK_API_KEY` | **worktree must still exist at land** |
| Langfuse dashboards (cost/latency/outcomes) | O | `dashboard/langfuse/*-dashboard.json`, `otelcol.yaml` | manual import | — | overlaps an Orca usage view |
| review-stamp MCP config | P | `.cursor/mcp.json`; agent frontmatter `.claude/agents/code-review.md:7-9` | Cursor / code-review agent | — | relative launch path |
| Personal MCP servers | — | `settings/cursor/mcp.json` (15), `settings/vscode/mcp.json` (12); env refs only, no inline secrets | IDE startup | Keychain/env | hardcoded `/opt/homebrew`, `~/.local/bin` |
| User install | — | `S/install.sh:49-58` | manual | symlinks into `$HOME` | **bug:** checks `$REPO_DIR/claude/settings.json` (`:55`) but the file is `settings/claude/settings.json`, so the Claude step never runs |
| Test runner & CI | P | `pyproject.toml:12-22` (`serial` marker); `requirements-dev.txt`; `tests/unit` (129) + `tests/integration` (4); `S/test-run.sh:56-65`; `S/ensure-test-venv.sh:29-65`; `.github/workflows/ci.yml:18,39,55,59,114,182` | pre-push / CI | `.testmondata`, `.venv` | `.venv` relative to worktree root |

## 3. Key env vars

`AI_TOOLKIT_BASE_BRANCH` (`K/lib/base-branch.sh:39`; survives re-sync, git config wins) · `AI_TOOLKIT_CONFIG` (`S/worktree-lib.sh:203`) ·
`AI_TOOLKIT_OTEL` / `_DEFAULT` / `_SPAN_ENDPOINT` (`S/worktree-lib.sh:111,129`; `S/spoke-relaunch.sh:172`) · `AI_TOOLKIT_TELEMETRY` / `_DIR` (`K/parent-span-export.sh:33`, `K/lib/telemetry.sh:417`) ·
`AI_TOOLKIT_PARENT_SPAN` (`S/spoke-push.sh:116`) · `AI_TOOLKIT_BATCH_CAP` / `_STAGGER` (`S/ai_toolkit_config.py:686,689`) · `AI_TOOLKIT_READY_FORCE` (`S/spoke-ready.sh:319`) ·
`AI_TOOLKIT_PLAN_GATE_OVERRIDE` (`K/plan-gate-guard.sh:116`) · `AI_TOOLKIT_SECURITY_GUARDS` (`K/lib/enabled.sh:99`) · `AI_TOOLKIT_GAUNTLET_BUDGET` (`K/commit-gauntlet.sh:264`) ·
`AI_TOOLKIT_NOTIFY_CMD` (`S/telemetry-ingest-spoke.sh:76`) · `AI_TOOLKIT_GH_LIFECYCLE_LABELS` / `_GH_TIMEOUT` (`S/worktree-gh-lib.sh:42,75`) · `AI_TOOLKIT_HUB_AGENTS_DIR` (`H/hub-agent.sh:61`).
Non-prefixed but load-bearing: **`WT_SPOKE`** (spoke identity, `S/worktree-new.sh:765`), `CLAUDE_PROJECT_DIR` (hook paths, afk shims), `CURSOR_PROJECT_DIR` (review-stamp root), `AFK_STATE_DIR`, `AFK_TICK_SECONDS`, `HUB_NOTIFY_CMD`.

## 4. Orca-collision summary (input to Phase 2)

1. **Worktree ownership conflict.** `worktree-new.sh` (create at `<repo>-<tag>`), `worktree-done.sh` (`git worktree remove` + `rm -rf`), `worktree-land.sh` teardown, and `git worktree prune` all assume the scripts own worktrees. With two owners, the scripts delete dirs behind Orca's back, or Orca's worktrees are invisible to `wt_resolve`.
2. **Un-provisioned worktrees lose all agent-side gates.** No `.claude/` copy means no Claude hooks; no `.ai-toolkit/` means no `task.md` Scope, `mode`, `spoke-run-id`, or review-stamp launcher. Native git hooks still fire. **This is the #1 thing Orca needs a post-create hook for.**
3. **Identity is inferred, not recorded.** It comes from `WT_SPOKE` + the git-dir pattern + the branch-slug issue number. Orca terminals lack `WT_SPOKE`, and branch names that aren't `<type>/<n>-<slug>` disable plan-gate, the afk hooks, in-flight detection, landing, and commit-quality's anchor. Candidate fix: one explicit identity record in `<wt>/.ai-toolkit/` read by all guards (principle #1).
4. **`/afk` is welded to tmux.** Pane lookup (`H/hub-inject.sh:190`), liveness (pane-pid walk), delivery (`send-keys`), forensics (`capture-pane`). Under Orca terminals every lookup returns empty, so `recover_dead_panes` reads it as a crash and resumes in a *new* tmux window: **duplicate agents**. The tier-2 watchdog's headless revive is already an invisible agent. Portable parts: marker tags, `SD/` state, transition log, event spool, permission hooks, batch-plan, answerer decision logic.
5. **Hub = main checkout on base branch** (land, arm preconditions, hub-guard). An Orca terminal opened on the main checkout inherits hub-guard blocks (this happened to the Phase-1 session itself); landing cannot happen from an Orca worktree.
6. **Review binding** depends on `@{upstream}` and the MCP server's cwd, and `approve_review` runs `git add -A`, which rewrites the staged/unstaged split Orca's diff review may display. An Orca "approve" is not a `.review/<hash>.json`, so spoke-ready would see "no review".
7. **Duplicated surfaces:** VS Code review window vs Orca diff view; `hub-notify.sh`/osascript vs Orca notifications (route via `HUB_NOTIFY_CMD` / `AI_TOOLKIT_NOTIFY_CMD`); Langfuse cost/latency dashboards vs Orca usage view; batch concurrency cap vs any Orca scheduler; supervisor + 2 watchdogs vs Orca automations.
8. **Telemetry ingest needs the worktree alive at land.** If Orca tears it down first, raw bodies are lost.
9. **Three worktree owners already** (the scripts, Claude Code's `.claude/worktrees` for lane 1, and Orca). Pick one per lane.

## 5. Stale or drifted items noticed (not fixed; out of scope for Phase 1)

- `S/install.sh:55` looks for `claude/settings.json`; the file is `settings/claude/settings.json`.
- `dist/cursor-plugin/agents` has 11 agents (missing bug-scoper, followup-scoper). The hub scripts synced into `.cursor/` and `.github/` lack the split `hub-afk-*`, `hub-inject`, `hub-watchdog-*`, `transition-log` modules.
- `shared/skills/land/SKILL.md:78` says "session-0 window"; the actual session is `<parentdir>-<repo>`.
- `docs/downstream-setup.md:56-59` says OTel is opt-in; the code defaults it on.
- The `settings/ai-toolkit.yml` comment says concurrency is "capped at 3"; the value is 8.
- `docs/gate-audit.md` rows 3 and 5 describe gaps already fixed in `S/install-git-hooks.sh:186-195`.
- `.claude/hooks/lib/utils.sh` is a stale May copy diverging from `K/lib/utils.sh` (the scripts use `.claude/hooks/scripts/lib/`).
- `test-select.sh` / `anti-gutting-scan.sh` ignore the `ai-toolkit.base-branch` resolver.
