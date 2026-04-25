# TDD Workflow

Guide through Test-Driven Development following the DEFINE → EXECUTE pattern.

## When to Use

- "Let's do TDD for..."
- "Write tests first..."
- "Test-driven..."
- "Red-green-refactor..."

**Do NOT use when:** user just wants tests after implementation (use `generate-tests`), or explicitly says "no TDD".

## TDD Cycle

```
RED → GREEN → REFACTOR → (repeat)
```

Each phase has a dedicated agent with strict scope boundaries:

| Phase | Agent | Scope | Artifact |
|-------|-------|-------|----------|
| RED | `tdd-red` | Write tests ONLY — no production code | Failing test + commit |
| GREEN | `tdd-green` | Write implementation ONLY — no test changes | Passing test + commit |
| REFACTOR | `tdd-refactor` | Improve quality ONLY — no behavior changes | Clean code + commit |

## Step-by-Step

### Step 1: Understand the Requirement

Before writing tests, clarify:

- What behavior are we implementing?
- What are the inputs and expected outputs?
- What are the edge cases?

If unclear, ask before proceeding.

### Step 2: RED — Write Failing Tests

Use the `tdd-red` agent. One test at a time, starting with the simplest happy path.

1. Write one test describing expected behavior.
2. Run it — verify it fails for the right reason (`NameError`, `ImportError`, `AssertionError`).
3. Commit tests separately.

**Critical:** Do NOT implement the function yet.

### Step 3: GREEN — Minimal Implementation

Use the `tdd-green` agent. Write just enough code to make tests pass.

1. Run the failing test to confirm what's needed.
2. Write the simplest code that satisfies the test.
3. Run all tests — new and existing must be green.
4. Do NOT modify any test.

### Step 4: REFACTOR — Clean Up

Use the `tdd-refactor` agent. Improve code quality without changing behavior.

1. Run all tests first — confirm green baseline.
2. Apply one improvement at a time, running tests after each.
3. If a test breaks, revert — the refactor was wrong, not the test.

### Step 5: Commit Implementation

```bash
git add .
git commit -m "feat(<scope>): implement <feature>"
```

### Step 6: Add Edge Cases

Return to RED for each edge case. Repeat the cycle.

## Two-Commit Pattern

| Commit | Type | Content |
|--------|------|---------|
| First | `test` | Tests only (RED phase) |
| Second | `feat`/`fix` | Implementation (GREEN phase) |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Writing too many tests at once | Start with ONE test, get it green |
| Implementing before tests fail | Always verify RED before GREEN |
| Testing implementation details | Test behavior, not internals |
| Skipping the commit checkpoint | Commit tests before implementing |
| Over-engineering in GREEN | Write minimal code, refactor later |

## Checklist

```
- [ ] Requirement understood
- [ ] Happy path test written
- [ ] Tests fail (RED verified)
- [ ] Tests committed
- [ ] Minimal implementation written
- [ ] Tests pass (GREEN verified)
- [ ] Code refactored (if needed)
- [ ] Implementation committed
- [ ] Edge cases covered
```
