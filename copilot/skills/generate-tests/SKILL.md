---
name: generate-tests
description: Generate or update pytest unit and integration tests following project conventions. Use when the user asks to create tests, write tests, update tests, fix tests, sync tests with code changes, or add test coverage.
---

# Generate & Update Tests

Generate or update pytest tests following project quality standards.

## When to Use

**Generate:** "Write tests for...", "Add tests to...", "Generate test cases...", "Create unit tests...", "Add test coverage..."

**Update:** "Update tests for...", "Fix the tests after...", "Sync tests with...", "Tests are failing, update them..."

**Do NOT use when:** implementing features, refactoring, or fixing bugs — unless tests are explicitly requested.

## Mode Selection

- **New tests?** → Generate Workflow
- **Updating existing tests?** → Update Workflow
- **TDD?** → Defer to `tdd-workflow` skill

## Generate Workflow

1. Read the code to test
2. Identify test categories: happy path, edge cases, error handling
3. Generate tests following patterns below
4. Verify imports and dependencies exist

## Update Workflow

1. Read the changed code — understand what changed (signature, behavior, return type)
2. Read existing tests — find the test file for the changed code
3. Compare and identify gaps
4. Update tests preserving existing structure
5. Verify all imports still valid

## Test File Structure

```python
"""Tests for {module_name}."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from {package}.{module} import {functions_or_classes}


class TestClassName:
    """Tests for ClassName."""

    def test_method_returns_expected_result(self):
        """Describe the expected behavior."""
        # Arrange / Act / Assert

    def test_method_handles_empty_input(self):
        """Handle empty input gracefully."""

    def test_method_raises_on_invalid_input(self):
        """Raise ValueError for invalid input."""
```

## Naming Conventions

| Pattern | Example |
|---------|---------|
| `test_{method}_{scenario}` | `test_parse_handles_empty_string` |
| `test_{method}_returns_{result}` | `test_calculate_returns_zero_for_empty_list` |
| `test_{method}_raises_{exception}` | `test_validate_raises_value_error_for_none` |

Bad names: `test_1`, `test_parse`, `test_it_works`

## Required Test Categories

1. **Happy Path** — basic functionality
2. **Edge Cases** — zero, empty, boundary values
3. **Boundary Conditions** — large inputs, limits
4. **Error Handling** — invalid inputs, expected exceptions
5. **Failure Modes** — specific error conditions

## Mocking External Dependencies

Always mock: database calls, API requests, file system operations, time-dependent operations, external services.

## Test Organization

```
tests/
├── conftest.py
├── unit/
│   ├── test_models.py
│   └── test_utils.py
└── integration/
    └── test_api.py
```

## Output Checklist

- [ ] Test file path follows `tests/test_{module}.py`
- [ ] All test names are descriptive
- [ ] Happy path, edge cases, and error handling covered
- [ ] External dependencies are mocked
- [ ] Imports are valid
- [ ] No hardcoded values that should be fixtures
