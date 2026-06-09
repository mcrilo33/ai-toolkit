# TDD Red Phase — Write Failing Tests

Write one failing pytest test at a time that describes desired behavior before any implementation exists.

## Scope Boundary

**You ONLY write tests. NEVER write production code.**

If the test passes without implementation, the test is wrong — fix it or discard it.

## Workflow

1. **Understand the requirement** — clarify behavior, inputs, outputs, edge cases. If unclear, ask.
2. **Break down into testable behaviors** — list the individual behaviors to verify.
3. **Confirm plan with user** — present the list. NEVER start without confirmation.
4. **Write ONE failing test** — start with the simplest happy path case.
5. **Run the test** — verify it fails for the right reason (`NameError`, `ImportError`, `AssertionError`).
6. **Hand off to GREEN phase** — do not proceed to implementation.

## Test Quality Standards

- **One test at a time** — never batch multiple tests before verifying each fails.
- **AAA pattern** — clear Arrange, Act, Assert sections.
- **Behavior-focused names** — `test_<function>_<scenario>` describing expected behavior.
- **Single assertion focus** — each test verifies one specific outcome.
- **Edge cases** — consider boundary conditions after happy path is green.

## Conventions

- Use `pytest` with plain functions or classes (follow existing test structure).
- Follow project `pytest-conventions` and `python-style` rules.
- Place tests in the project's existing test directory structure.
- Use `pytest.raises` for expected exceptions.
- Use `@pytest.mark.parametrize` for data-driven variations.

## Commit Checkpoint

After tests are written and confirmed failing, commit the failing test and
record which test was driven to RED with a `Tested-RED:` trailer. The trailer
value is the pytest node ID of the failing test you just wrote — it makes the
red-before-green step auditable in history and is later runnable to verify:

```bash
git add tests/
git commit -m "test(<scope>): add tests for <feature>" \
           -m "Tested-RED: tests/test_<scope>.py::test_<behavior>"
```

The `red-proof-warn` push hook checks for commits that add source files without
a matching `Tested-RED:` trailer. On Cursor it HARD-BLOCKS the push when the
trailer is missing (advisory `warn` on Claude/Copilot and native git hooks), so
write the trailer here at the RED step or the eventual `git push` will be denied.

## Checklist

- [ ] Requirement understood and confirmed
- [ ] Test describes expected behavior (not implementation details)
- [ ] Test fails for the right reason (missing implementation)
- [ ] Test name is descriptive and follows naming conventions
- [ ] Test follows AAA pattern
- [ ] No production code written
- [ ] Tests committed separately with a `Tested-RED:` trailer
