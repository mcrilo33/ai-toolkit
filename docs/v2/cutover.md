# Cutover checklist (v1 -> v2), rehearsed by `e2e/cutover-rehearsal.sh` (PASS: tests, shellcheck, sync twice, no-ff merge, self-sync, one worker landed)

Run it in a **fresh clone**, not in the hub checkout. Nothing here pushes to GitHub except steps 6-7 (marked PUSH). Replace `<tk>` with the clone path.

0. **Preconditions.** `v2` has every WP merged and reviewed; `e2e/github-scenario.sh` green 3x; no live v1 spoke or `/afk` drain (`hub-afk.sh --status` off: land or abandon the v1
   worktrees first, they keep v1 hooks); `pip install -r requirements-dev.txt` + `shellcheck` + `jq` available.
1. `git clone git@github.com:mcrilo33/ai-toolkit.git <tk> && cd <tk> && git checkout v2`
2. `git merge origin/main` (main must be an ancestor of v2; resolve modify/delete conflicts by deleting: v2 deletes those files). Re-run step 3 of the rehearsal's tests afterwards.
3. `bash v2/scripts/cutover.sh --dry-run`, read the list, then `bash v2/scripts/cutover.sh`. It tags `v1-final` at `origin/main` (local), deletes the v1 machinery, moves `v2/*` and its
   dotfiles (`.github`, `.shellcheckrc`) to the root, renames `tests_v2` to `tests`, trims `requirements-dev.txt`, ignores `/CLAUDE.md`, and makes ONE commit. Refuses (exit 2, nothing touched)
   on a dirty tree, an existing tag, or a missing `v2/`.
4. `pytest -n auto tests` and `shellcheck scripts/*.sh bin/claude-spoke hooks/claude/*.sh hooks/git/* e2e/*.sh` (the `ci.yml` steps, which cannot run on GitHub before the push).
5. `git checkout main && git merge --no-ff v2 -m "Merge v2: cutover"`. `bash scripts/sync.sh <tk> && bash scripts/install.sh <tk>`: `CLAUDE.md` appears (generated), `git status` stays clean.
6. **PUSH** `git push origin v1-final main` (the tag is the rollback point and keeps `docs/orca-migration/`). Watch the first `ci` run of `.github/workflows/ci.yml` on `main` (ubuntu + macOS) go green.
7. **The hub** (`~/Repos/ai-toolkit`, orca-registered): `git pull --ff-only`, then `bash scripts/sync.sh .`, `bash scripts/install.sh .` (repo-local `core.hooksPath`; it overrides a global v1 one).
   Collector: `docker rm -f lf-collector` (the v1 container owns the ports), then `bash scripts/otel.sh up` (Langfuse keys in `.ai-toolkit/ai-toolkit.local.env`). Dashboards are a later batch (D1).
8. **Downstream repos (`hex`, ...)**: check nothing reads their `.cursor/` or `.github/` copies, then `bash <tk>/scripts/sync.sh <repo> --migrate-v1`, commit a tracked `orca.yaml` (Orca reads the
   worktree's tracked file; a `--local-only` sync leaves it untracked and setup never runs), `install.sh <repo>`, `orca repo add --path <repo>` once, local env (`CHECK_CMD`, `LOCAL_GATE=1` if no CI).
9. Update or delete the memory notes that name deleted mechanisms (`spoke-ready`, `hub-afk`, gate-broker, tmux, review-stamp...). Optional: `orca orchestration reset` of the inert spike Runs.

**Rollback.** Before step 6: `git reset --hard v1-final` in the clone (the tag is local). After it: `git revert -m 1 <merge-sha> && git push origin main` (non-destructive), or, with the user's explicit
OK, `git push --force-with-lease origin v1-final:main`; then in the hub `git reset --hard v1-final`, re-sync with the v1 pipeline and re-install the v1 hooks (`scripts/install-git-hooks.sh`).
