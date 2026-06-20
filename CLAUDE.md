# AI Toolkit — Agent Context

This repository contains shared rules, skills, prompts, and agents for AI coding assistants (Copilot, Cursor, Claude Code).

## Key references

- `docs/available-tools.md` — Complete inventory of all tools accessible to agents (~140+ tools across file ops, terminal, Git, GitHub, browser, notebooks, Jira, Confluence, Notion, macOS automation, web research, GCP)
- `docs/rules-frontmatter.md` — Rules frontmatter field reference per platform
- `docs/skills-frontmatter.md` — Skills frontmatter field reference per platform
- `docs/metadata-and-sync.md` — How `shared/` files are synced to platform-specific configs
- `docs/parallel-worktrees.md` — Running parallel Claude sessions with one git worktree per task (`worktree-new.sh` / `worktree-done.sh`, review window, merge flow)
- `docs/telemetry-pull-layer.md` — Telemetry pull layer: on-machine transcript parsers that attribute tokens and reconcile per-session/run cost via `ccusage` (offline); retained as the Langfuse backfill source (the Streamlit dashboard + DuckDB store were removed in #90)
- `docs/spoke-otel-langfuse-spike.md` — Spike (#83): opt-in (`AI_TOOLKIT_OTEL=1`) Claude Code native-OTel trace export per spoke to Langfuse / a local OTLP collector; native-view-vs-dashboard comparison and the hybrid recommendation
- `dashboard/langfuse/otelcol.yaml` — OTel collector config for the Langfuse push path (the single observability surface since #90)

## Repository structure

- `shared/rules/` — Instruction rules (guidelines, code quality, security, etc.)
- `shared/skills/` — Skills with SKILL.md + optional scripts/references/templates/assets
- `shared/prompts/` — Reusable prompt templates
- `shared/agents/` — Agent mode definitions
- `scripts/` — Sync pipeline (`sync-to-repo.sh`, `metadata_parser.py`)
- `settings/` — IDE-specific settings (VS Code, Cursor, Claude)
- `tests/` — Unit and integration tests

## Sync pipeline

```bash
./scripts/sync-to-repo.sh <target-repo> [copilot|cursor|claude|all]
```

Reads `metadata.yml` per category, merges shared defaults with per-tool overrides, prepends YAML frontmatter, writes to platform-specific paths.

## Conventions

- All rules, skills, and prompts use `metadata.yml` for frontmatter definitions
- Skill folders must contain `SKILL.md` as the entry point
- Commit messages follow conventional commits format
- Tests use pytest with AAA structure
