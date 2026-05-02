# Search First

Research existing solutions before writing new code. Follow a structured search ladder
to avoid reinventing the wheel, discover prior art, and pick the best available tool
for the job.

## When to Use

- Before implementing any non-trivial feature or integration
- Before adding a new dependency or library
- Before building a utility, helper, or pattern that likely exists already
- When the user says "find a library for", "is there an MCP for", or "how do others do X"
- When scoping work for `context-map` or `source-task` and the approach is unclear

**Do NOT use when:**

- Fix is obvious and localized (typo, off-by-one, missing import)
- User explicitly says "I know what I want, just build it"
- Task is purely internal refactoring with no external dependencies

## Search Ladder

Work through each rung in order. Stop as soon as you find a satisfactory answer.

### Rung 1 — Codebase (already solved here?)

Check if the project or its dependencies already solve the problem:

```bash
# Search the workspace for existing implementations
grep -rl "<keyword>" --include="*.py" --include="*.ts" --include="*.go" .
```

Look for:

- Existing utility functions or helper modules
- Already-installed packages that cover the need
- Prior art in git history (`git log --all --oneline --grep="<keyword>"`)

**Exit if:** The codebase already has it — reuse, don't rebuild.

### Rung 2 — GitHub (prior art?)

Search GitHub for existing implementations and patterns:

```bash
# Search repos
gh search repos "<query>" --limit 5 --sort stars

# Search code for specific patterns
gh search code "<function or pattern>" --language <lang> --limit 10
```

Evaluate results by:

- Star count and recent activity (last commit < 6 months)
- License compatibility (MIT, Apache-2.0, BSD preferred)
- Maintenance status (open issues ratio, release cadence)
- Code quality (tests present, CI passing)

**Exit if:** A well-maintained repo or snippet solves it — link it, don't rewrite it.

### Rung 3 — Library Docs (right tool, right API?)

Use Context7 to fetch current documentation:

1. Resolve the library ID (`resolve-library-id`)
2. Query the specific topic (`query-docs`)
3. Verify the API exists in the version used by the project

Follow the full `library-research` rule for this step.

**Exit if:** An installed or readily-available library covers the use case.

### Rung 4 — Package Registries (something purpose-built?)

Search the relevant package registry for purpose-built solutions:

```bash
# Python
pip search "<query>"          # or: https://pypi.org/search/?q=<query>

# Node.js
npm search "<query>" --limit 5

# Rust
cargo search "<query>" --limit 5

# Go
# Browse https://pkg.go.dev/search?q=<query>
```

Evaluate by:

- Weekly downloads / recent version
- Dependency count (fewer is better)
- Security advisories (`npm audit`, `pip-audit`, `cargo audit`)

**Exit if:** A lightweight, well-maintained package exists — prefer it over hand-rolling.

### Rung 5 — MCP Servers (tool already available?)

Check if an MCP server already provides the capability:

- Review the workspace MCP configuration (`mcp.json`, `settings.json`)
- Check if any configured server's tools cover the need
- Search for community MCP servers: `gh search repos "mcp-server <topic>" --sort stars`

**Exit if:** An MCP server already exposes the functionality — use it via tool calls.

### Rung 6 — Web Research (broader context)

Search the web for articles, discussions, and patterns:

- Use Tavily or web search for recent approaches
- Check Stack Overflow, blog posts, official guides
- Look for comparison articles ("X vs Y for Z")

**Exit if:** You have enough context to make an informed build-vs-buy decision.

## Decision Output

After completing the search, produce a brief summary:

```markdown
## Search Summary: <topic>

**Rung reached:** <1–6>

### Findings
- <what was found at each rung checked>

### Recommendation
- **Use:** <library / repo / MCP / existing code>
- **Reason:** <why this is the best option>
- **Alternative:** <runner-up if any>

### If building from scratch
- <justify why none of the existing options work>
- <estimated effort vs adapting an existing solution>
```

## Rules

- **Never skip Rung 1** — always check the codebase first
- **Bias toward reuse** — building from scratch requires justification
- **License matters** — flag GPL/AGPL dependencies that may conflict
- **Recency matters** — deprioritize packages with no updates in 12+ months
- **Max 5 minutes of research** — if nothing emerges, state that and proceed
- Defer to `library-research` for Context7 workflow details
- Defer to `security` rule for dependency vetting
