# Frontmatter reference (Claude Code only)

The frontmatter lives in each source file under `shared/`; `scripts/sync.sh` copies the files as they are (there is no per-platform projection and no
`metadata.yml`). `tests/test_policy_lint.py` pins everything below. The block is flat YAML: `key: value`, `key: [a, b]` or `key:` + `  - item` lines.

| Content | Source | Synced to | Allowed keys |
|---|---|---|---|
| Rules | `shared/rules/*.md` | `.claude/rules/` | `description`, `paths` |
| On-demand rules | `shared/rules/on-demand/*.md` | `.ai-toolkit/rules/` (never auto-loaded) | `description` |
| Guidelines | `shared/rules/guidelines.md` | `CLAUDE.md` (frontmatter stripped; gitignored in this repo) | `description` |
| Skills | `shared/skills/<name>/SKILL.md` (+ references, scripts) | `.claude/skills/<name>/` | `name`, `description`, `argument-hint`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `paths`, `context`, `agent`, `when_to_use`, `arguments`, `model`, `effort`, `hooks`, `shell` |
| Agents | `shared/agents/<name>.md` | `.claude/agents/` | `name`, `description`, `model`, `effort`, `tools`, `disallowedTools`, `skills`, `maxTurns`, `color` |
| Commands | `shared/prompts/*.md` | `.claude/commands/` | `description`, `argument-hint`, `allowed-tools`, `model` |

## Rules of the lint

- `description` is mandatory, one double-quoted line, no inner double quote or backslash, at most 1024 characters.
- **Rule loading.** Claude Code loads every `.claude/rules/*.md` that has no `paths:`, and the others only when a matching file is open. Always-on rules are exactly
  `security`, `agent-orchestration`, `scientific-integrity`; every other top-level rule carries `paths:` (a YAML list of globs). `alwaysApply`, `applyTo` and `globs`
  (Cursor/Copilot syntax) do not exist any more. Policy an agent must read on demand (workflow, planning-hub, afk-answering, issue-hygiene, bug-triage) goes in `on-demand/`
  and is referenced by path from the skills that need it.
- **Agents.** `model` is one of `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-4-5-20251001` (never a `[1m]` variant); `effort` is
  `low|medium|high|xhigh|max`. The read-only agents (`architect`, `planner`, `code-review`, `security-reviewer`, `bug-scoper`, `followup-scoper`) declare
  `disallowedTools: Edit, Write, NotebookEdit`. Tool names are Claude Code's (`Bash`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Agent`, `Edit`, `Write`, `NotebookEdit`).
- **The `code-review` agent's last message is one JSON object** `{verdict, blockers, warnings, tdd_followed, tests_weakened, summary}`; `review.sh` rejects anything else.
- **Size caps** (lines): `solo-cycle` 120, `hub` 60, `start-task` 60, `afk` 40, `land` 30, `quick` 30, `workflow` 100, `planning-hub` 50, `afk-answering` 120, `code-review` 110.
- **No deleted mechanism.** Nothing under `shared/` may name `spoke-ready`, `review-stamp`, `tmux`, `.review/`, `hub-afk`, `metadata.yml`, Cursor or Copilot, and so on (the regex is `DELETED` in the lint).
  Nothing may point at a doc other than `docs/architecture.md` and `docs/frontmatter.md`.

## Adding content

A rule: a file in `shared/rules/` (always on only if it belongs to the three above, otherwise with `paths:`) or `shared/rules/on-demand/`. A skill: a folder with `SKILL.md`.
An agent: one `.md` with `model`. Run `pytest -n auto tests`, then `scripts/sync.sh <repo>`.
