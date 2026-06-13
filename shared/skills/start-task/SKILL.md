# Start Task

Dispatch a planned task from the **planning hub** into a running **execution spoke**
in one step: create the GitHub issue, spawn its worktree, and seed the spoke's first
prompt. This is the hub → spoke handoff in the parallel-worktrees workflow.

Use it in the long-lived planning session (on the main checkout) once scope is clear
and the user says "start this work", "spin it up", or "let's build X". For the model
and lifecycle see `docs/parallel-worktrees.md`.

## Preconditions

- Run from the **main checkout** (the hub), which stays on `main`.
- `gh` is authenticated and `.ai-toolkit/scripts/worktree-new.sh` is installed
  (every synced repo has it; `sync-to-repo.sh` puts it there).
- The scope has been discussed enough to write a clear issue.

## Workflow

### 0. Triage the lane

Before creating an issue or spawning a worktree, classify the task (~10 seconds):

- Does the change touch executable behavior? **No** → **Lane 1 (micro-spoke).** Do not
  create an issue and do not call `worktree-new.sh`. Dispatch a micro-spoke from the hub
  instead: spawn a subagent with `isolation: worktree`, review its diff, and land with
  `.ai-toolkit/scripts/worktree-land.sh <branch> --local`. Lane 1 is restricted to non-executable
  paths only (docs, comments, wording) — never `scripts/`, `shared/hooks/`, `tests/`, or
  skill scripts. See the hub skill's "Micro-spoke dispatch (lane 1)" section for the full
  flow.
- One subtask, obvious approach, small diff? → **Lane 2 (express spoke).** Skip issue
  creation (steps 1–3 below). Dispatch directly:

  ```bash
  .ai-toolkit/scripts/worktree-new.sh <slug> --prompt "<kickoff>"
  ```

  The spoke runs a single cycle under all push gates. No issue, no task ledger.
- Otherwise, when in doubt, or when the "why" should be findable later → **Lane 3
  (full).** Continue with step 1 below.

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
.ai-toolkit/scripts/worktree-new.sh N --type <type> --prompt "<kickoff>"
```

This creates `feature/N-<slug>` (or `<type>/N-<slug>`), copies `.claude/`, folds the
worktree into the VS Code review window, opens a tmux window named `N`, and launches
`claude` seeded with the kickoff. A good kickoff hands the spoke everything it needs to
run on its own:

```
You're in a dedicated worktree for issue #N. Run /source to anchor to issue #N and
read it. Before touching code, break the issue body into a task ledger (TaskCreate, or TodoWrite on older runtimes) — one
todo per subtask × the solo-cycle steps that apply (ANCHOR/RED/GREEN/REVIEW/PUSH),
exactly one in_progress. Then implement it following the solo-cycle (/cycle: RED → GREEN →
REVIEW → PUSH). The task ledger is ephemeral session scratch; issue #N stays the
durable contract — skip the ledger only if the task is genuinely single-step. Ask me
before any irreversible step.
```

### 5. Report the handoff

Tell the user: the issue URL, the branch, the worktree path, and the tmux window
(`prefix` + `N` to switch to it). The spoke is now running on its own.

## Rules of thumb

- The **hub stays on `main` and read-only** — it decides *what*; the spoke does the
  *how*. The `source-task` guard nudges you here if you start coding on the hub.
- The **issue is the contract** between hub and spoke. The spoke begins with a fresh,
  focused context containing just that issue — planning noise doesn't leak in. The
  spoke's task ledger is ephemeral session scratch; the issue stays the durable
  contract.
- For several **independent** tasks, repeat per task (each its own issue + worktree +
  tmux window). Sequence dependent tasks instead of fanning out.
- If the user already has an issue number, skip steps 1–3 and dispatch directly.

## Edge cases

| Situation | Action |
|-----------|--------|
| Not on the main checkout | `cd` to the hub first; worktrees are created relative to the main root |
| `gh` not authenticated | Ask the user to `gh auth login`, or proceed ad-hoc with a slug instead of an issue |
| Scope still fuzzy | Stay in the hub; use the `brainstorming` skill before dispatching |
| Task is tiny / docs-only | Lane 1 micro-spoke: no issue, no worktree-new — dispatch a subagent with `isolation: worktree` from the hub and land with `--local` |
| Task is small but touches code | Lane 2 express spoke: skip steps 1–3, dispatch `worktree-new.sh <slug>` ad-hoc |

## Related skills

- `source-task` — the spoke runs this first to anchor to the issue and confirm the branch
- `solo-cycle` — the per-subtask RED / GREEN / REVIEW / PUSH cycle the spoke follows
- `brainstorming` — refine a fuzzy idea in the hub before dispatching
- `land` — the hub-side `/land <id>` that ends the task once the spoke has pushed
