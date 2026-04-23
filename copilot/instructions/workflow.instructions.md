---
applyTo: ""
---

# Development Workflow

## Task Lifecycle

```
SOURCE → DEFINE → EXECUTE → CLOSE
```

| Step | Purpose | Artifacts |
|------|---------|-----------|
| SOURCE | Get or create work item | Issue/task linked to branch |
| DEFINE | Clarify "done" criteria | Tests (TDD) or checklist |
| EXECUTE | Implement until criteria met | Working code |
| CLOSE | Record and ship | Commit, PR, closed issue |

## TDD Pattern (when requested)
```
SOURCE → DEFINE (write tests) → EXECUTE (make tests pass) → CLOSE (2 commits)
```
- First commit: tests only (`test(<scope>): add tests for <feature>`)
- Second commit: implementation (`feat(<scope>): implement <feature>`)

## During Work (EXECUTE)
- Stay within scope defined in DEFINE step
- Flag if work expands beyond original definition
- Follow existing patterns in the codebase
