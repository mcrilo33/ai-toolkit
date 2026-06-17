# Issue Templates

Copy and customize these templates for issue bodies.

Every dispatchable issue ends with a **`Scope:`** line — a space- or comma-separated list
of the files/globs it will touch — so the planner can batch it (see the `issue-hygiene`
rule). A missing line or `Scope: *` marks the issue exclusive (runs alone, the slow path).

## Bug Report Template

```markdown
## Description
[Clear description of the bug]

## Steps to Reproduce
1. [First step]
2. [Second step]
3. [And so on...]

## Expected Behavior
[What should happen]

## Actual Behavior
[What actually happens]

## Environment
- Browser: [e.g., Chrome 120]
- OS: [e.g., macOS 14.0]
- Version: [e.g., v1.2.3]

## Screenshots/Logs
[If applicable]

## Additional Context
[Any other relevant information]

Scope: [files/globs this fix touches; '*' or omitted ⇒ exclusive]
```

## Feature Request Template

```markdown
## Summary
[One-line description of the feature]

## Motivation
[Why is this feature needed? What problem does it solve?]

## Proposed Solution
[How should this feature work?]

## Acceptance Criteria
- [ ] [Criterion 1]
- [ ] [Criterion 2]
- [ ] [Criterion 3]

## Alternatives Considered
[Other approaches considered and why they weren't chosen]

## Additional Context
[Mockups, examples, or related issues]

Scope: [files/globs this feature touches; '*' or omitted ⇒ exclusive]
```

## Task Template

```markdown
## Objective
[What needs to be accomplished]

## Details
[Detailed description of the work]

## Checklist
- [ ] [Subtask 1]
- [ ] [Subtask 2]
- [ ] [Subtask 3]

## Dependencies
[Any blockers or related work — set genuine ordering as native blocked-by, not file overlap]

## Notes
[Additional context or considerations]

Scope: [files/globs this task touches; '*' or omitted ⇒ exclusive]
```

## Minimal Template

For simple issues:

```markdown
## Description
[What and why]

## Tasks
- [ ] [Task 1]
- [ ] [Task 2]

Scope: [files/globs this issue touches; '*' or omitted ⇒ exclusive]
```
