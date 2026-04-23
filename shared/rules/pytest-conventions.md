# Pytest Conventions

## File Layout

```
tests/
├── conftest.py              # Project-wide fixtures
├── unit/
│   ├── conftest.py          # Unit-specific fixtures
│   └── test_{module}.py     # Mirror source module names
└── integration/
    ├── conftest.py
    └── test_{feature}.py
```

- Test file names: `test_{module}.py` — always prefixed with `test_`
- Mirror the source tree: `src/services/auth.py` → `tests/unit/test_auth.py`
- One test file per source module; split large test files by class or concern

## Naming

- Functions: `test_{method}_{scenario}` — e.g., `test_parse_handles_empty_string`
- Classes: `TestClassName` — group tests for a single class or function
- No numeric suffixes (`test_1`, `test_2`); names describe behavior

## Structure (Arrange-Act-Assert)

```python
def test_transfer_debits_source_account():
    source = Account(balance=100)
    target = Account(balance=0)

    transfer(source, target, amount=30)

    assert source.balance == 70
    assert target.balance == 30
```

- One logical assertion per test (multiple `assert` on the same result is fine)
- Keep tests independent — no test should depend on another's side effects
- No logic in tests (no `if`, `for`, `try`); if needed, parametrize instead

## Fixtures

- Define reusable setup in `conftest.py` at the appropriate scope
- Use the narrowest scope possible: `function` (default) > `class` > `module` > `session`
- Prefer factory fixtures over complex object fixtures
- Use `yield` fixtures for setup/teardown

```python
@pytest.fixture
def db_session():
    session = create_session()
    yield session
    session.rollback()
    session.close()

@pytest.fixture
def make_user():
    def _make(name: str = "test", **kwargs) -> User:
        return User(name=name, email=f"{name}@test.com", **kwargs)
    return _make
```

- Don't use fixtures for trivial data — inline simple values in the test
- Name fixtures after what they *provide*, not what they *do*: `db_session` not `setup_database`

## Parametrize

Use `@pytest.mark.parametrize` when the same logic is tested with different inputs:

```python
@pytest.mark.parametrize("raw,expected", [
    ("hello", "Hello"),
    ("HELLO", "Hello"),
    ("", ""),
])
def test_capitalize_normalizes_case(raw, expected):
    assert capitalize(raw) == expected
```

- Use `pytest.param(..., id="description")` for complex cases to get readable output
- Keep parameter lists short (<10 cases); for larger sets, consider a separate data file

## Assertions

- Use plain `assert` — not `self.assertEqual`, `self.assertTrue`, etc.
- Use `pytest.raises` for expected exceptions, with `match` for message validation
- Use `pytest.approx` for float comparisons
- Avoid asserting on internal state; assert on observable behavior

```python
with pytest.raises(ValueError, match="cannot be negative"):
    withdraw(account, amount=-10)

assert calculate_tax(100.0) == pytest.approx(7.25, abs=0.01)
```

## Mocking

- Mock at the boundary — mock external dependencies, not internal functions
- Patch where the object is *used*, not where it's *defined*
- Prefer dependency injection over patching when possible
- Use `autospec=True` to catch signature mismatches

```python
@patch("myapp.services.payment_gateway.charge", autospec=True)
def test_checkout_charges_correct_amount(mock_charge):
    mock_charge.return_value = Receipt(status="ok")

    checkout(cart, payment_method="card")

    mock_charge.assert_called_once_with(amount=cart.total, method="card")
```

## Async Tests

- Use `@pytest.mark.asyncio` (requires `pytest-asyncio`)
- Set `asyncio_mode = "auto"` in `pyproject.toml` to avoid per-test markers

```python
async def test_fetch_returns_data(http_client):
    result = await http_client.get("/api/data")
    assert result.status == 200
```

## Markers

- `@pytest.mark.slow` — tests that take >1s; exclude from default run
- `@pytest.mark.integration` — tests requiring external services
- Register custom markers in `pyproject.toml` to avoid warnings

```toml
[tool.pytest.ini_options]
markers = [
    "slow: marks tests as slow",
    "integration: marks integration tests",
]
```

## Anti-Patterns

| Avoid | Prefer |
|-------|--------|
| `unittest.TestCase` subclasses | Plain functions + `pytest` fixtures |
| `self.assertEqual(a, b)` | `assert a == b` |
| Test-order dependencies | Independent tests |
| Mocking everything | Mock only external boundaries |
| Giant fixture chains | Factory fixtures or inline setup |
| `assert result` (truthy check) | `assert result == expected_value` |
| Catching exceptions to assert | `pytest.raises(ExcType)` |
