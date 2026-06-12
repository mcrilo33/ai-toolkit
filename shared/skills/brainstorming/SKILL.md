# Brainstorming

Refine a rough idea into an agreed spec **before** any code is written. This implements the
DEFINE step for new work — replacing "jump straight to code" with Socratic design refinement
and an explicit sign-off.

## When to Use

- The user describes a feature, change, or goal that is not yet a precise spec
- "I want to build...", "Can we add...", "I'm thinking about...", "How should we do..."
- The request is ambiguous, open-ended, or has multiple viable approaches
- Automatically at the start of DEFINE, before `context-map` or `planner`

**Do NOT use when:**
- The task is a single-file, unambiguous change (proceed directly)
- The user explicitly says "just do it", "skip planning", or provides a complete spec
- A bug with a clear reproduction (use `debug`)

## Principles

- **Questions before answers.** Surface intent, constraints, and success criteria before proposing a design.
- **One topic at a time.** Don't dump twenty questions — ask the few that unblock the next decision.
- **Present in digestible chunks.** Show the design in sections short enough to actually read and approve, not one wall of text.
- **Explore alternatives.** Offer 2–3 approaches with trade-offs; recommend one. Never pick silently.
- **No code yet.** The output is an agreed spec, not an implementation.

## Workflow

### Step 1: Clarify Intent

Ask only the questions needed to understand the goal:

- What problem does this solve? Who is it for?
- What does "done" look like — observable behavior, not implementation?
- What is explicitly out of scope?
- What constraints exist (existing code, deadlines, dependencies, performance)?

If the answers are already clear from context, skip ahead — don't ask for the sake of asking.

### Step 2: Explore Approaches

Present 2–3 viable approaches:

```markdown
### Option A: <name>
- How it works: <one or two lines>
- Trade-offs: <pros / cons>

### Option B: <name>
- ...

**Recommendation:** Option <X>, because <reason>.
```

### Step 3: Draft the Spec in Sections

Present the design one section at a time and get sign-off on each before moving on:

```markdown
## Spec: <feature>

### Goal
<one paragraph — what and why>

### Scope
- In: <what's included>
- Out: <what's deliberately excluded>

### Behavior / Acceptance Criteria
- [ ] <observable behavior 1>
- [ ] <observable behavior 2>

### Approach
<chosen option and key design decisions>

### Open Questions
- <anything still undecided>
```

### Step 4: Confirm and Save

- Get explicit sign-off ("does this match what you want?").
- Save the agreed spec to a design doc (e.g. `docs/design/<feature>.md`) when the work is non-trivial or spans multiple sessions.
- Hand off to `context-map` (impact analysis) or `planner` (decomposition) for EXECUTE.

## Handoff

| Next step | Skill / Agent | When |
|-----------|---------------|------|
| Impact analysis | `context-map` | Spec touches existing code |
| Task decomposition | `planner` | path unclear or change crosses boundaries |
| Tests first | `tdd-workflow` | TDD approach chosen |

## Checklist

```
- [ ] Intent and "done" criteria clarified
- [ ] Out-of-scope items named
- [ ] Alternatives explored with a recommendation
- [ ] Spec presented in digestible sections
- [ ] Explicit sign-off obtained
- [ ] Design doc saved (if non-trivial)
- [ ] Handed off to context-map / planner / tdd-workflow
```
