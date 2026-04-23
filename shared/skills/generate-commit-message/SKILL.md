# Generate Commit Message

Generate commit messages by analyzing `git diff --staged` and following project git conventions.

## Workflow

1. Run `git diff --staged` to see what will be committed
2. If no staged changes, inform user and suggest `git add`
3. Extract issue reference from branch name: `git branch --show-current`
4. Analyze changes to determine: type, scope, concise description
5. Generate message — **shortest version only**, never offer variants
6. Include issue reference in footer for non-trivial changes

## Format

```
<type>(<scope>): <subject>

[optional body — only when subject alone would be unclear]

[Closes #<id> — required for feat/fix/refactor]
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

- Subject: imperative mood, max 72 chars, no period
- Body: explain "what" and "why", not "how"
- Footer: `Closes #<id>` for non-trivial changes
- Breaking changes: `BREAKING CHANGE:` in footer or `!` after type
- **Brevity**: always produce only the shortest version

## Output

Present the result as a ready-to-use command:

```bash
git commit -m "<message>"
```
