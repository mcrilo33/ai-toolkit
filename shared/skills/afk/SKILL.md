# AFK

Drain the backlog **unattended**. `/afk` is the single hub toggle that keeps issues
flowing — plan, dispatch, answer, land, refill — with zero human input for a bounded
window (or until the backlog is empty). It is the single unattended supervisor that
ties the parallel-worktrees workflow together.

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

- `/afk off` — stop the supervisor **and its watchdog** (clears the state file; both
  loops exit on their next tick).
- `/afk status` — report the active window and time remaining, `off`, or **`STALE`** when
  the window is armed but the supervisor process has died (see below).

## Staying alive: heartbeat + watchdog

The supervisor is a long-running loop, and a silent crash (it once exited `0`
mid-dispatch) used to strand the whole run: `.afk-state` still read `draining`, so
`status` reported a healthy run that no longer existed, and an in-flight spoke kept
running with no answerer. Two mechanisms close that gap (issue #107):

- **Heartbeat.** Each tick the supervisor stamps `<pid> <last_tick_epoch>` to
  `<git-common-dir>/.afk-heartbeat`. `status` cross-checks it against pid liveness, so a
  crashed supervisor is reported as `STALE — last tick <N>m ago, supervisor process not
  found` instead of echoing the stale state file.
- **Watchdog.** A thin keeper loop, auto-armed alongside the supervisor (and re-checked
  every tick, so the two keep each other alive), respawns the supervisor whenever the
  window is armed but no live process is stamping the heartbeat. The respawn is a no-arg
  resume: it reads the persisted window and **re-adopts** in-flight worktrees
  idempotently rather than re-dispatching. Exactly one watchdog runs per checkout; `off`
  clears the state, so it exits within one watchdog interval (`AFK_WATCHDOG_SECONDS`,
  default 60).

## Remote AFK (an always-on Mac)

When you are away from the machine that can run the backlog, `/afk --remote` triggers the
drain on a configured **always-on second Mac** (e.g. one at home) over SSH and returns. It
runs unattended there on the **same Claude subscription** — no API key, no proxy.

```bash
# from anywhere (work, travel) — needs AFK_REMOTE_HOST + AFK_REMOTE_REPO configured
shared/skills/hub/scripts/hub-afk.sh --remote
```

It SSHes to the host (a Tailscale hostname reaches it across networks), starts a detached,
`caffeinate`-wrapped `drain` in a named tmux session, confirms the session came up, and
prints the reattach command. The host is configured by env or a sourced `~/.afk-remote`:

| Variable | Meaning |
|----------|---------|
| `AFK_REMOTE_HOST` | the always-on Mac's (Tailscale) hostname — **required** |
| `AFK_REMOTE_REPO` | the repo path on that host — **required** |
| `AFK_REMOTE_SESSION` | the detached tmux session name (default `afk`) |
| `AFK_REMOTE_DRAIN_CMD` | the command run under `caffeinate` (default: the supervisor itself) |

The one-time host setup (subscription `/login`, auto-login + unlocked Keychain, `caffeinate`,
the Tailscale trigger, and a GitHub-poll fallback) is the runbook in
[`docs/remote-afk.md`](../../../docs/remote-afk.md).

If the subscription token cannot refresh mid-run, the supervisor blocks the affected spokes
(`blocked/<issue>`, visible on the dashboard) and **stops** rather than spinning — you fix
auth (`/login`) on the host and re-trigger.

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
shared/skills/hub/scripts/hub-afk.sh 1h
# or until a wall-clock time
shared/skills/hub/scripts/hub-afk.sh until 07:00
# or drain to empty (no clock)
shared/skills/hub/scripts/hub-afk.sh drain
```

Arming writes the end bound to `<git-common-dir>/.afk-state` (an epoch, or `drain`), so a
restart resumes the same window and a second shell can flip it off.

### 3. Observe on the dashboard

There is no report artifact. Watch the run on the observability dashboard: each
auto-answer is an `afk-answer` span on the answered spoke, and every land / block / reap
is a lifecycle outcome. The dashboard is where you see what AFK did.

### 4. Stop early or check in

```bash
shared/skills/hub/scripts/hub-afk.sh --status   # how long is left?
shared/skills/hub/scripts/hub-afk.sh --off       # stop now
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
