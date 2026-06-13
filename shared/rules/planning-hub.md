# Planning Hub

Defines the role of a session running on the **main checkout** in the parallel-worktrees
workflow. When you are on the main checkout, you are the **planning hub** — a launcher and
merge point, never a place where task code is written. Full model and lifecycle live in
`docs/parallel-worktrees.md`.

## The invariant

**The main checkout always stays on `main` and never holds task work.** Every task lives
in its own worktree, on its own branch, driven by its own session. The hub thinks,
decides, decomposes, dispatches, and merges — it does not implement.

**The hub never authors changes, not even docs.** Trivial documentation edits are
delegated to micro-spokes (lane-1 worktree-isolated subagents). A micro-spoke passes
`hub-guard.sh` by construction — the guard is path-anchored to the main checkout on the
default branch and is a no-op in any linked worktree. No carve-outs exist in
`hub-guard.sh` and none will be added. Authorship and review are kept separate: the hub
reviews the micro-spoke's diff and lands it; it never writes the diff.

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
  prompt. Lanes 2 and 3 both route through `start-task`; lane 1 is handled directly from
  the hub as a micro-spoke (see below).
- **Merge and tear down.** When a spoke's branch is pushed, land it from the hub
  (`git merge`), push `main`, then `scripts/worktree-done.sh` to remove the worktree.

### Delegating trivial work (micro-spokes)

A **micro-spoke** (lane 1) is a worktree-isolated subagent used for changes that touch
no executable paths. Path restriction: docs, comments, and wording only — never
`scripts/`, `shared/hooks/`, `tests/`, or skill scripts (`.sh`, `.py`).

Full lane-1 flow:

1. Hub spawns a subagent with `isolation: worktree` and a tight prompt specifying: the
   exact files to touch, the exact change to make, and that the commit must be a
   `docs:`/`chore:` type using `git add <files>` + `git commit -m` (no `-a`, no pathspec,
   no chaining). The subagent must return its branch name and a diff summary.
2. Subagent edits and commits inside its temp worktree. The `docs:`/`chore:` no-issue
   commit passes the `commit-quality` gate via the scoped exemption (only staged
   non-executable files, correct modes, outside the restricted directories).
3. Hub reviews the returned diff.
4. Hub lands it:

   ```bash
   scripts/worktree-land.sh <branch> --local
   ```

   `--local` skips upstream guards (micro-spokes never push), accepts a bare local branch
   whose temp worktree may already be gone, refuses any branch that has an upstream
   ("not a micro-spoke"), and refuses the default branch itself. No issue to close.
5. Nothing is left behind: no branch, no worktree, no tmux window.

## Hub must not

- Write or edit any file on the main checkout — code, docs, or otherwise. If you catch
  yourself about to implement or even make a small edit on `main`, stop and dispatch a
  spoke instead (lane 1 for docs, lane 2–3 for code). `source-task` guards this too.
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
