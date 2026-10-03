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

With `--local-only`, the same block additionally excludes the synced deployment files (`.claude/`, `CLAUDE.md`,
`orca.yaml`) for a personal deployment.

## Secrets are not a name-based ignore concern

Do not add broad name globs like `*secret*` or `*credentials*` to any ignore file: they
silently swallow legitimately-named files (`secrets_scan_test.py`, docs on secret handling)
— the fail-loud violation the secrets-scan hook exists to avoid. Secret **protection** is the
secrets-scan hook's job, not the ignore file's.
