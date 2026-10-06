---
description: "Standard .gitignore patterns for environment, IDE, dependencies, build artifacts, and secrets"
paths:
  - '**/.gitignore'
---
# .gitignore

The host project owns its own `.gitignore`. The toolkit does **not** ship or reconcile a
`.gitignore`, and does not mandate generic hygiene categories — those are the host's
responsibility and most projects already ignore them.

## What the toolkit ignores

The only paths the toolkit owns are its own generated, per-clone state, never committed by anyone. `sync.sh`
writes them to the target's `.git/info/exclude` (per-clone, unversioned, one marked block), so they never dirty
the tracked `.gitignore`:

- `/.ai-toolkit/` (scripts, hooks, env files, `sync-manifest`, and the per-machine `ai-toolkit.local.env`)
- every `.claude/` path the sync wrote, one line each (a `.claude/` file the project tracks is never listed)

The project's own `CLAUDE.md` is never read, written or ignored. `orca.yaml` is the one file the sync leaves
tracked; with `--local-only` the same block also excludes it, for a deployment that is never committed.

## Secrets are not a name-based ignore concern

Do not add broad name globs like `*secret*` or `*credentials*` to any ignore file: they
silently swallow legitimately-named files (`secrets_scan_test.py`, docs on secret handling)
— the fail-loud violation the secrets-scan hook exists to avoid. Secret **protection** is the
secrets-scan hook's job, not the ignore file's.
