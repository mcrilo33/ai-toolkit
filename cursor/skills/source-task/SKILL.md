---
name: source-task
description: >-
  Start a new task by fetching issue details and creating a feature branch.
  Use when the user says /source, "start task", "pick up issue", or wants
  to begin work on a GitHub issue or ad-hoc request.
---

# Source Task

Fetch a task and set up a branch for it. This implements the SOURCE step of the development workflow.

## Workflow

### 1. Identify the task

Determine the task origin. Ask the user if unclear:

- **GitHub issue URL or number** → fetch with GitHub MCP `issue_read`
- **Ad-hoc request** → clarify scope and acceptance criteria in chat

### 2. Fetch issue details (GitHub)

If working from a GitHub issue:

```
CallMcpTool: user-github / issue_read
  owner: <org-or-user>
  repo: <repo>
  issue_number: <number>
  method: get
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

If the repo uses GitHub MCP for branch creation:

```
CallMcpTool: user-github / create_branch
  owner: <org-or-user>
  repo: <repo>
  branch: <branch-name>
```

Then checkout locally: `git fetch && git checkout <branch-name>`

### 4. Summarize

Present to the user:
- Task: title + one-line summary
- Acceptance criteria (bulleted list)
- Branch name
- Ask: "Ready to define done criteria, or should we start coding?"

## Ad-hoc Tasks (No Issue)

When the user describes work without an issue:

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
