# Generate & Update Tests

Generate or update pytest tests following project quality standards. This skill activates only on explicit request.

## When to Use

**Trigger phrases (Generate):**
- "Write tests for..."
- "Add tests to..."
- "Generate test cases..."
- "Create unit tests..."
- "Add test coverage..."

**Trigger phrases (Update):**
- "Update tests for..."
- "Fix the tests after..."
- "Sync tests with..."
- "Tests are failing, update them..."
- "Update tests to match new..."

**Do NOT use when:**
- Implementing features (unless tests explicitly requested)
- Refactoring code (unless test update explicitly requested)
- Fixing bugs (unless user asks for regression tests)
- Code review suggestions

## Mode Selection

**Generating new tests?** → Follow "Generate Workflow" below
**Updating existing tests?** → Follow "Update Workflow" below
**Doing TDD?** → Follow "TDD Mode" below

---

## TDD Mode

When doing Test-Driven Development (tests before implementation):

1. **Write tests for expected behavior BEFORE implementation exists**
2. **Run tests and confirm they fail** — verify tests actually test something
3. **Commit tests separately** before implementing
4. **Only then proceed to implementation**

### TDD Test Generation

When generating tests for TDD:
- Focus on behavior specification, not implementation details
- Start with the simplest happy path test
- Name tests to describe expected behavior
- Don't assume implementation exists — tests should fail initially

### TDD Commit Pattern

```
Commit 1: test(<scope>): add tests for <feature>
Commit 2: feat(<scope>): implement <feature>
```

For full TDD workflow guidance, see the `tdd-workflow` skill.

---

## Generate Workflow

1. **Analyze** the code to test (read the file first)
2. **Identify** test categories: happy path, edge cases, error handling
3. **Generate** tests following the patterns below
4. **Verify** imports and dependencies exist

---

## Update Workflow

When updating tests after code changes:

1. **Read the changed code** - understand what changed (signature, behavior, return type)
2. **Read existing tests** - find the test file for the changed code
3. **Compare and identify gaps:**
   - Changed function signatures → update test calls
   - New parameters → add test cases for new params
   - Changed return types → update assertions
   - Removed functionality → remove or mark obsolete tests
   - New behavior → add new test cases
4. **Update tests** preserving existing test structure where possible
5. **Verify** all imports still valid after changes

### Update Checklist

```
- [ ] Function signature changes reflected in test calls
- [ ] New parameters have test coverage
- [ ] Changed return values have updated assertions
- [ ] Mocks updated if dependencies changed
- [ ] Removed code has tests removed/updated
- [ ] New edge cases from changes covered
```

### Common Update Patterns

**Function signature changed:**
```python
# Before: def process(data)
# After:  def process(data, validate=True)

# Update existing tests to use new signature
def test_process_returns_result(self):
    result = process(data)  # Still works (default param)

# Add new tests for new parameter
def test_process_skips_validation_when_disabled(self):
    result = process(invalid_data, validate=False)
    assert result is not None  # Doesn't raise
```

**Return type changed:**
```python
# Before: returns dict
# After:  returns Result object

# Update assertions
def test_fetch_returns_user_data(self):
    result = fetch_user("123")
    # Old: assert result["name"] == "John"
    assert result.name == "John"  # Updated for new type
```

**Dependency changed:**
```python
# Before: called external_api()
# After:  called new_api_client.fetch()

# Update mocks
@patch('module.new_api_client')  # Updated path
def test_fetch_calls_api(self, mock_client):
    mock_client.fetch.return_value = {'data': 'value'}  # Updated mock
```

## Test File Structure

```python
"""Tests for {module_name}."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from {package}.{module} import {functions_or_classes}


class TestClassName:
    """Tests for ClassName."""

    # Happy path tests first
    def test_method_returns_expected_result(self):
        """Describe the expected behavior."""
        # Arrange
        # Act
        # Assert

    # Edge cases
    def test_method_handles_empty_input(self):
        """Handle empty input gracefully."""

    # Error cases last
    def test_method_raises_on_invalid_input(self):
        """Raise ValueError for invalid input."""
```

## Naming Conventions

Test names must describe the behavior:

| Pattern | Example |
|---------|---------|
| `test_{method}_{scenario}` | `test_parse_handles_empty_string` |
| `test_{method}_returns_{result}` | `test_calculate_returns_zero_for_empty_list` |
| `test_{method}_raises_{exception}` | `test_validate_raises_value_error_for_none` |

**Bad names:**
- `test_1`, `test_parse`, `test_it_works`

## Required Test Categories

For each function/method, generate tests covering:

### 1. Happy Path (basic functionality)
```python
def test_add_returns_sum_of_two_numbers(self):
    """Return correct sum for valid inputs."""
    result = add(2, 3)
    assert result == 5
```

### 2. Edge Cases
```python
def test_add_handles_zero(self):
    """Handle zero values correctly."""
    assert add(0, 5) == 5
    assert add(5, 0) == 5

def test_add_handles_negative_numbers(self):
    """Handle negative numbers correctly."""
    assert add(-1, 1) == 0
```

### 3. Boundary Conditions
```python
def test_add_handles_large_numbers(self):
    """Handle large number inputs."""
    result = add(10**18, 10**18)
    assert result == 2 * 10**18
```

### 4. Error Handling
```python
def test_add_raises_type_error_for_string_input(self):
    """Raise TypeError when given non-numeric input."""
    with pytest.raises(TypeError):
        add("a", 1)
```

### 5. Failure Modes
```python
def test_divide_raises_zero_division_error(self):
    """Raise ZeroDivisionError for division by zero."""
    with pytest.raises(ZeroDivisionError):
        divide(10, 0)
```

## Mocking External Dependencies

Always mock:
- Database calls
- API requests
- File system operations
- Time-dependent operations
- External services

```python
@patch('module.external_api_call')
def test_fetch_data_returns_parsed_response(self, mock_api):
    """Return parsed data from API response."""
    mock_api.return_value = {'key': 'value'}

    result = fetch_data('endpoint')

    assert result == {'key': 'value'}
    mock_api.assert_called_once_with('endpoint')
```

### Mock Patterns

```python
# Patch decorator
@patch('module.dependency')
def test_with_patch(self, mock_dep):
    mock_dep.return_value = 'mocked'

# Context manager
def test_with_context(self):
    with patch('module.dependency') as mock_dep:
        mock_dep.return_value = 'mocked'

# Mock object attributes
mock_obj = Mock()
mock_obj.method.return_value = 'value'
mock_obj.attribute = 'value'

# Mock async
@patch('module.async_func', new_callable=AsyncMock)
async def test_async(self, mock_func):
    mock_func.return_value = 'result'
```

## Fixtures

Use fixtures for reusable test data:

```python
@pytest.fixture
def sample_user():
    """Create a sample user for testing."""
    return User(name="Test", email="test@example.com")

@pytest.fixture
def mock_database():
    """Mock database connection."""
    with patch('module.db_connection') as mock_db:
        yield mock_db

def test_user_creation(self, sample_user, mock_database):
    """Test user is saved to database."""
    save_user(sample_user)
    mock_database.save.assert_called_once()
```

## Parametrized Tests

Use parametrize for multiple input/output combinations:

```python
@pytest.mark.parametrize("input_val,expected", [
    (0, 0),
    (1, 1),
    (-1, 1),
    (100, 100),
])
def test_absolute_value(self, input_val, expected):
    """Return absolute value for various inputs."""
    assert abs_value(input_val) == expected
```

## Async Tests

```python
import pytest

@pytest.mark.asyncio
async def test_async_fetch_returns_data(self):
    """Return data from async fetch."""
    result = await async_fetch('url')
    assert result is not None
```

## Test Organization

```
tests/
├── conftest.py          # Shared fixtures
├── unit/
│   ├── test_models.py
│   └── test_utils.py
└── integration/
    └── test_api.py
```

## Output Checklist

Before completing, verify:

- [ ] Test file path follows `tests/test_{module}.py` or `tests/unit/test_{module}.py`
- [ ] All test names are descriptive (not `test_1`)
- [ ] Happy path, edge cases, and error handling covered
- [ ] External dependencies are mocked
- [ ] Imports are valid (check source file exists)
- [ ] No hardcoded values that should be fixtures
