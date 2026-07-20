# Bug Triage

When you discover a **confirmed defect** during any work — implementing, reviewing,
smoke-testing, answering a question — file it via the **`bug-scoper` agent
immediately, without asking permission**. "Want me to file that?" for a real bug is
the anti-pattern: the answer is always yes, and the question just loses the finding
to the transcript.

This binds every agent, not just the hub: a spoke that trips over an unrelated bug
mid-cycle files it and keeps going, rather than burying it in its own run.

## Confirmed defect vs open question

The rule is *file confirmed bugs without asking* — **not** *auto-file every
observation*. Draw the line by what you can prove:

- **Confirmed defect** — you can point at the code and the wrong behavior (a repro, a
  failing invariant, a contract the code contradicts). → Dispatch `bug-scoper` now.
- **Genuine "is this a bug or the intended design?"** — the behavior might be
  deliberate, a known trade-off, or the user's product choice. → Surface it in prose
  and let the human decide. That judgement is not the agent's to make.

When unsure which side a finding falls on, a quick check against the code usually
settles it. Bias toward filing once it's confirmed; bias toward asking only while
intent is genuinely ambiguous.

## Why routing through the agent is safe

`bug-scoper` is self-protecting, which is what makes "file by default" the right
default rather than a noise risk:

- It **verifies the evidence against the code** before filing, so a claim that
  doesn't hold up is dropped, not filed.
- It **dedups against open issues**, so a rediscovery becomes a comment, not a
  duplicate.
- It **derives the real `Scope:`/`Gate:` footer** (see the `issue-hygiene` rule) and
  applies labels, so the filed issue is dispatchable, not a slow-path stub.

Hand it the evidence you already have — `file:line`, the repro or failing invariant,
a fix direction, and any label/scope hints — so it verifies fast.

## Filing upstream from a downstream project

ai-toolkit is synced *into* other projects, so where a bug is filed depends on **whose
code is broken**, not on which repo you happen to be sitting in:

- A defect in the **ai-toolkit tooling** — a synced rule, skill, hook, script, agent,
  or the telemetry code (files matching the configured `issue_routing.tooling_paths`
  manifest: `shared/`, `.claude/`, `.ai-toolkit/`, `scripts/telemetry/`, …) — is filed to
  the **configured ai-toolkit upstream** (`issue_routing.upstream_repo` in
  `settings/ai-toolkit.yml`, default `mcrilo33/ai-toolkit`), even when discovered inside a
  host project. Otherwise the toolkit's own bugs scatter across downstream trackers and
  never reach its maintainer.
- A defect in the **host project's own code** is filed to the **host project's** repo,
  as normal.

The `bug-scoper` agent resolves the upstream repo from the configured
`issue_routing.upstream_repo` and classifies host-vs-tooling against `tooling_paths`,
rather than defaulting to the current project's git remote or naming a bare literal — see
its Phase 5. A fork/rename reroutes tooling defects by changing that config
(`issue_routing.upstream_repo` or `git config ai-toolkit.upstream-repo`), not agent prose.

## Deferred follow-ups: file them, don't lose them

A **grounded, deliberately-deferred follow-up** — an optimization, cleanup, or hardening
you consciously chose to leave out of the current issue, backed by evidence (a `file:line`,
a profile number, a measured cost) and a concrete fix direction — is filed via the
**`followup-scoper` agent immediately, without asking**, exactly as a confirmed bug is
routed to `bug-scoper`. This imperative **binds spokes too**: a drain-spoke that defers a
follow-up files it the moment it is made, then keeps going — rather than writing it into a
transcript that is torn down with the worktree and read by no one.

`followup-scoper` is self-protecting the same way `bug-scoper` is: it verifies the
follow-up is grounded (a vague "would be nice" is **dropped, not filed**), dedups against
open issues, derives the real `Scope:`/`Gate:` footer, applies the `enhancement` label,
and routes tooling follow-ups upstream. A follow-up that is really the parent issue's own
deferred scope is surfaced as a comment / `UPGRADE` marker, **not** a new issue.

**Best-effort, fail loud (AFK principles #2, #6):** dispatching `followup-scoper` must
**never fail the caller's cycle** — it rides alongside the work. But a follow-up it cannot
file is reported **loudly**, with the follow-up text preserved, never silently dropped: a
lost follow-up is a visible failure, not a no-op.

## What this does not cover

- **Speculative ideas and design musings** — a preference with no measured cost, an
  unfounded "would be nice", a "we might one day want" with no evidence — are not grounded
  follow-ups; raise them conversationally rather than filing. (A *grounded, deliberately-
  deferred* follow-up instead routes to `followup-scoper` above.)
- A bug in the user's **uncommitted work-in-progress** they are actively editing —
  mention it in the moment instead of filing; it may vanish on their next save.

## Related

- `github-issues` skill — the `bug-scoper` and `followup-scoper` agents reuse its filing mechanics
- `issue-hygiene` rule — the `Scope:`/`Gate:` footer the agent emits on every issue
- `planning-hub` rule — the hub authors and dispatches; this keeps discovered defects
  from being lost between those steps
