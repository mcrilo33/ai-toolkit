# Cutover checklist (v1 -> v2), rehearsed by `e2e/cutover-rehearsal.sh` (PASS: tests, shellcheck, sync twice, no-ff merge, self-sync, one worker landed)

Run it in a **fresh clone**, not in the hub checkout. Only steps 6 and 7 push to GitHub (marked PUSH). Replace `<tk>` with the clone path.

0. **Preconditions.** `v2` has every WP merged and reviewed; `e2e/github-scenario.sh` green 3x; no live v1 spoke or `/afk` drain (`hub-afk.sh --status` off: land or abandon the v1
   worktrees first, they keep v1 hooks); `pip install -r requirements-dev.txt` + `shellcheck` + `jq` available.
   **The coordinator has committed `docs/orca-migration` (05, 06, STATUS, LESSONS) and merged it into `main`, so `v1-final` keeps them.** Check, do not do:
   `git fetch origin && git cat-file -e origin/main:docs/orca-migration/06-target-architecture.md`.
   **Branch protection on main:** `gh api repos/mcrilo33/ai-toolkit/branches/main/protection` (404 = none; otherwise the push in step 7 may need an admin or a PR).
1. `git clone git@github.com:mcrilo33/ai-toolkit.git <tk> && cd <tk> && git checkout v2`
2. `git merge origin/main` (main must be an ancestor of v2; resolve modify/delete conflicts by deleting: v2 deletes those files).
3. `bash v2/scripts/cutover.sh --dry-run`, read the list, then `bash v2/scripts/cutover.sh`. It tags `v1-final` at `origin/main` (local), deletes the v1 machinery, moves `v2/*` and its
   dotfiles (`.github`, `.shellcheckrc`) to the root, renames `tests_v2` to `tests`, trims `requirements-dev.txt`, replaces v1's `.gitignore` (v2's ignores `/CLAUDE.md`), and makes ONE commit.
   Refuses (exit 2, nothing touched) on a dirty tree, an existing tag, or a missing `v2/`.
4. `pytest -n auto tests` and `shellcheck scripts/*.sh bin/claude-spoke hooks/claude/*.sh hooks/git/* e2e/*.sh` (the `ci.yml` steps, which cannot run on GitHub before a push).
5. `git checkout main && git merge --no-ff v2 -m "Merge v2: cutover"`. `bash scripts/sync.sh <tk> && bash scripts/install.sh <tk>`: `CLAUDE.md` appears (generated), `git status` stays clean.
6. **PUSH to a scratch branch first:** `git push origin main:refs/heads/v2-cutover-ci`. Wait for `ci` on that branch to be green on BOTH `ubuntu-latest` and `macos-latest`
   (`gh run list -R mcrilo33/ai-toolkit --branch v2-cutover-ci`, `gh run watch <id>`). Red: fix on the clone, amend the merge, force-push the scratch branch only. Then `git push origin :v2-cutover-ci`.
7. **PUSH the real thing:** `git push origin v1-final main` (the tag is the rollback point and keeps `docs/orca-migration/`). Check the `ci` run on `main` is green.
8. **The hub** (`~/Repos/ai-toolkit`, orca-registered): `git pull --ff-only`, then `bash scripts/sync.sh . --migrate-v1` (the v1 manifest otherwise leaves `.cursor/`, the Copilot `.github` files,
   v1 `.claude/hooks` and the deleted skills in place, and `setup.sh` copies `.claude/` into every spoke), `bash scripts/install.sh .` (sets a repo-local `core.hooksPath`; v1's installer never set one).
   Verify: `diff <(ls .claude/skills) <(ls shared/skills)` is empty and `.claude/hooks` holds only the three v2 hooks.
   Collector: `docker rm -f lf-collector` (the v1 container owns the ports), then `bash scripts/otel.sh up` (Langfuse keys in `.ai-toolkit/ai-toolkit.local.env`). Dashboards are a later batch (D1).
9. **Downstream repos (`hex`, ...)**: check nothing reads their `.cursor/` or `.github/` copies, then `bash <tk>/scripts/sync.sh <repo> --migrate-v1`, commit a tracked `orca.yaml` (Orca reads the
   worktree's tracked file; a `--local-only` sync leaves it untracked and setup never runs), `install.sh <repo>`, `orca repo add --path <repo>` once, local env (`CHECK_CMD`, `LOCAL_GATE=1` if no CI).
10. Update or delete the memory notes that name deleted mechanisms (`spoke-ready`, `hub-afk`, gate-broker, tmux, review-stamp...). Optional: `orca orchestration reset` of the inert spike Runs.

**Rollback.** Before step 7: `git reset --hard v1-final` in the clone (the tag is local). After it: `git revert -m 1 <merge-sha> && git push origin main` (non-destructive), or, with the user's explicit
OK, `git push --force-with-lease origin v1-final:main`. In the hub: `git reset --hard v1-final`, then `git config --local core.hooksPath "$PWD/.git/hooks"` (v2's `install.sh` overwrote the local
`core.hooksPath`; the v1 installer never sets it, so the v1 hooks would not run), then re-run the v1 hook installer (`scripts/install-git-hooks.sh`) and the v1 sync.
