# Git Commit

Analyze staged changes, stage files intelligently, generate a conventional commit message, and execute the commit.

## Workflow

### 1. Check Staging Status

```bash
git status --porcelain
git diff --staged --stat
```

- If files are staged → proceed to step 3
- If nothing staged → proceed to step 2
- If no changes at all → inform user, stop

### 2. Stage Files (if needed)

Group changes into logical units. One commit = one logical change.

```bash
# Stage specific files
git add path/to/file1 path/to/file2

# Stage by pattern
git add *.test.*
git add src/components/*

# Interactive staging for partial changes
git add -p
```

**Never stage secrets** (`.env`, credentials, private keys, tokens). If detected, warn and exclude.

### 3. Analyze Diff

```bash
git diff --staged
```

Determine:

- **Type**: What kind of change? (see Types table)
- **Scope**: What module/area is affected?
- **Description**: One-line summary (present tense, imperative mood, <72 chars)

### 4. Get Issue Reference

1. Extract from branch name: `git branch --show-current`
   - `feature/123-desc` → `Closes #123`
   - `fix/HEX-456-desc` → `Closes #HEX-456`
2. If no issue in branch, check if `/source` command was used earlier
3. Ask user if neither available and change is non-trivial (feat, fix, refactor)

### 5. Generate and Execute Commit

```bash
# Single line
git commit -m "<type>(<scope>): <description>"

# Multi-line with body/footer
git commit -m "<type>(<scope>): <description>" -m "<body>" -m "<footer>"
```

Present the message for user approval before executing.

## Commit Message Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

## Types

| Type       | When to Use                      |
| ---------- | -------------------------------- |
| `feat`     | New feature or functionality     |
| `fix`      | Bug fix                          |
| `docs`     | Documentation only               |
| `style`    | Formatting, no code change       |
| `refactor` | Code restructuring, no behavior change |
| `test`     | Adding or updating tests         |
| `chore`    | Maintenance, dependencies, configs |
| `perf`     | Performance improvement          |
| `ci`       | CI/CD configuration changes      |
| `build`    | Build system/dependencies        |
| `revert`   | Revert a previous commit         |

## Rules

### Brevity

- **Always produce only the shortest version.** Never offer multiple variants.
- Prefer a single-line subject when sufficient. Add body only when subject alone would be unclear.

### Subject Line

- Imperative mood: "add feature" not "added feature" or "adds feature"
- Maximum 72 characters
- No period at end
- Lowercase after type/scope

### Scope

- Optional but recommended
- Use the module, component, or area affected
- Examples: `auth`, `api`, `parser`, `readme`, `deps`

### Body

- Separate from subject with blank line
- Explain "what" and "why", not "how"
- Wrap at 72 characters
- Use when subject alone isn't sufficient

### Footer

- Reference issue/ticket IDs: `Closes #123`, `Fixes #456`
- Required for non-trivial changes (feat, fix, refactor)
- Breaking changes: `BREAKING CHANGE: description`

## Breaking Changes

Two valid formats:

```
feat(api)!: change response format for /users endpoint

BREAKING CHANGE: response now returns array instead of object
```

Or just the footer:

```
feat(api): change response format for /users endpoint

BREAKING CHANGE: response now returns array instead of object
```

## Examples

**Simple feature:**
```
feat(auth): add OAuth2 login flow
```

**Bug fix with issue ref:**
```
fix(parser): handle empty input gracefully

Closes #42
```

**Documentation:**
```
docs(readme): update installation instructions
```

**Refactoring with body:**
```
refactor(api): extract validation into separate module

Move input validation logic to dedicated validator class
to improve testability and reduce controller complexity.
```

## Git Safety Protocol

- NEVER update git config
- NEVER run destructive commands (`--force`, `hard reset`) without explicit request
- NEVER skip hooks (`--no-verify`) unless user asks
- NEVER force push to main/master
- If commit fails due to hooks, fix the issue and create a NEW commit (don't amend)
