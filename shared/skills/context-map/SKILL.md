# Context Map

Analyze the blast radius of a task before making changes. This implements the DEFINE step
for implementation work — producing a scoped impact analysis that determines whether to
proceed autonomously or pause for confirmation.

## When to Use

- Before any task that touches 2+ files
- When scope or impact is unclear
- When the user says "map this", "what files are involved", "analyze impact"
- Automatically before EXECUTE when `source-task` hands off a non-trivial task

**Do NOT use when:**
- Single-file, clear-scope change (proceed directly)
- Full codebase discovery needed (use `acquire-codebase-knowledge`)
- User explicitly says "just do it" or "skip planning"

## Scope Boundaries

- **Depth:** Max 2 levels of transitive dependencies (A imports B imports C — stop at C)
- **Width:** Max 20 files in the map. If more are found, group by module and summarize
- **Focus:** Only files that would need changes or could break. Ignore unaffected consumers

## Workflow

### Step 1: Identify Entry Points

From the task description, identify the primary targets:

```bash
# Find files matching the feature area
grep -rl "<keyword>" --include="*.py" --include="*.ts" src/
```

List the files that will be **directly modified**.

### Step 2: Trace Dependencies

For each file to modify, find what depends on it:

```bash
# Find files that import from the target module
grep -rl "from <module> import\|import <module>" --include="*.py" .
grep -rl "from ['\"].*<module>" --include="*.ts" --include="*.tsx" .
```

Classify each dependency:
- **Will break** — uses the specific function/class being changed
- **May break** — imports the module but may not use affected symbols
- **Safe** — unrelated imports from the same package

### Step 3: Find Tests

```bash
# Find test files for affected modules
find . -name "test_*.py" -o -name "*_test.py" -o -name "*.test.ts" | \
  xargs grep -l "<module_or_function_name>"
```

If no tests exist for affected code, flag it as a risk.

### Step 4: Find Reference Patterns

Search for similar changes already made in the codebase:

```bash
# Recent commits touching similar files
git log --oneline -10 -- <target_file>

# Similar patterns in other modules
grep -rl "<pattern>" --include="*.py" .
```

### Step 5: Assess Risk

Check each item. Mark `[x]` if applicable:

- Breaking changes to public API or exported symbols
- Database migrations or schema changes needed
- Configuration or environment variable changes
- New dependencies required
- Cross-module or cross-service impact
- No existing test coverage for affected code

### Step 6: Produce the Map

```markdown
## Context Map: <task summary>

### Files to Modify
| File | Purpose | Change |
|------|---------|--------|
| path | what it does | what changes |

### Affected Dependencies
| File | Risk | Reason |
|------|------|--------|
| path | will break / may break | uses X from modified file |

### Tests
| Test File | Status |
|-----------|--------|
| path | exists / missing / needs update |

### Reference Patterns
| File | Relevance |
|------|-----------|
| path | similar change to follow |

### Risks
- [ ] Public API breaking change
- [ ] Missing test coverage
- [ ] Config/env changes needed
- [ ] New dependency required

### Execution Plan

#### Phase 1: Types and Interfaces
- [ ] Step 1.1: [action] in `file`
- [ ] Verify: [how to check it worked]

#### Phase 2: Implementation
- [ ] Step 2.1: [action] in `file`
- [ ] Verify: [how to check]

#### Phase 3: Tests
- [ ] Step 3.1: [action] in `test_file`
- [ ] Verify: run tests

#### Phase 4: Cleanup
- [ ] Remove deprecated code
- [ ] Update documentation

### Rollback Plan
If something fails:
1. [Step to undo]
2. [Step to undo]
```

### Step 7: Decide — Proceed or Pause

Apply the autonomy threshold:

| Condition | Action |
|-----------|--------|
| ≤ 2 files to modify, no risks checked | Proceed to EXECUTE |
| 3+ files, no risks checked | Show map, ask to confirm |
| Any risk checked | Show map, ask to confirm |
| User said "just do it" | Proceed regardless |

## Relationship to Other Skills

| Skill | Scope | When |
|-------|-------|------|
| `acquire-codebase-knowledge` | Whole repo | Onboarding, discovery |
| **`context-map`** | **Single task** | **Before implementation** |
| `source-task` | Task setup | Before context-map |
| `generate-tests` | Test creation | After implementation |
