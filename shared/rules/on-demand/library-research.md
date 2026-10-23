---
description: "Library documentation research — read the installed version, fetch its official docs, and answer from them for any language"
---
# Library Documentation Research

Fetch current, version-specific documentation for any library or framework from its official docs before answering or writing code.

**Applies to**: Any question involving an external library, framework, SDK, or package — regardless of language.

## Workflow

### 1. Find the installed version

Read the project's dependency file (`package.json`, `requirements.txt`, `pyproject.toml`, `Gemfile`, `go.mod`, `Cargo.toml`, etc.) or lockfile. If the library is not installed yet, use the latest stable release.

### 2. Find the official docs

Use `WebSearch` for the library's official documentation or changelog, scoped to the installed version (e.g., `"express 5 routing docs"`). Prefer the project's own site or repository over third-party posts.

### 3. Fetch the page

Use `WebFetch` on the page for the **specific topic** (`"middleware"`, `"hooks"`, `"routing"` — not `"how to use middleware"`). Fetch the changelog or migration guide when the question involves a version change.

### 4. Answer from the docs

Use **only** information from the fetched documentation:
- API signatures, parameters, return types
- Code examples and recommended patterns
- Deprecation warnings and migration notes

Always reference the version your answer applies to.

### 5. Check for version upgrades (when relevant)

If a newer version than the installed one exists, mention it briefly with key breaking changes. Only provide a full migration guide if the user asks.

## Fallback

If the official docs are unreachable or do not cover the topic, say so and mark any answer not sourced from them.

## Rules

- **Never guess API signatures** — if it's not in the docs, say so
- **Be version-specific** — "In Express 5.x..." not "In Express..."
- **No forced upgrade ceremonies** — mention upgrades, don't lecture
- Defer to `python-style`, `code-quality`, `security`, and other rules for coding standards
