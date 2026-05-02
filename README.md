# ai-toolkit

Centralized AI agent configs for **GitHub Copilot**, **Cursor**, and **Claude Code**.

## Overview

Single source of truth for AI agent rules, skills, prompts, and settings across multiple AI-powered development tools. Everything lives in `shared/` — the sync script generates tool-specific formats (frontmatter, file extensions) on demand.

## Structure

```
ai-toolkit/
├── shared/                   # Single source of truth
│   ├── rules/                # Coding guidelines, conventions, style guides
│   ├── skills/               # Reusable agent skills (close-task, TDD, etc.)
│   ├── prompts/              # Reusable prompts (commit-msg, etc.)
│   └── agents/               # Agent definitions
│
├── claude/                   # Claude Code settings (tool-specific only)
│   └── settings.json
│
├── copilot/                  # Copilot placeholder (agents)
│   └── agents/
│
├── settings/                 # IDE/CLI settings (LLM-related only)
│   ├── vscode/               # Copilot settings, MCP servers, custom models
│   ├── cursor/               # MCP servers
│   └── claude/               # Claude Code settings
│
└── scripts/
    ├── install.sh            # Symlinks tool settings to expected locations
    └── sync-to-repo.sh       # Generates tool configs from shared/ into a repo
```

## How it works

### `shared/` = source of truth

All rules, skills, prompts, and agents live in `shared/` as plain Markdown — no tool-specific frontmatter, no vendor lock-in.

### `sync-to-repo.sh` = generates tool configs

The sync script reads from `shared/` and generates the correct format for each tool:

| Tool | Output location | What it generates |
|------|----------------|-------------------|
| **Copilot** | `<repo>/.github/` | `copilot-instructions.md`, `instructions/*.instructions.md` (with `applyTo`), `skills/`, `prompts/*.prompt.md`, `agents/` |
| **Cursor** | `<repo>/.cursor/` | `rules/*.mdc` (with `description`/`globs`/`alwaysApply`), `skills/` |
| **Claude** | `<repo>/.claude/` + `CLAUDE.md` | `CLAUDE.md` (guidelines), `skills/` |

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/mcrilo33/ai-toolkit.git ~/Repos/ai-toolkit
cd ~/Repos/ai-toolkit
```

### 2. Install settings

```bash
./scripts/install.sh
```

This symlinks tool settings (MCP configs, Claude settings) to their expected locations:

| Tool | What gets linked |
|------|-----------------|
| Cursor | `settings/cursor/mcp.json` → `~/.cursor/mcp.json` |
| Claude | `claude/settings.json` → `~/.claude/settings.json` |

> [!NOTE]
> VS Code settings can't be symlinked (partial `settings.json`). Copy keys manually from `settings/vscode/copilot-settings.jsonc`.

### 3. Sync configs to a project

```bash
# Sync all tools
./scripts/sync-to-repo.sh ~/Repos/my-project

# Sync a specific tool
./scripts/sync-to-repo.sh ~/Repos/my-project copilot
./scripts/sync-to-repo.sh ~/Repos/my-project cursor
./scripts/sync-to-repo.sh ~/Repos/my-project claude
```

### 4. Configure environment variables

#### Secrets (store in macOS Keychain)

| Variable | Purpose | Used by |
|----------|---------|---------|
| `LITELLM_MASTER_KEY` | LiteLLM proxy API key | VS Code (Copilot custom models) |
| `OPENAI_API_KEY` | OpenAI API key | General LLM access |
| `GITHUB_MCP_TOKEN` | GitHub personal access token | Cursor MCP server |
| `TAVILY_API_KEY` | Tavily search API key | VS Code & Cursor MCP servers |
| `JIRA_API_TOKEN` | Jira API token | VS Code & Cursor MCP servers |
| `CONFLUENCE_API_TOKEN` | Confluence API token | VS Code & Cursor MCP servers |

```bash
# Store each secret in macOS Keychain:
security add-generic-password -a "$USER" -s "LITELLM_MASTER_KEY" -w "your-key"
security add-generic-password -a "$USER" -s "GITHUB_MCP_TOKEN" -w "your-token"
```

#### Configuration (non-secret, user-specific)

| Variable | Purpose | Used by |
|----------|---------|---------|
| `JIRA_URL` | Jira instance URL | VS Code & Cursor MCP servers |
| `JIRA_USERNAME` | Jira username / email | VS Code & Cursor MCP servers |
| `CONFLUENCE_URL` | Confluence instance URL | VS Code & Cursor MCP servers |
| `CONFLUENCE_USERNAME` | Confluence username / email | VS Code & Cursor MCP servers |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID for Vertex AI | Claude Code |
| `CLOUD_ML_REGION` | GCP region for Vertex AI | Claude Code |

## Agents

Eleven specialized agents are defined in `shared/agents/`. Each has a dedicated
persona, tool restrictions, handoff chains, and (where relevant) scoped hooks.

| Agent | Role | Edits code? |
| ----- | ---- | :---------: |
| **architect** | System design, ADRs, diagrams | ❌ |
| **planner** | Decompose tasks into ordered steps | ❌ |
| **tdd-red** | Write failing pytest tests | ✅ |
| **tdd-green** | Make failing tests pass | ✅ |
| **tdd-refactor** | Improve code quality, tests green | ✅ |
| **code-review** | Review changes, report findings | ❌ |
| **debug** | Reproduce, diagnose, fix bugs | ✅ |
| **refactor** | Cross-cutting renames/restructures | ✅ |
| **security-reviewer** | Security audit | ❌ |
| **devops** | CI/CD, infrastructure, deployment | ✅ |
| **documentation** | Write/update docs | ✅ |

### Handoffs

Agents chain together via handoffs — buttons that transition to the next agent
with context. Deterministic transitions (`send: true`) auto-submit; decision
points let the user choose.

```text
# TDD full cycle (auto)
architect → planner → tdd-red → tdd-green → tdd-refactor → code-review

# Code review with findings (manual)
code-review → { debug | refactor | security-reviewer } → code-review
```

### Agent-scoped hooks

Code-editing agents run PostToolUse hooks (`post-edit-format.sh`,
`quality-gate.sh`) to auto-format and lint after every edit. `debug` also runs
`console-log-warn.sh`. `devops` runs PreToolUse hooks (`secrets-scan.sh`,
`config-protection.sh`) to guard infrastructure files.

See [`docs/agents-frontmatter.md`](docs/agents-frontmatter.md) for the full
configuration reference.

## Adding new content

### New rule

1. Create `shared/rules/<name>.md`
2. Add frontmatter mappings in `sync-to-repo.sh` (Copilot `applyTo`, Cursor `description`/`globs`/`alwaysApply`)
3. Run `sync-to-repo.sh` on your repos

### New skill

1. Create `shared/skills/<name>/SKILL.md`
2. Add frontmatter mappings in `sync-to-repo.sh` (Copilot and Cursor skill metadata)
3. Run `sync-to-repo.sh` on your repos

### New prompt

1. Create `shared/prompts/<name>.md`
2. Run `sync-to-repo.sh` — Copilot prompts get `*.prompt.md` format automatically

### New agent

1. Create `shared/agents/<name>.agent.md`
2. Run `sync-to-repo.sh` — agents are copied to `<repo>/.github/agents/`

## Environment variables

All secrets are referenced via `${ENV_VAR}` placeholders. **Never commit actual secrets.** Store them in macOS Keychain:

```bash
security add-generic-password -a "$USER" -s "KEY_NAME" -w "value"
```

Load in shell:

```bash
export KEY_NAME=$(security find-generic-password -a "$USER" -s "KEY_NAME" -w 2>/dev/null)
```

## License

Private — personal use only.
