# Python Code Style

## Type Annotations

- All function parameters and return types must be annotated
- Use `int | str | None` syntax, not `Optional` or `Union`
- Use `list[str]`, `dict[str, int]` (lowercase), not `List`, `Dict`
- Use `X | None` instead of `Optional[X]`
- For complex types, create type aliases at module level
- Use `TypeVar` for generic functions; `ParamSpec` for decorators

## Docstrings (Google Style)

- Required for all public functions, classes, and modules
- Include: summary line, Args, Returns, Raises (when applicable)
- First line: concise imperative sentence ending with period
- Skip docstrings for obvious one-liners and private helpers (`_func`)
- Example format:

```python
def process(data: list[dict], strict: bool = False) -> Result:
    """Process input data and return validated result.

    Args:
        data: List of dictionaries containing raw records.
        strict: If True, raise on validation errors instead of skipping.

    Returns:
        Validated Result object with processed records.

    Raises:
        ValidationError: When strict=True and validation fails.
    """
```

## Naming

- `snake_case`: functions, variables, methods, modules
- `PascalCase`: classes, type aliases, exceptions
- `UPPER_SNAKE_CASE`: module-level constants
- `_prefix`: internal/private (single underscore)
- `__prefix`: name mangling (double underscore, rare)
- Prefix unused variables with `_` (e.g., `for _ in range(n)`)
- Allowed single letters: `i`, `j`, `k` (indices), `x`, `y` (coords), `e` (exceptions), `f` (files), `n` (counts)

## Imports

```python
# 1. Standard library
import os
from collections import defaultdict
from pathlib import Path

# 2. Third-party
import pandas as pd
from pydantic import BaseModel

# 3. Local/project
from hex.models import Spec
from .utils import helper
```

- Blank line between each group
- Alphabetize within groups
- Prefer explicit imports over `from x import *`
- Use absolute imports for cross-package; relative for within-package
- Avoid circular imports (restructure if needed)

## Modern Python (3.10+)

### Prefer

- `match` statements over long if/elif chains
- Structural pattern matching for type dispatch
- `int | str` over `Union[int, str]`
- `list[str]` over `List[str]` (no import needed)
- `@dataclass(slots=True, frozen=True)` for immutable data
- `@functools.cache` over `@lru_cache(maxsize=None)`
- Walrus operator `:=` when it improves clarity
- `with` statements for resource management (always)

### Avoid

- `typing.Optional`, `typing.Union`, `typing.List`, `typing.Dict`
- `@dataclass` without `slots=True` (unless inheritance needed)
- String formatting with `%` or `.format()` — use f-strings

## Module Size

- Aim for <300 lines per module; consider splitting if larger

## Functions

- Max ~50 lines per function (prefer shorter, single-purpose)
- Single return type when possible (avoid `int | str | None`)
- Use keyword-only args (`*, arg`) for functions with >3 parameters
- Default arguments: immutable only (`None`, not `[]` or `{}`)
- Use `*args` and `**kwargs` sparingly; prefer explicit params

```python
# Good: keyword-only after *
def query(table: str, *, limit: int = 100, offset: int = 0) -> list[Row]:
    ...

# Good: None default for mutable
def process(items: list[str] | None = None) -> list[str]:
    items = items or []
    ...
```

## Classes

- Prefer `@dataclass` for data containers
- Use `__slots__` for memory efficiency in non-dataclasses
- Favor composition over inheritance
- Keep `__init__` simple; use `@classmethod` for alternative constructors
- Use `@property` for computed attributes, not getters/setters

## Error Handling

- Catch specific exceptions: `except ValueError as e:`
- Never bare `except:` — minimum is `except Exception as e:`
- Reraise with context: `raise NewError(...) from e`
- Use guard clauses for early returns
- Custom exceptions inherit from appropriate base (`ValueError`, `RuntimeError`)

```python
# Good: guard clause
def process(data: Data | None) -> Result:
    if data is None:
        raise ValueError("data cannot be None")
    if not data.is_valid:
        return Result.empty()
    return _do_process(data)
```

## Formatting (Ruff/Black Compatible)

- Max line length: 88 characters
- Trailing commas in multi-line collections
- Double quotes for strings (Ruff default)
- No trailing whitespace
- Single blank line between functions; two between classes
- No blank lines at start/end of blocks

## Anti-Patterns to Avoid

| Instead of | Use |
|------------|-----|
| `type(x) == int` | `isinstance(x, int)` |
| `def f(items=[])` | `def f(items=None)` |
| `len(x) == 0` | `not x` |
| `if x == True` | `if x` |
| `if x == None` | `if x is None` |
| `except: pass` | Handle or log explicitly |
| `from module import *` | Explicit imports |
| `global var` | Pass as parameter or use class |
| Manual file handling | `with open(...) as f:` |

## Pydantic Models

### Model Definition

- Inherit from `BaseModel` for API/data boundary models
- Use `model_config = ConfigDict(...)` (class-level), not inner `class Config`
- Set `frozen=True` for immutable models; `strict=True` to disable coercion
- Use `Field()` for constraints, defaults, and descriptions

```python
from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    email: str
    age: int = Field(ge=0, le=150)
    role: str = "viewer"
```

### Validators

- Use `@field_validator` for single-field validation
- Use `@model_validator` for cross-field or whole-model validation
- Prefer `mode="before"` for input normalization, `mode="after"` for business rules
- Return the value from field validators; raise `ValueError` on failure

```python
from pydantic import field_validator, model_validator


class Order(BaseModel):
    quantity: int
    unit_price: float
    discount: float = 0.0

    @field_validator("discount")
    @classmethod
    def discount_in_range(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError("discount must be between 0 and 1")
        return v

    @model_validator(mode="after")
    def total_must_be_positive(self) -> "Order":
        if self.quantity * self.unit_price * (1 - self.discount) <= 0:
            raise ValueError("order total must be positive")
        return self
```

### Serialization

- Use `model_dump()` and `model_validate()`, not deprecated `.dict()` / `.parse_obj()`
- Control output with `exclude`, `by_alias`, `exclude_none`
- Define `alias` or `serialization_alias` in `Field()` for API key mapping
- Use `@computed_field` for derived properties that should appear in output

```python
class APIResponse(BaseModel):
    internal_id: int = Field(exclude=True)
    user_name: str = Field(alias="userName")

    @computed_field
    @property
    def display_name(self) -> str:
        return self.user_name.title()
```

### Patterns

| Use case | Approach |
|----------|----------|
| Plain data container | `@dataclass(slots=True)` |
| API request/response | `BaseModel` with `ConfigDict` |
| Settings / config | `BaseSettings` (from `pydantic-settings`) |
| Discriminated unions | `Annotated[A | B, Field(discriminator="type")]` |
| Reusable field sets | Mixin models or shared `Field()` definitions |

### Anti-Patterns

| Avoid | Prefer |
|-------|--------|
| `class Config:` inner class | `model_config = ConfigDict(...)` |
| `.dict()`, `.parse_obj()` | `model_dump()`, `model_validate()` |
| `@validator` (v1) | `@field_validator` (v2) |
| `@root_validator` (v1) | `@model_validator` (v2) |
| Mutable models for API schemas | `frozen=True` |
| Validators with side effects | Pure validation; side effects in service layer |

## Async (asyncio)

### General

- Use `async def` for I/O-bound operations (network, file, database)
- Keep CPU-bound work synchronous; offload to threads with `asyncio.to_thread()`
- Never call blocking I/O inside `async def` — it blocks the entire event loop
- Annotate return types: `async def fetch(url: str) -> Response:`

### Coroutines and Tasks

- `await` for sequential async calls
- `asyncio.gather()` for concurrent independent calls
- `asyncio.TaskGroup` (3.11+) over `gather()` — structured concurrency with proper error handling
- Cancel tasks explicitly when no longer needed

```python
# Good: TaskGroup for concurrent work (3.11+)
async def fetch_all(urls: list[str]) -> list[Response]:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch(url)) for url in urls]
    return [t.result() for t in tasks]


# Good: sequential when order matters
async def pipeline(data: RawData) -> Result:
    validated = await validate(data)
    enriched = await enrich(validated)
    return await store(enriched)
```

### Context Managers and Iterators

- Use `async with` for async resources (sessions, connections, locks)
- Use `async for` for async iteration (streams, paginated APIs)
- Implement `__aenter__` / `__aexit__` for custom async context managers

```python
async def process_stream(url: str) -> list[Record]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return [parse(chunk) async for chunk in resp.content.iter_any()]
```

### Event Loop

- Never call `asyncio.run()` inside an already-running loop
- Use `asyncio.run()` as the single top-level entry point
- Prefer `asyncio.to_thread()` over `loop.run_in_executor()` (3.9+)

### Testing Async Code

- Use `@pytest.mark.asyncio` with `pytest-asyncio`
- Set `asyncio_mode = "auto"` in `pyproject.toml` to avoid per-test markers
- Use `async def test_...` directly — no manual event loop management

```python
async def test_fetch_returns_data(mock_client: AsyncClient) -> None:
    result = await mock_client.get("/api/items")
    assert result.status_code == 200
```

### Async Error Handling

- Catch `ExceptionGroup` (3.11+) when using `TaskGroup`
- Use `except*` for selective handling of specific exception types within a group
- Always log or propagate — never silently swallow async errors

```python
async def process_batch(items: list[Item]) -> list[Result]:
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(process(item)) for item in items]
    except* ValueError as eg:
        for exc in eg.exceptions:
            logger.warning("Validation failed: %s", exc)
        raise
    except* OSError as eg:
        for exc in eg.exceptions:
            logger.error("I/O error: %s", exc)
        raise
    return [t.result() for t in tasks]
```

### Cancellation

- Check `asyncio.current_task().cancelled()` in long loops
- Use `asyncio.timeout()` (3.11+) instead of `asyncio.wait_for()`
- Always clean up resources in `finally` blocks — cancellation raises `CancelledError`

```python
async def poll_until_ready(resource_id: str) -> Status:
    async with asyncio.timeout(30):
        while True:
            status = await check_status(resource_id)
            if status.is_ready:
                return status
            await asyncio.sleep(1)
```

### Async Anti-Patterns

| Avoid | Prefer |
| ----- | ------ |
| `time.sleep()` in async code | `await asyncio.sleep()` |
| `requests` in async code | `aiohttp` or `httpx.AsyncClient` |
| Bare `asyncio.gather()` without error handling | `asyncio.TaskGroup` (3.11+) |
| `loop.run_in_executor()` | `asyncio.to_thread()` (3.9+) |
| `asyncio.get_event_loop()` | `asyncio.run()` at top level |
| Fire-and-forget tasks | Track tasks; await or cancel them |
| Mixing sync and async in one module | Clear boundary: async at edges, sync core |

### Async Patterns

| Scenario | Use |
| -------- | --- |
| Run tasks concurrently, fail-fast | `asyncio.TaskGroup` |
| Run tasks concurrently, tolerate partial failure | `asyncio.gather(return_exceptions=True)` |
| Offload blocking call | `asyncio.to_thread(sync_fn, *args)` |
| Timeout a single operation | `async with asyncio.timeout(seconds)` |
| Background fire-and-forget | `asyncio.create_task()` + store reference |
| Graceful shutdown | `asyncio.Event` signal + `task.cancel()` |
