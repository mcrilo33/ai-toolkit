# Specification Template

Reference template for creating AI-ready specification documents. Adapt sections to the project — omit what doesn't apply, add what's missing.

## File Conventions

- Save in the project's `spec/` directory (or wherever the team keeps specs).
- Name: `spec-<purpose>-<topic>.md` (e.g., `spec-schema-user-profile.md`, `spec-process-deployment.md`).
- Purpose prefix: `schema`, `tool`, `data`, `infrastructure`, `process`, `architecture`, or `design`.

## Template

```markdown
---
title: [Concise title]
version: [e.g., 1.0]
date_created: [YYYY-MM-DD]
last_updated: [YYYY-MM-DD]
tags: [e.g., infrastructure, process, design]
---

# Introduction

[1–2 sentences: what this spec covers and why it exists.]

## 1. Purpose & Scope

[What problem does this solve? Who is the audience? What is in/out of scope?]

## 2. Definitions

[Define acronyms, abbreviations, and domain terms used in this spec.]

| Term | Definition |
|------|------------|
| ... | ... |

## 3. Requirements & Constraints

[List requirements, constraints, and guidelines. Use a consistent prefix.]

- **REQ-001**: [Functional requirement]
- **CON-001**: [Constraint]
- **SEC-001**: [Security requirement]
- **GUD-001**: [Guideline / recommendation]

## 4. Interfaces & Data Contracts

[Describe APIs, schemas, data contracts, or integration points. Use code blocks or tables.]

## 5. Acceptance Criteria

[Testable criteria — prefer Given/When/Then format.]

- **AC-001**: Given [context], When [action], Then [expected outcome]

## 6. Dependencies

[External systems, services, infrastructure, or data sources this spec depends on.]

- **DEP-001**: [Dependency] — [purpose and integration type]

## 7. Examples & Edge Cases

[Concrete examples, sample data, and edge cases that clarify the requirements.]

## 8. Related Specifications

[Links to related specs or external documentation.]
```

## Adaptation Guidelines

- **Small feature spec**: sections 1, 3, 5 may be sufficient.
- **API spec**: expand section 4 with request/response schemas and error codes.
- **Infrastructure spec**: expand section 6 with environment and platform details.
- **Always include**: Purpose & Scope (§1), Requirements (§3), Acceptance Criteria (§5).
- **Omit empty sections** rather than leaving them as placeholders.

## Writing Principles

- Use precise, unambiguous language — no idioms or metaphors.
- Distinguish requirements (MUST) from guidelines (SHOULD).
- Define all domain terms in §2.
- Make the document self-contained — don't rely on external context.
- Include examples for anything non-obvious.
