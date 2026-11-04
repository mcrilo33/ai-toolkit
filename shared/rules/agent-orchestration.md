---
description: "Which agents exist, when to spawn each, and the independent review before ship"
---
# Agent Orchestration

Four agents exist. Everything else (planning, design, debugging, TDD, docs) is done inline.

## Routing Table

| Situation | Agent | Trigger signals |
| --------- | ----- | --------------- |
| Diff, branch or PR review; the ship gate | `code-review` | "review this", PR link, "check my changes", before every push |
| Security audit or vulnerability check | `security-reviewer` | auth, payments, PII, secrets, public API changes |
| A defect found outside the issue's Scope | `bug-scoper` | see `bug-triage` |
| A deferred follow-up found outside the issue's Scope | `followup-scoper` | see `bug-triage` |

### When not to spawn

- User said "don't use agents" or "do it inline".
- Anything outside the table: work it inline.

## What's enforced

The machinery enforces *outcomes*, not which agent produced them: CI green on the branch, and an independent
`code-review` (run by the coordinator with a different model) whose verdict reports `tdd_followed` and
`tests_weakened`. So the one load-bearing delegation is an independent `code-review` before you ship: an
independent reader catches what the implementer is blind to. **Always separate, before ship.**

TDD is inline: write the RED test, the GREEN implementation and any refactor yourself. The test is written
first, so a separate agent has nothing to add. What the machinery enforces is the commit split: only a `test:`
commit may change a test file, whoever writes it.

## Review depth

Every `code-review` runs two stages, and the first gates the second:

1. **Spec compliance** — does the change do what the plan/issue asked, with no scope
   creep? A correct implementation of the wrong thing fails here.
2. **Code quality** — only once intent is confirmed: correctness, quality, security.

For high-stakes changes (auth, payments, PII, public API, schema migrations, wide blast
radius), run `security-reviewer` next to `code-review`, in parallel (they share no data).
An internal refactor, docs-only, or test-only change needs just one review.

## When spawning an agent

State the goal in one sentence, hand over the relevant context (paths, diff, constraints),
and name the output format you need back. Don't over-specify method — let the specialist work.
