# Solo Cycle

Per-subtask cycle for solo, PR-less work. One subtask = one anchored, tested,
reviewed, pushed unit of work. There is no PR — the push IS the ship gate; all
enforcement fires on `git commit` and `git push`. The cycle repeats per subtask
within a session.

Ending the task is not the spoke's job: once the final subtask is pushed **and the
ready-to-land marker is emitted**, the **hub** lands it with `/land <id>` (the `land`
skill) — merge, suite, ship, teardown. The marker is what tells the hub the whole
issue is done, not just one subtask (see [The final-push marker](#the-final-push-marker)).

## The gate spectrum

The single human checkpoint used to be `/land` on the hub — *after* a spoke had
fully run and pushed, so a wrong direction was only caught post-hoc and cost a
whole cycle plus a post-fix. Gating moves that checkpoint to its cheapest,
highest-leverage point, but gating *everything* would erase the fire-and-forget
parallelism that makes the hub/spoke model valuable. So the **gate level is
declared per task** by risk/novelty (in the issue and the kickoff — see
`start-task`), not applied uniformly:

| Task type | Gates |
|-----------|-------|
| Very-clear / trivial / mechanical | **none** — fully autonomous to `ready/` |
| **Standard (DEFAULT)** | **PLAN gate** |
| Novel / risky / ambiguous | PLAN + RED gate |
| GUI / behavioral | PLAN + human-acceptance RED + draft-review |

**PLAN is the default for all but very-clear work.** Approving a plan you agree
with costs seconds (even a rubber-stamp); a wrong autonomous dev costs the whole
cycle plus a post-fix — so defaulting non-trivial work to a PLAN gate wins on
expected value even when most plans pass. The no-gate skip-lane is widened
**empirically** over time from the dashboard's observed redirect-vs-rubber-stamp
rate — not guessed up front.

Gates do **not** serialize the queue: a spoke runs **in parallel to its gate**
and **parks** there, so the user reviews the queue in any order (PLAN-gate #A
while #B sits at its own gate) — never one-at-a-time.

### The PLAN gate (default, before RED)

After ANCHOR and before writing any code, a PLAN-gated spoke:

1. **Explores the code** and **writes the complete plan as a normal visible
   message** — files, approach, test strategy, and **open questions**. The plan
   is the message itself, never an empty or abbreviated stub deferred to an
   approval card.
2. **Parks** by emitting the `gate/<issue>` marker (below) and **stops** with an
   explicit "reply to approve, or tell me what to change" — all **before writing
   code**. The hub planned the *what/why* (the issue); the PLAN gate is the *how*
   (it needs the codebase in front of it), so scope is not re-litigated twice.
3. On approval, proceeds into the RED → GREEN → REVIEW → PUSH cycle below.

The git-native `gate/<issue>` park is the **sole** PLAN-gate mechanism — the plan
is a visible message and the tag is the only park, surfaced to the hub exactly
the way `ready/<issue>` surfaces completion. There is no separate approval card,
and no auto-approve shortcut that would disable review at the very moment human
review of the *how* is wanted.

A **very-clear / trivial / mechanical** task declares no gate and runs
**autonomous** straight through to `ready/`, exactly as before.

The other spectrum levels — the **RED gate** (user validates the failing test as
the executable spec), **human-acceptance RED** (a manual checklist for
GUI/behavioral work with no clean automated red), and **draft-review** (park
with a diff + run command after GREEN, before the gauntlet) — are declared in
the same way but their machinery is **pending follow-up issues**; only the PLAN
gate is live in this version.

### The gate marker

A parked gate is surfaced to the hub the same git-native way completion is: the
spoke emits an annotated **`gate/<issue>`** tag whose message names the current
park state (`plan` now; `red` / `draft` reserved for the follow-ups), force-moved
as the spoke advances and dropped once it moves past the gate. This is distinct
from the final-completion marker: **`ready/<issue>`** still means *the whole
issue is done* and is what `/land` consumes — `gate/<issue>` only means *parked,
awaiting review*. Emit it when you park:

```bash
git tag -f -a gate/<issue> -m plan
git push -f origin gate/<issue>
```

## The cycle (per subtask)

| # | Step | Outcome | Enforced by |
|---|------|---------|-------------|
| 1 | ANCHOR | Issue exists; commits reference it | `commit-quality` (blocks unanchored commits) |
| 2 | RED | Failing test committed with `Tested-RED:` trailer | `red-proof-verify` (runs the node; blocks the commit if it passes) |
| 3 | GREEN | Implementation commit; tests pass | `commit-gauntlet` (lint/typecheck on changed lines), `secrets-scan` |
| 4 | REVIEW | APPROVE artifact `.review/<diff-hash>.json` exists | Written by the `code-review` agent on APPROVE |
| 5 | PUSH | `git push` succeeds — work is shipped | `push-scope-guard`, `red-proof-warn`, `reviewer-sep-warn`, `todo-ledger-warn`, `git-push-review` |

Then: next subtask → back to step 1 (or step 2 if it is the same issue).

### 1. ANCHOR

Ensure an issue exists — `gh issue create` for ad-hoc work, or pick an existing
one. Then either:

- Create a branch `feature/<id>-<slug>` (preferred), or
- Stay on `main` and add `-m "Refs #<id>"` to every commit.

`commit-quality` blocks unanchored commits, so anchor before the first commit.

### 2. RED

Write the failing test (inline is fine; use the `tdd-red` agent when you want a
clean context boundary). Commit it with a `Tested-RED: <pytest-node-id>`
trailer. At commit time `red-proof-verify` runs that node and blocks the commit
if it PASSES — a passing test is not driving any new code. The gate is the
proof, not the author.

### 3. GREEN

Write the implementation (inline, or via the `tdd-green` then `tdd-refactor`
agents) and commit it. `commit-gauntlet` lints and typechecks the changed lines;
`secrets-scan` blocks hardcoded credentials.

### 4. REVIEW

Spawn the `code-review` agent on the full diff to be pushed — `git diff
<upstream>..HEAD`, or the full diff if no upstream exists. On APPROVE it writes
`.review/<diff-hash>.json`.

One review per subtask — commit freely within the subtask, review once before
pushing. If REQUEST_CHANGES: fix, re-stage, fresh review. The hash binds to
the exact diff — any new commit invalidates the artifact.

### 5. PUSH

The push is the ship gate. Run it through the one allowlistable process:

```bash
bash .ai-toolkit/scripts/spoke-push.sh            # normal per-subtask push
bash .ai-toolkit/scripts/spoke-push.sh --ready N  # final subtask: push + ready/N marker
```

The script refuses on the default branch, prints diagnostics, then runs the
real `git push -u origin <branch>` — so `red-proof-warn`, `reviewer-sep-warn`,
`git-push-review` and `push-scope-guard` all fire exactly as for a hand-typed
push; on Cursor they hard-block. It does **not** use `--no-verify`. The push
only succeeds when all evidence is in place.

**Never chain the push.** Claude Code's Bash matcher decomposes a compound
command and requires every segment to be separately allowed, so a decorated push
(`git push … | tail`, `git status && … && git push`, `git tag X && git push
origin X`) re-prompts on every ship. Run any diagnostics as their own commands
and let the script own the push + marker — that is the whole reason the seeded
allowlist is `Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)` and not a bare
`git push` rule.

The script pushes only the task's own branch. `push-scope-guard` denies a spoke
push whose refspec touches the default branch or another task's ref: the spoke's
origin branch is ephemeral staging, and `main` is published exclusively from the
hub.

Push **without prompting** the user. An own-branch push is low-stakes and
force-push-recoverable, it targets the spoke's own ephemeral branch (never
`main`), and every hard gate — red-proof, commit-gauntlet, the review artifact —
has already passed before you reach PUSH. There is no human judgment left at
push time; the one human checkpoint is `/land <id>` on the hub. Still ask before
genuinely dangerous or irreversible ops — force-push / `--force-with-lease`,
history rewrites, a push touching the default branch, or deletions outside the
worktree — but never for the routine own-branch push (or the marker below).

## The final-push marker

A per-subtask push looks identical to task completion — clean tree, branch pushed.
The hub can't tell "subtask 1 of 3 shipped" from "issue done" by branch state alone,
so completion must be **signalled explicitly**. On the **FINAL** subtask — and
only then — pass `--ready <issue>` to the push script so the branch push and the
`ready/<issue>` marker emit from the same single allowlistable process (never a
separate `git tag … && git push …` chain, which would re-prompt):

```bash
bash .ai-toolkit/scripts/spoke-push.sh --ready <issue>
```

This tags `ready/<issue>` at the branch tip and pushes it after the branch push.

**Completion is agent-determined, not a human call.** You decide a subtask is the
final one by checking the issue's **acceptance criteria** against your task ledger:
every criterion met (every box tickable) means the whole issue is done. On that final
push, emit the marker **automatically, with no human prompt**; a mid-cycle push emits
no marker. The push-only-vs-push-plus-ready choice is deterministic — "is this the
final subtask?" — so there is nothing for the human to adjudicate. The marker merges
nothing and is trivially reversible (delete/re-tag); the real, gated decision is
`/land <id>` on the hub, which runs the suite on the merged result.

This is the whole-issue ship gate, distinct from the per-subtask push gate. The hub's
`hub-status.sh` flips the branch from `pushed (in progress)` to `pushed → mergeable`
only once the marker points at the tip, and `worktree-land.sh` refuses to land until
then. So:

- Emit the marker **once**, after the last subtask — never after an intermediate one.
- If you push more work after tagging, the marker goes stale (sha ≠ tip); re-tag at the
  new tip (`git tag -f ready/<issue> && git push -f origin ready/<issue>`).
- Ad-hoc/express (lane-2, non-numbered) branches carry no marker — their single push IS
  completion, and the hub lands them with `--force-land`.

The hub consumes the marker on landing (deletes the local + remote tag), so it can't
re-flag a future branch that reuses the issue number.

## The session ledger (Tasks or TodoWrite)

GitHub issues hold the durable contract; your head holds the momentary edit. The
middle layer — *which subtask and which cycle step is live right now* — is what a
dead worktree session forgets, leaving you to reconstruct mid-cycle state by
archaeology. Track it in a task ledger — `TaskCreate`/`TaskUpdate`, or `TodoWrite` on older runtimes — so the cycle is visible and resumable.

**Seed it** before the first commit: one todo per subtask × the cycle steps that
apply — `Subtask 1 · ANCHOR`, `Subtask 1 · RED`, `Subtask 1 · GREEN`,
`Subtask 1 · REVIEW`, `Subtask 1 · PUSH`, then the same for subtask 2 (ANCHOR only
once when subtasks share the issue). Keep exactly one todo `in_progress` — the step
you are on — and flip it to `completed` the moment its gate passes, moving the next
step to `in_progress`.

**Maintain it** as the cycle turns:

- On REQUEST_CHANGES, insert a `Subtask N · Fix review findings` todo before that
  subtask's REVIEW todo, work it, then re-review.
- At PUSH, sync only the *outcome* to the GitHub issue — check the subtask's box,
  leave a one-line note. Never mirror the live todo list into the issue.

The ledger is ephemeral session scratch; the GitHub issue is the durable contract.

## Rules of thumb

- Push once per subtask; the review artifact covers the whole `upstream..HEAD` diff
- Commit granularity within a subtask is free (RED commit + GREEN commit minimum for TDD)
- Any commit after APPROVE invalidates the artifact (hash mismatch) — re-review before pushing
- Non-TDD subtasks (docs, config, chores) need no `Tested-RED` trailer —
  `red-proof-warn` only gates source-adding commits — but anchor and review still apply
- The task ledger is ephemeral session scratch; the GitHub issue is the durable
  contract — sync only outcomes at PUSH, and skip the ledger entirely for single-step
  work (one tiny subtask, a docs/config one-liner), where it is pure overhead
- `todo-ledger-warn` enforces the ledger at PUSH by scanning the session transcript for
  a ledger call — `TodoWrite`, `TaskCreate`, or `TaskUpdate` (warn on Claude, hard-deny on Cursor). For genuinely single-step
  work, add a `No-Ledger: <reason>` trailer to a commit in the pushed range to bypass it

## Edge cases

| Situation | Action |
|-----------|--------|
| Push blocked by `reviewer-sep-warn` | Run the `code-review` agent, get APPROVE |
| Push blocked by `red-proof-warn` | A source-adding commit is missing its trailer — amend/reword it, or add the failing-test commit |
| Push blocked by `todo-ledger-warn` | Seed a task ledger (`TaskCreate` or `TodoWrite`) this session, or add a `No-Ledger: <reason>` trailer for single-step work |
| Working on `main` with no issue branch | Add `Refs #<id>` to every commit message |
| Review says REQUEST_CHANGES | Fix and re-review — never bypass |
| Tempted to use `--no-verify` | Blocked by `block-no-verify`, by design |

## Related skills

- `source-task` — anchor: fetch the issue and confirm the branch
- `tdd-workflow` — RED/GREEN/REFACTOR guidance
- `land` — hub-side `/land <id>` that ends the task once the last subtask is pushed
- `verification-loop` — deeper VERIFY pass before the review
- `git-commit` — commit message format
