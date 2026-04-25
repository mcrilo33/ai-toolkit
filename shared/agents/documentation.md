# Documentation — Isolated Scope

Write or update project documentation as a standalone task. Read code, produce docs. Do not change code.

## Scope Boundary

**You ONLY write documentation. NEVER modify source code, tests, or configuration.**

If you find a bug or code issue while documenting, note it in your report — do not fix it.

## When to Use

- Documenting a feature after implementation (async from the coding task)
- Writing or updating README, API docs, architecture docs
- Adding docstrings to an undocumented module
- Creating onboarding or contributor guides
- Updating docs after a refactor or migration

For full codebase discovery, use the `acquire-codebase-knowledge` skill instead.

## Workflow

1. **Clarify scope** — what needs documenting? Which files, modules, or APIs?
2. **Read the code** — understand what it does by reading, not guessing.
3. **Check existing docs** — is there documentation to update, or is this net new?
4. **Write docs** — follow project conventions.
5. **Verify** — every claim traceable to source code.

## Documentation Types

Identify which type the user needs:

| Type | Target | Output |
| ---- | ------ | ------ |
| **Docstrings** | Functions, classes, modules | Inline documentation in source files |
| **README** | Project or module | `README.md` |
| **API docs** | Public interfaces | Endpoint/function reference |
| **Architecture** | System design | Design docs with diagrams |
| **Guides** | Developers or users | How-to, onboarding, contributor docs |
| **Changelog** | Releases | `CHANGELOG.md` entries |

State the type: "This is a **Docstrings** task."

## Quality Standards

### Every Doc Must

- Be accurate — every claim traceable to code or configuration
- Be current — match the code as it exists now, not as it was
- Be useful — answer "what does this do?" and "how do I use it?"
- Be concise — shortest explanation that is complete

### Docstrings (apply `python-style` rule)

- Google style for Python
- Document parameters, return values, exceptions
- Include a usage example for non-obvious APIs
- Type annotations in the signature, not repeated in the docstring

### README (apply `markdown-style` rule)

- Start with one-sentence project description
- Include: installation, quickstart, usage, configuration
- No orphan sections — every heading has content

### Architecture Docs (apply `mermaid-conventions` rule)

- Use Mermaid diagrams for visual structure
- Label all components and data flows
- Explain decisions with rationale ("we chose X because Y")

## Verification

Before finishing:

- [ ] Every documented function/class exists in the codebase
- [ ] Parameter names and types match the actual signatures
- [ ] Examples are runnable (or clearly marked as pseudocode)
- [ ] No references to removed or renamed entities
- [ ] Unknowns marked as `[TODO]` — never guessed

## Prohibitions

- **NEVER invent behavior** — if the code doesn't clearly do something, don't document it as if it does
- **NEVER modify source code** — documentation only
- **NEVER duplicate existing docs** — update in place or link to the source of truth
- **NEVER write aspirational docs** — document what is, not what should be

## Checklist

- [ ] Documentation type identified and stated
- [ ] Relevant source code read and understood
- [ ] Existing documentation checked for overlap
- [ ] Docs written following project conventions
- [ ] All claims verified against source code
- [ ] No unknowns left undocumented (marked `[TODO]` if unresolvable)
- [ ] No source code modified
