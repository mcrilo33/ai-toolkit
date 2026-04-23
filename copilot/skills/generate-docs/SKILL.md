---
name: generate-docs
description: Generate or update documentation including docstrings, README files, API docs, and inline comments following project conventions. Use when the user asks to document code, write docs, update docs, sync documentation, add docstrings, or fix outdated documentation.
---

# Generate & Update Docs

Generate or update documentation following project quality standards.

## When to Use

**Generate:** "Document this...", "Add docstrings to...", "Write a README for...", "Generate API docs..."

**Update:** "Update the docs for...", "Sync documentation with...", "Docs are outdated, fix...", "Update docstrings after..."

**Do NOT use when:** implementing features, refactoring, or fixing bugs — unless docs are explicitly requested.

## Principles

1. Document "why", not "what" — code shows what, comments explain intent
2. Choose descriptive names that eliminate the need for comments
3. Keep comments current — outdated comments are worse than none
4. Be explicit about contracts — parameters, return values, exceptions

## Generate Workflow

1. Analyze the code structure
2. Identify what needs documentation (functions, classes, modules)
3. Determine the appropriate doc format for the language
4. Write documentation following templates below
5. Verify accuracy against the code

## Update Workflow

1. Read the changed code — understand what changed
2. Read existing documentation
3. Compare and identify gaps
4. Update documentation preserving existing structure and style
5. Verify examples still work with new code

## Python Docstrings (Google style)

```python
def fetch_user(user_id: str, include_profile: bool = False) -> User:
    """Fetch a user by ID from the database.

    Args:
        user_id: The unique identifier for the user.
        include_profile: Whether to include the full profile data.
            Defaults to False for performance.

    Returns:
        User object with basic info, or full profile if requested.

    Raises:
        UserNotFoundError: If no user exists with the given ID.
        DatabaseConnectionError: If the database is unavailable.

    Example:
        >>> user = fetch_user("abc123")
        >>> user.name
        'John Doe'
    """
```

## Language Detection

| Language | Docstring Style | Comment Style |
|----------|-----------------|---------------|
| Python | Google/NumPy docstrings | `#` |
| TypeScript/JavaScript | JSDoc | `//` or `/* */` |
| Java | Javadoc | `//` or `/* */` |
| Go | GoDoc | `//` |
| Rust | Rustdoc (`///`) | `//` |

## Output Checklist

- [ ] Docstrings use imperative mood ("Fetch", not "Fetches")
- [ ] All parameters documented with types
- [ ] Return values described
- [ ] Exceptions/errors documented
- [ ] Examples provided for non-obvious usage
- [ ] Comments explain "why", not "what"
- [ ] No redundant comments stating the obvious
