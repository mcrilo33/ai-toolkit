# ai-toolkit

Centralized AI agent configs for **GitHub Copilot**, **Cursor**, and **Claude Code**.

## Overview

This repository is the single source of truth for AI agent rules, skills, prompts, and settings across multiple AI-powered development tools. Instead of maintaining separate configurations in each tool's dotfiles, everything lives here — with tool-specific overlays generated from shared canonical configs.

## Structure

```
ai-toolkit/
├── shared/                   # Canonical, tool-agnostic configs
│   ├── rules/                # Coding guidelines, conventions, style guides
│   └── skills/               # Reusable agent skills (close-task, TDD, etc.)
│
├── copilot/                  # GitHub Copilot overlay (.github/ format)
│   ├── copilot-instructions.md
│   ├── instructions/         # *.instructions.md with applyTo frontmatter
│   ├── skills/               # SKILL.md with Copilot frontmatter
│   ├── agents/               # *.agent.md
│   └── prompts/              # *.prompt.md
│
├── cursor/                   # Cursor overlay
│   ├── rules/                # *.mdc with description/globs/alwaysApply frontmatter
│   └── skills/               # SKILL.md with Cursor conventions
│
├── claude/                   # Claude Code overlay
│   ├── settings.json
│   └── skills/
│
├── settings/                 # IDE/CLI settings (LLM-related only)
│   ├── vscode/               # Copilot settings, MCP servers, custom models
│   ├── cursor/               # MCP servers
│   └── claude/               # Claude Code settings
│
└── scripts/
    ├── install.sh            # Symlinks settings + tool overlays to expected locations
    ├── sync-to-repo.sh       # Copies copilot/ into a target repo's .github/
    └── diff-check.sh         # Detects drift between shared/ and tool overlays
```

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/mcrilo33/ai-toolkit.git ~/Repos/ai-toolkit
cd ~/Repos/ai-toolkit
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your actual values
# OR store secrets in macOS Keychain (recommended):
security add-generic-password -a "$USER" -s "LITELLM_MASTER_KEY" -w "your-key"
```

### 3. Install symlinks

```bash
./scripts/install.sh
```

This creates symlinks from the repo to each tool's expected config location:

| Tool | What gets linked |
|------|-----------------|
| Cursor | `cursor/rules/*.mdc` → `~/.cursor/rules/` |
| Cursor | `cursor/skills/*` → `~/.cursor/skills/` |
| Cursor | `settings/cursor/mcp.json` → `~/.cursor/mcp.json` |
| Claude | `claude/settings.json` → `~/.claude/settings.json` |
| Claude | `claude/skills/*` → `~/.claude/skills/` |

> [!NOTE]
> VS Code settings can't be symlinked (partial `settings.json`). Copy keys manually from `settings/vscode/copilot-settings.jsonc`.

### 4. Sync Copilot configs to a repo

```bash
./scripts/sync-to-repo.sh ~/Repos/my-project
```

This copies `copilot/` → `<repo>/.github/` for per-repo Copilot instructions.

## How it works

### Shared configs (`shared/`)

The `shared/` directory contains **tool-agnostic** rules and skills. These are the canonical source of truth — no tool-specific frontmatter, no vendor lock-in.

### Tool overlays

Each tool directory (`copilot/`, `cursor/`, `claude/`) contains the same content adapted with tool-specific frontmatter:

| Tool | Rule format | Frontmatter |
|------|------------|-------------|
| Copilot | `*.instructions.md` | `applyTo` glob |
| Cursor | `*.mdc` | `description`, `globs`, `alwaysApply` |
| Claude | `SKILL.md` | `name`, `description` |

### Drift detection

```bash
./scripts/diff-check.sh
```

Compares `shared/` content against tool overlays to flag when they've diverged.

## Adding new rules

1. Create the canonical rule in `shared/rules/<name>.md`
2. Create the Copilot version in `copilot/instructions/<name>.instructions.md` (add `applyTo` frontmatter)
3. Create the Cursor version in `cursor/rules/<name>.mdc` (add `description`/`globs`/`alwaysApply` frontmatter)
4. Run `./scripts/diff-check.sh` to verify consistency

## Adding new skills

1. Create the canonical skill in `shared/skills/<name>/SKILL.md`
2. Copy to `copilot/skills/<name>/SKILL.md` (add Copilot frontmatter)
3. Copy to `cursor/skills/<name>/SKILL.md` (add Cursor conventions)
4. Optionally add to `claude/skills/<name>/SKILL.md`

## Environment variables

All secrets are referenced via `${ENV_VAR}` placeholders. See `.env.example` for the full list.

**Never commit actual secrets.** Store them in macOS Keychain:

```bash
security add-generic-password -a "$USER" -s "KEY_NAME" -w "value"
```

Load in shell:

```bash
export KEY_NAME=$(security find-generic-password -a "$USER" -s "KEY_NAME" -w 2>/dev/null)
```

## License

Private — personal use only.
