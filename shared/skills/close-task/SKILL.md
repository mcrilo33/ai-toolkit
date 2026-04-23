# Close Task

Commit, push, and create a PR. This implements the CLOSE step of the development workflow.

## Workflow

### 1. Verify work is complete

Before closing:

- Run `git status` and `git diff` to review changes
- Check acceptance criteria from SOURCE step are met
- Ensure tests pass if applicable: `pytest` or project test command

If anything is incomplete, flag it and ask whether to proceed or fix first.

### 2. Generate commit message

Use the `generate-commit-message` skill. Follow `git-conventions.md` for format.

Key rules:

- Extract issue ID from branch name for the footer
- Use conventional commit format: `<type>(<scope>): <subject>`
- Brevity: shortest version only

For TDD workflows, create two commits:

1. `test(<scope>): add tests for <feature>`
2. `feat(<scope>): implement <feature>`

### 3. Commit and push

```bash
git add -A
git commit -m "<message>"
git push -u origin HEAD
```

### 4. Create pull request

```bash
gh pr create --title "<pr-title>" --body "$(cat <<'EOF'
## Summary
<bullet points from commit message>

## Test plan
<how to verify the changes>

Closes #<issue-number>
EOF
)"
```

PR body should include:

- Summary of what changed and why
- Link to source issue (`Closes #<id>`)
- Test plan or verification steps
- Breaking changes if any

### 5. Report

Present to the user:

- Commit SHA and message
- PR URL
- Linked issue

## Edge Cases

| Situation | Action |
|-----------|--------|
| No staged changes | Inform user, suggest `git add` |
| No issue in branch name | Ask for issue reference or skip |
| Tests failing | Warn and ask whether to proceed |
| Merge conflicts | Help resolve before pushing |
| No remote set up | Run `git remote add origin <url>` first |
| Large diff (>400 lines) | Suggest splitting into smaller PRs |
