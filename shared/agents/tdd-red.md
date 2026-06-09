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

Two hooks enforce this, and the trailer is no longer a mere claim — it is
executed:

- `red-proof-verify` (commit time) RUNS the node named in the `Tested-RED:`
  trailer against the staged tree and requires it to FAIL. At the RED commit the
  implementation does not exist yet, so a genuine red-before-green test must
  fail here. If the node PASSES, the commit is BLOCKED on Cursor — a passing
  test needs no new code and cannot be driving the implementation.
- `red-proof-warn` (push time) checks every source-adding commit for the trailer
  and re-runs each `Tested-RED:` node as a GREEN backstop, requiring it to pass
  now that the implementation exists. On Cursor a missing trailer or a still-
  failing node HARD-BLOCKS the push (advisory `warn` on Claude/Copilot and
  native git hooks).

If pytest cannot run (no runner, missing deps, collection bootstrap failure),
the execution check degrades to trailer-presence only — it never produces a
false block. Write a runnable node ID here so the RED proof actually fires.

## Checklist

- [ ] Requirement understood and confirmed
- [ ] Test describes expected behavior (not implementation details)
- [ ] Test fails for the right reason (missing implementation)
- [ ] Test name is descriptive and follows naming conventions
- [ ] Test follows AAA pattern
- [ ] No production code written
- [ ] Tests committed separately with a `Tested-RED:` trailer naming a runnable pytest node
- [ ] `red-proof-verify` observed the node FAIL at commit time (RED proven, not just claimed)
