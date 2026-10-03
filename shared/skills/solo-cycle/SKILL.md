---
name: solo-cycle
description: "Per-subtask cycle for a dispatched spoke, PR-less: anchor to the issue, PLAN gate (full lane), RED test, GREEN implementation, REFACTOR, one in-spoke code-review per subtask, push, then worker_done. Use when working a task from .ai-toolkit/task.md, when the user says /cycle, or wants commit+push per subtask."
argument-hint: "[subtask description or issue number]"
---
# Solo Cycle

The cycle of a **spoke**: one Orca worktree, one branch `<n>-<slug>`, one issue. There is no PR: the push is
the ship gate for a subtask, and `worker_done` ends the task. The coordinator (`coordinator.sh`, see `afk`)
then runs the independent review, waits for CI, and lands it (`land`). A spoke never merges, never lands,
and never pushes the base branch.

## What you start with

- `.ai-toolkit/task.md` — the issue (title, body, `Scope:` / `Gate:` / `Model:` footer). Read it first; it is
  the contract. Stay inside `Scope:`; flag anything that needs more.
- Your preamble from Orca carries the exact `ask` and `worker_done` commands for this dispatch (handle,
  task id, dispatch id). Run them verbatim. There is no other channel to the coordinator: a question or
  a result left in your terminal never reaches it.

## The PLAN gate (`Gate: plan`, the default)

Before any code: explore, then run your preamble's `ask` with the **complete plan as the question** (files,
approach, test strategy, open questions) and `--options approve,revise`. It blocks until the coordinator
(`answer.sh` or the human) replies.

| Reply | You |
|-------|-----|
| `approve` | start the cycle |
| `revise: …` | amend the plan, `ask` again (max 2 rounds, then `worker_done --outcome failed` naming the blocker) |
| the call times out | re-run the same `ask`; never start coding unanswered |

`Gate: none` is the **light lane**, set only by the user for that issue: it skips this gate and the in-spoke
`code-review` (step 5); anchor, RED/GREEN, push and the independent review at land all stay. Never edit code, tests, or config before `approve` on a `plan` issue.

## The cycle (per subtask)

| # | Step | Outcome |
|---|------|---------|
| 1 | ANCHOR | commits reference the issue (`#<n>` in the message; `commit-msg` checks the format) |
| 2 | RED | a failing test, committed alone; you saw it fail for the right reason |
| 3 | GREEN | the minimal implementation; the new test and the existing suite pass |
| 4 | REFACTOR | tidy with tests green; no behavior change |
| 5 | REVIEW | full lane only: the `code-review` subagent on the subtask's diff; fix every blocker, then re-review |
| 6 | PUSH | `git push -u origin <branch>`, plain, one per subtask |

Then the next subtask, from step 2.

### RED and GREEN

Write the failing test first and run it. Delegating to `tdd-red` / `tdd-green` / `tdd-refactor` is optional
(clean context for larger work; each runs on the model in its own frontmatter). Commit RED and GREEN
separately. Never weaken a test to get green: no deleted or loosened assertions, no `skip`/`xfail`, no
`sys.exit(0)`. The independent reviewer reports `tdd_followed` and `tests_weakened`, and either one blocks
the land.

Non-TDD subtasks (docs, config, chores) skip RED; anchor, review, and push still apply.

### REVIEW

Spawn `code-review` once per subtask on `git diff origin/<base>...HEAD`. It is advisory in the spoke (no
artifact, no gate): its job is to cut the rounds of the real review, which the coordinator runs on your
pushed branch with a different model. REQUEST_CHANGES: fix and re-run it. Any commit after a review means
the next push needs a fresh one.

### PUSH

`git push -u origin <branch>` as its own command (no pipes, no `&&` chains, so the exit code is the push's).
Push without asking: it is your own ephemeral branch and nothing merges from it until the land. `push-guard`
denies the base branch, `--force*`, and `--no-verify`; a denial is by design, so change the approach.
CI runs on your branch; format and lint before committing so it stays green.

## Finishing

When every acceptance criterion in `task.md` holds and the last subtask is pushed, run your preamble's
`worker_done --outcome succeeded` with a 3-sentence body: what you did, what you found, what is left.
Then stop; do not poll and do not start new work.

If you are stuck (ambiguity you cannot resolve, a failing dependency you do not own, a scope breach), send
`--outcome failed` with the blocker. The coordinator labels the issue `blocked` and tells the human.

If the coordinator re-engages you with `address: …`, that is the independent review (or a CI/merge
conflict): fix exactly those items, run steps 5-6, and send `worker_done` again.

## Rules of thumb

- Push once per subtask; commit granularity inside it is free (RED + GREEN minimum for TDD).
- Strong criteria ("tests X, Y pass") beat weak ones ("make it work"); state them in the plan.
- A session todo list is fine scratch for the live subtask; nothing reads it. The issue is the contract.
- Never touch `.github/workflows/`, `orca.yaml`, or `~/.claude`; `danger-guard` denies it.
- Format and lint locally; the suite is CI's job, run the tests of the code you touched.

## Related

`tdd-workflow` (RED/GREEN/REFACTOR guidance), `verification-loop` (pre-review pass), `git-commit` (message
format), `land` (what happens after `worker_done`).
