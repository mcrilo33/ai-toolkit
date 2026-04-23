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

| Phase | What | Artifact |
|-------|------|----------|
| RED | Write failing test | Test that fails |
| GREEN | Minimal code to pass | Passing test |
| REFACTOR | Improve without changing behavior | Clean code, tests still pass |

## Step-by-Step

### Step 1: Understand the Requirement

Before writing tests, clarify:

- What behavior are we implementing?
- What are the inputs and expected outputs?
- What are the edge cases?

If unclear, ask before proceeding.

### Step 2: Write Failing Tests (RED)

1. Start with the simplest case — happy path
2. Name tests to describe behavior: `test_<function>_<scenario>`
3. Write assertion first, then work backwards

```python
def test_add_returns_sum_of_two_numbers():
    result = add(2, 3)
    assert result == 5
```

**Critical:** Do NOT implement the function yet.

### Step 3: Verify Tests Fail

```bash
pytest <test_file> -v
```

Expected: `NameError` or `AssertionError`. If tests pass without implementation, the tests are wrong.

### Step 4: Commit Tests (Checkpoint)

```bash
git add tests/
git commit -m "test(<scope>): add tests for <feature>"
```

### Step 5: Write Minimal Code (GREEN)

Just enough to make tests pass. Don't optimize yet.

```python
def add(a, b):
    return a + b
```

### Step 6: Verify Tests Pass

```bash
pytest <test_file> -v
```

### Step 7: Refactor (if needed)

Improve code quality without changing behavior. Run tests after each refactor.

### Step 8: Commit Implementation

```bash
git add .
git commit -m "feat(<scope>): implement <feature>"
```

### Step 9: Add Edge Cases

Return to RED for each edge case. Repeat cycle.

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
