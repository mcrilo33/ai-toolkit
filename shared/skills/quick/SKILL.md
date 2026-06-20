# Quick

The **express-interactive** lane: build a small fix *conversationally from the hub
session* on its own branch/worktree, keeping the push-time quality gates
(lint/typecheck/tests) but dropping the spoke **process** ceremony — no issue, no
`source-task` kickoff, no separate tmux session, no PLAN gate, no RED-before-green,
no review-artifact requirement.

Drop **process**, keep **quality**. `main` stays the clean launch/integration point:
quick work happens on a branch/worktree, never by dirtying `main` directly.

## When to use it

- A one-off small fix you want to drive interactively from where you are.
- The change is obvious and low-risk; full TDD/PLAN-gate ceremony is overkill.

Reach for `/cycle` (solo-cycle) instead when the work is novel, risky, multi-subtask,
or when the "why" should be findable later from an issue.

## Workflow

### 1. Create the worktree

From the hub (the main checkout), run:

```bash
scripts/worktree-quick.sh <slug>          # branch quick/<slug>
scripts/worktree-quick.sh <slug> -t chore # branch chore/<slug>
```

This creates a worktree + branch, copies the gitignored `.claude/` runtime config,
mints the `spoke_run_id`, sets the `.ai-toolkit/` exclude, and drops the
`hub-guard-allow` marker in the git-dir so the hub session may commit into the
worktree. It does **not** create an issue, seed a prompt, spawn a tmux window, or
launch a separate agent. It prints the worktree path on the final line.

### 2. Enter the worktree

`cd` into the printed path. **This same session** does the work — no new agent:

```bash
cd <printed-worktree-path>
```

### 3. Iterate conversationally

Edit, run lint/typecheck/tests, and commit as you go. The quality gates still fire:

- `commit-gauntlet` lints/typechecks staged changes; `secrets-scan` blocks credentials.
- The pre-push test gate runs on push.

**Commit anchoring.** A `quick/<slug>` branch carries no issue number, so the
`commit-quality` anchor gate applies as it does for any express lane:

- Non-executable docs/wording → a `docs:`/`chore:` commit whose entire staged set is
  documentation needs no anchor.
- Executable code → add a message anchor referencing the relevant issue, e.g. a
  second `-m "Refs #<id>"`.

What is **dropped** vs `/cycle`: no `source-task`, no RED-before-green, no PLAN gate,
and no review-artifact requirement (`reviewer-sep-warn` is advisory on Claude).

### 4. Land via the express path

From the **hub** (not the worktree):

```bash
# unpushed local branch (never sent to origin):
scripts/worktree-land.sh <slug> --local

# or, if you pushed the branch first:
scripts/worktree-land.sh <slug>
```

A non-numbered `quick/`/`chore/` branch carries no `ready/<issue>` marker, so it needs
neither the marker nor `--force-land`. Landing merges into the default branch, runs the
pre-push test gate, pushes, and tears the worktree down — which **revokes** the
`hub-guard-allow` marker.

## Notes / gotchas

- The `hub-guard-allow` marker bypasses **all** hub-guard checks while present (incl. on
  `main`) — it is the conscious escape hatch. The lane itself always works on a
  branch/worktree, never by editing `main` directly.
- The marker is a single global toggle: if two `/quick` lanes run at once, tearing one
  down revokes the bypass for the other until re-granted. Quick lanes are short
  interactive one-offs, so this is rarely an issue — re-run `worktree-quick.sh` or
  re-create the marker if a concurrent lane loses its bypass.
- This touches `scripts/` + `shared/hooks/`; after landing a change to the lane itself,
  re-sync the hub (`sync-to-repo.sh . claude`) so new spokes inherit it.

## Related skills

- `solo-cycle` — the full per-subtask RED / GREEN / REVIEW / PUSH cycle (Lane 3).
- `land` — the hub-side land that this lane reuses (`--local` for unpushed branches).
- `source-task` — anchors a numbered task to its issue (skipped by this lane).
