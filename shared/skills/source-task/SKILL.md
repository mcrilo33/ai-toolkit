# Source Task

Anchor the session to its task. This implements the SOURCE step of the development
workflow: fetch the issue, confirm the branch, and restate the contract before any code.

In the hub flow (the default) the branch **already exists** — `worktree-new.sh` created
it when the hub dispatched this spoke — so anchoring is the whole job. Creating a branch
is the fallback for non-hub work only (step 3b).

## Workflow

### 0. Check workspace isolation (worktree guard)

Before creating a branch or writing any code, check whether this session is in a
dedicated worktree or the shared main checkout:

```bash
# Empty output → this IS the main checkout (shared). Non-empty → a linked worktree.
git rev-parse --git-dir | grep -q '/worktrees/' && echo "worktree" || echo "main checkout"
```

If this is the **main checkout** and the task will write code, **warn and offer** —
do not silently proceed:

> You're in the shared main checkout. If another session is also editing here, the
> staging area, `.review/` artifacts, and commit/push hooks collide. For an
> isolated task, run `scripts/worktree-new.sh <issue>` and `/source` in the new
> window. Want me to do that, or proceed here anyway?

Proceed in the main checkout only when the user confirms (e.g. a quick one-off where
a worktree is overkill) or when no other session is active. In a **worktree
already**, skip the warning — the worktree creation already made the branch, so go
straight to anchoring the issue.

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

### 3. Confirm the branch (hub flow — the default)

In a worktree the branch was already created by `worktree-new.sh`. Confirm it matches
the issue and move on — do **not** create another one:

```bash
git branch --show-current   # expect <type>/<id>-<slug>, e.g. feature/123-user-auth
```

If the branch doesn't reference the issue being anchored, stop and ask — the worktree
may have been spawned for a different task.

### 3b. Create branch (non-hub fallback only)

Only when working outside the hub flow (no worktree, branch doesn't exist yet), derive
the branch name from the issue:

| Issue type | Branch pattern | Example |
|-----------|----------------|---------|
| Feature | `feature/<id>-<slug>` | `feature/123-user-auth` |
| Bug fix | `fix/<id>-<slug>` | `fix/456-null-pointer` |
| Chore | `chore/<id>-<slug>` | `chore/789-update-deps` |

Slug rules: lowercase, hyphens, max 4 words from title.

Before creating, verify the name is available:

```bash
git fetch origin
git branch -a | grep "<branch-name>"
```

If a conflict is found, append an incremental suffix (`-v2`, `-v3`) or ask the user.

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
2. If uncommitted changes exist, run `git diff` (or `git diff --cached` for staged) and analyze them to propose a branch name and type
3. Suggest creating a GitHub issue for non-trivial work
4. Create a branch with a descriptive name: `feature/<slug>` or `fix/<slug>`
5. Proceed to DEFINE step

## Edge Cases

| Situation | Action |
|-----------|--------|
| No repo context | Ask for owner/repo |
| Issue not found | Verify number, check repo access |
| Branch already exists | Ask to reuse or create new |
| Multiple repos | Ask which repo to use |
