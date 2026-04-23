---
name: tdd-workflow
description: Guide through Test-Driven Development cycle. Use when the user wants to do TDD, write tests first, or follow red-green-refactor pattern. Integrates with generate-tests skill for test creation.
---
# TDD Workflow

Guide the user through Test-Driven Development following the DEFINE → EXECUTE pattern.

## When to Use

**Trigger phrases:**
- "Let's do TDD for..."
- "Write tests first..."
- "Test-driven..."
- "Red-green-refactor..."
- "I want to define behavior with tests"

**Do NOT use when:**
- User just wants tests after implementation
- Adding tests to existing code (use `generate-tests` skill directly)
- User explicitly says "no TDD"

## TDD Cycle

```
RED → GREEN → REFACTOR → (repeat)
```

| Phase | What | Artifact |
|-------|------|----------|
| RED | Write failing test | Test that fails |
| GREEN | Minimal code to pass | Passing test |
| REFACTOR | Improve without changing behavior | Clean code, tests still pass |

## Workflow

### Step 1: Understand the Requirement

Before writing tests, clarify:
- What behavior are we implementing?
- What are the inputs and expected outputs?
- What are the edge cases?

If unclear, ask the user to clarify before proceeding.

### Step 2: Write Failing Tests (RED)

Use `generate-tests` skill patterns to create tests:

1. **Start with the simplest case** — happy path
2. **Name tests to describe behavior**: `test_<function>_<scenario>`
3. **Write assertion first**, then work backwards

```python
# Example: Start with what you want to assert
def test_add_returns_sum_of_two_numbers():
    result = add(2, 3)
    assert result == 5
```

**Critical:** Do NOT implement the function yet.

### Step 3: Verify Tests Fail

Run the tests:
```bash
pytest <test_file> -v
```

**Expected:** Tests should fail with:
- `NameError` (function doesn't exist), or
- `AssertionError` (wrong result)

If tests pass without implementation, the tests are wrong.

### Step 4: Commit Tests (Checkpoint)

Before implementing, commit the tests:
```bash
git add tests/
git commit -m "test(<scope>): add tests for <feature>

RED phase - tests fail, implementation pending"
```

This creates a clear separation between test definition and implementation.

### Step 5: Write Minimal Code (GREEN)

Implement just enough to make tests pass:

1. **Start with the simplest implementation** — even if ugly
2. **Don't optimize yet** — that's REFACTOR
3. **Run tests after each change**

```python
# Minimal implementation
def add(a, b):
    return a + b
```

### Step 6: Verify Tests Pass

```bash
pytest <test_file> -v
```

**Expected:** All tests pass.

### Step 7: Refactor (if needed)

Improve code quality without changing behavior:
- Extract functions
- Rename variables
- Remove duplication
- Improve performance

**Run tests after each refactor** — they must stay green.

### Step 8: Commit Implementation

```bash
git add .
git commit -m "feat(<scope>): implement <feature>

GREEN phase - all tests pass"
```

### Step 9: Add Edge Cases

Return to RED phase for edge cases:
1. Write test for edge case
2. Verify it fails
3. Implement handling
4. Verify it passes

Repeat until all requirements are covered.

## Checklist

Track progress with this checklist:

```
TDD Progress:
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

## Two-Commit Pattern

TDD produces two commits per feature:

| Commit | Type | Content |
|--------|------|---------|
| First | `test` | Tests only (RED phase) |
| Second | `feat`/`fix` | Implementation (GREEN phase) |

This makes code review easier and documents the TDD process.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Writing too many tests at once | Start with ONE test, get it green |
| Implementing before tests fail | Always verify RED before GREEN |
| Testing implementation details | Test behavior, not internals |
| Skipping the commit checkpoint | Commit tests before implementing |
| Over-engineering in GREEN | Write minimal code, refactor later |

## Integration with Workflow

TDD maps to the development workflow:

| Workflow Step | TDD Phase |
|---------------|-----------|
| DEFINE | Write tests (RED) |
| EXECUTE | Implement (GREEN) + Refactor |
| CLOSE | Two commits (test + impl) |

## Example Session

**User:** "Let's do TDD for a function that validates email addresses"

**Agent:**
1. "What should valid emails look like? Any specific rules?"
2. Write test: `test_validate_email_returns_true_for_valid_email`
3. Run tests → verify failure
4. "Tests fail as expected. Commit tests now?"
5. Implement `validate_email()` minimally
6. Run tests → verify pass
7. "Tests pass. Ready to commit implementation?"
8. "Any edge cases? Empty string? Missing @?"
9. Add edge case tests, repeat cycle
