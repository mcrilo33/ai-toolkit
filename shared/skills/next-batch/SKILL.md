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
- **Greedy disjoint-scope pack** — it walks ready issues in priority order and adds
  one only when its `Scope:` is disjoint from every issue already in the batch **and**
  every in-flight spoke. `Scope: *` or a missing line marks an issue **exclusive** —
  it runs alone, never batched.

It prints issue numbers only and never dispatches.

## Workflow

### 1. Gather the in-flight scopes

A spoke that is already running owns its files for the duration. Pass each live
spoke's `Scope:` to the planner so a ready issue colliding with live work is held
back. Read the in-flight worktrees and their issues' scopes — `hub-status.sh` lists
the active worktrees and their issue numbers; the `Scope:` line is in each issue
body (`gh issue view <n> --json body`).

Pass one `--inflight` flag per live spoke:

```bash
.ai-toolkit/scripts/batch-plan.sh \
  --inflight "shared/hooks/foo.sh tests/unit/test_foo.py" \
  --inflight "dashboard/app.py"
```

With no spokes running, call it bare:

```bash
.ai-toolkit/scripts/batch-plan.sh
```

### 2. Show the proposed batch

Present the computed issue numbers to the user with each issue's title and `Scope:`,
and note any notable exclusions (an issue held back by a scope collision or an open
blocker). Get a quick OK before spawning anything — the planner proposes; the human
confirms the fan-out.

### 3. Dispatch each issue

For every issue `N` in the batch, spawn its worktree and seed the spoke with the full
ultra kickoff (same handoff `start-task` uses). Each gets its own issue, worktree, and
tmux window:

```bash
.ai-toolkit/scripts/worktree-new.sh N --prompt "<kickoff>"
```

The kickoff hands the spoke everything it needs to run on its own — anchor to the
issue, build a task ledger, honor the issue's `Gate:` line, then run the solo-cycle
(RED → GREEN → REVIEW → PUSH) and push + emit `ready/N` when the acceptance criteria
are met. Use the kickoff template documented in the `start-task` skill (step 4); do
not invent a new one.

### 4. Report the fan-out

Tell the user the issues dispatched, their branches, worktree paths, and tmux windows.
The spokes are now running in parallel. The hub lands each one (`/land <id>`) as it
reaches `ready/N`.

## Rules of thumb

- The **hub stays on `main` and read-only** — `/next-batch` decides *what* runs
  concurrently; each spoke does the *how*.
- Re-run it after a spoke lands to compute the **next** batch — closing an issue can
  unblock its dependents and free its scope for a colliding peer.
- Keep `Scope:` lines tight and honest. An over-broad scope (or `*`) needlessly
  serializes work that could have run in parallel.
- It honors **no concurrency cap** — the batch is whatever the dependency graph and
  scope-disjointness allow. Dispatch a subset by hand if you want fewer live spokes.

## Related skills

- `hub` — orient the planning session and survey what is in flight before fanning out
- `start-task` — the single-issue hub → spoke handoff; `/next-batch` dispatches a whole
  batch using the same kickoff
- `land` — the hub-side `/land <id>` that ends each task once its spoke has pushed
- `solo-cycle` — the per-subtask RED / GREEN / REVIEW / PUSH cycle each spoke follows
