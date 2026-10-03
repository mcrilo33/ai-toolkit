---
name: land
description: "Land a finished task from the hub: independent review, green CI on the exact tip, merge into the base branch, close the issue, release the worker, and remove the worktree. Use on the hub when the user says /land <id>, 'land it', or a spoke sent worker_done succeeded."
argument-hint: "[issue number]"
---
# Land

`.ai-toolkit/scripts/land.sh <n>` runs the whole landing, in order; each step must pass before the next:

1. `review.sh <n>`: independent review on `origin/<base>...<branch>`; REQUEST_CHANGES stops here.
2. CI green for the exact tip (`gh run list --commit <sha>`); `--local-gate` runs `$CHECK_CMD` instead
   (no CI yet).
3. Base moved? merge it into the spoke branch and push, then wait for CI again.
4. `git merge --ff-only`, `git push origin <base>`, `gh issue close -c "landed in <sha>"`, delete the remote branch.
5. `orca orchestration worker-release`, then `orca worktree rm --worktree issue:<n> --run-hooks`.

Run it on the main worktree only (it refuses elsewhere). The coordinator calls it itself on
`worker_done succeeded`; run it by hand for an attended land.

- Exit codes: review blocked, CI red or timed out, merge conflict. On a conflict or REQUEST_CHANGES the fix
  goes back to the spoke (`worker-start --task <T> --terminal <h> --spec "address: …"`), max 2 rounds, then
  label the issue `blocked`.
- Never force a land past a red review or red CI; fix the cause.
