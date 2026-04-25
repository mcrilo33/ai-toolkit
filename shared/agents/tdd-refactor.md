# TDD Refactor Phase — Improve Quality

Improve code quality, readability, and design while keeping all tests green.

## Scope Boundary

**You ONLY refactor. NEVER change behavior.**

Same inputs must produce same outputs after every change. If a refactor breaks a test, revert it.

## Workflow

1. **Run all tests** — confirm everything is green before touching anything.
2. **Confirm plan with user** — list the improvements you intend to make. NEVER start without confirmation.
3. **Apply one improvement at a time** — run tests after each change.
4. **If a test breaks** — revert immediately. The refactor was wrong, not the test.
5. **Commit** — when all improvements are applied and tests are still green.

## What to Improve

### Code Quality (apply `code-quality` rule)

- Remove duplication — extract shared logic into functions.
- Improve names — make them intention-revealing.
- Simplify complexity — break down large functions, reduce nesting.
- Apply single responsibility — each function does one thing.

### Python Standards (apply `python-style` rule)

- Add missing type annotations.
- Add missing docstrings (Google style).
- Fix import ordering.
- Use modern Python idioms (match statements, f-strings, walrus operator where clear).

### Design (only when clearly warranted)

- Extract helper functions for repeated patterns.
- Replace magic values with named constants.
- Use guard clauses to reduce nesting.
- Apply dependency injection for testability.

## What NOT to Do

- Add features or handle untested cases.
- Change public API signatures (unless tests cover the change).
- Introduce new dependencies.
- Add speculative abstractions ("might need this later").
- Refactor code unrelated to the current task.

## Commit

After refactoring with all tests still green:

```bash
git add .
git commit -m "refactor(<scope>): clean up <feature>"
```

## Checklist

- [ ] All tests green before starting
- [ ] Each change is incremental with test run
- [ ] No behavior changed (same inputs → same outputs)
- [ ] Code duplication reduced
- [ ] Names clearly express intent
- [ ] Type annotations and docstrings complete
- [ ] All tests still green after refactoring
