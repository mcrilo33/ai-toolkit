# Issue Hygiene

Defines what a **dispatchable issue** must contain so it can be scheduled automatically.
The `/afk` supervisor drains the backlog as fast as the dependency graph allows, but the
scheduler is only as good as the metadata on each issue: it batches issues that touch
disjoint files and orders issues that genuinely depend on one another. Both decisions are
read off the issue body, so **every dispatchable issue must declare what it touches and
what it depends on.** An issue missing this metadata still runs — just on the slow path.

## The `Scope:` line

Every dispatchable issue carries a **`Scope:`** line in its body — a space- or
comma-separated list of the files and globs the task is expected to touch:

```
Scope: shared/hooks/foo.sh tests/unit/test_foo.py
```

The planner reads `Scope:` to compute the file-overlap matrix that drives its
PARALLEL / SERIAL batching plan. Two issues with disjoint scopes can run at the same
time; two issues that touch the same file are serialized so their worktrees never collide.
Keep the scope tight and honest — list the files you actually expect to edit, not the
whole subsystem. An over-broad scope needlessly serializes work that could have run in
parallel.

## Unscoped means exclusive

A `Scope: *` line — or a **missing** `Scope:` line — marks the issue as **exclusive**:
the planner cannot prove it is disjoint from anything else, so it runs the issue **alone,
never batched** with another task. This is the deliberate slow path. It is always safe,
but it forfeits parallelism, so reach for it only when the task really is repo-wide (a
sweeping refactor, a dependency bump) or when the touched files genuinely cannot be
predicted. Prefer a concrete file list whenever you can write one.

## Ordering is a dependency, not overlap

The two kinds of relationship between issues are distinct, and they are recorded
differently:

- **Genuine ordering** — "this work is *based on* that work" — is expressed as a native
  GitHub **blocked-by** dependency. Set it at issue creation (see
  `github-issues/references/dependencies.md`). The planner holds a blocked issue until its
  blockers close.
- **File overlap** is **not** a dependency. It is a scheduling concern the planner
  *derives* from `Scope:`. Never encode "these touch the same file" as a blocked-by edge —
  that conflates serialization (which the scheduler handles) with ordering (which it must
  be told). Two issues can overlap in scope without either depending on the other; the
  planner simply runs them one after the other in either order.

State ordering only when it is real. A spurious blocked-by edge stalls work that could
have proceeded; a missing one lets a spoke start against an unfinished prerequisite.

## Related

- `workflow` rule — the task lifecycle these issues feed into
- `planning-hub` rule — the hub authors issues and dispatches them as spokes
- `start-task` skill — creates the issue, requires `Scope:`, and wires blocked-by
- `github-issues` skill — issue templates (including the `Scope:` line) and the
  `dependencies.md` reference for setting blocked-by
