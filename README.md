# ai-toolkit

Centralized AI agent configs for **GitHub Copilot**, **Cursor**, and **Claude Code**.

## Overview

Single source of truth for AI agent rules, skills, prompts, and settings across multiple AI-powered development tools. Everything lives in `shared/` — the sync script generates tool-specific formats (frontmatter, file extensions) on demand.

## Structure

```
ai-toolkit/
├── shared/                   # Single source of truth
│   ├── rules/                # Coding guidelines, conventions, style guides
│   ├── skills/               # Reusable agent skills (land, TDD, etc.)
│   ├── prompts/              # Reusable prompts (commit-msg, etc.)
│   ├── agents/               # Agent definitions
│   └── hooks/                # Lifecycle hook scripts (pre/post tool use)
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
├── docs/                     # Frontmatter references, sync model, workflow guides
│
├── scripts/
│   ├── install.sh            # Symlinks tool settings to expected locations
│   └── sync-to-repo.sh       # Generates tool configs from shared/ into a repo
│
└── tests/                    # pytest unit + integration suites
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

## Hooks

Hooks execute shell scripts at key lifecycle points during agent sessions.
They enforce security policies, automate code quality, and validate operations.
All hooks live in `shared/hooks/` and are synced to platform-specific formats.

| Hook | Event | Enforcement | Purpose |
| ---- | ----- | ----------- | ------- |
| `block-no-verify` | PreToolUse | **hard** | Block `git --no-verify` and improper force pushes |
| `secrets-scan` | PreToolUse | **hard** | Block hardcoded secrets in file writes |
| `config-protection` | PreToolUse | **hard** | Block modification of CI/linter config files |
| `commit-quality` | PreToolUse | **hard** | Validate conventional-commit format + issue-anchor |
| `commit-gauntlet` | PreToolUse | **hard** | Lint/typecheck staged files; block on failure |
| `git-push-review` | PreToolUse | advisory | Show diff summary before `git push` |
| `red-proof-warn` | PreToolUse | advisory | Warn on push when source commits lack `Tested-RED:` |
| `reviewer-sep-warn` | PreToolUse | advisory | Warn on push when commits lack `Reviewed-by:` |
| `post-edit-format` | PostToolUse | advisory | Auto-format edited files (ruff, prettier, biome) |
| `quality-gate` | PostToolUse | advisory | Run linter + typechecker on edited files |
| `console-log-warn` | PostToolUse | advisory | Warn when debug statements are left behind |

### Hard vs advisory gates

- **Hard gates** (`deny`, exit 2) abort the operation: the commit/write does not
  proceed. These are the deterministic cage — `commit-quality` and
  `commit-gauntlet` block bad commits; `block-no-verify`, `secrets-scan`, and
  `config-protection` block dangerous writes.
- **Advisory gates** print a warning and **always exit 0** — they never block.
  `red-proof-warn` and `reviewer-sep-warn` surface TDD/review gaps at push time
  but cannot (and do not) stop the push.

The cage's behavior is fully deterministic *given the same command*: piping an
identical `{"tool_input":{"command":"..."}}` payload into a script always yields
the same verdict. See `tests/unit/test_commit_hooks.py`.

### Enforcement caveats

- **Tooling must be on `PATH`.** `commit-gauntlet` detects the project's linter
  and typechecker (ruff, eslint/biome; pyright, mypy, tsc). If none is installed
  or configured, the check **degrades gracefully and SKIPS** — meaning it
  enforces *nothing*. For real enforcement in a target repo, ensure ruff/pyright
  (or your stack) are installed and resolvable.
- **TDD carve-out.** `commit-gauntlet` skips the **typecheck** stage on commits
  carrying a `Tested-RED:` trailer (an unresolved import is the expected state of
  a red-before-green test commit); lint still runs. Lint is scoped to **changed
  lines**, so pre-existing debt on untouched lines never blocks a clean addition.
- **Agent-runtime dependency.** PreToolUse hooks only fire if the agent runtime
  invokes them. To enforce the blocking gates on *real* `git commit`/`git push`
  regardless of runtime (or for human-driven git), install them as native git
  hooks:

  ```bash
  scripts/install-git-hooks.sh [target-repo]   # wires commit-msg + pre-push
  scripts/install-git-hooks.sh --uninstall [target-repo]
  ```

  This reuses the same scripts (single source of truth), mapping
  `commit-quality` + `commit-gauntlet` to `commit-msg` and the advisory warnings
  to `pre-push`.

Hooks run globally via workspace-level config files. Some are also scoped to
specific agents (see [Agent-scoped hooks](#agent-scoped-hooks) above).

See [`docs/hooks-frontmatter.md`](docs/hooks-frontmatter.md) for the full
configuration reference, platform mapping, and script contract.

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
