# Code Quality

## Error Handling

- Provide informative error messages with context (what failed, why, what to do)
- Use guard clauses for early returns to reduce nesting
- Fail fast: validate inputs at function boundaries
- Catch specific exceptions; never bare `except:`
- Reraise with context when wrapping errors
- Language-specific details live in their respective style rules

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
- No abstractions or speculative "flexibility" that wasn't requested
- No error handling for impossible scenarios
- If the implementation is significantly longer than needed, rewrite shorter
- "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## Single Responsibility

- Each function should do one thing well
- Each file/module should have a clear, singular purpose
- If a function needs a comment explaining what it does, consider renaming or splitting it

## Consistency

- Match the style of surrounding code
- Follow existing patterns in the codebase before inventing new ones
- When in doubt, check how similar functionality is implemented elsewhere
- Only fix style violations in code you're actively modifying

## Performance

### Resource Management

- Use context managers / RAII for resource lifecycle
- Release large objects when no longer needed
- Prefer lazy evaluation and generators for large datasets

### Efficiency

- Cache expensive computations when inputs are repeated
- Avoid N+1 query patterns in database operations
- Use appropriate data structures (sets for membership, dicts for lookup)

## Documentation

### Code as Documentation

- Choose descriptive names that eliminate the need for comments
- Document "why", not "what" — code shows what, comments explain intent
- Keep comments current; outdated comments are worse than none

### API Contracts

- Document function signatures: parameters, return values, exceptions
- Be explicit about nullability and optional parameters
- Use type annotations as living documentation

## Surgical Changes

- Don't "improve" adjacent code, comments, or formatting
- Don't refactor things that aren't broken
- Match existing style, even if you'd do it differently
- If you notice unrelated dead code, mention it — don't delete it
- Remove imports/variables/functions that YOUR changes made unused
- Don't remove pre-existing dead code unless asked
- Every changed line should trace directly to the user's request

## Dependency Management

### Minimize

- Don't add a library for something achievable in 10 lines
- Evaluate maintenance status and community support before adopting
- Prefer standard library solutions when adequate

### Isolate

- Avoid global state; pass dependencies explicitly
- Use dependency injection for testability
- Isolate third-party integrations behind abstractions
