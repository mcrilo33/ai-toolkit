---
name: generate-commit-message
description: Generate git commit messages following project conventions. Analyzes staged changes and produces properly formatted messages. Use ONLY when the user explicitly asks to generate, create, or write a commit message. Do NOT generate commit messages proactively during code changes.
---

# Generate Commit Message

Generate commit messages by analyzing `git diff --staged` and following the project's git conventions.

## Workflow

1. Run `git diff --staged` to see what will be committed
2. If no staged changes, inform user and suggest `git add`
3. **Get issue reference:**
   - Extract from branch name: `git branch --show-current`
   - If no issue in branch, check if `/source` command was used earlier
4. Analyze the changes to determine:
   - Primary type of change (feat, fix, docs, etc.)
   - Appropriate scope (module/component affected)
   - Concise description of what changed
5. Generate message following the format below
6. **Include issue reference** in footer for non-trivial changes
7. Present the message for user approval

## Commit Message Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

## Types

| Type | When to Use |
|------|-------------|
| `feat` | New feature or functionality |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `style` | Formatting, no code change |
| `refactor` | Code restructuring, no behavior change |
| `test` | Adding or updating tests |
| `chore` | Maintenance, dependencies, configs |
| `perf` | Performance improvement |
| `ci` | CI/CD configuration changes |

## Rules

### Brevity
- **Always produce only the shortest version.** Never offer multiple variants (short vs long).
- Prefer a single-line subject when sufficient. Add body only when subject alone would be unclear.

### Subject Line
- Use imperative mood: "add feature" not "added feature" or "adds feature"
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

### Footer (Required for non-trivial changes)
- Reference issue/ticket IDs: `Closes #123`, `Fixes #456`
- **Always extract issue ID from branch name**: `feature/123-desc` → `Closes #123`
- If no branch issue ID, check SOURCE step context for linked issue
- Warn if no issue reference for non-trivial changes (feat, fix, refactor)
- Breaking changes: `BREAKING CHANGE: description`

### Issue Reference Priority
1. Branch name: `feature/HEX-123-*` or `fix/123-*`
2. SOURCE step context (if `/source` was used)
3. Ask user if neither available and change is non-trivial

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

**Bug fix:**
```
fix(parser): handle empty input gracefully
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

**With issue reference:**
```
feat(user): add profile image upload

Closes #123
```

**Breaking change:**
```
feat(api)!: change response format for /users endpoint

BREAKING CHANGE: response now returns array instead of object
```

## Edge Cases

| Situation | Action |
|-----------|--------|
| No staged changes | Inform user, suggest `git add` |
| Very large diff | Summarize key changes, suggest splitting if multiple concerns |
| Mixed changes (feat + fix) | Suggest separate commits, or use dominant type |
| Unclear category | Ask user for clarification |
| Multiple files, single concern | Use appropriate scope or omit scope |
| Multiple scopes affected | Omit scope or use broader scope |

## Output Format

Present the generated message in a code block that can be directly used:

```bash
git commit -m "$(cat <<'EOF'
feat(scope): subject line here

Optional body explaining what and why.

Closes #123
EOF
)"
```

Or for simple messages:

```bash
git commit -m "feat(scope): subject line here"
```
