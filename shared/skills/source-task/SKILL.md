# Source Task

Fetch a task and set up a branch for it. This implements the SOURCE step of the development workflow.

## Workflow

### 1. Identify the task

Determine the task origin. Ask the user if unclear:

- **GitHub issue URL or number** → fetch issue details
- **Ad-hoc request** → clarify scope and acceptance criteria in chat

### 2. Fetch issue details (GitHub)

```bash
gh issue view <number> --json title,body,labels,assignees
```

Extract from the response:

- Title and description
- Acceptance criteria (look for checklists, "AC", "done when")
- Labels and assignees

### 3. Create branch

Derive the branch name from the issue:

| Issue type | Branch pattern | Example |
|-----------|----------------|---------|
| Feature | `feature/<id>-<slug>` | `feature/123-user-auth` |
| Bug fix | `fix/<id>-<slug>` | `fix/456-null-pointer` |
| Chore | `chore/<id>-<slug>` | `chore/789-update-deps` |

Slug rules: lowercase, hyphens, max 4 words from title.

```bash
git checkout -b <branch-name>
```

### 4. Summarize

Present to the user:

- Task: title + one-line summary
- Acceptance criteria (bulleted list)
- Branch name
- Ask: "Ready to define done criteria, or should we start coding?"

## Ad-hoc Tasks (No Issue)

1. Summarize the request back to confirm understanding
2. Suggest creating a GitHub issue for non-trivial work
3. Create a branch with a descriptive name: `feature/<slug>` or `fix/<slug>`
4. Proceed to DEFINE step

## Edge Cases

| Situation | Action |
|-----------|--------|
| No repo context | Ask for owner/repo |
| Issue not found | Verify number, check repo access |
| Branch already exists | Ask to reuse or create new |
| Multiple repos | Ask which repo to use |
