# TDD Green Phase — Make Tests Pass

Write the minimal Python code to make the failing tests pass. Nothing more.

## Scope Boundary

**You ONLY write implementation code. NEVER modify existing tests.**

If a test seems wrong, stop and flag it — do not "fix" the test to match your implementation.

## Workflow

1. **Run failing tests** — confirm exactly what needs to be implemented.
2. **Confirm plan with user** — state what you'll implement and where. NEVER start without confirmation.
3. **Write minimal code** — just enough to satisfy the failing tests.
4. **Run all tests** — ensure new code passes AND existing tests remain green.
5. **Hand off to REFACTOR phase** — do not optimize or clean up.

## Core Principles

### Minimal Implementation

- Write the simplest code that makes the test pass.
- Resist the urge to handle cases not covered by tests.
- No design patterns, no abstractions, no "future-proofing".
- If you think "this should also handle X" — stop. Is there a test for X? No? Then don't.

### Speed Over Perfection

- Hardcode values if that's what makes the test pass (the refactor phase will fix it).
- Use the most direct approach, even if inelegant.
- Duplication is acceptable — refactor phase handles DRY.

## Conventions

- Follow project `python-style` rules for type annotations, naming, and imports.
- Place implementation in the module the tests import from.
- Use existing project patterns for module organization.

## Commit

After all tests pass:

```bash
git add .
git commit -m "feat(<scope>): implement <feature>"
```

## Checklist

- [ ] All failing tests now pass
- [ ] No existing tests broken
- [ ] No test files modified
- [ ] No more code written than necessary
- [ ] Implementation committed
- [ ] Ready for refactor phase
