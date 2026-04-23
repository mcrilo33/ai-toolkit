---
name: commit-msg
description: Generate a conventional commit message from staged changes
agent: agent
---

Analyze `git diff --staged` and produce a commit message following project conventions.

## Steps

1. Run `git diff --staged`. If empty, inform me and suggest `git add`.
2. Extract issue reference from branch name: `git branch --show-current` (e.g. `feature/123-desc` → `Closes #123`).
3. Determine: type (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`), scope (module affected), concise description.
4. Generate a single commit message — **shortest version only**, never offer variants.

## Format

```
<type>(<scope>): <subject>

[optional body — only when subject alone would be unclear]

[Closes #<id> — required for feat/fix/refactor]
```

## Rules

- Subject: imperative mood, max 72 chars, no period
- Body: explain "what" and "why", not "how"
- Footer: `Closes #<id>` for non-trivial changes; warn if no issue ref for feat/fix/refactor
- Breaking changes: `BREAKING CHANGE:` in footer or `!` after type

## Output

Present the result as a ready-to-use command:

```bash
git commit -m "<message>"
```
