# .gitignore

The host project owns its own `.gitignore`. The toolkit does **not** ship or reconcile a
`.gitignore`, and does not mandate generic hygiene categories — those are the host's
responsibility and most projects already ignore them.

## What the toolkit ignores

The only paths the toolkit owns are its own generated, per-clone state — never committed by
anyone. `sync-to-repo.sh` writes these to the target's `.git/info/exclude` (per-clone,
unversioned, idempotent marked block), so they never dirty the tracked `.gitignore`:

- `/.ai-toolkit/`, `.ai-toolkit-manifest.json`
- `/.review/`
- `.testmondata`, `.testmondata-shm`, `.testmondata-wal`
- `pyrightconfig.json`

With `--local-only`, the same block additionally excludes the synced deployment dirs
(`.claude/`, `.cursor/`, the `.github/` ai-toolkit subdirs) for a personal deployment.

## Secrets are not a name-based ignore concern

Do not add broad name globs like `*secret*` or `*credentials*` to any ignore file: they
silently swallow legitimately-named files (`secrets_scan_test.py`, docs on secret handling)
— the fail-loud violation the secrets-scan hook exists to avoid. Secret **protection** is the
secrets-scan hook's job, not the ignore file's.
