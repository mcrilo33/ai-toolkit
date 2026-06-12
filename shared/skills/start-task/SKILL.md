# Start Task

Dispatch a planned task from the **planning hub** into a running **execution spoke**
in one step: create the GitHub issue, spawn its worktree, and seed the spoke's first
prompt. This is the hub → spoke handoff in the parallel-worktrees workflow.

Use it in the long-lived planning session (on the main checkout) once scope is clear
and the user says "start this work", "spin it up", or "let's build X". For the model
and lifecycle see `docs/parallel-worktrees.md`.

## Preconditions

- Run from the **main checkout** (the hub), which stays on `main`.
- `gh` is authenticated and `scripts/worktree-new.sh` exists on `main`.
- The scope has been discussed enough to write a clear issue.

## Workflow

### 1. Confirm the scope

Restate the decided task back to the user as a draft issue — **title** plus a short
**body** (problem, proposal, acceptance criteria drawn from the planning conversation).
Get a quick OK before creating anything. Don't invent scope the user didn't agree to.

### 2. Pick the branch type

Choose one: `feature` (new capability), `fix` (bug), or `chore` (maintenance).

### 3. Create the issue

```bash
gh issue create --title "<title>" --body "<plan>"
```

Capture the issue number `N` from the returned URL.

### 4. Dispatch the worktree + seed the spoke

```bash
scripts/worktree-new.sh N --type <type> --prompt "<kickoff>"
```

This creates `feature/N-<slug>` (or `<type>/N-<slug>`), copies `.claude/`, folds the
worktree into the VS Code review window, opens a tmux window named `N`, and launches
`claude` seeded with the kickoff. A good kickoff hands the spoke everything it needs to
run on its own:

```
You're in a dedicated worktree for issue #N. Run /source to anchor to issue #N and
read it, then implement it following the solo-cycle (/cycle: RED → GREEN → REVIEW →
PUSH). Ask me before any irreversible step.
```

### 5. Report the handoff

Tell the user: the issue URL, the branch, the worktree path, and the tmux window
(`prefix` + `N` to switch to it). The spoke is now running on its own.

## Rules of thumb

- The **hub stays on `main` and read-only** — it decides *what*; the spoke does the
  *how*. The `source-task` guard nudges you here if you start coding on the hub.
- The **issue is the contract** between hub and spoke. The spoke begins with a fresh,
  focused context containing just that issue — planning noise doesn't leak in.
- For several **independent** tasks, repeat per task (each its own issue + worktree +
  tmux window). Sequence dependent tasks instead of fanning out.
- If the user already has an issue number, skip steps 1–3 and dispatch directly.

## Edge cases

| Situation | Action |
|-----------|--------|
| Not on the main checkout | `cd` to the hub first; worktrees are created relative to the main root |
| `gh` not authenticated | Ask the user to `gh auth login`, or proceed ad-hoc with a slug instead of an issue |
| Scope still fuzzy | Stay in the hub; use the `brainstorming` skill before dispatching |
| Task is tiny / docs-only | Still dispatch a worktree so gates apply, or handle inline if truly trivial |

## Related skills

- `source-task` — the spoke runs this first to anchor to the issue and confirm the branch
- `solo-cycle` — the per-subtask RED / GREEN / REVIEW / PUSH cycle the spoke follows
- `brainstorming` — refine a fuzzy idea in the hub before dispatching
- `close-task` — use in the spoke instead of solo-cycle when a PR is wanted
