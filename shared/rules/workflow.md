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

## Commands

| Command | Skill | Purpose |
|---------|-------|---------|
| `/hub` | `hub` | Survey what is in flight, propose the next move (hub) |
| `/source` | `source-task` | Anchor to the issue; branch creation only as non-hub fallback |
| `/cycle` | `solo-cycle` | Solo per-subtask cycle: anchor, RED, GREEN, review, push |
| `/land <id>` | `land-task` | Land a finished task from the hub: merge, suite, push, teardown |

## Phase Checklists

### SOURCE — Research & Setup

```
- [ ] Task identified (issue URL or ad-hoc scope)
- [ ] Issue details read and understood
- [ ] Branch created from latest default branch
- [ ] Existing code/patterns reviewed for context
- [ ] Dependencies or blockers identified
```

### DEFINE — Plan & Acceptance Criteria

**⚠️ MANDATORY SCOPE CHECK — see the `agent-orchestration` rule for the binding thresholds.**

Before planning, assess file count and consult the Routing Table in
`agent-orchestration`. If a row matches, spawn that agent there — do NOT plan inline.
The thresholds, the TDD agent sequence, and the violation rules all live in
`agent-orchestration`; this phase only enforces that the check actually happens here.

```
- [ ] Scope check: file count assessed, routing table consulted
- [ ] If 3+ files: planner agent spawned and plan received
- [ ] If TDD: tdd-red agent spawned (not inline test writing)
- [ ] Acceptance criteria written (specific, testable)
- [ ] Scope boundaries stated ("this PR will NOT do X")
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
- [ ] Tests written/updated for every functional change (do NOT wait to be asked)
- [ ] Documentation updated for any behavior change (docstrings, README, API docs — do NOT defer)
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
- [ ] If 5+ files changed: code-review agent spawned for diff review
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
SOURCE → DEFINE (spawn tdd-red) → EXECUTE (spawn tdd-green → tdd-refactor) → VERIFY → PUSH (2 commits)
```

- **MANDATORY:** Use `tdd-red` agent for RED phase — do NOT write tests inline
- **MANDATORY:** Use `tdd-green` agent for GREEN phase — do NOT implement inline
- **MANDATORY:** Use `tdd-refactor` agent for REFACTOR phase
- First commit: tests only (from tdd-red)
- Second commit: implementation (from tdd-green + tdd-refactor)
- Use `tdd-workflow` skill for guidance

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
