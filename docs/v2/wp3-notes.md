# v2 WP3 notes (hooks + settings)

Deny = exit 2 + stderr; an ERR trap makes every crash (bad JSON, **missing jq**) exit 2 too (any other code is non-blocking
in Claude Code). Hooks are self-contained, bash 3.2 (tests run them with `/bin/bash`).

## Decisions (coordinator-approved in the plan)
- Hooks live at `<target>/.claude/hooks/<name>.sh` (sync copies `hooks/claude/*`; setup.sh copies `.claude/` into worktrees).
  Registered as `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh"`: no exec bit needed, so `chmod -x` cannot disarm a guard.
- `AskUserQuestion` is denied **in danger-guard, only when `$CLAUDE_PROJECT_DIR/.ai-toolkit/spoke-run-id` exists** (not in
  settings.json), so the human's own sessions keep it. Same marker gates the spoke-only protection of the project
  `.claude/settings.json`, `settings.local.json` and `.claude/hooks/` (Write/Edit/Bash writes, incl. chmod/rm/cd into them).
- `rm -r` is allowed strictly below `/tmp`, `/private/tmp`, `$TMPDIR` (scratch cleanup); never the worktree root, `$HOME` or
  an unresolvable `$VAR`/`~user` target. `git reset --hard` is denied only in the main checkout (also with `--work-tree`).
- `pre-commit` escape hatch `AI_TOOLKIT_ALLOW_BASE_COMMIT=1`; a repo's first commit passes. Base branch = `$BASE_BRANCH`,
  else `origin/HEAD`, else `main` (the hooks do not read `ai-toolkit.local.env`).
- macOS is case-insensitive: the guards use `nocasematch` (`GIT push`, `ORCA.YAML`, `.GitHub/Workflows` are caught).

## Deviations
- secrets-scan: same pattern *set* but one regex (ghp/gho/ghs and sk_live/pk_live merged; `LANGFUSE_BASIC_AUTH[^B]{0,4}Basic`);
  Write/Edit/MultiEdit/NotebookEdit only (matchers in settings.json); the reason never echoes the secret; settings.json: no model/effort defaults.
- Extra denies: `core.hooksPath` edits, `commit -n`, `push --all/--mirror`, glob refspecs.
- Sizes: claude hooks 134/140, git hooks 19/40, settings 18/40. `test_hooks.py` is 307 lines vs ~250 (215 cases, ~60 bypass variants).

## Known limits (a deny-list over command text, D8: not a sandbox)
- Indirection the text hides: `g=git; $g push -f`, `git push $(echo origin) main`, `${IFS}`, `$'\x67it'`, `eval`, base64, `python -c
  'subprocess...'`, a script file written earlier and run later (only its *writing* to a protected path is checked), git aliases.
- State changes: `cd dir` then a *relative* `rm -rf`/write (only `cd` into a protected dir is caught); `find / -delete`, `xargs rm -rf`,
  `rsync --delete`, `mv x /dev/null`; `git update-ref`/`branch -f` on the base; `gh pr merge`, `gh api .../git/refs`; `GIT_DIR=` prefix
  on `reset --hard`; patch files (`patch < x.diff` where the path is inside the diff); writes through MCP tools.
- Over-denies on purpose: `git push -f` text inside an `echo`/commit message, `cp orca.yaml /tmp/x`. Secret reads, exfiltration
  and the user's `gh` token (D9) are out of scope: stronger isolation is the D8 caveat.

## For later WPs
- WP4 sync: copy `hooks/claude/*.sh` to `.claude/hooks/` and `settings/claude/settings.json` to `.claude/settings.json`.
- install.sh (+ setup.sh): check `jq` is on PATH (without it every guarded tool call is blocked) and set the global
  `core.hooksPath` to `<toolkit>/hooks/git` (it replaces per-repo `.git/hooks`). Coordinator is routing both.
- e2e step 2 commits the sync output on main of the scratch repo: set `AI_TOOLKIT_ALLOW_BASE_COMMIT=1` or use a branch.
  A `--no-ff` merge commit does not run pre-commit and `Merge ...` passes commit-msg.
- Export `BASE_BRANCH` in `bin/claude-spoke` if a base is not `origin/HEAD`. [verify live] PreToolUse fires for `AskUserQuestion`
  and in Orca `--dangerously-skip-permissions` sessions (unit tests feed payloads).
