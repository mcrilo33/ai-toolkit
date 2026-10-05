---
description: "When you discover a confirmed defect during any work, decide from evidence whether fixing it saves time; if so, file it via the bug-scoper agent immediately without asking — the agent verifies evidence against the code and dedups. A rare, theoretical or cosmetic finding is dropped with a one-line reason, not filed. Only surface genuine is-this-intended-design uncertainty. ai-toolkit tooling defects go to UPSTREAM_REPO (when set) even from a downstream project, never the host repo. Surfaced on demand, not auto-applied to every session."
---
# Bug Triage

When you discover a **confirmed defect** during any work — implementing, reviewing,
smoke-testing, answering a question — and it is worth filing (see "Worth filing?"),
file it via the **`bug-scoper` agent immediately, without asking permission**. "Want me to file that?" for a real bug is
the anti-pattern: the answer is yes, and the question just loses the finding
to the transcript.

This binds every agent, not just the hub: a spoke that trips over an unrelated bug
mid-cycle files it (if worth filing) and keeps going, rather than burying it in its own run.

"Without asking" applies only to a finding that is worth filing (next section): the
human is never asked, and a finding that is not worth an issue is dropped, not filed.

## Worth filing?

A real finding is not enough: every filed issue costs a worker cycle, a review and a
land, and its review produces more findings. Before filing, answer from evidence, not
from imagination:

1. Has it happened, or how would it happen in normal use?
2. How often?
3. What does one occurrence cost, against the cost of the fix (a worker cycle, a
   review, a land, more code)?

File only if the fix saves time: a bug that has happened or will often enough to cost
more than its fix, an enhancement that really improves productivity. **Dropped by
default:**

- a failure that needs a worker acting against its coordinator;
- two independent failures coinciding, or a race needing a commit in the instant
  between a check and a merge;
- a cosmetic or wording nit;
- a test tidy-up;
- a refactor with no behaviour change;
- a hardening against something never observed.

Dropping is not silent: report the finding in one line with the reason ("dropped:
rare", "dropped: no time saved") in `worker_done` or the land report, and file
nothing. A dropped finding that later happens is filed then.

## Confirmed defect vs open question

The rule is *file worthwhile confirmed bugs without asking* — **not** *auto-file every
observation*. Draw the line by what you can prove:

- **Confirmed defect** — you can point at the code and the wrong behavior (a repro, a
  failing invariant, a contract the code contradicts). → Dispatch `bug-scoper` now.
- **Genuine "is this a bug or the intended design?"** — the behavior might be
  deliberate, a known trade-off, or the user's product choice. → Surface it in prose
  and let the human decide. That judgement is not the agent's to make.

When unsure which side a finding falls on, a check against the code usually
settles it. Bias toward filing once it's confirmed and worth filing; bias toward asking only while
intent is genuinely ambiguous.

## Why routing through the agent is safe

`bug-scoper` is self-protecting, which keeps filing a worthwhile finding from being a
noise risk:

- It applies the "Worth filing?" bar and **drops** what does not meet it.
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

- A defect in the **ai-toolkit tooling** — a synced rule, skill, agent, hook, script, `CLAUDE.md`, or
  `orca.yaml` (anything under `.claude/` or `.ai-toolkit/` in a host project, `shared/` in the toolkit
  repo) — is filed to `UPSTREAM_REPO` (`owner/repo`; `sync.sh` fills it from the toolkit's `origin`, and
  `.ai-toolkit/ai-toolkit.local.env` overrides it), even when discovered inside a host project. Otherwise
  the toolkit's own bugs scatter across downstream trackers and never reach its maintainer.
- **Empty `UPSTREAM_REPO` fails closed.** It means "file here" only in the toolkit's own checkout, which
  is the one holding `shared/`, `hooks/claude/` and `scripts/sync.sh` at its root (the test `land.sh` uses). Anywhere
  else it is a host project with no upstream configured: the scoper returns the draft, says plainly that
  no upstream is set and that `UPSTREAM_REPO` goes in `.ai-toolkit/ai-toolkit.local.env` (or re-run
  `sync.sh` from a toolkit checkout with an `origin`), and never files to the host repo.
- **Synced copies are not edited in a host project.** There, the toolkit's files are synced copies; a
  defect or wish about one is filed upstream through the scoper, and the project picks the fix up at its
  next sync.
- A defect in the **host project's own code** is filed to the **host project's** repo, as normal.

The `bug-scoper` agent reads `UPSTREAM_REPO` and classifies host-vs-tooling by path, rather than
defaulting to the current project's git remote or naming a bare literal (see its Phase 5). A fork or
rename reroutes tooling defects by changing `UPSTREAM_REPO`, not agent prose.

## Deferred follow-ups: file the worthwhile ones, don't lose them

A **grounded, deliberately-deferred follow-up that meets the bar in "Worth filing?"** — an optimization, cleanup, or hardening
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

A worker that defers an item it does not file itself puts it in `worker_done` on its own
line, `DEFERRED: <item>`, one item per line. After the land only those lines are routed;
an unmarked mention is not routed, and an item already filed is never marked. A worker
marks `DEFERRED:` only what meets the bar in "Worth filing?"; the rest is a one-line
"dropped: <reason>".

**Best-effort, fail loud (AFK principles #2, #6):** dispatching `followup-scoper` must
**never fail the caller's cycle** — it rides alongside the work. But a follow-up it cannot
file is reported **loudly**, with the follow-up text preserved, never silently dropped: a
lost follow-up is a visible failure, not a no-op.

## What happens to the filed issue

The filing agent decides the issue's dispatch outcome, and this is the only place it is stated:

- **`bug-scoper`** files an ordinary dispatchable issue. A confirmed bug may be dispatched automatically,
  except one whose fix touches the coordinator's own scripts: those carry `hold` and land attended.
- **`followup-scoper`** files the issue with the `hold` label, which keeps it out of dispatch until
  the human removes the label. A follow-up waits for the human's go.

Both agents file with the `gh` CLI when no GitHub MCP is available. When neither works they
draft instead, and report it loudly with the finding text preserved. A follow-up is never filed
without `hold`: if the label is missing from the repo, create it first.

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
