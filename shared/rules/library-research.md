# Library Documentation Research

Fetch current, version-specific documentation for any library or framework using Context7 before answering or writing code.

**Applies to**: Any question involving an external library, framework, SDK, or package — regardless of language.

## Workflow

### 1. Resolve library ID

Call `resolve-library-id` with the library name. Select the best match by:
- Exact name match
- High source reputation and benchmark score
- Most code snippets available

### 2. Fetch documentation

Call `query-docs` with the resolved ID and a **specific topic** (e.g., `"middleware"`, `"hooks"`, `"routing"` — not `"how to use middleware"`).

Adjust `tokens` based on complexity:
- Simple syntax check: 2000–3000
- Standard feature usage: 5000 (default)
- Complex integration: 7000–10000

### 3. Answer from docs

Use **only** information from the retrieved documentation:
- API signatures, parameters, return types
- Code examples and recommended patterns
- Deprecation warnings and migration notes

Always reference the version your answer applies to.

### 4. Check for version upgrades (when relevant)

If the user's workspace has a dependency file (`package.json`, `requirements.txt`, `pyproject.toml`, `Gemfile`, `go.mod`, `Cargo.toml`, etc.):
- Compare installed version against the latest available from Context7's version list
- If a newer version exists, mention it briefly with key breaking changes
- Only provide a full migration guide if the user asks

## Fallback

If Context7 returns no results or insufficient documentation:
1. State that Context7 didn't cover the topic
2. Use web search as fallback
3. Clearly mark answers not sourced from Context7

## Rules

- **Never guess API signatures** — if it's not in the docs, say so
- **Be version-specific** — "In Express 5.x..." not "In Express..."
- **No forced upgrade ceremonies** — mention upgrades, don't lecture
- **Max 3 Context7 calls per question** — resolve → query → (optional second query)
- Defer to `python-style`, `code-quality`, `security`, and other rules for coding standards
