# Development Workflow

## Task Lifecycle

```
SOURCE → DEFINE → EXECUTE → VERIFY → CLOSE
```

| Step | Purpose | Artifacts |
|------|---------|-----------|
| SOURCE | Get or create work item | Issue/task linked to branch |
| DEFINE | Clarify "done" criteria | Tests (TDD) or checklist |
| EXECUTE | Implement until criteria met | Working code |
| VERIFY | Validate quality gates pass | Pass/fail report |
| CLOSE | Record and ship | Commit, PR, closed issue |

## Commands

| Command | Skill | Purpose |
|---------|-------|---------|
| `/source` | `source-task` | Fetch task, create branch |
| `/close` | `close-task` | Commit, push, create PR |

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

**⚠️ MANDATORY SCOPE CHECK — before proceeding, evaluate:**

1. Count files to create or modify. If **3+ files** → you MUST spawn the `planner` agent to decompose the task. Do NOT plan inline.
2. Check the routing table in `agent-orchestration` rule. If ANY row matches → spawn that agent. This is not optional.
3. If TDD is chosen → you MUST use `tdd-red` → `tdd-green` → `tdd-refactor` agents sequentially. Do NOT write tests and implementation in the same agent loop.

Skipping this gate is a process violation. If you catch yourself implementing a 3+ file task without having spawned `planner`, stop and correct.

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
```

### VERIFY — Quality Gates

Run the `verification-loop` skill before closing. All gates must pass:

```
- [ ] Build succeeds
- [ ] Type check passes (if applicable)
- [ ] Linter passes (no new warnings)
- [ ] All tests pass (existing + new)
- [ ] Security scan clean (no hardcoded secrets)
- [ ] Diff review (no unintended changes, reasonable size)
- [ ] If 5+ files changed: code-review agent spawned for diff review
```

### CLOSE — Ship

```
- [ ] Commit message follows conventional format
- [ ] PR created with summary, test plan, linked issue
- [ ] No debug/temp code left behind
- [ ] Documentation updated (if behavior changed)
```

## Workflow Variations

### TDD Development

```
SOURCE → DEFINE (spawn tdd-red) → EXECUTE (spawn tdd-green → tdd-refactor) → VERIFY → CLOSE (2 commits)
```

- **MANDATORY:** Use `tdd-red` agent for RED phase — do NOT write tests inline
- **MANDATORY:** Use `tdd-green` agent for GREEN phase — do NOT implement inline
- **MANDATORY:** Use `tdd-refactor` agent for REFACTOR phase
- First commit: tests only (from tdd-red)
- Second commit: implementation (from tdd-green + tdd-refactor)
- Use `tdd-workflow` skill for guidance

### Simple Development

```
SOURCE → DEFINE (mental/checklist) → EXECUTE → VERIFY → CLOSE (1 commit)
```

## During Work (EXECUTE)

- Stay within scope defined in DEFINE step
- Flag if work expands beyond original definition
- Follow existing patterns (see `guidelines.md`)
- For multi-step tasks, state a brief plan with verification checkpoints:
  1. [Step] → verify: [how to confirm]
  2. [Step] → verify: [how to confirm]
- Strong success criteria ("tests X, Y, Z pass") > weak criteria ("make it work")

## Related Skills

- `source-task` — SOURCE step automation
- `close-task` — CLOSE step automation
- `verification-loop` — VERIFY step automation
- `tdd-workflow` — TDD guidance for DEFINE
- `generate-commit-message` — Commit message format
- `generate-tests` — Test generation
