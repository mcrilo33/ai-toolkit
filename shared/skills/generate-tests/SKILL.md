# Generate & Update Tests

Generate or update pytest tests following project quality standards.

## When to Use

**Generate:** "Write tests for...", "Add tests to...", "Generate test cases...", "Create unit tests..."

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

1. Read the changed code — understand what changed
2. Read existing tests — find the test file
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
        # Arrange
        # Act
        # Assert

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

## Required Test Categories

### 1. Happy Path

```python
def test_add_returns_sum_of_two_numbers(self):
    result = add(2, 3)
    assert result == 5
```

### 2. Edge Cases

```python
def test_add_handles_zero(self):
    assert add(0, 5) == 5
    assert add(5, 0) == 5
```

### 3. Error Handling

```python
def test_add_raises_type_error_for_string_input(self):
    with pytest.raises(TypeError):
        add("a", 1)
```

## Mocking External Dependencies

Always mock: database calls, API requests, file system operations, external services.

```python
@patch('module.external_api_call')
def test_fetch_data_returns_parsed_response(self, mock_api):
    mock_api.return_value = {'key': 'value'}
    result = fetch_data('endpoint')
    assert result == {'key': 'value'}
    mock_api.assert_called_once_with('endpoint')
```

## Fixtures

```python
@pytest.fixture
def sample_user():
    return User(name="Test", email="test@example.com")

@pytest.fixture
def mock_database():
    with patch('module.db_connection') as mock_db:
        yield mock_db
```

## Parametrized Tests

```python
@pytest.mark.parametrize("input_val,expected", [
    (0, 0), (1, 1), (-1, 1), (100, 100),
])
def test_absolute_value(self, input_val, expected):
    assert abs_value(input_val) == expected
```

## Output Checklist

- [ ] Test file path follows `tests/test_{module}.py`
- [ ] All test names are descriptive
- [ ] Happy path, edge cases, and error handling covered
- [ ] External dependencies are mocked
- [ ] Imports are valid
- [ ] No hardcoded values that should be fixtures
