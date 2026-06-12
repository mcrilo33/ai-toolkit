# Solo Cycle

Per-subtask cycle for solo, PR-less work. One subtask = one anchored, tested,
reviewed, pushed unit of work. There is no PR — the push IS the ship gate; all
enforcement fires on `git commit` and `git push`. The cycle repeats per subtask
within a session.

Ending the task is not the spoke's job: once the final subtask is pushed, the **hub**
lands it with `/land <id>` (the `land-task` skill) — merge, suite, ship, teardown.

## The cycle (per subtask)

| # | Step | Outcome | Enforced by |
|---|------|---------|-------------|
| 1 | ANCHOR | Issue exists; commits reference it | `commit-quality` (blocks unanchored commits) |
| 2 | RED | Failing test committed with `Tested-RED:` trailer | `red-proof-verify` (runs the node; blocks the commit if it passes) |
| 3 | GREEN | Implementation commit; tests pass | `commit-gauntlet` (lint/typecheck on changed lines), `secrets-scan` |
| 4 | REVIEW | APPROVE artifact `.review/<diff-hash>.json` exists | Written by the `code-review` agent on APPROVE |
| 5 | PUSH | `git push` succeeds — work is shipped | `red-proof-warn`, `reviewer-sep-warn`, `todo-ledger-warn`, `git-push-review` |

Then: next subtask → back to step 1 (or step 2 if it is the same issue).

### 1. ANCHOR

Ensure an issue exists — `gh issue create` for ad-hoc work, or pick an existing
one. Then either:

- Create a branch `feature/<id>-<slug>` (preferred), or
- Stay on `main` and add `-m "Refs #<id>"` to every commit.

`commit-quality` blocks unanchored commits, so anchor before the first commit.

### 2. RED

Spawn the `tdd-red` agent. The failing test is committed with a
`Tested-RED: <pytest-node-id>` trailer. At commit time `red-proof-verify` runs
that node and blocks the commit if it PASSES — a passing test is not driving
any new code.

### 3. GREEN

Spawn the `tdd-green` agent (then `tdd-refactor` if needed) and commit the
implementation. `commit-gauntlet` lints and typechecks the changed lines;
`secrets-scan` blocks hardcoded credentials.

### 4. REVIEW

Spawn the `code-review` agent on the full diff to be pushed — `git diff
<upstream>..HEAD`, or the full diff if no upstream exists. On APPROVE it writes
`.review/<diff-hash>.json`.

One review per subtask — commit freely within the subtask, review once before
pushing. If REQUEST_CHANGES: fix, re-stage, fresh review. The hash binds to
the exact diff — any new commit invalidates the artifact.

### 5. PUSH

`git push` is the ship gate. `red-proof-warn`, `reviewer-sep-warn`, and
`git-push-review` all fire here; on Cursor they hard-block. The push only
succeeds when all evidence is in place.

## The session ledger (TodoWrite)

GitHub issues hold the durable contract; your head holds the momentary edit. The
middle layer — *which subtask and which cycle step is live right now* — is what a
dead worktree session forgets, leaving you to reconstruct mid-cycle state by
archaeology. Track it in a `TodoWrite` ledger so the cycle is visible and resumable.

**Seed it** before the first commit: one todo per subtask × the cycle steps that
apply — `Subtask 1 · ANCHOR`, `Subtask 1 · RED`, `Subtask 1 · GREEN`,
`Subtask 1 · REVIEW`, `Subtask 1 · PUSH`, then the same for subtask 2 (ANCHOR only
once when subtasks share the issue). Keep exactly one todo `in_progress` — the step
you are on — and flip it to `completed` the moment its gate passes, moving the next
step to `in_progress`.

**Maintain it** as the cycle turns:

- On REQUEST_CHANGES, insert a `Subtask N · Fix review findings` todo before that
  subtask's REVIEW todo, work it, then re-review.
- At PUSH, sync only the *outcome* to the GitHub issue — check the subtask's box,
  leave a one-line note. Never mirror the live todo list into the issue.

The ledger is ephemeral session scratch; the GitHub issue is the durable contract.

## Rules of thumb

- Push once per subtask; the review artifact covers the whole `upstream..HEAD` diff
- Commit granularity within a subtask is free (RED commit + GREEN commit minimum for TDD)
- Any commit after APPROVE invalidates the artifact (hash mismatch) — re-review before pushing
- Non-TDD subtasks (docs, config, chores) need no `Tested-RED` trailer —
  `red-proof-warn` only gates source-adding commits — but anchor and review still apply
- The TodoWrite ledger is ephemeral session scratch; the GitHub issue is the durable
  contract — sync only outcomes at PUSH, and skip the ledger entirely for single-step
  work (one tiny subtask, a docs/config one-liner), where it is pure overhead
- `todo-ledger-warn` enforces the ledger at PUSH by scanning the session transcript for
  a `TodoWrite` call (warn on Claude, hard-deny on Cursor). For genuinely single-step
  work, add a `No-Ledger: <reason>` trailer to a commit in the pushed range to bypass it

## Edge cases

| Situation | Action |
|-----------|--------|
| Push blocked by `reviewer-sep-warn` | Run the `code-review` agent, get APPROVE |
| Push blocked by `red-proof-warn` | A source-adding commit is missing its trailer — amend/reword it, or add the failing-test commit |
| Push blocked by `todo-ledger-warn` | Seed a `TodoWrite` ledger this session, or add a `No-Ledger: <reason>` trailer for single-step work |
| Working on `main` with no issue branch | Add `Refs #<id>` to every commit message |
| Review says REQUEST_CHANGES | Fix and re-review — never bypass |
| Tempted to use `--no-verify` | Blocked by `block-no-verify`, by design |

## Related skills

- `source-task` — anchor: fetch the issue and confirm the branch
- `tdd-workflow` — RED/GREEN/REFACTOR guidance
- `land-task` — hub-side `/land <id>` that ends the task once the last subtask is pushed
- `verification-loop` — deeper VERIFY pass before the review
- `git-commit` — commit message format
