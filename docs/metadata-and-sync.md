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
│   ├── close-task/SKILL.md
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
```

## Example: end-to-end for a skill

**Source** — `shared/skills/metadata.yml`:

```yaml
close-task:
  name: "close-task"
  description: "Close a task by committing, pushing, and creating a PR."
  cursor:
    description: "Close a task by committing, pushing, and creating a PR. Use when the user says /close."
```

**Copilot output** — `.github/skills/close-task/SKILL.md`:

```yaml
---
name: close-task
description: Close a task by committing, pushing, and creating a PR.
---
```

**Cursor output** — `.cursor/skills/close-task/SKILL.md`:

```yaml
---
name: close-task
description: "Close a task by committing, pushing, and creating a PR. Use when the user says /close."
---
```

**Claude output** — `.claude/skills/close-task/SKILL.md`:

```yaml
---
name: close-task
description: Close a task by committing, pushing, and creating a PR.
---
```
