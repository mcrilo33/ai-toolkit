---
applyTo: "**/*.{py,ts,tsx,js,jsx,go,rs,java}"
---

# Code Quality

## Error Handling
- Provide informative error messages with context (what failed, why, what to do)
- Use guard clauses for early returns to reduce nesting
- Fail fast: validate inputs at function boundaries
- Catch specific exceptions; never bare `except:`
- Reraise with context when wrapping errors

## Logging
- Use the language's logging framework, not print statements
- Log at appropriate levels: DEBUG for dev, INFO for flow, WARNING for recoverable, ERROR for failures
- Include correlation IDs or context for traceability

## Testing
- Write tests for edge cases, not just happy paths
- Test failure modes explicitly
- Prefer isolated unit tests; mock external dependencies
- Name tests to describe the behavior being verified

## Clarity Over Cleverness
- Write code that reads like prose; optimize for the next developer
- Explicit is better than implicit
- Avoid premature optimization; profile before optimizing

## Single Responsibility
- Each function should do one thing well
- Each file/module should have a clear, singular purpose
- If a function needs a comment explaining what it does, consider renaming or splitting it

## Consistency
- Match the style of surrounding code
- Follow existing patterns in the codebase before inventing new ones
- Only fix style violations in code you're actively modifying

## Performance
- Use context managers / RAII for resource lifecycle
- Prefer lazy evaluation and generators for large datasets
- Cache expensive computations when inputs are repeated
- Avoid N+1 query patterns in database operations
- Use appropriate data structures (sets for membership, dicts for lookup)

## Documentation
- Choose descriptive names that eliminate the need for comments
- Document "why", not "what"
- Keep comments current; outdated comments are worse than none
- Document function signatures: parameters, return values, exceptions
- Use type annotations as living documentation

## Dependency Management
- Don't add a library for something achievable in 10 lines
- Prefer standard library solutions when adequate
- Avoid global state; pass dependencies explicitly
- Use dependency injection for testability
- Isolate third-party integrations behind abstractions
