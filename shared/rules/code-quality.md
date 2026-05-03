# Code Quality

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

### Size Limits

- **Functions**: ≤ 50 lines (excluding docstring/comments). If longer, split.
- **Files**: ≤ 800 lines. If larger, decompose into modules.
- **Classes**: ≤ 10 public methods. If more, extract a collaborator.

## Consistency

- Match the style of surrounding code
- Follow existing patterns in the codebase before inventing new ones
- When in doubt, check how similar functionality is implemented elsewhere
- Only fix style violations in code you're actively modifying

## Error Handling

- Provide informative error messages with context (what failed, why, what to do)
- Use guard clauses for early returns to reduce nesting
- **Max nesting depth: 4 levels.** Flatten with early returns, helper functions, or inversion.
- Fail fast: validate inputs at function boundaries
- Catch specific exceptions; never bare `except:`
- Reraise with context when wrapping errors
- Language-specific details live in their respective style rules

## Logging

- Use the language's logging framework, not print statements
- Log at appropriate levels: DEBUG for dev, INFO for flow, WARNING for recoverable, ERROR for failures
- Include correlation IDs or context for traceability

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

### External API Verification

- Verify framework/library API usage against current, authoritative docs before writing code
- Use Context7 proactively to fetch version-specific docs — don't wait for the user to ask
- Never assume an API exists, kept its name, or behaves the same across versions
- Cite the source when a decision relies on external documentation

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

## Tests Are Not Optional

**When you write or modify functional code, you must write or update tests.** Do not wait for the user to ask.

- New function/class/module → write tests covering happy path + key edge cases
- Modified behavior → update existing tests or add new ones to cover the change
- Bug fix → add a regression test that would have caught the bug
- The only exceptions: config files, documentation-only changes, simple renames with no behavior change
- If unsure whether tests are needed, they are

## Documentation Follows Code

**When tests pass, update documentation before moving on.** Do not defer to CLOSE.

- New function/class/module → add docstrings (parameters, return, exceptions)
- Changed behavior → update affected docstrings and any user-facing docs (README, API docs)
- New CLI command/flag/config option → update usage docs
- The only exceptions: pure refactors with no behavior change, internal-only helpers with self-documenting names
- If unsure whether docs need updating, they do

## Pre-Completion Checklist

Before marking work as done, verify every changed/added file against:

- [ ] Functions are small (≤ 50 lines)
- [ ] Files are focused (≤ 800 lines)
- [ ] No deep nesting (> 4 levels)
- [ ] No hardcoded values (URLs, ports, credentials, magic numbers) — use constants or config
- [ ] No unnecessary mutation — prefer immutable data and pure functions
- [ ] No dead code introduced (unused imports, variables, functions)
- [ ] Tests written/updated for all functional changes (not deferred, not optional)
