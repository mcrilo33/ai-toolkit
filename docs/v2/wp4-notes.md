# v2 WP4 notes (sync + policy, Claude-only)

## Decisions

- **Rule routing** (Claude Code loads every `.claude/rules/*.md` unless it has `paths:`; `alwaysApply`/`applyTo`/`globs` are Cursor/Copilot
  syntax and are gone): always-on = no `paths:` (`security`, `agent-orchestration`, `scientific-integrity`); conditional = `paths:` list;
  `guidelines.md` becomes `CLAUDE.md` (frontmatter stripped); on-demand rules live in `shared/rules/on-demand/` and sync to
  `.ai-toolkit/rules/` (never auto-loaded). Skills and `answer.sh` point at them by path. The lint pins this.
- **Prompts** go to `.claude/commands/` (slash commands); the only one, `commit-msg`, is not Copilot-specific, so it stays.
- **Agents**: `model`/`effort` from the old `ai-toolkit.yml` routing (Fable 5.1 architect/planner; Opus 5.5 debug, security-reviewer,
  code-review, tdd-red, devops, both scopers; Sonnet 5.5 tdd-green/refactor/refactor/documentation). Dropped `handoffs`, `argument-hint`,
  `hooks` (pointed at deleted hooks), `mcpServers: review-stamp`. `disallowedTools` is now real (`Edit, Write, NotebookEdit`) on the six
  read-only agents; v1 carried Copilot tool names that Claude Code silently ignored, so those agents were never read-only.
- Skills drop `allowed-tools` (Copilot names, no-ops in Claude). `hub/scripts/` is deleted so sync cannot ship the drain.
- **sync.sh** (113 lines): marker block in `.git/info/exclude` (`/.ai-toolkit/`; `--local-only` adds `.claude/ CLAUDE.md orca.yaml`), manifest
  `.ai-toolkit/sync-manifest`, cmp-before-write (no mtime churn), GC ignores absolute/`..` entries, unmanaged `CLAUDE.md`/`settings.json`/`orca.yaml`
  are kept once as `.bak`. Generated `orca.yaml` runs `bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/{setup,archive}.sh"` (WP0 finding).
- **Added, not in the brief**: `--migrate-v1` removes the files a v1 sync listed in `.ai-toolkit-manifest.json` (`.cursor/`, `.github/`, v1
  `.claude/hooks`). `hex` has one (154/165/163 paths); without the flag sync only warns. Opt-in because D4 says check `hex` first.
- `frontmatter.py` and `ci.yml` (06 section 8) are not built: D4 removed the projection; D7 has no CI until cutover (WP6).

## Findings for later WPs

- WP3 (per its follow-up): `v2/hooks/claude/` syncs to `.claude/hooks/` (setup.sh copies only `.claude/`), `v2/hooks/git/` to
  `.ai-toolkit/hooks/git/`; `settings.json` is read from `v2/settings/claude/` (missing = warning). `install.sh` now sets `core.hooksPath` to
  `<here>/hooks/git` for the repo it runs in and explains the jq hard requirement. Both are tested with copies/stubs, not WP3's real files.
- WP1/WP2: the skills name these interfaces, so keep them: `dispatch.sh <n>|--next`, `land.sh <n> [--local-gate]`, `coordinator.sh --status|--answer
  auto|human|--until|--drain|--cap`, `review.sh <n>`, `orca terminal create --command` for the coordinator. `answer.sh` reads
  `.ai-toolkit/rules/afk-answering.md`; its last lines are `EVIDENCE/REVERSIBILITY/WARN/ANSWER`, and `ANSWER:` is `approve` or `revise: ...`
  (`gate_answer_class` approve=1, revise=2). The `code-review` final message is exactly one JSON object `{verdict, blockers, warnings,
  tdd_followed, tests_weakened, summary}`; `review.sh` should reject anything else.
- WP6: `--local-only` leaves `orca.yaml` untracked; whether Orca reads an untracked `orca.yaml` for a new worktree is [verify]. Not verified live:
  a Claude session in a synced target loading the rules/skills/agents/commands (YAML-list `paths:`, comma-string `disallowedTools`). Cutover also
  needs `docs/frontmatter.md` and a `ci.yml` step running sync twice into a temp repo.
- Policy size: 11.2k lines in `shared/` (incl. skill scripts) against the 8k target; generic skills (api/docs/tests/docker/db/deploy/backend/
  frontend: ~1.9k) and python-style (413) dominate, untouched here. Pruning them is a separate decision.

## Budgets (lines)
sync.sh 114/200, install.sh 32/40 · test_sync.py 214 + test_policy_lint.py 151 + test_install_hooks.py 41 = 406/450 · solo-cycle 95/120 · hub 58/60 · afk 40/40 · land 23/30 · start-task 56/60 ·
quick 20/30 · workflow 70/100 · planning-hub 39/50 · afk-answering 73/120 · code-review 88/110. `pytest -n auto tests_v2`: 105 tests, ~3 s.
