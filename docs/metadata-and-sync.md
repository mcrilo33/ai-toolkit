# Metadata & sync pipeline

How `shared/` files are transformed into platform-specific configs by `sync-to-repo.sh`.

## Shared directory structure

```text
shared/
├── rules/
│   ├── metadata.yml          # Frontmatter definitions for rules
│   ├── guidelines.md
│   ├── code-quality.md
│   └── …
├── skills/
│   ├── metadata.yml          # Frontmatter definitions for skills
│   ├── land/SKILL.md
│   └── …
├── prompts/
│   ├── metadata.yml          # Frontmatter definitions for prompts
│   └── commit-msg.md
└── agents/
    ├── metadata.yml          # (reserved, currently empty)
    └── *.agent.md
```

Each category (rules, skills, prompts) has a `metadata.yml` that declares frontmatter fields shared across platforms, with optional per-tool overrides.

## How metadata.yml works

### Structure

```yaml
<item-key>:                   # filename without extension
  # ── shared defaults ──
  name: "Display Name"
  description: "What this item does"
  applyTo: "**/*.py"
  globs: "**/*.py"
  alwaysApply: false

  # ── per-tool overrides (optional) ──
  copilot:
    applyTo: "**/*.py,**/*.pyi"
  cursor:
    description: "More specific description for Cursor"
```

### Merge logic

The sync script merges values with: **shared defaults → per-tool overrides** (tool values win).

If a field exists both at the top level and under a tool key, the tool-specific value takes precedence.

### Field selection

Each category declares which fields each platform receives:

| Category | Copilot | Cursor | Claude Code |
| -------- | ------- | ------ | ----------- |
| **Rules** | `name`, `description`, `applyTo`, `excludeAgent` | `description`, `globs`, `alwaysApply` | `paths` |
| **Skills** | `name`, `description`, `allowed-tools`, `license`, `disable-model-invocation`, `user-invocable`, `argument-hint` | `name`, `description`, `license`, `compatibility`, `metadata`, `disable-model-invocation` | `name`, `description`, `allowed-tools`, `disable-model-invocation`, `user-invocable`, `argument-hint`, `paths`, `context`, `agent`, `when_to_use`, `arguments`, `model`, `effort`, `hooks`, `shell` |
| **Prompts** | `name`, `description`, `agent` | — | `name`, `description` |
| **Agents** | `name`, `description`, `model`, `tools`, `disallowedTools`, `user-invocable`, `disable-model-invocation`, `target`, `argument-hint`, `agents`, `handoffs`, `mcp-servers`, `hooks`, `metadata` | `description`, `model`, `readonly`, `is_background` | `name`, `description`, `model`, `tools`, `disallowedTools`, `mcp-servers`, `hooks`, `effort`, `maxTurns`, `permissionMode`, `memory`, `background`, `isolation`, `skills`, `color`, `initialPrompt` |

## Output paths

| Category | Copilot | Cursor | Claude Code |
| -------- | ------- | ------ | ----------- |
| **Rules** | `.github/instructions/*.instructions.md` | `.cursor/rules/*.md` or `.mdc` | `.claude/rules/*.md` |
| **Skills** | `.github/skills/<name>/SKILL.md` | `.cursor/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` |
| **Prompts** | `.github/prompts/*.prompt.md` | — | `.claude/prompts/*.md` |
| **Agents** | `.github/agents/*.agent.md` | `.cursor/agents/*.md` | `.claude/agents/*.md` |

## Special cases

| Source file | Copilot | Claude Code |
| ----------- | ------- | ----------- |
| `rules/guidelines.md` | `.github/copilot-instructions.md` (root system prompt) | `CLAUDE.md` at repo root |

These are copied without frontmatter — they serve as the global system prompt for each tool.

## Sync pipeline

```text
shared/<category>/metadata.yml  ─┐
                                 │  sync-to-repo.sh
shared/<category>/*.md           ─┤  ──────────────→  target repo
                                 │
                                 │  1. Parse metadata.yml
                                 │  2. Merge shared defaults + tool overrides
                                 │  3. Select fields for target tool
                                 │  4. Prepend YAML frontmatter to .md body
                                 │  5. Write to tool-specific path + extension
                                 └─
```

## Usage

```bash
./scripts/sync-to-repo.sh <repo-path>           # All tools
./scripts/sync-to-repo.sh <repo-path> copilot    # Copilot only
./scripts/sync-to-repo.sh <repo-path> cursor     # Cursor only
./scripts/sync-to-repo.sh <repo-path> claude     # Claude only
./scripts/sync-to-repo.sh <repo-path> cursor --dry-run   # Preview only
```

## Sync manifest and GC

Every sync records the files it writes into `.ai-toolkit-manifest.json` at the
target root, one sorted list per tool (`copilot`, `cursor`, `claude`) plus the
toolkit git revision. On the next sync, `scripts/sync_manifest.py` compares the
old list with the new one and deletes stale files — paths a previous sync wrote
but the current one no longer produces (e.g. a rule removed from `shared/`).

Safety guarantees:

- **Per-tool scoping** — finalizing one tool never touches another tool's list.
- **User files are never touched** — only paths listed in the manifest are
  eligible for deletion; anything else in `.cursor/`, `.github/`, or `.claude/`
  is left alone.
- **Protected paths** — `.cursor/hooks.json`, `.claude/settings.json` (owned by
  the hook reconciler — which also forces `preferredNotifChannel:
  notifications_disabled` to silence Claude Code's idle notifications, issue
  #146), and `*.bak` backups are never deleted.
- **Path validation** — absolute paths and `..` traversal segments are rejected,
  both in recorded paths and in old manifest entries.
- A corrupt or missing manifest is treated as a first run: nothing is deleted.

## --dry-run

`--dry-run` previews a sync without touching the target: each write is printed
as `[dry-run] would write <path>`, stale files are reported as would-delete,
and nothing is created — no directories, no configs, and no manifest.

## Cursor plugin build

`build-cursor-plugin.sh` packages the same Cursor emission (identical metadata
fields and frontmatter) as a distributable Cursor plugin. It is additive — the
existing `.cursor/` sync above is unchanged.

```bash
./scripts/build-cursor-plugin.sh                 # Build into dist/cursor-plugin
./scripts/build-cursor-plugin.sh /tmp/my-plugin  # Custom output directory
```

Output layout:

```text
dist/cursor-plugin/
├── .cursor-plugin/plugin.json    # Manifest (version from the VERSION file)
├── rules/<name>.mdc              # Same content as the .cursor/rules sync output
├── skills/<name>/SKILL.md        # Same content as .cursor/skills, + subdirs
├── agents/<name>.md              # Same content as the .cursor/agents sync output
├── hooks/hooks.json              # Hook wiring with plugin-relative script paths
├── scripts/<hook>.sh             # All shared/hooks/*.sh (executable)
├── scripts/lib/utils.sh          # Shared hook utilities
└── README.md                     # Generated plugin README
```

Hook commands in `hooks/hooks.json` point at `./scripts/<hook>.sh` (plugin-relative)
instead of `.cursor/hooks/scripts/<hook>.sh`, via the optional
`--script-prefix` argument of `hooks_generator.py`. The build is a clean rebuild
(`rm -rf` of the output dir), so repeated runs are byte-identical.

## Example: end-to-end for a skill

**Source** — `shared/skills/metadata.yml`:

```yaml
land:
  name: "land"
  description: "Land a finished task from the hub: merge, suite, push, teardown."
  cursor:
    description: "Land a finished task from the hub: merge, suite, push, teardown. Use when the user says /land."
```

**Copilot output** — `.github/skills/land/SKILL.md`:

```yaml
---
name: land
description: "Land a finished task from the hub: merge, suite, push, teardown."
---
```

**Cursor output** — `.cursor/skills/land/SKILL.md`:

```yaml
---
name: land
description: "Land a finished task from the hub: merge, suite, push, teardown. Use when the user says /land."
---
```

**Claude output** — `.claude/skills/land/SKILL.md`:

```yaml
---
name: land
description: "Land a finished task from the hub: merge, suite, push, teardown."
---
```
