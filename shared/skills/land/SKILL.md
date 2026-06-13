# Land

Land a finished task from the hub: `/land <id>`. The hub starts and ends tasks; spokes
only execute. A spoke's push is its ship gate — landing (merge, suite, push, teardown,
issue close) happens **here**, on the main checkout, never inside the worktree.

The deterministic sequence lives in `.ai-toolkit/scripts/worktree-land.sh`; this skill orchestrates
it: pick the target, confirm, run, then report and refresh the hub picture.

## Preconditions

- Run from the **hub** (main checkout) on the default branch, with a clean tree —
  the script aborts otherwise, with the precise reason.
- The spoke has pushed (`hub-status.sh` shows the branch `pushed → mergeable`).
- `gh` authenticated for the issue close (degrades to a warning without it).

## Workflow

### 1. Identify the landing target

Resolve `<id>` (issue number, slug, branch, or worktree path) against the live
worktrees — `bash .ai-toolkit/scripts/hub-status.sh` if the state is not already
known. If the branch is `dirty` or `unpushed`, stop: the spoke is still working; landing
verifies pushes, it never rescues them.

### 2. Confirm, then run the landing script

Restate what is about to happen (branch, merge target, teardown) and get a quick yes —
a land is a merge plus an irreversible teardown. Then:

```bash
.ai-toolkit/scripts/worktree-land.sh <id>
```

| Flag | Effect |
|------|--------|
| `--skip-tests` | Land without running the suite on the merged hub |
| `--keep-branch` | Keep the local + remote branch after landing |
| `--test-cmd <cmd>` | Override the suite command (default `pytest -q` when pytest exists) |
| `--local` | Micro-spoke landing: skips upstream guards; accepts a bare local branch with no upstream; refuses any branch that has an upstream and refuses the default branch itself |

The script runs, in order, aborting safely at the first failure:

1. **Guards** — hub on a clean default branch; worktree resolved, clean, fully pushed
   (neither ahead of nor behind its upstream).
2. **Merge** — fast-forward when possible, else a merge commit.
3. **Gate** — full suite on the merged hub; on failure it rolls back with
   `git reset --keep` and nothing is pushed.
4. **Ship** — `git push origin <default>` → `worktree-done.sh` (removes the worktree,
   prunes the merged branch local + origin) → `gh issue close <id>` with a ship comment.
5. **tmux** — kills the task's session-0 window when its pane's directory vanished with
   the worktree; live windows are kept.

### 3. Handle a refused landing

The script's abort message names the failed guard. Typical moves:

| Abort reason | Action |
|--------------|--------|
| Hub dirty / off default branch | Commit or stash, `git checkout <default>`, re-run |
| Worktree has uncommitted changes | Spoke finishes its cycle first — switch to its window |
| Branch never pushed / ahead of upstream | Spoke pushes (`/cycle` PUSH step), re-run |
| Branch behind upstream | Reconcile on the spoke (`git pull`), re-run |
| Merge conflict | Rebase the spoke on the default branch, push, re-run |
| Suite failed (rolled back) | Fix on the spoke, push, re-run — main was restored |

### 4. Report

Relay the script's summary: merged SHA, suite result, what was pruned, issue closed.
Then re-run `hub-status.sh` so the next move starts from a fresh picture.

## Edge cases

| Situation | Action |
|-----------|--------|
| Micro-spoke (lane 1) branch | Review the diff first — lane 1 is non-executable paths only (docs, comments, wording; never `scripts/`, `shared/hooks/`, `tests/`, skill scripts). Then `.ai-toolkit/scripts/worktree-land.sh <branch> --local`: skips upstream guards; no issue to close; the branch is deleted after merge; temp worktree is torn down if still registered |
| Ad-hoc branch (no issue number) | Lands normally; the issue-close step is skipped |
| `gh` missing or close fails | Warns; close by hand: `gh issue close <id>` |
| Push succeeded but teardown failed | Work is shipped; re-run `worktree-done.sh <id>` alone and close the issue by hand |
| Want the branch kept for follow-up | `--keep-branch`, then prune later via `worktree-done.sh` |

## Related skills

- `hub` — survey what is in flight; proposes `/land <id>` for mergeable branches
- `start-task` — the hub-side counterpart that begins a task
- `solo-cycle` — the spoke's per-subtask cycle whose PUSH step makes a branch landable
- `verification-loop` — deeper VERIFY pass; the landing suite gate is its last line
