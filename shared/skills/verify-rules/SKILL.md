# Verify Rules

Verify that all expected rule files exist on disk and report their load status.

Works across platforms: **Cursor**, **Copilot**, and **Claude Code**.

## When to Use

- After opening a new conversation to confirm rules are available
- When behavior seems off and rules may not be loaded
- When user says "verify rules", "check rules", "are rules loaded?"

**Do NOT use when:**
- User asks about rule content (just read the rule file directly)
- Debugging unrelated issues

## Expected Rules

The following 11 rules must exist on disk. Match by **filename stem** (without extension).

| # | Filename |
|---|----------|
| 1 | `guidelines` |
| 2 | `security` |
| 3 | `code-quality` |
| 4 | `python-style` |
| 5 | `gitignore-template` |
| 6 | `markdown-style` |
| 7 | `mermaid-conventions` |
| 8 | `pytest-conventions` |
| 9 | `workflow` |
| 10 | `github-actions` |
| 11 | `library-research` |

## Platform Detection

Determine which platform you are running on and use the corresponding rules directory and extension:

| Platform | Rules directory | Extension | Example |
|----------|----------------|-----------|---------|
| Cursor | `.cursor/rules/` | `.mdc` | `.cursor/rules/guidelines.mdc` |
| Copilot | `.github/instructions/` | `.instructions.md` | `.github/instructions/guidelines.instructions.md` |
| Claude Code | `.claude/rules/` | `.md` | `.claude/rules/guidelines.md` |

**How to detect the platform:**
- **Cursor** — you are in the Cursor IDE (mentioned in system prompt or tool context)
- **Copilot** — you are GitHub Copilot (mentioned in system prompt)
- **Claude Code** — you are Claude Code / `claude` CLI

## Procedure

### Step 1 — Disk inventory (ground truth)

List the platform's rules directory to check which of the 11 expected files exist:

```bash
# Cursor
ls .cursor/rules/

# Copilot
ls .github/instructions/

# Claude Code
ls .claude/rules/
```

For each file found, determine its load mode from the frontmatter:

| Platform | Always-on | On-demand | Agent-requestable |
|----------|-----------|-----------|-------------------|
| Cursor | `alwaysApply: true` | `globs: ...` present | `alwaysApply: false`, no `globs` |
| Copilot | `applyTo: "**"` | `applyTo: <pattern>` | no `applyTo` |
| Claude Code | no `paths` field | `paths: ...` present | — |

### Step 2 — Context check

Distinguish two levels of presence:

- **Indexed** — the platform lists the rule in its instruction index (knows it exists, shows its path and description). All rules on disk should be indexed.
- **Loaded** — the rule's full content is injected into the current conversation. Only always-on rules are loaded unconditionally; on-demand rules are loaded only when a matching file is open; agent-requestable rules are loaded only when the agent selects them.

For each expected rule, check whether its content is actually loaded (attached/injected) in the conversation — not merely indexed.

Rules on disk but **not loaded** are expected when:
- No matching files are open (on-demand / applyTo rules)
- The agent didn't select them (agent-requestable rules)

**Always-on rules not loaded are failures** — they should be injected into every conversation regardless of open files.

### Step 3 — Report

Combine both checks into a single table:

| # | Rule | On Disk | Indexed | Loaded | Mode |
|---|------|---------|---------|--------|------|
| 1 | `guidelines` | ✅ / ❌ | ✅ / ❌ | ✅ / ❌ | always / on-demand / agent-requestable |
| ... | ... | ... | ... | ... | ... |

- **Indexed** = listed in the platform's instruction index (discoverable)
- **Loaded** = full content injected into the current conversation

### Step 4 — Verdict

PASS requires **both** conditions:

1. All 11 rules found on disk (Step 1)
2. All **always-on** rules are **loaded** in the conversation (Step 2)

Verdicts:

- `RESULT: PASS` — all 11 on disk AND all always-on rules loaded
- `RESULT: FAIL (X missing from disk)` — one or more expected rules have no rule file
- `RESULT: FAIL (X always-on rules not loaded)` — rule files exist but always-on rules are not loaded

On-demand or agent-requestable rules that are indexed but not loaded are **not failures**.
