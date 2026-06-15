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

### Pre-flight batching scout (night mode, optional)

Parallel night spokes are blind to each other, so before launching the dispatcher,
run the **scout** at setup (while you are awake) to avoid collisions:

```bash
bash .ai-toolkit/scripts/hub-scout.sh
```

It prints a dossier of facts — each `night` issue's file-scope hints (from its
`Scope:` line, see `start-task`), the raw file overlap between issues, and a
critical-path feasibility check (does an all-parallel / all-serial makespan fit
before `NIGHT_END`?). Then, reading that dossier, classify each overlapping pair and
stamp the verdict into the issues as body lines (an Opus scout agent does this; you
**approve** the plan before any spoke starts):

- **PARALLEL** — disjoint (or only incidentally overlapping) → run concurrently; no
  directive.
- **SERIAL** — would textually conflict → add `Serial-after: <earlier>` to the later
  issue so the dispatcher defers it until the earlier one has landed.
- **MERGE** — logically coupled (one spoke should own both, `Closes #a #b`) → add
  `Merge-into: <owner>` to the child so it is never dispatched alone. Keep merge
  clusters small (accept/reject as a unit); a large cluster falls back to SERIAL.

The scout only reports facts and the agent only classifies — the mechanical overlap
and feasibility math stay in the script (`#43`: don't let the LLM narrate mechanical
work). The supervisor then honors `Serial-after:` / `Merge-into:` from the issue
bodies; an over-committed night ("these 4 serialize → won't fit before 07:00") is
caught here, at setup, not wasted overnight.

### Overnight queue dispatcher (optional)

To drain a queue of pre-scoped issues overnight without supervision, run the night
dispatcher on the hub before bed:

```bash
bash .ai-toolkit/scripts/hub-night.sh
```

It reads the `night`-labelled open issues (`gh issue list --label night`) and dispatches
each via `worktree-new.sh`, recomputing an adaptive concurrency target every tick —
`clamp(ceil(tasks_left × T_task / time_left), 1, NIGHT_MAX_CONCURRENCY)` — so a short queue
runs sequentially and a long one ramps up to the cap as wake time approaches. It never
starts a spoke once less than `T_task` of the night remains, reuses a freed slot when a
spoke finishes (`ready/N`) or goes idle, and is idempotent — re-running skips branches
already in flight. Knobs (env, defaults): `NIGHT_END=07:00`, `NIGHT_MAX_CONCURRENCY=3`,
`NIGHT_TASK_MINUTES=90`. Use `--once` for a single tick (e.g. under cron). It dispatches
only — the hub still lands finished spokes on `/land`. The dispatcher also enforces a
per-spoke wall-clock ceiling (`NIGHT_SPOKE_MAX_MINUTES`, default 180): a hung, idle, or
runaway spoke is reaped (its window killed and a `blocked/N` emitted on its behalf) so
the unattended night never runs unbounded. At end of night it pre-computes the
land-triage for the morning report.

### Morning report (night mode)

On waking, run the morning report on the hub to turn the drained queue into a worklist
sorted fastest → slowest human effort:

```bash
bash .ai-toolkit/scripts/hub-morning.sh
```

It reads the terminal markers and tiers them — **LAND** (`ready/N`, merges clean,
agent-approved → rubber-stamp `/land N`), **EYEBALL** (`accept/N`, built + reviewed →
glance then land/send back), **THINK** (`blocked/N` → read the parked blocker, answer +
re-queue), **CONFLICTS** (`ready/N` whose throwaway merge hit a conflict → hand-resolve)
— with each row's diff size, trust summary (the marker's annotated-tag body), per-spoke
cost (reused from the #35 dashboard's pull layer), and the exact next command. A `gate/N`
still parked at the PLAN gate shows in a footer. The land-triage that decides LAND vs
CONFLICTS is pre-computed at end of night (`hub-morning.sh --triage`, called by the
dispatcher) in a hermetic throwaway worktree — a merge-conflict probe only, never the
test suite (the real gate fires at `/land`).

The terminal markers are also mirrored to GitHub as issue comments
(`hub-morning.sh --comments`, also run by the dispatcher at end of night): the spoke
only ever writes the git tag (it stays gh-read-only), and the hub — which has
gh-write — echoes each ready/accept/blocked marker's reason as a `gh issue comment`,
idempotently. A marker is thus durable as a git tag and visible on GitHub.

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
point the user at the observability dashboard (`dashboard/README.md`) — cost is reconciled
from `ccusage` offline; see `docs/telemetry-pull-layer.md` for the pull layer.

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
