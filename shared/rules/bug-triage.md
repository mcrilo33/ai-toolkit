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

## What this does not cover

- **Enhancements and design ideas** are not bugs; raise them conversationally or file
  them yourself with the standard footer.
- A bug in the user's **uncommitted work-in-progress** they are actively editing —
  mention it in the moment instead of filing; it may vanish on their next save.

## Related

- `github-issues` skill — the `bug-scoper` agent reuses its filing mechanics
- `issue-hygiene` rule — the `Scope:`/`Gate:` footer the agent emits on every issue
- `planning-hub` rule — the hub authors and dispatches; this keeps discovered defects
  from being lost between those steps
