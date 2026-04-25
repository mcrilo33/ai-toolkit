# Pytest Coverage

Increase test coverage to 100% by iteratively finding and covering untested lines.

## Prerequisites

- Follow `pytest-conventions` instructions for all generated tests
- Follow `generate-tests` skill workflow when adding new tests

## Workflow

### 1. Run coverage

Generate both a terminal summary and annotated source output:

```bash
pytest --cov=<module> --cov-report=term-missing --cov-report=annotate:cov_annotate
```

To check the entire project:

```bash
pytest --cov --cov-report=term-missing --cov-report=annotate:cov_annotate
```

To scope to specific tests:

```bash
pytest tests/test_module.py --cov=module --cov-report=term-missing --cov-report=annotate:cov_annotate
```

### 2. Review terminal summary

Identify files below 100% coverage. Focus on these files only.

### 3. Inspect annotated files

For each file below 100%, open the matching file in `cov_annotate/`.

- Lines starting with `!` are **not covered** by tests
- If a file has 100% coverage, skip it

### 4. Add tests for uncovered lines

Write meaningful tests with real assertions — do not just touch lines to inflate coverage.

If a line is intentionally uncovered (defensive branch, `TYPE_CHECKING`, platform-specific), mark it with `# pragma: no cover` and add a brief justification comment.

### 5. Re-run and iterate

Repeat steps 1–4 until all files reach 100% or only `# pragma: no cover` lines remain.

**Maximum 5 iterations.** If not converging, report the remaining gaps and stop.

### 6. Verify with a hard gate

```bash
pytest --cov=<module> --cov-fail-under=100
```

### 7. Cleanup

Remove the generated annotation directory:

```bash
rm -rf cov_annotate
```

## Stop Conditions

- All lines covered, **or**
- Remaining uncovered lines are marked `# pragma: no cover` with justification
- Maximum 5 iterations reached — report remaining gaps to the user
