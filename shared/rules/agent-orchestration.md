# Agent Orchestration

Delegate to specialist agents when a task matches their expertise. Over-delegating
costs a little time; under-delegating on a complex task is the failure worth avoiding.
When in doubt, spawn.

## Routing Table

The single source of truth for who handles what. A matching row names the specialist;
whether to spawn it or work inline is governed by the rubric and enforcement split below.

| Situation | Agent | Trigger signals |
| --------- | ----- | --------------- |
| Complex multi-file feature | `planner` | path unclear, crosses module/API/data boundaries, DEFINE phase |
| System design or structure decision | `architect` | "design the architecture", new service/module, data model |
| Security audit or vulnerability check | `security-reviewer` | "audit security", auth/payments/PII changes |
| PR or diff review requested | `code-review` | "review this", PR link, "check my changes" |
| Bug report, error, stack trace | `debug` | "it's broken", stack trace, `FAILED`, unexpected behavior |
| New feature — write tests first | `tdd-red` | TDD mode, "write tests for X", DEFINE phase |
| Make failing tests pass | `tdd-green` | "make tests pass", GREEN phase, failing test output |
| Tests pass — clean up | `tdd-refactor` | "refactor", REFACTOR phase, all tests green |
| Cross-cutting rename/restructure | `refactor` | "rename X to Y across the codebase", pattern migration |
| CI/CD, infra, deployment | `devops` | workflow files, Docker, deploy, pipeline |
| Write or update docs only | `documentation` | "document this", docs-only task |

Spawn `planner` when the **path is unclear** (real unknowns, multiple viable designs) or
the **blast radius** is wide (crosses module/service boundaries, a public API, a data
model, or shared state). Skip it for large-but-mechanical work — a rename, find/replace,
or dependency bump — and do that directly or via `refactor`. File count is a smell, not a
trigger: a 5-file rename needs no planner; a 1-file auth change needs review. The
`delegation-gate-warn` hook nudges on file count but no longer blocks the ship on it — the
hard ship gate is `code-review`.

### When not to spawn

- Single file, or exactly 2 files with trivial scope — do it yourself.
- User said "don't use agents" or "do it inline".
- Internal refactor with no behavior change.

## Watch for multi-turn drift

Small requests accumulate into a multi-file change across turns. Re-assess each turn
against the rubric above using the *cumulative* scope of the whole task, not just the
current request: once the change crosses a boundary or the path stops being obvious,
pause and spawn `planner` with full context of what's done and what remains. Gradual
scope growth is the most common miss.

## What's enforced vs. how you do it

The commit/push hooks enforce *outcomes*, not which agent produced them: a
failing-then-passing test (`red-proof-verify`), clean lint/types on changed lines
(`commit-gauntlet`), and a `code-review` APPROVE artifact from a *separate* reviewer
before push (`reviewer-sep-warn`). So the one load-bearing delegation — the only one a
hook hard-blocks on — is an independent `code-review` before you ship.

That splits the agents by what separation actually buys:

- `code-review` / `security-reviewer` — high value: an independent reader catches what
  the implementer is blind to. It is the review a solo dev otherwise lacks. **Always
  separate, before ship.**
- `planner` / `architect` — real value on hard or uncertain work. Spawn by the rubric above.
- `tdd-red` / `tdd-green` / `tdd-refactor` — low value as a separate agent: the test is
  written first, so there is nothing to leak, and `red-proof-verify` enforces the RED
  discipline from the trailer, not the agent boundary. **Inline is fine.**

**Solo / PR-less work (`solo-cycle`):** write the RED test, GREEN implementation, and any
refactor inline — the hooks still prove the test failed first and the lint/type gates
pass. The one non-negotiable separate agent is `code-review` before each push. Reach for
the `tdd-*` agents only when you want a clean context boundary (larger or collaborative
work).

## Parallel vs sequential

- Independent sub-tasks (no shared data) → run in parallel: e.g. `code-review` +
  `security-reviewer` + `documentation` on one diff.
- One agent's output feeds the next → sequential: `architect` → `planner` →
  `tdd-red` → `tdd-green` → `tdd-refactor`.
- Don't parallelize work whose outputs must be merged back together — the coordination
  cost outweighs the speedup.

## Review depth

Every `code-review` runs two stages, and the first gates the second:

1. **Spec compliance** — does the change do what the plan/issue asked, with no scope
   creep? A correct implementation of the wrong thing fails here.
2. **Code quality** — only once intent is confirmed: correctness, quality, security.

For high-stakes changes (auth, payments, PII, public API, schema migrations, wide blast
radius), split the review across perspectives — `code-review` (correctness), `security-reviewer`
(vulnerabilities), `architect` (coupling/scale) — rather than a single pass. An internal
refactor, docs-only, or test-only change needs just one review.

## Hook signals

The `delegation-gate-warn` hook emits routing hints in tool output. The `code-review` ship
hint is a **hard gate** — resolve it before pushing (`git push`, PR create). The planner
and `tdd-*` hints are advisory nudges: act on them when the rubric agrees, not reflexively.

## When spawning an agent

State the goal in one sentence, hand over the relevant context (paths, diff, constraints),
and name the output format you need back. Don't over-specify method — let the specialist work.
