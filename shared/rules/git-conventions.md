# Git Conventions

## Commit Messages

### Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

### Types

- `feat`: New feature or functionality
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Formatting, no code change
- `refactor`: Code restructuring, no behavior change
- `test`: Adding or updating tests
- `chore`: Maintenance, dependencies, configs
- `perf`: Performance improvement
- `ci`: CI/CD configuration changes

### Rules

- **Brevity**: Always produce the shortest version only. Never offer multiple variants.
- Subject line: imperative mood, max 72 characters
- No period at end of subject
- Body: explain "what" and "why", not "how"
- Reference issue/ticket IDs (required for non-trivial changes)
- Breaking changes: add `BREAKING CHANGE:` in footer or `!` after type

### Examples

```
feat(auth): add OAuth2 login flow

fix(parser): handle empty input gracefully

docs(readme): update installation instructions

refactor(api): extract validation into separate module

Closes #123
```

```
feat(api)!: change response format for /users endpoint

BREAKING CHANGE: response now returns array instead of object
```

## Branching

### Branch Types

- `main` / `master`: Production-ready, protected
- `dev` / `develop`: Integration branch (if used)
- `feature/*`: New features
- `fix/*`: Bug fixes
- `hotfix/*`: Urgent production fixes
- `release/*`: Release preparation
- `chore/*`: Maintenance tasks

### Naming

- Use lowercase with hyphens: `feature/add-user-auth`
- Include ticket ID when available: `feature/HEX-123-user-auth`
- Keep names short but descriptive
- Avoid special characters except `-` and `/`

### General Rules

- Never commit directly to protected branches
- Keep branches short-lived; merge or delete promptly
- Rebase feature branches on target before merging (when appropriate)
- Delete branches after merge
- One logical change per branch

## Pull Requests / Merge Requests

### Before Opening

- Self-review your diff
- Ensure tests pass locally
- Update documentation if needed
- Squash WIP commits if appropriate

### PR Description

- Summarize what changed and why
- Link related issues/tickets
- Verify acceptance criteria from DEFINE step are met
- Note any breaking changes
- Include testing instructions if non-obvious

### Review Etiquette

- Keep PRs focused and reasonably sized (<400 lines ideal)
- Respond to feedback promptly
- Approve only after understanding the change
- Use suggestions for minor fixes

## Tags & Releases

- Use semantic versioning: `v{major}.{minor}.{patch}`
- Tag releases on main/master only
- Include changelog or release notes
- `major`: Breaking changes
- `minor`: New features, backward compatible
- `patch`: Bug fixes, backward compatible

## Collaboration

### Do

- Pull/fetch before starting work
- Communicate before force-pushing shared branches
- Write meaningful commit messages
- Keep main/master always deployable

### Don't

- Commit large binary files (use Git LFS if needed)
- Commit generated files that can be rebuilt
- Rewrite history on shared branches without coordination
- Leave stale branches lingering
