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

### 2. Pick the branch type and the gate level

Choose a branch type: `feature` (new capability), `fix` (bug), or `chore`
(maintenance).

Then choose the **gate level** — where the spoke pauses for human review, declared
per task by **risk / novelty** (see the gate spectrum in `solo-cycle`):

| Task type | Gate level |
|-----------|------------|
| Very-clear / trivial / mechanical | **none** — autonomous to `ready/` |
| **Standard (DEFAULT)** | **PLAN gate** |
| Novel / risky / ambiguous | PLAN + RED gate |
| GUI / behavioral | PLAN + human-acceptance RED + draft-review |

**PLAN is the default for all but very-clear work** — a rubber-stamped plan costs
seconds, a wrong autonomous dev costs the whole cycle. Only declare `none` when the
task is genuinely very-clear. (Only the PLAN gate is live today; the RED, human-
acceptance, and draft levels are declared the same way but their machinery is pending
follow-up issues — see `solo-cycle`.)

### 3. Create the issue

Record the chosen gate level as a **`Gate:`** line in the issue body so it is part of
the durable contract (the spoke reads it when it anchors), then create the issue:

```bash
gh issue create --title "<title>" --body "<plan>

Gate: plan   # plan (default) or none; the richer levels are pending follow-ups"
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
You're in a dedicated worktree for issue #N (Gate: <level>). Run /source to anchor to
issue #N and read it. Before touching code, break the issue body into a task ledger
(TaskCreate, or TodoWrite on older runtimes) — one todo per subtask × the solo-cycle
steps that apply (ANCHOR/RED/GREEN/REVIEW/PUSH), exactly one in_progress.

This task's gate is <level>. If it is `plan` (the default for non-trivial work): the
PLAN gate comes first — explore the code, then **print the full implementation plan
(files, approach, test strategy, open questions) as a normal visible message** before
any approval ask, and WAIT for my approval before writing code (before GREEN). Do not
defer the plan into an approval card — the message itself is the plan. Park there
rather than blocking: emit the `gate/N` marker (`bash .ai-toolkit/scripts/spoke-ready.sh
--gate N`) so the hub sees you parked, then stop with an explicit "reply to approve, or
tell me what to change" and proceed into the cycle once I approve. If
the gate is `none` (very-clear work), skip the PLAN gate and run autonomous straight
through.

Then implement it following the solo-cycle (/cycle: RED → GREEN → REVIEW → PUSH). The
task ledger is ephemeral session scratch; issue #N stays the durable contract — skip the
ledger only if the task is genuinely single-step. Push your own branch on every subtask
without asking; when your ledger shows the issue's acceptance criteria are all met, that
is the final subtask — push and emit the `ready/N` marker, also without asking. The
routine own-branch push plus ready emission needs no approval. Still ask me before
genuinely dangerous or irreversible ops: force-push / `--force-with-lease`, history
rewrites, anything touching the default branch (`main`), or deletions outside the
worktree. Do NOT self-land — the hub lands #N.
```

> [!NOTE]
> On Claude the marker tag pushes (`gate/N`, `ready/N`) are advisory and proceed.
> On Cursor `push-scope-guard` denies a spoke's tag pushes by default (it allows
> only the spoke's own branch), so approve the marker-push prompt when it appears —
> the same applies to the existing `ready/N` push.

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
