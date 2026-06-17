# AFK

Drain the backlog **unattended**. `/afk` is the single hub toggle that keeps issues
flowing — plan, dispatch, answer, land, refill — with zero human input for a bounded
window (or until the backlog is empty). It is the supervisor that ties the
parallel-worktrees workflow together, and it replaces the legacy night mode
(`hub-night` / scout / morning).

Use it on the **main checkout** (the hub), on the default branch, when you are stepping
away: "drain the backlog while I'm out", "run AFK for an hour", or `/afk <duration>`.

## What it does each tick

`hub-afk.sh` (`shared/skills/hub/scripts/hub-afk.sh`) runs a continuous supervisor loop.
Every tick it:

1. **Plans + dispatches** the next concurrent batch via `batch-plan.sh` (`/next-batch`'s
   planner), seeding each new spoke with the standard ultra kickoff. Already-in-flight
   spokes are picked up, not re-spawned.
2. **Auto-answers** every spoke parked on a question or PLAN gate (⚠ WAITING ON INPUT). It
   extracts the prompt from the spoke's transcript and hands it to an **answerer** — a
   headless `claude` reasoning step with a thinking budget that follows the
   `afk-answering` rule — then injects the returned answer into the spoke's tmux pane.
   This is the **one** reasoning step in an otherwise scripted control plane; a decision
   that is genuinely the human's (irreversible, outward-facing, or scope-changing) is
   **escalated** to `blocked/<issue>` instead of answered.
3. **Auto-lands** every `ready/<issue>` via `worktree-land.sh` (suite + merge + push +
   teardown + close). A failed land (merge conflict / suite failure) emits
   `blocked/<issue>` and the drain continues; a landed issue frees its scope and unblocks
   its dependents for the next tick's plan.
4. **Reaps** a hung or over-ceiling spoke (`blocked/<issue>`) so a doom-loop can't burn
   the window.

It **writes no report**. Every auto-answer and every outcome (landed / blocked / running)
is a **telemetry span on the observability dashboard** — the single source of truth for
what happened during the run.

## Stop conditions

How the run ends is the argument:

| Command | Stops when |
|---------|-----------|
| `/afk <duration>` | the duration elapses — e.g. `90` (minutes), `30m`, `1h`, `1h30m` |
| `/afk until <HH:MM>` | the next `HH:MM` is reached (today if still ahead, else tomorrow) |
| `/afk drain` | the backlog is empty **and** nothing is in flight — no clock bound |

Use a clock bound (`<duration>` / `until HH:MM`) when you will be back at a known time.
Use **`drain`** for a trip: it has no clock and stops only when the work is genuinely done
(this is the mode remote AFK builds on).

Plus two control subcommands:

- `/afk off` — stop the supervisor (clears the state file; the loop exits on its next
  tick).
- `/afk status` — report the active window and time remaining, or `off`.

## Workflow

### 1. Preconditions

Run from the **hub** (main checkout), on the default branch, with a clean tree —
`worktree-land.sh` refuses to land from a dirty hub. `gh` must be authenticated. Confirm
the stop condition with the user before arming if it is not explicit.

### 2. Arm the supervisor

The loop is long-running, so launch it in the **background** and let the hub session stay
responsive. It self-terminates at the stop condition.

```bash
# clock-bound
.ai-toolkit/scripts/hub/hub-afk.sh 1h
# or until a wall-clock time
.ai-toolkit/scripts/hub/hub-afk.sh until 07:00
# or drain to empty (no clock)
.ai-toolkit/scripts/hub/hub-afk.sh drain
```

Arming writes the end bound to `<git-common-dir>/.afk-state` (an epoch, or `drain`), so a
restart resumes the same window and a second shell can flip it off.

### 3. Observe on the dashboard

There is no report artifact. Watch the run on the observability dashboard: each
auto-answer is an `afk-answer` span on the answered spoke, and every land / block / reap
is a lifecycle outcome. The dashboard is where you see what AFK did.

### 4. Stop early or check in

```bash
.ai-toolkit/scripts/hub/hub-afk.sh --status   # how long is left?
.ai-toolkit/scripts/hub/hub-afk.sh --off       # stop now
```

## Rules of thumb

- **Concurrency is graph-bound only** — the batch is whatever disjoint scopes and the
  dependency graph allow (no machine cap in v1). Keep `Scope:` lines tight so independent
  work actually runs in parallel.
- **The answerer's bar is answer quality, not speed.** Escalation is the safe fallback: a
  wrong auto-answer costs a cycle, a needless escalation costs minutes of your morning.
  The policy lives in the `afk-answering` rule.
- **AFK plays the human, so spokes run in their normal attended posture** — they pause at
  the PLAN gate and ask questions as usual, and the supervisor answers. Dispatch a single
  task by hand with `/next-batch` or `start-task` if you only want one spoke.
- **It only dispatches and lands — it never authors code.** The hub invariant holds: the
  supervisor decides *what* runs; each spoke does the *how*.

## Related skills

- `next-batch` — the same planner, run once and by hand; `/afk` calls it every tick. Use
  `/next-batch` when you are attending and want to fan out a single batch.
- `land` — the hub-side `/land <id>`; `/afk` runs `worktree-land.sh` automatically.
- `hub` — orient the planning session and survey what is in flight.
- `solo-cycle` — the per-subtask RED / GREEN / REVIEW / PUSH cycle each spoke follows; its
  gate action is **agent-review / auto-answer** under `/afk`.
