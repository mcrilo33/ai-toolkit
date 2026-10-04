---
name: start-task
description: "Dispatch a planned task from the hub into a running Orca worker: draft and file the GitHub issue with its Scope:/Gate:/Model: footer and blocked-by edges, then run dispatch.sh. Use in the hub session when the user says 'start this work', 'spin it up', or 'let's build X' after scope is clear."
argument-hint: "[issue number, or the task to file]"
---
# Start Task

Hub to spoke: the **issue is the contract**. The worker starts with a fresh context holding just that issue
(`.ai-toolkit/task.md`), so planning noise does not leak in. Run from the main checkout (see `hub`).

## 1. Scope

Restate the task as a draft issue: **title**, **body** (problem, proposal, acceptance criteria drawn from the
conversation). Get an OK before filing; do not invent scope. If the work falls into independent file groups,
draft one issue per group (`issue-hygiene`, Split what does not collide) and tell the user.

## 2. File the issue

```bash
gh issue create --title "<title>" --body "<body>

Scope: <files/globs the task touches>
Gate: plan
Model: <optional model id>"
```

The footer is the dispatch contract (see `.ai-toolkit/rules/issue-hygiene.md`):

| Line | Rule |
|------|------|
| `Scope:` | mandatory, concrete files/globs; missing or `*` runs the issue alone. Overlap with an in-flight issue serializes it |
| `Gate:` | `plan` (always, unless told otherwise: the worker `ask`s for approval of its plan). `none` is the light lane (no PLAN gate, no in-worker review): write it only when the user asked for light on this issue, never because the work looks small |
| `Model:` | optional override of `SPOKE_MODEL`; reasoning-heavy work gets an Opus id |

Labels: `priority` (dispatched first), `hold` (never dispatched). **Blocked-by**: answer "does this depend on
open work?" for every issue and declare a real native edge if yes (`github-issues/references/dependencies.md`);
never use one to encode file overlap. A chain with colliding `Scope:` is one issue with subtasks.

## 3. Dispatch

```bash
.ai-toolkit/scripts/dispatch.sh <n>      # or --next for the next ready issue
```

It starts one worker (`orca orchestration worker-start`, worktree `<n>-<slug>`, seed prompt from
`Gate:`), links the issue, and sets the workspace status. Report the issue URL, the branch, and the worktree to
the user; open the worktree in Orca to watch. A `Gate: plan` worker blocks in `ask` until someone replies: the
coordinator (`afk`) or you via `hub`.

## Edge cases

| Situation | Action |
|-----------|--------|
| Scope still fuzzy | stay in the hub; use `brainstorming` first |
| Issue already exists | skip to step 3 |
| `dispatch.sh` fails | it retries once; the failing stage is printed; do not re-run blindly |
