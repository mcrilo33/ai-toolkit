# Generate & Update Docs

Generate or update documentation following project quality standards. This skill activates only on explicit request.

## When to Use

**Trigger phrases (Generate):**
- "Document this..."
- "Add docstrings to..."
- "Write a README for..."
- "Generate API docs..."
- "Add comments to..."
- "Create documentation..."

**Trigger phrases (Update):**
- "Update the docs for..."
- "Sync documentation with..."
- "Docs are outdated, fix..."
- "Update docstrings after..."
- "README needs updating..."
- "Fix the API docs..."

**Trigger phrases (Specification):**
- "Create a spec..."
- "Write a specification..."
- "Draft a spec for..."
- "Create a specification..."

**Do NOT use when:**
- Implementing features (unless docs explicitly requested)
- Refactoring code (unless doc update explicitly requested)
- Fixing bugs
- Code review (suggest docs separately, don't add)

## Mode Selection

**Generating new docs?** → Follow "Generate Workflow" below
**Updating existing docs?** → Follow "Update Workflow" below
**Creating a specification?** → Follow "Specification Workflow" below

---

## Update Workflow

When updating documentation after code changes:

1. **Read the changed code** - understand what changed (signature, behavior, parameters)
2. **Read existing documentation** - find docstrings, README sections, API docs
3. **Compare and identify gaps:**
   - Changed function signatures → update Args/params
   - New parameters → document new params with types
   - Changed return types → update Returns section
   - New exceptions → add to Raises section
   - Removed functionality → remove from docs
   - Changed behavior → update description and examples
4. **Update documentation** preserving existing structure and style
5. **Verify** examples still work with new code

### Update Checklist

```
- [ ] Function signature changes reflected in docstring
- [ ] New parameters documented with types and descriptions
- [ ] Changed return types updated
- [ ] New exceptions documented in Raises
- [ ] Examples updated to match new API
- [ ] README updated if public API changed
- [ ] API docs updated if endpoints changed
```

### Common Update Patterns

**Parameter added:**
```python
# Before: def fetch(user_id: str) -> User
# After:  def fetch(user_id: str, include_deleted: bool = False) -> User

# Update docstring Args section:
"""Fetch a user by ID.

Args:
    user_id: The unique identifier for the user.
    include_deleted: Whether to include soft-deleted users.
        Defaults to False.
"""
```

**Return type changed:**
```python
# Before: returns dict
# After:  returns User object

# Update Returns section:
"""
Returns:
    User object with id, name, and email attributes.
    Returns None if user not found.
"""
```

**New exception:**
```python
# Added validation that raises ValueError

# Add to Raises section:
"""
Raises:
    UserNotFoundError: If no user exists with the given ID.
    ValueError: If user_id is empty or invalid format.
"""
```

**Example outdated:**
```python
# API changed from positional to keyword argument

# Update Example section:
"""
Example:
    >>> user = fetch_user(user_id="abc123")  # Updated
    >>> user.name
    'John Doe'
"""
```

---

## Generate Workflow

1. **Determine document type** — pick the right quadrant for the user's goal:
   - **Tutorial** (learning-oriented) — guides a newcomer through a lesson to a successful outcome
   - **How-to Guide** (problem-oriented) — steps to solve a specific problem (a recipe)
   - **Reference** (information-oriented) — technical description of the machinery (a dictionary)
   - **Explanation** (understanding-oriented) — clarifies a topic with context and rationale (a discussion)
2. **Clarify audience and scope** — who is reading this, and what is in/out of scope
3. **Analyze** the code structure
4. **Identify** what needs documentation (functions, classes, modules)
5. **Determine** the appropriate doc format for the language
6. **Write** documentation following templates below
7. **Verify** accuracy against the code

---

## Specification Workflow

Create a structured specification document optimized for AI consumption.

1. **Clarify scope** — ask what the spec covers if not clear from the request
2. **Explore context** — read relevant code, configs, and existing docs to understand the domain
3. **Read the template** — load `references/spec-template.md` from this skill's directory
4. **Draft the spec** — adapt the template to the project (omit irrelevant sections, expand relevant ones)
5. **Review** — verify requirements are testable, language is unambiguous, and the doc is self-contained

### Specification Checklist

```
- [ ] Purpose & Scope clearly defined
- [ ] All domain terms defined
- [ ] Requirements are testable and unambiguous
- [ ] Acceptance criteria use Given/When/Then where appropriate
- [ ] No empty placeholder sections
- [ ] Document is self-contained
```

---

## Documentation Principles

From project guidelines:

1. **Document "why", not "what"** - code shows what, comments explain intent
2. **Code as documentation** - choose descriptive names that eliminate need for comments
3. **Keep comments current** - outdated comments are worse than none
4. **Be explicit about contracts** - parameters, return values, exceptions

## Documentation Types

### 1. Function/Method Docstrings

**Python (Google style):**
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

**TypeScript/JavaScript (JSDoc):**
```typescript
/**
 * Fetch a user by ID from the database.
 *
 * @param userId - The unique identifier for the user
 * @param includeProfile - Whether to include full profile data
 * @returns User object with basic info or full profile
 * @throws {UserNotFoundError} If no user exists with the given ID
 *
 * @example
 * const user = await fetchUser("abc123");
 * console.log(user.name); // "John Doe"
 */
```

**Docstring Requirements:**
- One-line summary (imperative mood: "Fetch", not "Fetches")
- Args/params with types and descriptions
- Return value description
- Exceptions that can be raised
- Example for non-obvious usage

### 2. Class Docstrings

```python
class UserService:
    """Service for managing user operations.

    Handles user CRUD operations with caching and validation.
    Uses connection pooling for database efficiency.

    Attributes:
        cache: Redis cache instance for user data.
        db: Database connection pool.

    Example:
        >>> service = UserService(cache, db)
        >>> user = service.create_user({"name": "John"})
    """
```

### 3. Module/File Headers

```python
"""User management module.

This module provides user authentication and profile management.
It integrates with the OAuth provider and local database.

Key components:
    - UserService: Main service class for user operations
    - AuthMiddleware: Request authentication middleware
    - UserValidator: Input validation for user data

Usage:
    from users import UserService
    service = UserService()
    user = service.get_current_user(request)
"""
```

### 4. README Files

**Template:**

```markdown
# Project Name

Brief one-line description of what this project does.

## Overview

2-3 sentences explaining the purpose and key functionality.

## Installation

pip install package-name

## Quick Start

from package import main_function
result = main_function()

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| API_KEY | API authentication key | Required |
| DEBUG | Enable debug mode | false |

## API Reference

Brief overview of main functions/classes.
Link to full API docs if available.

## Contributing

Link to CONTRIBUTING.md or brief guidelines.

## License

MIT License - see LICENSE file.
```

### 5. API Documentation

**Endpoint documentation:**

```markdown
## POST /api/users

Create a new user account.

### Request

{ "name": "John Doe", "email": "john@example.com", "password": "securepassword" }

### Response

**201 Created**
{ "id": "abc123", "name": "John Doe", "email": "john@example.com" }

**400 Bad Request**
{ "error": "validation_error", "message": "Email already exists" }

### Authentication

Requires Authorization: Bearer <token> header.
```

### 6. Inline Comments

**When to use:**
- Complex algorithms that aren't self-explanatory
- Workarounds for known issues (with ticket reference)
- Performance-critical code explaining optimization
- Regex patterns

**Good inline comments:**
```python
# Binary search requires sorted input - caller must ensure this
index = binary_search(sorted_items, target)

# Workaround for API bug returning null dates (see JIRA-1234)
date = response.date or datetime.now()

# O(n log n) - using timsort for stable sorting of user scores
users.sort(key=lambda u: u.score)

# Match: user@domain.tld with optional +tag
email_pattern = r'^[\w\+\.-]+@[\w\.-]+\.\w+$'
```

**Avoid:**
```python
# Bad: states the obvious
i += 1  # increment i

# Bad: explains "what" not "why"
users = get_users()  # get users from database
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

Before completing, verify:

- [ ] Docstrings use imperative mood ("Fetch", not "Fetches")
- [ ] All parameters documented with types
- [ ] Return values described
- [ ] Exceptions/errors documented
- [ ] Examples provided for non-obvious usage
- [ ] Comments explain "why", not "what"
- [ ] No redundant comments stating the obvious
- [ ] README follows template structure
