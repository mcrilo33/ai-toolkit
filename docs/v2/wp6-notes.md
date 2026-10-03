# v2 WP6 notes: real end-to-end + cutover tooling
Branch `v2-wp6`. Budgets (`wc -l`): coordinator.sh 236/240 (+7 for the human-gate UX) · github-scenario.sh 150/150 · cutover.sh 46/80 · cutover-rehearsal.sh 93 (not budgeted) · ci.yml 33/60 · architecture.md 103/300 ·
frontmatter.md 33 + cutover.md 22 + README 12 (extras) · test_cutover.py 139 (not budgeted). `pytest -n auto tests_v2`: 555 pass, ~36 s wall here (the 10 s cap needs a quiet machine, see WP1/WP2).

## Results (all real: GitHub repo `mcrilo33/ai-toolkit-e2e`, CI on the exact SHA, Orca, Claude)
- `e2e/github-scenario.sh` (phases `next happy negative`): auto PASS 212 s; scripted `--reply` PASS; `E2E_HUMAN_WAIT=1` with a simulated human PASS 243 s (prints the question, the reply line, asserts the worktree comment). The user's first attended run timed out with no reply (see UX below).
- `--next` on 5 real issues: P (priority) -> X -> K (blocked-by X, numbered BEFORE X, so only the dependency keeps it behind) -> R; H (`hold`) never. Exit 3 when only H is left.
- Negative path: REVIEW_CMD wrapper rejects issue B every time (coordinator-approved: a real reviewer would accept the fix in round 1); 2 `round n: address` comments, label `blocked`, issue open, worktree kept, no active worker, nothing landed.
- Negative path: the stub's blocker must be ACTIONABLE (a worker refused a non-actionable one with `worker_done failed`, which also blocks but after 1 round): it now asks for a new comment line each round.
- The REAL reviewer caught a REAL red CI in the first run (bare `pytest` could not import `hello`, the base repo lacked `pytest.ini`); the round loop fixed and landed it in 2.5 min.
- Rehearsal (`e2e/cutover-rehearsal.sh`) PASS 123 s on `/private/tmp/aitk-cutover-rehearsal` (origin = a local bare repo): 555 tests green after the move, shellcheck, sync twice no drift, `--no-ff`
  merge, self-sync generates `CLAUDE.md` (= guidelines minus frontmatter, gitignored) and leaves the tracked `orca.yaml`, `install.sh` repo-local hooks, one worker reviewed and landed on the local origin.
- Lines after cutover (06 s1 targets): code 1095 (<=2500; e2e 390 extra) · config 140 (<=400) · policy 11219 (<=8000, WP4: generic skills + python-style untouched) · tests 2276 (<=3000) · docs 148 (<=800).

## Decisions and deviations
- e2e clone `/private/tmp/aitk-e2e-gh`, registered once; per run reset = force-push of tag `e2e-base-3` (README, tracked `orca.yaml`, `.gitignore`, `pytest.ini`, one pytest job, one test) to the e2e main, all open issues
  closed, remote branches deleted; refuses an origin that is not the e2e repo. `sync.sh --local-only`, so no ai-toolkit policy reaches the public repo. A blocked issue's branch stays on GitHub until the next reset.
- `cutover.sh`: `v1-final` at `origin/main` (`--v1 <ref>`), moves by plain `cp`/`rm` + `git add -A`, commits with the host hooks off. Extras (approved): `.test-select-exempt`, root `pyproject.toml`/`ruff.toml`
  (grepped: nothing in v2 reads them), `shared/{pyproject,ruff}.toml`, all of `docs/` but `architecture.md` + `frontmatter.md` (moved to `docs/`), old `CLAUDE.md`/`README.md`/`orca.yaml`/CI workflow replaced.
- `CLAUDE.md` is sync's output (coordinator): `sync.sh` now skips `orca.yaml` when the target is the toolkit itself (tracked `./scripts/setup.sh` one); cutover adds `/CLAUDE.md` to `.gitignore`.
- `tests_v2/conftest.py` `V2` is layout-agnostic (`v2/` or the root). Policy fixes the lint found: `hub`/`afk` skills answer gates with `coordinator.sh --reply` (a bare `orca orchestration reply` is refused),
  no `shared/` or script text names a deleted doc or path (`test_nothing_points_at_a_file_the_cutover_deletes`).

## Findings for the coordinator / later work
1. **[verify] closed: Orca runs the TRACKED `orca.yaml` of the new worktree**, not the main checkout's. A `--local-only` sync leaves it untracked, so setup never ran and `dispatch.sh` died after ~3 min with
   "setup did not finish". Targets must commit `orca.yaml` (sync still writes it; documented in architecture.md and cutover.md).
2. **Fixed:** `dispatch.sh --next` listed worktrees of ALL registered repos, so another repo's linked issue #n made the same number "busy" (now `--repo path:`; test pins it).
3. **Open (design):** `issue:<n>` selectors are global across registered repos (`orca worktree show --worktree issue:N` in land.sh, review.sh, setup.sh, dispatch retry): two repos with a linked #N collide, and
   `land.sh` could act on the wrong repo's worktree. Proposal: resolve the path from `worktree list --repo path:<main>` and use `path:`.
4. Real GitHub confirmed the two stub-only items of WP1/WP2: the `gh api graphql {owner}/{repo}` placeholders and `blockedBy`, and `gh run list --commit <sha>`. Blocked-by is set with
   `gh api -X POST repos/<o>/<r>/issues/<k>/dependencies/blocked_by -F issue_id=<id of the blocker>` (`gh api` has no `-R`).
5. The scenario's cleanup runs ~20 s after `PASS`: never start a second run before the process exits (they share the clone). Claude's trust entries for the two clones are in `~/.claude.json` (written by Claude, not removed).
6. `ci.yml` cannot run on GitHub before the push: validated with `actionlint` and by running its three steps in the rehearsal clone. Langfuse session check: not run (no keys, optional by D1).
7. **Human-gate UX (from the user's first attended run):** the question text is now printed above the reply line (coordinator log/terminal, `--status`, e2e); the spoke worktree comment is `GATE waiting: ...` and is
   overwritten with `gate answered by the human: ...` after the reply (`orca worktree set --comment ""` is accepted but does NOT clear; `--workspace-status` is not used). `osascript` exits 0 here and still shows nothing:
   macOS attributes it to the launching app, so it cannot be detected (warned only on a non-zero exit). Bell: written to `/dev/tty` of the coordinator terminal; Orca's bundle (static read) marks the worktree + tab unread
   and, after 250 ms, raises its own OS notification (`source: terminal-bell`, settings `terminalBell` + `notifications.enabled` are true, `suppressWhenFocused` true). Not observable from the CLI (`isUnread` never flipped for a CLI-created,
   unmounted terminal): the attended run is the live check.
