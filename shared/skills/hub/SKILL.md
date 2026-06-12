# Hub

Orient a fresh **planning hub** session and dispatch from it. This is the entry point for
a main-checkout session in the parallel-worktrees workflow: it shows what is in flight,
then proposes the next move. Read the `planning-hub` rule for the role and
`docs/parallel-worktrees.md` for the full model.

Use it when you sit down at the hub — the user says `/hub`, "what's in flight", "hub
status", or starts a fresh main-checkout session and wants to get oriented.

## Preconditions

- Run from the **main checkout** (the hub), which stays on `main`.
- `gh` authenticated (issue survey degrades gracefully without it).
- The worktree scripts exist on `main` (`scripts/worktree-new.sh`, `worktree-done.sh`).

## Workflow

### 1. Confirm you are on the hub

You are the hub only on the main checkout, on the default branch. If `git branch --show-current`
is a task branch or you are inside a worktree, you are a **spoke** — stop and follow
`source-task` / `solo-cycle` instead. The hub never writes task code (see `planning-hub`).

### 2. Survey what is in flight

Run the dashboard:

```bash
bash shared/skills/hub/scripts/hub-status.sh
```

It reports, read-only:

- **Worktrees** — each task branch with ahead/behind vs the default branch, its state
  (`dirty`, `unpushed`, or `pushed → mergeable`), its issue (`#N OPEN`, `#N ?` when `gh`
  is unreachable), and its live tmux pane (`tmux <session>:<window>`, matched across
  **all** sessions by pane path, or `no pane`). Rows with a pane include a copy-paste
  `↳ jump:` command (`select-window` / `switch-client` / `attach`, picked for where you
  are). A `↳ todos:` sub-line shows the spoke's TodoWrite ledger from its latest Claude
  session (`<done>/<total> · in_progress: <item>`); `todos: none` means the spoke never
  seeded a ledger — that absence is signal, since kickoffs mandate one.
- **Open issues** — flagged `worktree active` or `no worktree` so you can see what is
  unstarted.

### 3. Propose the next move — act only on confirmation

From the dashboard, surface concrete next steps and wait for the user's OK before doing
anything that changes state:

| Dashboard signal | Proposed action | How |
|------------------|-----------------|-----|
| Open issue, `no worktree` | Start it | `start-task` skill (creates issue if needed + spawns spoke) |
| New idea, no issue yet | Define then dispatch | discuss scope → `start-task` |
| Branch `pushed → mergeable` | Land and tear down | `/land <id>` (`land-task` skill → `scripts/worktree-land.sh`) |
| Branch `unpushed` / `dirty` | Leave it — spoke still working | paste the row's `↳ jump:` command to reach its pane |

Never auto-merge or auto-teardown. Restate the branch/issue and the exact command, get a
quick yes, then run it. Merges and teardowns happen **on the hub**; task edits never do.

### 4. Report

Give the user a short read: how many spokes are running, which issues are unstarted, which
branches are ready to merge, and your single recommended next action.

## Rules of thumb

- One survey per sit-down — re-run after a merge or a dispatch to refresh the picture.
- Keep the hub on `main`. If a survey shows the hub checkout dirty or off `main`, flag it.
- The issue is the contract — dispatch with a kickoff that lets the spoke run on its own
  (`/source` then `/cycle`).
