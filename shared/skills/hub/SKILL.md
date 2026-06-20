# Hub

Orient a fresh **planning hub** session and dispatch from it. This is the entry point for
a main-checkout session in the parallel-worktrees workflow: it shows what is in flight,
then proposes the next move. Read the `planning-hub` rule for the role and
`docs/parallel-worktrees.md` for the full model.

Use it when you sit down at the hub — the user says `/hub`, "what's in flight", "hub
status", or starts a fresh main-checkout session and wants to get oriented.

## Preconditions

- Run from the **main checkout** (the hub), which stays on `main`.
- `gh` authenticated (issue survey degrades gracefully without it).
- The worktree scripts are installed at `.ai-toolkit/scripts/` (`worktree-new.sh`,
  `worktree-done.sh`) — `sync-to-repo.sh` puts them there in any synced repo.

## Workflow

### 1. Confirm you are on the hub

You are the hub only on the main checkout, on the default branch. If `git branch --show-current`
is a task branch or you are inside a worktree, you are a **spoke** — stop and follow
`source-task` / `solo-cycle` instead. The hub never writes task code (see `planning-hub`).

### 2. Survey what is in flight

Run the dashboard:

```bash
bash .ai-toolkit/scripts/hub-status.sh
```

It reports, read-only:

- **Worktrees** — each task branch with ahead/behind vs the default branch, its state
  (`dirty`, `unpushed`, `pushed (in progress)`, or `pushed → mergeable`), its issue
  (`#N OPEN`, `#N ?` when `gh` is unreachable), and its live tmux pane
  (`tmux <session>:<window>`, matched across
  **all** sessions by pane path, or `no pane`). Rows with a pane include a copy-paste
  `↳ jump:` command (`select-window` / `switch-client` / `attach`, picked for where you
  are). A `↳ todos:` sub-line shows the spoke's task ledger (Tasks system, or TodoWrite
  on older runtimes) from its latest Claude session: `<done>/<total> · step: <X> ·
  <activity>`, where `step:` is the in_progress item's cycle keyword
  (ANCHOR/RED/GREEN/REVIEW/PUSH) or its truncated text, activity is `active Ns ago` /
  `idle Nm`, and `⚠ WAITING ON INPUT` is appended when the spoke is blocked on an
  unanswered question; `todos: none` means the spoke never seeded a ledger of either
  kind — that absence is signal, since kickoffs mandate one.
- **Open issues** — flagged `worktree active` or `no worktree` so you can see what is
  unstarted.

A fully-pushed branch reads `pushed → mergeable` only when a `ready/<issue>` completion
marker (a git tag the spoke pushes after its FINAL subtask) points at the branch tip;
otherwise it reads `pushed (in progress)` — the spoke is between subtasks and a
per-subtask push must not be mistaken for a finished issue. Ad-hoc/express branches
(non-numbered slug) need no marker — their single push IS completion.

### Proactive ready-to-land watch (optional)

`hub-status.sh` is **pull** — you only see `pushed → mergeable` when you run it. To be
told the moment a spoke finishes, run the **push** companion on a loop:

```bash
bash .ai-toolkit/scripts/hub-ready-watch.sh
```

Each run best-effort fetches tags, diffs the `ready/<issue>` markers against a last-seen
set (kept under the git common dir), and prints `#N → run /land N <branch> ↑ahead ↓behind`
for each **newly**-ready spoke — nothing when there is no change, so it is quiet enough to
loop (`/loop 2m bash .ai-toolkit/scripts/hub-ready-watch.sh`). It is detection only:

- Only a `ready/*` tag **at its branch tip** fires — a mid-task push (no tag) or a stale
  marker (tag behind the tip) is ignored, so it never false-fires between subtasks.
- It **never merges.** The surfaced `/land N` stays a human-invoked one-confirm step
  (section 3) — the watcher proposes, you land.
- Offline-safe: a finished spoke's tag is locally visible (shared ref store), so a failed
  fetch is non-fatal and local markers still surface.

### Unattended drain and parallel batching

Two skills move a backlog without hands-on dispatching, with the observability
dashboard as the single source of truth for what happened during a run:

- **`/next-batch`** — compute and dispatch the largest disjoint-scope set of ready
  issues that can run concurrently right now. `batch-plan.sh` reads the open backlog
  in one `gh api graphql` round-trip, ranks ready issues by critical-path depth,
  greedily packs a batch whose file-scopes don't collide (honoring in-flight spoke
  scopes), then spawns each via `worktree-new.sh`. Run it whenever you want to fill
  idle capacity; it is independent of `/afk`.
- **`/afk`** — drain the backlog unattended for a bounded window or until it is empty.
  The hub keeps plan → dispatch → auto-answer → auto-land → reap running with zero
  human input (`hub-afk.sh`); a parked spoke is answered on the human's behalf by a
  reasoning answerer or escalated to `blocked`. Use it when stepping away:
  `/afk <duration>`, `/afk until HH:MM`, `/afk drain`, plus `/afk off` and
  `/afk status`.

### 3. Propose the next move — act only on confirmation

From the dashboard, surface concrete next steps and wait for the user's OK before doing
anything that changes state:

| Dashboard signal | Proposed action | How |
|------------------|-----------------|-----|
| Open issue, `no worktree` | Start it | `start-task` skill (creates issue if needed + spawns spoke) |
| New idea, no issue yet | Define then dispatch | discuss scope → `start-task` |
| Branch `pushed → mergeable` | Land and tear down | `/land <id>` (`land` skill → `.ai-toolkit/scripts/worktree-land.sh`) |
| Branch `pushed (in progress)` | Leave it — spoke pushed a subtask but isn't done | paste the row's `↳ jump:` command; land only once it flips to `mergeable` (or `--force-land` for a branch that never carries a marker) |
| Branch `unpushed` / `dirty` | Leave it — spoke still working | paste the row's `↳ jump:` command to reach its pane |
| Trivial non-executable change (docs/wording) | Lane 1 micro-spoke | spawn subagent with `isolation: worktree`, review diff, land with `.ai-toolkit/scripts/worktree-land.sh <branch> --local` |
| Small obvious one-subtask change (code) | Lane 2 express spoke | `.ai-toolkit/scripts/worktree-new.sh <slug>` (no issue), single cycle, all push gates |

Never auto-merge or auto-teardown. Restate the branch/issue and the exact command, get a
quick yes, then run it. Merges and teardowns happen **on the hub**; task edits never do.

### 4. Report

Give the user a short read: how many spokes are running, which issues are unstarted, which
branches are ready to merge, and your single recommended next action.

`hub-status.sh` does not surface cost. For per-spoke token/cost attribution across runs,
point the user at Langfuse (the observability surface since #90) — cost is computed by
Langfuse from token usage (`ccusage` was retired in #91); see
`docs/telemetry-pull-layer.md` for the on-machine backfill source.

### Micro-spoke dispatch (lane 1)

Use a micro-spoke for any change that touches only non-executable paths: docs, comments,
and wording. Path restriction is absolute — lane 1 must never touch `scripts/`,
`shared/hooks/`, `tests/`, or any skill script (`.sh`, `.py`).

For the triage heuristic and lane definitions see `shared/rules/workflow.md`.

**Full lane-1 flow:**

1. **Spawn** a subagent with `isolation: worktree` and a tight prompt. The prompt must
   specify:
   - The exact files to touch and the exact change to make.
   - That the commit must be `docs:` or `chore:` type.
   - That staging and committing must use plain `git add <files>` followed by a
     standalone `git commit -m "<message>"` — no `-a`, no pathspec on the commit command,
     no chaining or prefixes (the gate only exempts a standalone plain commit).
   - That the subagent must return its branch name and a diff summary when done.
2. **Review** the returned diff on the hub before doing anything else.
3. **Land** with:

   ```bash
   .ai-toolkit/scripts/worktree-land.sh <branch> --local
   ```

   `--local` skips upstream guards (micro-spokes never push). It accepts a bare local
   branch whose temp worktree may already be gone, refuses any branch that has an
   upstream (that is not a micro-spoke), and refuses the default branch itself. No issue
   to close. `--keep-branch` is honored if you need the branch for follow-up.

4. **Verify cleanup.** A landed micro-spoke leaves nothing behind: no branch, no
   worktree, no tmux window (none were created beyond the temp worktree).

## Rules of thumb

- One survey per sit-down — re-run after a merge or a dispatch to refresh the picture.
- Keep the hub on `main`. If a survey shows the hub checkout dirty or off `main`, flag it.
- The issue is the contract — dispatch with a kickoff that lets the spoke run on its own
  (`/source` then `/cycle`).
