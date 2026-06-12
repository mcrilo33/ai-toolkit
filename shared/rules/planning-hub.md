# Planning Hub

Defines the role of a session running on the **main checkout** in the parallel-worktrees
workflow. When you are on the main checkout, you are the **planning hub** — a launcher and
merge point, never a place where task code is written. Full model and lifecycle live in
`docs/parallel-worktrees.md`.

## The invariant

**The main checkout always stays on `main` and never holds task work.** Every task lives
in its own worktree, on its own branch, driven by its own session. The hub thinks,
decides, decomposes, dispatches, and merges — it does not implement.

```
 PLANNING HUB  (main checkout, long-lived)   think → decide → write issue → dispatch
        │  hands off via an issue + start-task
        ▼
 SPOKE N  (worktree + branch + seeded session)   /source → /cycle → push
```

| Aspect | Planning hub | Execution spoke |
| ------ | ------------ | --------------- |
| Lives on | the main checkout (`main`) | its own worktree + branch |
| Lifespan | long — reused across tasks | short — created, worked, merged, destroyed |
| Touches code | no — explores and decides | yes — gated RED / GREEN / REVIEW / PUSH |
| Produces | GitHub issues + the launch command | commits on the task branch |

## Hub responsibilities

- **Orient.** On a fresh hub session, survey what is in flight before acting — run the
  `hub` skill (`/hub`) for the live dashboard of worktrees, open issues, and mergeable
  branches.
- **Decide and decompose.** Turn rough ideas into focused, self-contained GitHub issues.
  The **issue is the contract** between hub and spoke, so the spoke starts with clean
  context and planning noise never leaks in.
- **Dispatch.** Once scope is clear, hand off with the `start-task` skill — it creates the
  issue, spawns the worktree via `scripts/worktree-new.sh`, and seeds the spoke's first
  prompt.
- **Merge and tear down.** When a spoke's branch is pushed, land it from the hub
  (`git merge`), push `main`, then `scripts/worktree-done.sh` to remove the worktree.

## Hub must not

- Write or edit task code on the main checkout. If you catch yourself about to implement
  on `main`, stop and dispatch a spoke instead — `source-task` guards this too.
- Create branches for task work on the main checkout. Branches are created inside their
  worktrees by `worktree-new.sh`.

This rule is not auto-applied to every session. It is surfaced on demand — invoke the
`hub` skill (`/hub`) at the start of a main-checkout session to load the role and survey
what is in flight.

## Related

- `docs/parallel-worktrees.md` — full topology, scripts, and daily loop
- `start-task` skill — the hub → spoke handoff in one step
- `source-task` skill — anchors a spoke to its issue; warns on the shared main checkout
- `hub` skill — live status dashboard for the hub
