---
description: "What a dispatchable issue must contain so `dispatch.sh --next` can pick and order it — a mandatory Scope: line of touched files/globs (missing or '*' means exclusive) plus a Gate: plan line (none = the user-requested light lane), with genuine ordering expressed as native GitHub blocked-by dependencies rather than file overlap. Programmatic filers emit the footer too. Surfaced on demand when authoring issues, not auto-applied to every session."
---
# Issue Hygiene

Defines what a **dispatchable issue** must contain so it can be scheduled automatically.
The coordinator (`coordinator.sh`, see `afk`) drains the backlog as fast as the dependency graph allows, but
`dispatch.sh --next` is only as good as the metadata on each issue: it runs issues in parallel when they
touch disjoint files and holds issues that genuinely depend on one another. Both decisions are
read off the issue body, so **every dispatchable issue must declare what it touches, how it
gates, and what it depends on.** An issue missing this metadata still runs — just on the
slow path.

Concretely, every filed issue carries two machine-read footer lines — **`Scope:`** and
**`Gate:`** — plus a native blocked-by edge when it genuinely depends on open work. They
are plain `Key: value` body lines, not `##` markdown headers: a scripted dispatcher reads
them, and a `## Scope` prose header is invisible to it (exactly the gap that silently
serialized two batches of agent-authored issues).

## The `Scope:` line

Every dispatchable issue carries a **`Scope:`** line in its body — a space- or
comma-separated list of the files and globs the task is expected to touch:

```
Scope: scripts/foo.sh tests/test_foo.py
```

The dispatcher reads `Scope:` to compute the file-overlap matrix against the in-flight issues; it
decides what runs in parallel and what waits. Two issues with disjoint scopes can run at the same
time; two issues that touch the same file are serialized so their worktrees never collide.
Keep the scope tight and honest — list the files you actually expect to edit, not the
whole subsystem. An over-broad scope needlessly serializes work that could have run in
parallel.

## Unscoped means exclusive

A `Scope: *` line — or a **missing** `Scope:` line — marks the issue as **exclusive**:
the dispatcher cannot prove it is disjoint from anything else, so it runs the issue **alone,
never batched** with another task. This is the deliberate slow path. It is always safe,
but it forfeits parallelism, so reach for it only when the task really is repo-wide (a
sweeping refactor, a dependency bump) or when the touched files genuinely cannot be
predicted. Prefer a concrete file list whenever you can write one.

## The `Gate:` line

Every dispatchable issue also carries a **`Gate:`** line recording where the spoke pauses
for human review:

```
Gate: plan
```

`Gate: plan` (the full cycle, and what an **omitted** line means) makes the spoke send its plan as an
Orca question and wait for `approve` before any code is written. `Gate: none` is the **light lane**: no
PLAN gate and no in-spoke review, same dispatch and same `land.sh`. Only the user chooses it, for that issue,
in the conversation; no filer, agent, template or `answer.sh` ever writes or applies it by default. The line
records *which* gate, not *who* answers it: `coordinator.sh --answer auto` (`answer.sh`) or
`--answer human`, chosen per run.

## Programmatic filers emit the footer too

The convention binds **any** path that files issues, not just a human typing them into the
`start-task` flow. Audits, sweeps, and workflows that create issues **programmatically**
must emit the `Scope:` + `Gate:` footer on every issue they open. The two silent
serializations this rule exists to prevent both came from bulk-filed issues that shipped
without a machine-readable `Scope:` line — a missing line fails closed to exclusive, which
is safe but forfeits all parallelism until a human notices. `dispatch.sh --next` logs each
ready scope-less issue by number so the next such gap is diagnosable from its output
rather than surfacing only as a mysteriously serial drain.

## Ordering is a dependency, not overlap

The two kinds of relationship between issues are distinct, and they are recorded
differently:

- **Genuine ordering** — "this work is *based on* that work" — is expressed as a native
  GitHub **blocked-by** dependency. Set it at issue creation (see
  `github-issues/references/dependencies.md`). The dispatcher holds a blocked issue until its
  blockers close.
- **File overlap** is **not** a dependency. It is a scheduling concern the dispatcher
  *derives* from `Scope:`. Never encode "these touch the same file" as a blocked-by edge —
  that conflates serialization (which the scheduler handles) with ordering (which it must
  be told). Two issues can overlap in scope without either depending on the other; the
  the dispatcher simply runs them one after the other in either order.

State ordering only when it is real. A spurious blocked-by edge stalls work that could
have proceeded; a missing one lets a spoke start against an unfinished prerequisite.

## Colliding scopes: merge toward one spoke

When several ready issues declare overlapping `Scope:` lines, the dispatcher can only
serialize them — splitting bought no parallelism, and each issue still pays full
spoke overhead (spawn, cold context read, PLAN gate, independent review, CI, land).
Batch such a cluster into ONE umbrella issue with ordered subtasks instead, within
a single spoke's budget (~3–5 subtasks, a couple of hours, comfortable context).
A larger cluster becomes 2–3 sequential umbrellas chained by real blocked-by.

Guardrails:

- **Priority escapes the chain.** A priority item gets its own small issue at the
  head of the cluster so it lands immediately; the umbrella follows it.
- **Keep the specs.** Close the merged originals with a pointer to the umbrella;
  the umbrella references them per subtask (`gh issue view <n>` reads closed issues).
- **Recurring collisions are an architecture signal.** When clusters keep forming
  on the same file, file a decoupling refactor for that hotspot (blocked by the
  umbrella) so future work touches disjoint files — converting chains into real
  parallelism instead of managing them.

## Declared as a checked step at creation

Answering "does this depend on open work?" is a **checked step** of issue creation — the
same status as `Scope:`, not an afterthought. Before filing, state the answer explicitly:
either record the real blocked-by edge (see
`github-issues/references/dependencies.md`), or note that the issue has no open
dependency. The `start-task` skill carries this as a required question. Declaring is
mandatory; only the *answer* varies — and, per the caveat above, you declare the
**genuine** edges, never fabricate one to serialize or scope a run.

## What the dispatcher guarantees

- **Ordering** — the dispatcher never dispatches an issue while any of its blockers is still
  open; a blocker *closing* is what releases the dependent into the next batch.
- **Concurrency** — independent issues with disjoint `Scope:` still batch and run at the
  same time. A declared edge serializes *only* the pair it names.
- **Lint** — `dispatch.sh --next` prints a stderr-only `possible undeclared dependency`
  warning when two open issues share a not-yet-created scope path with no edge between
  them, so a likely-missing edge surfaces at plan time. Detection-only: it never changes
  the batch, the exit code, or which issues dispatch.

## Related

- `workflow` rule — the task lifecycle these issues feed into
- `planning-hub` rule — the hub authors issues and dispatches them as spokes
- `start-task` skill — creates the issue, requires `Scope:`, and wires blocked-by
- `github-issues` skill — issue templates (including the `Scope:` + `Gate:` footer) and
  the `dependencies.md` reference for setting blocked-by
