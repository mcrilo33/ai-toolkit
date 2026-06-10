# Solo Cycle

Per-subtask cycle for solo, PR-less work. One subtask = one anchored, tested,
reviewed, pushed unit of work. There is no PR — the push IS the ship gate; all
enforcement fires on `git commit` and `git push`. The cycle repeats per subtask
within a session.

When a PR IS wanted, use the `close-task` skill instead.

## The cycle (per subtask)

| # | Step | Outcome | Enforced by |
|---|------|---------|-------------|
| 1 | ANCHOR | Issue exists; commits reference it | `commit-quality` (blocks unanchored commits) |
| 2 | RED | Failing test committed with `Tested-RED:` trailer | `red-proof-verify` (runs the node; blocks the commit if it passes) |
| 3 | GREEN | Implementation commit; tests pass | `commit-gauntlet` (lint/typecheck on changed lines), `secrets-scan` |
| 4 | REVIEW | APPROVE artifact `.review/<diff-hash>.json` exists | Written by the `code-review` agent on APPROVE |
| 5 | PUSH | `git push` succeeds — work is shipped | `red-proof-warn`, `reviewer-sep-warn`, `git-push-review` |

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

## Rules of thumb

- Push once per subtask; the review artifact covers the whole `upstream..HEAD` diff
- Commit granularity within a subtask is free (RED commit + GREEN commit minimum for TDD)
- Any commit after APPROVE invalidates the artifact (hash mismatch) — re-review before pushing
- Non-TDD subtasks (docs, config, chores) need no `Tested-RED` trailer —
  `red-proof-warn` only gates source-adding commits — but anchor and review still apply

## Edge cases

| Situation | Action |
|-----------|--------|
| Push blocked by `reviewer-sep-warn` | Run the `code-review` agent, get APPROVE |
| Push blocked by `red-proof-warn` | A source-adding commit is missing its trailer — amend/reword it, or add the failing-test commit |
| Working on `main` with no issue branch | Add `Refs #<id>` to every commit message |
| Review says REQUEST_CHANGES | Fix and re-review — never bypass |
| Tempted to use `--no-verify` | Blocked by `block-no-verify`, by design |

## Related skills

- `source-task` — anchor: fetch the issue and create the branch
- `tdd-workflow` — RED/GREEN/REFACTOR guidance
- `close-task` — use instead when a PR IS wanted
- `verification-loop` — deeper VERIFY pass before the review
- `git-commit` — commit message format
