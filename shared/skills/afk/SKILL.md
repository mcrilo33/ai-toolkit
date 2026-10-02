# AFK

Drain the backlog **unattended**. `/afk` is the single hub toggle that keeps issues
flowing — plan, dispatch, answer, land, refill — with zero human input for a bounded
window (or until the backlog is empty). It is the single unattended supervisor that
ties the parallel-worktrees workflow together.

Use it on the **main checkout** (the hub), on the default branch, when you are stepping
away: "drain the backlog while I'm out", "run AFK for an hour", or `/afk <duration>`.

## What it does each tick

`hub-afk.sh` (`.ai-toolkit/scripts/hub-afk.sh`) runs a continuous supervisor loop.
Every tick it:

1. **Plans + dispatches** the next concurrent batch via `batch-plan.sh` (`/next-batch`'s
   planner), seeding each new spoke with the standard ultra kickoff. Already-in-flight
   spokes are picked up, not re-spawned.
2. **Auto-answers** every spoke parked on a question or PLAN gate (⚠ WAITING ON INPUT). It
   reads the prompt from the Orca inbox (a worker's `ask`) or the waiting agent's tool input
   and hands it to an **answerer** — a headless `claude` reasoning step with a thinking budget
   that follows the `afk-answering` rule — then replies to the recorded question (the ack is
   the proof) or types into the agent's Orca terminal.
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
Use **`drain`** for a long absence: it has no clock and stops only when the work is
genuinely done.

Plus two control subcommands:

- `/afk off` — stop the supervisor **and its watchdog** (clears the state file; both
  loops exit on their next tick).
- `/afk status` — report the active window and time remaining, `off`, or **`STALE`** when
  the window is armed but the supervisor process has died (see below).

## Prerequisite: keep the Mac awake (Orca)

`/afk` no longer holds the machine awake itself. Turn on Orca's
**`keepComputerAwakeWhileAgentsRun`** setting before an unattended window: system sleep
freezes the supervisor, every spoke, and the OTel stack mid-run, and wall-clock timers (reap
ceilings, staleness checks) misfire on wake. Caveat: the setting only holds **while an agent
is running**, so a drain idling between spokes (waiting on a gate, a land, or the next tick)
can still sleep — plug in and keep the lid open for a long window.

## Staying alive: heartbeat + watchdog

The supervisor is a long-running loop, and a silent crash (it once exited `0`
mid-dispatch) used to strand the whole run: `.afk-state` still read `draining`, so
`status` reported a healthy run that no longer existed, and an in-flight spoke kept
running with no answerer. Two mechanisms close that gap (issue #107):

- **Heartbeat.** Each tick the supervisor stamps `<pid> <last_tick_epoch> wake1` to
  `<git-common-dir>/.afk-heartbeat`. `status` cross-checks it against pid liveness, so a
  crashed supervisor is reported as `STALE — last tick <N>m ago, supervisor process not
  found` instead of echoing the stale state file. The trailing `wake1` is the
  wake-capability token (issue #207): a trap-armed supervisor advertises it so the
  Notification wake (`afk-notify-wake`) SIGUSR1s only a supervisor that can absorb the
  signal — a pre-trap supervisor stamps a bare two-field line and is left un-signalled (its
  default `SIGUSR1` action would terminate it) rather than killed. (`spoke-ready.sh`'s
  scripted-marker wake still signals unconditionally — folding it behind the same token gate
  is a pending follow-up.)
- **Watchdog.** A thin keeper loop, auto-armed alongside the supervisor (and re-checked
  every tick, so the two keep each other alive), respawns the supervisor whenever the
  window is armed but no live process is stamping the heartbeat. The respawn is a no-arg
  resume: it reads the persisted window and **re-adopts** in-flight worktrees
  idempotently rather than re-dispatching. Exactly one watchdog runs per checkout; `off`
  clears the state, so it exits within one watchdog interval (`AFK_WATCHDOG_SECONDS`,
  default 60).

## Self-update: the drain deploys its own code (issue #250)

When a land merges a change to the supervisor's **own** code (`hub-afk.sh`,
`gate-broker.sh`, the answerer rule, the sibling worktree/land/batch scripts), the running
drain redeploys onto the new code with **zero operator commands** — no `off → sync →
re-arm` recycle. After the land, the drain flags a self-update; at the **next tick
boundary** (never mid-tick) it validates + smoke-tests the merged source, re-syncs the
gitignored `.ai-toolkit/scripts`, then `exec`s itself in place as a no-arg resume onto the
new code. `exec` preserves the pid, so the heartbeat survives untouched and
the keeper keeps reading `live`; the resume **re-adopts** in-flight spokes. A broken new
version fails safe — the source is proven healthy **before** the re-sync, so the synced copy
the keeper respawns is never overwritten with broken code, and the drain stays on the old
code with a loud warning. Every self-deploy is journaled. `AFK_SELFUPDATE_SCOPE` overrides
the trigger set.

If the subscription token cannot refresh mid-run, the supervisor blocks the affected spokes
(`blocked/<issue>`, visible on the dashboard) and **stops** rather than spinning — see
[`docs/afk-arm-selfcheck.md`](../../../docs/afk-arm-selfcheck.md) for recovery.

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
.ai-toolkit/scripts/hub-afk.sh 1h
# or until a wall-clock time
.ai-toolkit/scripts/hub-afk.sh until 07:00
# or drain to empty (no clock)
.ai-toolkit/scripts/hub-afk.sh drain
```

Arming writes the end bound to `<git-common-dir>/.afk-state` (an epoch, or `drain`), so a
restart resumes the same window and a second shell can flip it off.

### 3. Observe on the dashboard

There is no report artifact. Watch the run on the observability dashboard: each
auto-answer is an `afk-answer` span on the answered spoke, and every land / block / reap
is a lifecycle outcome. The dashboard is where you see what AFK did.

### 4. Stop early or check in

```bash
.ai-toolkit/scripts/hub-afk.sh --status   # how long is left?
.ai-toolkit/scripts/hub-afk.sh --off       # stop now
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
