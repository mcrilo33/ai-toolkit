# Verify Agents

Verify that all expected agent files exist on disk and report their load status.

Works across platforms: **Cursor**, **Copilot**, and **Claude Code**.

## When to Use

- After opening a new conversation to confirm agents are available
- When an agent doesn't seem to trigger and you want to check availability
- When user says "verify agents", "check agents", "are agents loaded?"

**Do NOT use when:**
- User asks about agent content (just read the agent file directly)
- Debugging unrelated issues

## Expected Agents

The following 8 agents must exist on disk. Match by **filename stem** (without extension).

| # | Agent |
|---|-------|
| 1 | `code-review` |
| 2 | `debug` |
| 3 | `devops` |
| 4 | `documentation` |
| 5 | `refactor` |
| 6 | `tdd-green` |
| 7 | `tdd-red` |
| 8 | `tdd-refactor` |

## Platform Detection

Determine which platform you are running on and use the corresponding agents directory and extension:

| Platform | Agents directory | Extension | Example |
|----------|-----------------|-----------|---------|
| Cursor | `.cursor/agents/` | `.md` | `.cursor/agents/code-review.md` |
| Copilot | `.github/agents/` | `.agent.md` | `.github/agents/code-review.agent.md` |
| Claude Code | `.claude/agents/` | `.md` | `.claude/agents/code-review.md` |

**How to detect the platform:**
- **Cursor** — you are in the Cursor IDE (mentioned in system prompt or tool context)
- **Copilot** — you are GitHub Copilot (mentioned in system prompt)
- **Claude Code** — you are Claude Code / `claude` CLI

## Procedure

### Step 1 — Disk inventory (ground truth)

List the platform's agents directory to check which of the 8 expected agent files exist:

```bash
# Cursor
ls .cursor/agents/

# Copilot
ls .github/agents/

# Claude Code
ls .claude/agents/
```

For each agent found, note key frontmatter fields:

| Field | Meaning |
|-------|---------|
| `description` | Agent purpose — used for routing and auto-discovery |
| `disallowedTools` | Tools the agent cannot use (enforces boundaries) |
| `tools` | Tools the agent can use (if restricted) |

### Step 2 — Context check

Distinguish two levels of presence:

- **Indexed** — the platform lists the agent in its agent index (knows it exists, shows its name and description). All agents on disk should be indexed.
- **Loaded** — the agent's description is available in the current conversation context for routing or invocation.

For each expected agent, check whether it appears in the conversation's agent index (listed in the `<agents>` section or equivalent).

Agents on disk but **not indexed** are failures — the platform should always be aware of all agents in the agents directory.

### Step 3 — Report

Combine both checks into a single table:

| # | Agent | On Disk | Indexed | Description match |
|---|-------|---------|---------|-------------------|
| 1 | `code-review` | ✅ / ❌ | ✅ / ❌ | ✅ / ❌ |
| ... | ... | ... | ... | ... |

- **On Disk** = agent file exists in the platform's agents folder with the correct extension
- **Indexed** = listed in the platform's agent index (discoverable for invocation)
- **Description match** = the indexed description matches the on-disk frontmatter description

### Step 4 — Verdict

PASS requires **all** conditions:

1. All 8 agent files found on disk (Step 1)
2. All 8 agents are **indexed** in the platform's agent context (Step 2)
3. All 8 agent descriptions match between disk and index (Step 3)

Verdicts:

- `RESULT: PASS` — all 8 on disk AND all indexed with matching descriptions
- `RESULT: FAIL (X missing from disk)` — one or more expected agent files are missing
- `RESULT: FAIL (X not indexed)` — agent files exist but platform is not aware of them
- `RESULT: FAIL (X description mismatch)` — agent is indexed but description differs from on-disk frontmatter
