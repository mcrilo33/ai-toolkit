# Development Workflow

## Task Lifecycle

**The hub starts and ends tasks; spokes only execute.** A task begins on the hub
(`start-task` drafts the issue and spawns the worktree spoke) and ends on the hub
(`land-task` merges, gates, ships, and tears down). The spoke's endpoint is its
**push** — there is no PR.

```
HUB:    START (start-task)                                LAND (/land <id>)
            │ issue + worktree + seeded session               ▲ merge → suite → push → teardown → close issue
            ▼                                                 │
SPOKE:  SOURCE → DEFINE → EXECUTE → VERIFY → PUSH  (per subtask — solo-cycle)
```

| Step | Where | Purpose | Artifacts |
|------|-------|---------|-----------|
| START | hub | Draft issue, spawn the spoke | Issue + worktree + branch |
| SOURCE | spoke | Anchor to the issue | Issue read; branch confirmed |
| DEFINE | spoke | Clarify "done" criteria | Tests (TDD) or checklist |
| EXECUTE | spoke | Implement until criteria met | Working code |
| VERIFY | spoke | Validate quality gates pass | Pass/fail report |
| PUSH | spoke | Ship gate — push the subtask | Pushed branch + review evidence |
| LAND | hub | Merge, re-gate, ship, tear down | Merged default branch, closed issue |

## Task triage — the three lanes

Before starting any task, the hub classifies it into one of three lanes (~10 seconds):

| Lane | Executor | Contract | Gates |
|------|----------|----------|-------|
| 1 — Micro-spoke | Subagent in a temp worktree (no issue, no tmux window, no session) | The prompt | Hub diff-review before merge |
| 2 — Express spoke | Full spoke via `worktree-new.sh <slug>` (ad-hoc, no issue) | Kickoff prompt | All push gates, single cycle, no ledger |
| 3 — Full | Full spoke from a GitHub issue | The issue | Everything (current flow) |

**Triage heuristic:**

- Does the change touch executable behavior? **No** → Lane 1 — restricted to non-executable
  paths only: docs, comments, wording. Lane 1 must never touch `scripts/`, `shared/hooks/`,
  `tests/`, or skill scripts.
- One subtask, obvious approach, small diff? → Lane 2.
- Otherwise, when in doubt, or when the "why" should be findable later → Lane 3.

The Task Lifecycle diagram above describes **Lane 3** — the full flow for anything
substantial. Lanes 1 and 2 shed orchestration overhead (no issue, no ledger for lane 2; no
worktree, no issue, no tmux for lane 1), but **never shed gates**: lane 2 runs all push
gates in a single cycle; lane 1's gate is the hub's diff review before `--local` landing.

`docs:`/`chore:` commits whose entire staged set is non-executable documentation pass the
commit-anchor gate without an issue anchor — this is the sanctioned no-issue commit path
for lanes 1 and 2. See the `commit-quality` hook for the exact file-set rules.

## Commands

| Command | Skill | Purpose |
|---------|-------|---------|
| `/hub` | `hub` | Survey what is in flight, propose the next move (hub) |
| `/source` | `source-task` | Anchor to the issue; branch creation only as non-hub fallback |
| `/cycle` | `solo-cycle` | Solo per-subtask cycle: anchor, RED, GREEN, review, push |
| `/land <id>` | `land-task` | Land a finished task from the hub: merge, suite, push, teardown |
| `/land <branch> --local` | `land-task` | Land a micro-spoke (lane 1): skips upstream guards, merges the local-only branch and ships, no issue to close |

## Phase Checklists

### SOURCE — Research & Setup

```
- [ ] Task identified (issue URL or ad-hoc scope)
- [ ] Issue details read and understood
- [ ] Branch confirmed (hub flow) — or created from latest default branch (non-hub fallback)
- [ ] Existing code/patterns reviewed for context
- [ ] Dependencies or blockers identified
```

### DEFINE — Plan & Acceptance Criteria

**Scope check — see the `agent-orchestration` rule for the delegation rubric.**

Before planning, assess complexity and blast radius (not just file count) and consult the
routing table in `agent-orchestration`. Spawn `planner` when the path is unclear or the
change crosses module/API/data boundaries; otherwise plan inline.

```
- [ ] Scope check: complexity and blast radius assessed, routing table consulted
- [ ] If path unclear or change crosses boundaries: planner spawned and plan received
- [ ] If TDD: a failing test is written first (inline, or via tdd-red)
- [ ] Acceptance criteria written (specific, testable)
- [ ] Scope boundaries stated ("this task will NOT do X")
- [ ] Approach chosen (TDD vs simple)
- [ ] Files to create/modify listed
- [ ] Edge cases and error scenarios identified
- [ ] If TDD: failing tests written and committed
```

### EXECUTE — Implement

```
- [ ] One change at a time, verifying after each
- [ ] Stays within DEFINE scope — flag if expanding
- [ ] Follows existing patterns (see guidelines.md)
- [ ] Error handling included (not deferred)
- [ ] No placeholder/stub implementations
- [ ] Tests written/updated for every functional change (binding rule in `code-quality`)
- [ ] Documentation updated for any behavior change (binding rule in `code-quality`)
```

### VERIFY — Quality Gates

Run the `verification-loop` skill before the subtask's push. All gates must pass:

```
- [ ] Build succeeds
- [ ] Type check passes (if applicable)
- [ ] Linter passes (no new warnings)
- [ ] All tests pass (existing + new)
- [ ] Security scan clean (no hardcoded secrets)
- [ ] Diff review (no unintended changes, reasonable size)
- [ ] Before shipping: code-review agent spawned on the diff (mandatory gate)
```

### PUSH — Ship the subtask (spoke endpoint)

```
- [ ] Commit messages follow conventional format, anchored to the issue
- [ ] Review evidence in place (APPROVE artifact) — push hooks verify it
- [ ] No debug/temp code left behind
- [ ] Documentation updated (if behavior changed)
- [ ] Pushed — outcome synced to the issue (check the subtask's box)
```

### LAND — End the task (hub only)

Run `/land <id>` (the `land-task` skill) from the hub once the spoke has pushed:

```
- [ ] Spoke branch fully pushed; hub clean on the default branch
- [ ] Merged (ff when possible), full suite green on the merged hub
- [ ] Pushed to origin; worktree torn down; branch pruned; issue closed
```

## Workflow Variations

### TDD Development

```
SOURCE → DEFINE (RED) → EXECUTE (GREEN → REFACTOR) → VERIFY → PUSH (2 commits)
```

- RED, GREEN, and REFACTOR may be done **inline** — `red-proof-verify` and
  `commit-gauntlet` enforce the evidence (a test that failed first, clean lint/types),
  not which agent wrote it
- Use the `tdd-red`/`tdd-green`/`tdd-refactor` agents when you want a clean context
  boundary (larger or collaborative work) — they are optional, not required
- First commit: tests only. Second commit: implementation
- The one mandatory separate agent before shipping is `code-review`
- Use the `tdd-workflow` skill for guidance

### Simple Development

```
SOURCE → DEFINE (mental/checklist) → EXECUTE → VERIFY → PUSH (1 commit)
```

### Solo Development (the default — no PR)

```
SOURCE → DEFINE → EXECUTE → VERIFY → PUSH — per subtask; the hub lands when done
```

- No PR — the push is the ship gate; commit and push hooks enforce all evidence
- One code-review (APPROVE artifact) and one push per subtask
- Use the `solo-cycle` skill for the per-subtask mechanics
- When the last subtask is pushed, the **hub** runs `/land <id>` — never the spoke

## During Work (EXECUTE)

- Stay within scope defined in DEFINE step
- Flag if work expands beyond original definition
- Follow existing patterns (see `guidelines.md`)
- For multi-step tasks, state a brief plan with verification checkpoints:
  1. [Step] → verify: [how to confirm]
  2. [Step] → verify: [how to confirm]
- Strong success criteria ("tests X, Y, Z pass") > weak criteria ("make it work")

## Related Skills

- `brainstorming` — Spec refinement for DEFINE (use before context-map/planner on ambiguous work)
- `start-task` — START step automation (hub: issue + worktree + seeded spoke)
- `source-task` — SOURCE step automation
- `land-task` — LAND step automation (hub: merge, suite, ship, teardown)
- `solo-cycle` — Per-subtask cycle for solo, PR-less work
- `verification-loop` — VERIFY step automation
- `tdd-workflow` — TDD guidance for DEFINE
- `generate-commit-message` — Commit message format
- `generate-tests` — Test generation
