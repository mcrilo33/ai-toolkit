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
| `/cycle` | `solo-cycle` | Solo per-subtask cycle: anchor, RED, GREEN, review, push |

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

**Scope check — see the `agent-orchestration` rule for the delegation rubric.**

Before planning, assess complexity and blast radius (not just file count) and consult the
routing table in `agent-orchestration`. Spawn `planner` when the path is unclear or the
change crosses module/API/data boundaries; otherwise plan inline.

```
- [ ] Scope check: complexity and blast radius assessed, routing table consulted
- [ ] If path unclear or change crosses boundaries: planner spawned and plan received
- [ ] If TDD: a failing test is written first (inline, or via tdd-red)
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
- [ ] Tests written/updated for every functional change (binding rule in `code-quality`)
- [ ] Documentation updated for any behavior change (binding rule in `code-quality`)
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
- [ ] Before shipping: code-review agent spawned on the diff (mandatory gate)
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
SOURCE → DEFINE (RED) → EXECUTE (GREEN → REFACTOR) → VERIFY → CLOSE (2 commits)
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
SOURCE → DEFINE (mental/checklist) → EXECUTE → VERIFY → CLOSE (1 commit)
```

### Solo Development (no PR)

```
SOURCE → DEFINE → EXECUTE → VERIFY → CLOSE (push only) — per subtask
```

- No PR — the push is the ship gate; commit and push hooks enforce all evidence
- One code-review (APPROVE artifact) and one push per subtask
- Use the `solo-cycle` skill for the per-subtask mechanics

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
- `source-task` — SOURCE step automation
- `close-task` — CLOSE step automation
- `solo-cycle` — Per-subtask cycle for solo, PR-less work
- `verification-loop` — VERIFY step automation
- `tdd-workflow` — TDD guidance for DEFINE
- `generate-commit-message` — Commit message format
- `generate-tests` — Test generation
