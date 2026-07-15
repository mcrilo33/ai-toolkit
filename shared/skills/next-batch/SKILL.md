# Next Batch

Compute and dispatch the next set of issues that can safely run **concurrently**.
The PARALLEL / SERIAL / MERGE batching is a mechanical graph computation, not an LLM
judgment call: now that every issue carries a `Scope:` line and native blocked-by
edges, the next parallel batch stays scripted — no LLM tokens in the control plane.

Use it on the **main checkout** (the hub) when you want to fan out the backlog:
"what can run in parallel right now", "dispatch the next batch", or `/next-batch`. It
is usable manually and is **independent of `/afk`** — it runs on demand, not only
during an unattended drain.

## What the planner does

`batch-plan.sh` (`.ai-toolkit/scripts/batch-plan.sh`, synced from
the hub skill's `scripts/` directory) reads the open backlog in one `gh api graphql`
round-trip and prints the issue numbers of the next concurrent batch:

- **Eligibility** — an issue is *ready* only when all its blocked-by issues are
  closed.
- **Priority = critical-path depth** — ready issues are ranked by the longest
  blocked-by chain rooted at each one, so the longest serial tail is unblocked
  earliest (minimizes makespan). Ties break on direct-dependent count, then issue
  number.
- **Greedy disjoint-scope pack** — it walks ready issues in priority order and opens a new
  dispatch **unit** only when its `Scope:` is disjoint from every unit already in the batch
  **and** every in-flight spoke. `Scope: *` or a missing line marks an issue **exclusive** —
  it runs alone, never batched.
- **Same-scope subtask pack** (issue #278) — a unit is a **group** of issues, not one issue.
  When an issue wins a slot, the planner sweeps the remaining ready set for peers whose scope
  sits *inside* its own and folds them in as ordered subtasks on **one branch**. Such peers
  could never run concurrently anyway (they collide), so filing them separately used to pay
  the whole spoke lifecycle — worktree, first-push suite seed, PLAN gate, review, land —
  twice for nothing.

It prints issue numbers only and never dispatches.

### Output shape

Members are comma-joined within a unit, units space-separated. A lone issue looks exactly as
it always did:

```
263,265 270
```

That reads: **two spokes.** One on `#263`'s branch also shipping `#265` as a subtask, and one
on `#270`. `--cap` counts units (spokes), so a packed group consumes **one** slot.

Packing is deliberately conservative — it only ever *adds* work to a spoke that was already
going to run, and never changes which units form:

- A **partial overlap** (`a.py m.py` vs `a.py z.py`) does **not** pack. Neither spoke owns the
  other's full footprint, so they still serialize.
- A **superset** peer does not pack either — absorbing it would widen the unit past the scope
  its slot was granted for. It simply waits, and packs the other way round when *it* leads.
- `Split: intentional` opts an issue out entirely: it records that you *decided* these stay
  apart, and auto-packing them is exactly the merge you declined.

## Workflow

### 1. Gather the in-flight scopes

A spoke that is already running owns its files for the duration. Pass each live
spoke's `Scope:` to the planner so a ready issue colliding with live work is held
back. Read the in-flight worktrees and their issues' scopes — `hub-status.sh` lists
the active worktrees and their issue numbers; the `Scope:` line is in each issue
body (`gh issue view <n> --json body`).

Pass one `--inflight` flag per live spoke, and `--cap N` to bound the total live
spokes (issue #151 — a wide fan-out can starve the box and the co-located Langfuse):

```bash
.ai-toolkit/scripts/batch-plan.sh \
  --cap "$cap" \
  --inflight "shared/hooks/foo.sh tests/unit/test_foo.py" \
  --inflight "dashboard/app.py"
```

Resolve `$cap` the same way `/afk` does: the configured `batch.concurrency_cap`
(`python3 scripts/ai_toolkit_config.py batch-env settings/ai-toolkit.yml` emits
`AI_TOOLKIT_BATCH_CAP=<n>` when set), else auto `min(2, cores/4)` from the core count.
`batch-plan.sh` truncates the batch so `in-flight + newly-dispatched ≤ cap`; pass
`--cap 0` (or omit it) only when you deliberately want the historical unbounded batch.

With no spokes running and no cap, call it bare:

```bash
.ai-toolkit/scripts/batch-plan.sh
```

### 2. Show the proposed batch

Present the computed issue numbers to the user with each issue's title and `Scope:`,
and note any notable exclusions (an issue held back by a scope collision or an open
blocker). Get a quick OK before spawning anything — the planner proposes; the human
confirms the fan-out.

### 3. Dispatch each unit

Spawn **one worktree per unit**, not per issue. For a lone issue `N`, unchanged:

```bash
.ai-toolkit/scripts/worktree-new.sh N --prompt "<kickoff>"
```

For a packed unit `263,265`, the **primary leads** and the peers ride along:

```bash
.ai-toolkit/scripts/worktree-new.sh 263 --subtasks 265 --prompt "<kickoff>"
```

The branch still leads with the primary (`feature/263-<slug>`) — that is load-bearing, since
`inflight_worktrees` and `worktree-land` both parse the issue out of the leading digits of the
slug. `--subtasks` seeds the peers into the spoke's queued-subtask channel and appends a chain
note to the kickoff, so you do not write anything extra into the prompt yourself.

The kickoff hands the spoke everything it needs to run on its own — anchor to the
issue, build a task ledger, honor the issue's `Gate:` line, then run the solo-cycle
(RED → GREEN → REVIEW → PUSH) and push + emit `ready/N` when the acceptance criteria
are met. Use the kickoff template documented in the `start-task` skill (step 4); do
not invent a new one.

A packed spoke works its primary first, then re-anchors on each queued issue
(`/source-task <N>`) and emits `ready/<N>` per subtask. Its **terminal** `ready/<primary>` is
refused until the queue drains — that refusal is what stops the branch being landed with
subtasks outstanding — and `/land <primary>` then closes **every** issue the branch shipped.

### 4. Report the fan-out

Tell the user the issues dispatched, their branches, worktree paths, and tmux windows —
naming which issues share a spoke as packed subtasks, since that is the one thing the branch
name does not tell them. The spokes are now running in parallel. The hub lands each one
(`/land <primary>`) as it reaches `ready/<primary>`, closing every issue on that branch.

## Rules of thumb

- The **hub stays on `main` and read-only** — `/next-batch` decides *what* runs
  concurrently; each spoke does the *how*.
- Re-run it after a spoke lands to compute the **next** batch — closing an issue can
  unblock its dependents and free its scope for a colliding peer.
- Keep `Scope:` lines tight and honest. An over-broad scope (or `*`) needlessly
  serializes work that could have run in parallel.
- Two issues on the **same** scope are no longer a scheduling mistake — the planner packs them
  onto one spoke automatically (issue #278), which is what the `⚠ merge candidates` lint used
  to ask you to do by hand. The lint now fires only for clusters packing *cannot* absorb.
- It honors the **`batch.concurrency_cap`** ceiling (issue #151): pass `--cap` so the
  batch (plus in-flight spokes) never exceeds the cap. Beyond that, the batch is
  whatever the dependency graph and scope-disjointness allow; dispatch a subset by hand
  if you want fewer live spokes than the cap permits.

## Related skills

- `hub` — orient the planning session and survey what is in flight before fanning out
- `start-task` — the single-issue hub → spoke handoff; `/next-batch` dispatches a whole
  batch using the same kickoff
- `land` — the hub-side `/land <id>` that ends each task once its spoke has pushed
- `solo-cycle` — the per-subtask RED / GREEN / REVIEW / PUSH cycle each spoke follows
