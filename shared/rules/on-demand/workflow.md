---
description: "Task lifecycle on Orca (issue, dispatch, PLAN gate, TDD cycle, push, worker_done, independent review, CI, land), the three lanes, commands, and the coordinator design principles"
---
# Development Workflow

**The hub starts and ends tasks; spokes only execute.** A spoke's endpoint is `worker_done`, not a merge.

```text
HUB:    issue + dispatch (start-task)                      review → CI → land (land.sh)
            │ orca worker-start: worktree + agent + seed      ▲
            ▼                                                │ worker_done succeeded
SPOKE:  task.md → PLAN gate (ask/reply) → RED → GREEN → REFACTOR → code-review → push   (solo-cycle)
```

| Step | Where | Outcome |
|------|-------|---------|
| ISSUE | hub | GitHub issue with `Scope:` / `Gate:` / `Model:` footer, `priority`/`hold`/blocked-by |
| DISPATCH | hub | `dispatch.sh <n>`: Orca worktree `<n>-<slug>`, agent launched, seed prompt (Orca runs `setup.sh` first) |
| PLAN | spoke | `Gate: plan`: the plan goes out as `orchestration ask`; coding starts after `approve` |
| CYCLE | spoke | per subtask: RED, GREEN, REFACTOR, in-spoke `code-review` (advisory), `git push -u origin <branch>` |
| DONE | spoke | `worker_done --outcome succeeded` with a 3-sentence summary |
| REVIEW | hub | `review.sh`: a different model, read-only; JSON verdict; REQUEST_CHANGES goes back (max 2 rounds) |
| LAND | hub | CI green on the exact tip, FF merge, push, close issue, `worker-release`, `worktree rm` |

Enforced by machinery: CI on the branch, the independent review (`tdd_followed`, `tests_weakened`), the
deny-list hooks. Everything else (TDD, the PLAN gate) is policy, measured rather than blocked.

## Task triage: three lanes

| Lane | Executor | Contract | Gates |
|------|----------|----------|-------|
| Micro | subagent with `isolation: worktree`, non-executable paths only (docs, comments, wording) | the prompt | hub reads the diff |
| Quick (`/quick`) | its own worktree and Claude session, no issue | the conversation | CI |
| Full | dispatched spoke from an issue | the issue | everything above |

When unsure, or when the "why" should be findable later, use Full. A blocked-by chain with colliding `Scope:`
is one issue with subtasks (`Split: intentional — <why>` records a deliberate split). Disjoint scopes run in
parallel up to `CONCURRENCY_CAP` (~3-4: the usage window saturates).

## Commands

| Command | Skill | Purpose |
|---------|-------|---------|
| `/hub` | `hub` | survey in-flight work, propose the next move |
| `/start-task` | `start-task` | file the issue, dispatch the spoke |
| `/afk` | `afk` | run the coordinator loop unattended |
| `/land <n>` | `land` | review, CI, merge, teardown |
| `/quick <slug>` | `quick` | small interactive fix in its own worktree |
| `/cycle` | `solo-cycle` | the spoke's per-subtask cycle |

## Checklists

- **DEFINE**: acceptance criteria specific and testable; scope boundaries stated; `planner` for unclear or
  cross-boundary work (see `agent-orchestration`); failing test first for behavior changes.
- **EXECUTE**: one change at a time; stay in `Scope:`; tests and docs updated with behavior (`code-quality`).
- **VERIFY** (`verification-loop`): build, types, lint, tests, secrets, diff review, then `code-review`.
- **PUSH**: conventional commits anchored `#<n>`; no debug leftovers; never push the base branch or `--no-verify`.

## Coordinator principles

For the control plane (`coordinator.sh`, `dispatch.sh`, `land.sh`, `answer.sh`). A reviewer cites the number a diff breaks.

1. **Explicit state over inferred state.** Orca, git, and labels hold state; never reconstruct it from file mtimes, terminal text, or transcripts.
2. **Fail loud; fall back to the safe, cheap option.** A fallback emits a visible signal (warning, `blocked`, notification) and never defaults to the priciest model or the most destructive action.
3. **Act when unattended; escalate only the irreversible** (force-push, history rewrite, base branch, deletions outside the worktree). Journal every decision (`answer_class`).
4. **Liveness is Orca's verdict** (`worker-list`: `live`, `unverifiable`, `exited`). Only `exited` is dead; unknown is never a reason to recover.
5. **One writer per piece of shared state**; never reinterpret another consumer's record.
6. **Best-effort writes never fail the caller**, and "unknown" is never the basis for a destructive or escalating action.
7. **Structure for parallelism**: a file every change touches serializes the backlog, so split by responsibility.
8. **Never release a settled worker without a next owner**; after a restart, re-bind the Run (`run-use`) and re-derive from `worker-list`.
