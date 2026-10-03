---
name: quick
description: "Express-interactive lane: build a small fix conversationally in its own Orca worktree and branch, without an issue, PLAN gate, or coordinator. Use when the user says /quick <slug> or wants a quick interactive fix without a full cycle."
argument-hint: "[slug]"
---
# Quick

For a small fix you want to drive interactively. No issue, no gate, no review round, no coordinator:

```bash
orca worktree create --name quick-<slug> --agent claude --prompt "<what to fix>"
```

The new worktree has its own Claude session (open it in Orca); the hub session does not edit code.

- Work in that session: failing test first when behavior changes, small commits, `git push -u origin quick-<slug>`.
- Landing is by hand: merge when CI is green (`git merge --ff-only`), then
  `orca worktree rm --worktree path:<worktree> --run-hooks`.
- The hooks still apply (`push-guard`, `danger-guard`, `secrets-scan`, `commit-msg`); CI is the test gate.
- If it grows beyond one subtask, file an issue and `start-task` it.
