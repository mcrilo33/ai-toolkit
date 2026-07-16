# Using ai-toolkit over a consuming project

`sync-to-repo.sh` deploys the toolkit (rules, skills, agents, the control-plane
scripts, and — with `--with-git-hooks` — the native commit/push cage) into another
repo. This note covers the config that belongs in the CONSUMING project vs the
ai-toolkit SOURCE, so a downstream setup never corrupts the shared toolkit.

## The golden rule

`settings/ai-toolkit.yml` in the ai-toolkit checkout is the **source** config. It is
committed, it is copied (in effect) into every project you sync, and it governs
ai-toolkit's own hub. **Never encode a consuming project's specifics there** — a
value meant for one downstream propagates to all of them and to ai-toolkit itself.

## Setting the consuming project's base branch

The `wt_base_branch` resolver (issue #117) resolves, in order:

1. `git config ai-toolkit.base-branch` (HYPHENATED key)
2. `AI_TOOLKIT_BASE_BRANCH` environment variable
3. `origin/HEAD` → `main`/`master`

Do **not** rely on option 1 for a downstream: `sync-to-repo.sh`'s
`apply_base_branch_config` treats the ai-toolkit yml as the owner of that git config
key, so every re-sync overwrites (or clears) it. Use option 2 instead — it is not
stored in git config, so re-sync cannot clobber it:

```bash
# in the consuming project's shell / profile / direnv .envrc
export AI_TOOLKIT_BASE_BRANCH=annotation/live
```

Two footguns this avoids:

- Editing `settings/ai-toolkit.yml` in the ai-toolkit checkout to your branch — it
  breaks the ai-toolkit drain (its base branch becomes a branch that does not exist
  here) and propagates downstream.
- `git config ai-toolkit.baseBranch <branch>` — the camelCase key stores as
  `basebranch`, a DIFFERENT key from the hyphenated `ai-toolkit.base-branch` the
  resolver reads, so the value is silently ignored and you fall through to
  `origin/HEAD`.

## Keeping the deployment personal (not committed to the shared repo)

If the toolkit is for your workflow only, sync with `--local-only`:

```bash
./scripts/sync-to-repo.sh /path/to/project claude --local-only
```

That excludes the synced ai-toolkit paths (`.claude/`, `.cursor/`, `.ai-toolkit/`,
the ai-toolkit `.github/` subdirs, `.testmondata*`) in the project's
`.git/info/exclude` — per-clone, never committed, and surgical (the project's real
`.github/workflows/` stays tracked).

## Telemetry

OTel export is opt-in (`AI_TOOLKIT_OTEL=1` + the collector stack). A fresh project
that does not set it stays dark, so "telemetry off" is the default — nothing to do.
