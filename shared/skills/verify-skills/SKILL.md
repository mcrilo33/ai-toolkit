# Verify Skills

Verify that all expected skill folders exist on disk and report their load status.

Works across platforms: **Cursor**, **Copilot**, and **Claude Code**.

## When to Use

- After opening a new conversation to confirm skills are available
- When a skill doesn't seem to trigger and you want to check availability
- When user says "verify skills", "check skills", "are skills loaded?"

**Do NOT use when:**
- User asks about skill content (just read the SKILL.md directly)
- Debugging unrelated issues

## Expected Skills

The following 29 skills must exist on disk. Match by **directory name**.

| # | Skill |
|---|-------|
| 1 | `acquire-codebase-knowledge` |
| 2 | `api-design` |
| 3 | `backend-patterns` |
| 4 | `brainstorming` |
| 5 | `ci-cd-review` |
| 6 | `context-map` |
| 7 | `create-readme` |
| 8 | `database-migrations` |
| 9 | `deployment-patterns` |
| 10 | `docker-patterns` |
| 11 | `frontend-patterns` |
| 12 | `generate-docs` |
| 13 | `generate-tests` |
| 14 | `git-commit` |
| 15 | `github-issues` |
| 16 | `hub` |
| 17 | `iterative-retrieval` |
| 18 | `land-task` |
| 19 | `pytest-coverage` |
| 20 | `search-first` |
| 21 | `security-review` |
| 22 | `solo-cycle` |
| 23 | `source-task` |
| 24 | `start-task` |
| 25 | `tdd-workflow` |
| 26 | `verification-loop` |
| 27 | `verify-agents` |
| 28 | `verify-rules` |
| 29 | `verify-skills` |

## Platform Detection

Determine which platform you are running on and use the corresponding skills directory:

| Platform | Skills directory | Entry file | Example |
|----------|-----------------|------------|---------|
| Cursor | `.cursor/skills/` | `SKILL.md` | `.cursor/skills/source-task/SKILL.md` |
| Copilot | `.github/skills/` | `SKILL.md` | `.github/skills/source-task/SKILL.md` |
| Claude Code | `.claude/skills/` | `SKILL.md` | `.claude/skills/source-task/SKILL.md` |

**How to detect the platform:**
- **Cursor** — you are in the Cursor IDE (mentioned in system prompt or tool context)
- **Copilot** — you are GitHub Copilot (mentioned in system prompt)
- **Claude Code** — you are Claude Code / `claude` CLI

## Procedure

### Step 1 — Disk inventory (ground truth)

List the platform's skills directory to check which of the 29 expected skill folders exist and contain a `SKILL.md` entry file:

```bash
# Cursor
ls .cursor/skills/

# Copilot
ls .github/skills/

# Claude Code
ls .claude/skills/
```

For each skill found, determine its invocation mode from the frontmatter in `SKILL.md`:

| Mode | Condition |
|------|-----------|
| Auto | No `disable-model-invocation` or set to `false` — agent can auto-activate |
| Manual | `disable-model-invocation: true` — only triggered via `/skill-name` |
| Hidden | `user-invocable: false` — not shown in `/` menu, agent-only |

### Step 2 — Context check

Distinguish two levels of presence:

- **Indexed** — the platform lists the skill in its skill index (knows it exists, shows its name and description). All skills on disk should be indexed.
- **Loaded** — the skill's description is available in the current conversation context for the agent to decide when to invoke it.

For each expected skill, check whether it appears in the conversation's skill index (listed in the `<skills>` section or equivalent).

Skills on disk but **not indexed** are failures — the platform should always be aware of all skills in the skills directory.

### Step 3 — Report

Combine both checks into a single table:

| # | Skill | On Disk | SKILL.md | Indexed | Mode |
|---|-------|---------|----------|---------|------|
| 1 | `acquire-codebase-knowledge` | ✅ / ❌ | ✅ / ❌ | ✅ / ❌ | auto / manual / hidden |
| ... | ... | ... | ... | ... | ... |

- **On Disk** = skill directory exists in the platform's skills folder
- **SKILL.md** = entry file exists inside the skill directory
- **Indexed** = listed in the platform's skill index (discoverable by the agent)

### Step 4 — Verdict

PASS requires **all** conditions:

1. All 29 skill directories found on disk (Step 1)
2. All 29 skill directories contain a `SKILL.md` entry file (Step 1)
3. All 29 skills are **indexed** in the platform's skill context (Step 2)

Verdicts:

- `RESULT: PASS` — all 29 on disk with SKILL.md AND all indexed
- `RESULT: FAIL (X missing from disk)` — one or more expected skill directories are missing
- `RESULT: FAIL (X missing SKILL.md)` — skill directory exists but has no entry file
- `RESULT: FAIL (X not indexed)` — skill files exist but platform is not aware of them
