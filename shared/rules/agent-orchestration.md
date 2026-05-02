# Agent Orchestration

## When to Spawn Agents

Don't do everything yourself. Delegate to specialist agents when the task matches their expertise.

### Routing Table

| Situation | Agent | Trigger signals |
| --------- | ----- | --------------- |
| Complex multi-file feature | `planner` | "plan how to", "break this down", 3+ files, DEFINE phase |
| System design or structure decision | `architect` | "design the architecture", "how should we structure", new service/module, data model |
| Security audit or vulnerability check | `security-reviewer` | "audit security", "is this safe?", auth/payments/PII changes |
| PR or diff review requested | `code-review` | "review this", PR link, "check my changes" |
| Bug report, error, stack trace | `debug` | "it's broken", stack trace, `FAILED`, unexpected behavior |
| New feature — write tests first | `tdd-red` | TDD mode, "write tests for X", DEFINE phase |
| Make failing tests pass | `tdd-green` | "make tests pass", GREEN phase, failing test output |
| Tests pass — clean up | `tdd-refactor` | "refactor", REFACTOR phase, all tests green |
| Cross-cutting rename/restructure | `refactor` | "rename X to Y across the codebase", pattern migration |
| CI/CD, infra, deployment | `devops` | workflow files, Docker, deploy, pipeline |
| Write or update docs only | `documentation` | "document this", "update the README", docs-only task |

### When NOT to Spawn

- Task is a single file with clear scope — do it yourself
- User explicitly says "don't use agents" or "do it inline"
- The overhead of delegation exceeds the task complexity

## Parallel Execution

When a task decomposes into **independent** sub-tasks, run agents in parallel:

```text
# Good — independent tasks, run simultaneously
├── Agent 1: code-review (reviews the diff)
├── Agent 2: security-reviewer (audits for vulnerabilities)
├── Agent 3: documentation (updates docs for the change)

# Good — multi-perspective review in parallel
├── Agent 1: code-review (correctness + quality)
├── Agent 2: security-reviewer (vulnerabilities + data protection)

# Good — plan first, then build
├── Agent 1: planner (decompose the feature)      ← sequential
└── Then: tdd-red → tdd-green → tdd-refactor       ← sequential per step

# Bad — dependent tasks, must be sequential
├── Agent 1: architect (design the structure)  ← must finish first
└── Agent 2: planner (plan the implementation) ← depends on architecture
```

**Rules:**

- If sub-tasks share no data dependencies → parallel
- If one agent's output feeds another → sequential
- Never spawn more than 3 agents in parallel — diminishing returns

## Multi-Perspective Review

For high-stakes changes (security-sensitive, public API, data model, auth), split the review into perspectives:

| Perspective | Focus | Agent |
| ----------- | ----- | ----- |
| Correctness | Logic, edge cases, regressions | `code-review` |
| Security | Vulnerabilities, input validation, secrets | `security-reviewer` |
| Architecture | Coupling, scalability, design patterns | `architect` |

**When to use multi-perspective:**

- Changes touching auth, payments, or PII
- Public API surface changes
- Database schema migrations
- Changes spanning 5+ files

**When single review is enough:**

- Internal refactor with no behavior change
- Documentation-only changes
- Test additions

## Delegation Protocol

When spawning an agent:

1. **State the goal** — one sentence on what you need back
2. **Provide context** — relevant file paths, diff, constraints
3. **Specify output format** — "return a list of findings", "return the commit message"
4. **Don't over-specify** — let the specialist agent use its own methodology
