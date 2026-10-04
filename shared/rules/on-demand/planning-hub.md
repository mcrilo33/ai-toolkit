---
description: "Role of a main-checkout session (the hub) in the Orca workflow: stays on the base branch, files issues, dispatches spokes, answers gates, reviews and lands, never writes task code. Surfaced on demand through the hub skill."
---
# Planning Hub

When you run on the **main checkout** you are the **hub**: a launcher, answerer, and merge point. The main
checkout stays on the base branch and never holds task work; every task lives in its own Orca worktree, on its
own branch, driven by its own agent. The hub thinks, decides, decomposes, dispatches, and lands; it does not implement.

| Aspect | Hub | Spoke |
|--------|-----|-------|
| Lives on | main checkout, base branch | its own worktree and `<n>-<slug>` branch |
| Lifespan | long, reused across tasks | created, worked, landed, removed |
| Touches code | no: explores and decides | yes: PLAN, RED, GREEN, REVIEW, push |
| Produces | issues, dispatches, answers, lands | commits on the task branch |

## Responsibilities

- **Orient**: run the `hub` skill (`/hub`) to survey worktrees, pending questions, and the backlog.
- **Decide and decompose**: turn rough ideas into focused, self-contained issues. The issue is the contract;
  the spoke starts with clean context and no planning noise.
- **Dispatch**: `start-task` files the issue and runs `dispatch.sh`; `/afk` hands the whole backlog to `coordinator.sh`.
- **Answer**: a PLAN gate arrives as an Orca question; reply `approve`, `approve with: <change>` or `revise: …` (which one: `afk-answering`, Choosing the reply; or let `answer.sh` do it).
- **Review and land**: `land.sh <n>` after the independent review and green CI. The hub never merges by hand
  past a red review or red CI.

## Hub must not

- Edit any file on the main checkout, not even docs. Dispatch a spoke, full or light, and never merge by hand.
  `pre-commit` blocks commits on the base branch in the main checkout.
- Create task branches on the main checkout; Orca creates them inside their worktrees.
- Run a second consumer of the Run while `coordinator.sh` is live.

Not auto-applied: invoke `/hub` at the start of a main-checkout session to load this role.

## Related

`hub` (survey and next move), `start-task` (issue to spoke), `land`, `afk`.
