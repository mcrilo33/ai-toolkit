---
name: github-issues
description: "Create, update, and manage GitHub issues using the gh CLI. Use this skill when users want to create bug reports, feature requests, or task issues, update existing issues, add labels/assignees/milestones, set issue fields (dates, priority, custom fields), set issue types, manage issue workflows, link issues, add dependencies, or track blocked-by/blocking relationships. Triggers on requests like 'create an issue', 'file a bug', 'request a feature', 'update issue X', 'set the priority', 'set the start date', 'link issues', 'add dependency', 'blocked by', 'blocking', or any GitHub issue management task."
argument-hint: "[issue title, number, or URL]"
---
# GitHub Issues

Manage GitHub issues with the `gh` CLI. Use `gh api` for what the porcelain commands lack (issue `type`, REST and GraphQL features).

## Commands

| Operation | Command |
|-----------|---------|
| Create issue | `gh issue create -t ... -b ... -l label -a user -m milestone` |
| Create issue with type | `gh api repos/{owner}/{repo}/issues -X POST -f title=... -f body=... -f type=Bug` |
| View issue, comments, labels | `gh issue view {number} --json title,body,state,labels,comments` |
| Edit issue | `gh issue edit {number} --title ... --add-label ...` |
| Close issue | `gh issue close {number}` |
| Add comment | `gh issue comment {number} -b ...` |
| Search issues | `gh issue list --search "..."` (cross-repo: `gh search issues "..."`) |
| List repo labels | `gh label list` |

**Note:** `gh issue create` does **not** support a `--type` flag. Use `gh api` when you need to set issue types.

## Workflow

1. **Determine action**: Create, update, or query?
2. **Gather context**: Get repo info, existing labels, milestones if needed
3. **Structure content**: Use appropriate template from [references/templates.md](references/templates.md)
4. **Execute**: Use `gh issue` commands; use `gh api` for unsupported params (e.g., `type`)
5. **Confirm**: Report the issue URL to user

## Creating Issues

### Using gh api (when type is needed)

```bash
gh api repos/{owner}/{repo}/issues \
  -X POST \
  -f title="Issue title" \
  -f body="Issue body in markdown" \
  -f type="Bug" \
  --jq '{number, html_url}'
```

#### Optional Parameters

Add any of these flags to the `gh api` call:

```
-f type="Bug"                    # Issue type (Bug, Feature, Task, Epic, etc.)
-f labels[]="bug"                # Labels (repeat for multiple)
-f assignees[]="username"        # Assignees (repeat for multiple)
-f milestone=1                   # Milestone number
```

**Issue types** are organization-level metadata. To discover available types, use GraphQL:

```bash
gh api graphql -f query='{ organization(login: "ORG") { issueTypes(first: 10) { nodes { name } } } }' --jq '.data.organization.issueTypes.nodes[].name'
```

**Prefer issue types over labels for categorization.** When issue types are available (e.g., Bug, Feature, Task), use the `type` parameter instead of applying equivalent labels like `bug` or `enhancement`. Issue types are the canonical way to categorize issues on GitHub. Only fall back to labels when the org has no issue types configured.

### Title Guidelines

- Be specific and actionable
- Keep under 72 characters
- When issue types are set, don't add redundant prefixes like `[Bug]`
- Examples:
  - `Login fails with SSO enabled` (with type=Bug)
  - `Add dark mode support` (with type=Feature)
  - `Add unit tests for auth module` (with type=Task)

### Body Structure

Always use the templates in [references/templates.md](references/templates.md). Every
template ends with the `Scope:` + `Gate:` footer that makes the issue schedulable — keep
both lines (a missing `Scope:` silently serializes the issue; see the `issue-hygiene`
rule). Choose based on issue type:

| User Request | Template |
|--------------|----------|
| Bug, error, broken, not working | Bug Report |
| Feature, enhancement, add, new | Feature Request |
| Task, chore, refactor, update | Task |

## Updating Issues

### Using gh issue

```bash
gh issue edit {number} --title "Updated title" --add-label bug
gh issue close {number}
```

Only pass the fields you want to change.

### Using gh api (fields without a flag)

```bash
gh api repos/{owner}/{repo}/issues/{number} \
  -X PATCH \
  -f state=closed \
  -f title="Updated title" \
  --jq '{number, html_url}'
```

## Examples

### Example 1: Bug Report

**User**: "Create a bug issue - the login page crashes when using SSO"

**Action**:
```bash
gh api repos/github/awesome-copilot/issues \
  -X POST \
  -f title="Login page crashes when using SSO" \
  -f type="Bug" \
  -f body="## Description
The login page crashes when users attempt to authenticate using SSO.

## Steps to Reproduce
1. Navigate to login page
2. Click 'Sign in with SSO'
3. Page crashes

## Expected Behavior
SSO authentication should complete and redirect to dashboard.

## Actual Behavior
Page becomes unresponsive and displays error." \
  --jq '{number, html_url}'
```

### Example 2: Feature Request

**User**: "Create a feature request for dark mode with high priority"

**Action**:
```bash
gh api repos/github/awesome-copilot/issues \
  -X POST \
  -f title="Add dark mode support" \
  -f type="Feature" \
  -f labels[]="high-priority" \
  -f body="## Summary
Add dark mode theme option for improved user experience and accessibility.

## Motivation
- Reduces eye strain in low-light environments
- Increasingly expected by users

## Proposed Solution
Implement theme toggle with system preference detection.

## Acceptance Criteria
- [ ] Toggle switch in settings
- [ ] Persists user preference
- [ ] Respects system preference by default" \
  --jq '{number, html_url}'
```

## Tips

- Always confirm the repository context before creating issues
- Ask for missing critical information rather than guessing
- Link related issues when known: `Related to #123`
- For updates, fetch current issue first to preserve unchanged fields
- Discover repo labels with `gh label list` rather than assuming standard labels exist

## Edge Cases

| Situation | Action |
|-----------|--------|
| No repo context | Ask for owner/repo |
| Issue not found | Verify number, check repo access |
| `gh` CLI not authenticated | Suggest `gh auth login` |
| Org has no issue types | Skip `type` param, use labels instead |
| Need to set `type` | Use `gh api` (`gh issue create` has no `--type` flag) |

## Extended Capabilities

The following features require REST or GraphQL APIs beyond the basic `gh issue` commands. Each is documented in its own reference file so the agent only loads the knowledge it needs.

| Capability | When to use | Reference |
|------------|-------------|-----------|
| Advanced search | Complex queries with boolean logic, date ranges, cross-repo search, issue field filters (`field.name:value`) | [references/search.md](references/search.md) |
| Sub-issues & parent issues | Breaking work into hierarchical tasks | [references/sub-issues.md](references/sub-issues.md) |
| Issue dependencies | Tracking blocked-by / blocking relationships | [references/dependencies.md](references/dependencies.md) |
| Issue types (advanced) | GraphQL operations beyond the `type` param | [references/issue-types.md](references/issue-types.md) |
| Projects V2 | Project boards, progress reports, field management | [references/projects.md](references/projects.md) |
| Issue fields | Custom metadata: dates, priority, text, numbers (private preview) | [references/issue-fields.md](references/issue-fields.md) |
| Images in issues | Embedding images in issue bodies and comments via CLI | [references/images.md](references/images.md) |
