# v2 WP3 notes (hooks + settings)
Deny = exit 2 + stderr; an ERR trap (`set -E`, so also inside functions) makes every crash (bad JSON, **missing jq**) exit 2
too (any other code is non-blocking in Claude Code). Hooks are self-contained, bash 3.2 (tests run them with `/bin/bash`).

## Decisions (coordinator-approved in the plan)
- Hooks live at `<target>/.claude/hooks/<name>.sh` (sync copies `hooks/claude/*`; setup.sh copies `.claude/` into worktrees).
  Registered as `bash "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh"`: no exec bit needed, so `chmod -x` cannot disarm a guard.
- `AskUserQuestion` is denied **in danger-guard, only when `$CLAUDE_PROJECT_DIR/.ai-toolkit/spoke-run-id` exists** (not in
  settings.json), so the human's own sessions keep it. Same marker gates the spoke-only protection of the project
  `.claude/{settings.json,settings.local.json,hooks}`, `.ai-toolkit/spoke-run-id`, and rm/mv of `.claude`/`.ai-toolkit` whole.
  Write/Edit are checked on the canonical path; Bash only as text: a write verb or `cd` *before* the path in one segment.
- The root is `$CLAUDE_PROJECT_DIR` (else the git toplevel), so a `cd` out of the repo cannot move it. orca.yaml, `.github/workflows/`
  and `.claude/*` match only there (`v2/orca.yaml` is fine); `~/.claude/settings.json` always.
- `rm -r` is allowed strictly below `/tmp`, `/private/tmp`, `$TMPDIR` (scratch cleanup); never the worktree root, `$HOME` or
  an unresolvable `$VAR`/`~user` target (trailing slashes and absent targets are handled). `git reset --hard` is denied only in
  the main checkout (also with `--work-tree`); `git clean` with x/X is denied everywhere (it deletes the ignored `.claude/`).
- `pre-commit` escape hatch `AI_TOOLKIT_ALLOW_BASE_COMMIT=1`; a first commit passes. Base = `$BASE_BRANCH`, else `origin/HEAD`,
  else `main` (`ai-toolkit.local.env` is not read).
- macOS is case-insensitive: the guards use `nocasematch` (`GIT push`, `ORCA.YAML`).

## Deviations
- Extra denies: `core.hooksPath` edits, `commit -n`, `push --all/--mirror`, glob refspecs. commit-msg skips only the `#N` rule on
  `quick-*` branches (the /quick lane). secrets-scan: same patterns in one regex (+ `sk-ant-`), write tools only; no model defaults in settings.
- Sizes: claude hooks 139/140, git hooks 20/40, settings 18/40. `test_hooks.py` 335 lines vs ~250 (~260 cases).
## Known limits (a deny-list over command text, D8: not a sandbox)
- Indirection the text hides: `g=git; $g push -f`, `git push $(echo origin) main`, `${IFS}`, `$'\x67it'`, `eval`, base64, `python -c
  'subprocess...'`, a script written earlier and run later (only its *writing* to a protected path is checked), git aliases.
- State changes: `cd dir` then a *relative* `rm -rf`/write (only `cd` straight into a protected dir is caught; a Bash write
  with no verb in the same segment, e.g. `exec 3<>`, `awk -i inplace`, `sponge`, `ed`, is missed); `find / -delete`, `xargs rm -rf`,
  `rsync --delete`, `mv x /dev/null`; `git update-ref`/`branch -f` on the base; `gh pr merge`, `gh api .../git/refs`; `GIT_DIR=` on
  `reset --hard`; paths inside a patch file; writes through MCP tools.
- Over-denies on purpose: `git push -f` text in an `echo`/commit message, `cp orca.yaml /tmp/x`. Secret reads, exfiltration and
  the user's `gh` token (D9) are out of scope (the D8 caveat).

## For later WPs
- WP4 sync: `hooks/claude/*.sh` to `.claude/hooks/`, `settings/claude/settings.json` to `.claude/settings.json`. install.sh
  (+ setup.sh): check `jq` (without it every guarded tool call is blocked); global `core.hooksPath` = `<toolkit>/hooks/git`.
- e2e step 2 commits on main of the scratch repo: use `AI_TOOLKIT_ALLOW_BASE_COMMIT=1` or a branch (merge commits skip pre-commit).
- Export `BASE_BRANCH` in `bin/claude-spoke` if the base is not `origin/HEAD`. [verify live] PreToolUse fires for `AskUserQuestion`
  and in Orca yolo sessions (tests feed payloads).
