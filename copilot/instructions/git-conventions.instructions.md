---
applyTo: ""
---

# Git Conventions

## Commit Messages

Format:
```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `perf`, `ci`

Rules:
- **Brevity**: Always produce the shortest version only. Never offer multiple variants.
- Subject line: imperative mood, max 72 characters
- No period at end of subject
- Body: explain "what" and "why", not "how"
- Reference issue/ticket IDs (required for non-trivial changes)
- Breaking changes: add `BREAKING CHANGE:` in footer or `!` after type

## Branching

Branch types:
- `feature/*`: New features (e.g. `feature/123-user-auth`)
- `fix/*`: Bug fixes
- `hotfix/*`: Urgent production fixes
- `chore/*`: Maintenance tasks

Naming: lowercase with hyphens, include ticket ID when available, max 4 words from title.

Rules:
- Never commit directly to protected branches
- Keep branches short-lived; merge or delete promptly
- One logical change per branch
- Delete branches after merge

## Pull Requests

Before opening:
- Self-review your diff
- Ensure tests pass locally
- Update documentation if needed
- Squash WIP commits if appropriate

PR description must include:
- Summary of what changed and why
- Link to related issues/tickets
- Breaking changes if any
- Testing instructions if non-obvious
- Keep PRs focused and under ~400 lines

## Tags & Releases
- Semantic versioning: `v{major}.{minor}.{patch}`
- Tag releases on main/master only
- Include changelog or release notes
