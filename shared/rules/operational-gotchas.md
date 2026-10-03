---
description: "Recurring mechanical traps a spoke hits on this host: merge origin/main before pushing, locale-sensitive system-tool output (LC_ALL=C), scratch files kept inside the worktree, and ship discipline. Teaches how to satisfy the guard hooks, never to bypass them."
paths:
  - '**'
---
# Operational Gotchas

Recurring mechanical traps a spoke hits while committing and pushing. Each entry teaches how to **satisfy** an
existing guard, not how to bypass it: the hooks stay authoritative. If a hook and this rule ever disagree, the
hook wins and this rule is stale.

## Merge `origin/<base>` before pushing when behind

When a push is rejected or the branch is behind, `git fetch origin` then `git merge origin/<base>`, resolve
conflicts, and push normally. The bug you are hitting may already be fixed on the base branch, and a sibling
task may have landed changes to the files your work touches. Never force-push, never `--no-verify` (`push-guard`
denies both), and never self-land: the coordinator lands.

## Commit messages

`commit-msg` checks a conventional type and a `#<n>` anchor. Pass the message inline with separate `-m` flags,
one subject line and one line per body paragraph, and avoid backticks inside a `-m` string (the shell eats them).

## Locale traps in system-tool output

This dev host runs a non-C, non-English locale. Any helper that parses system-tool output by English keyword must
force `LC_ALL=C`:

- **Never conclude "process not running" from a bare `pgrep -f`.** On non-ASCII argv it dies with "illegal byte
  sequence" and exits non-zero, so `pgrep -f foo || echo "(not running)"` reports a live process as dead. Use
  `LC_ALL=C pgrep -fl`, or check the port with `lsof -nP -iTCP:<port> -sTCP:LISTEN`.
- **`ps -o lstart=` is locale-formatted.** Parsing it without `LC_ALL=C` strands the epoch empty (no error), so any
  staleness check silently never fires. Force `LC_ALL=C` on both the `ps` read and the `date -j -f` conversion.
- A shell `$var` followed by a non-ASCII glyph (`$fails×`) is absorbed into the variable name under a non-UTF-8
  locale (`unbound variable`); write ASCII in shell comments and strings.

## Keep debug scratch inside the worktree

Write probe output, repro scripts, and temp dirs under your own worktree (`mktemp -d ./tmp.XXXXXX`, not the
`/tmp` default), never `/tmp/...`, `$HOME/...`, or a sibling worktree. `danger-guard` denies destructive commands
outside the worktree, so scratch elsewhere gets blocked for no benefit. In pytest, use the `tmp_path` fixture,
not a hard-coded `/tmp/<name>` path.

## Ship discipline

Push every subtask's branch without asking; send `worker_done` when the acceptance criteria are all met. Ask first
before genuinely irreversible operations: force-push, history rewrites, anything touching the base branch, or
deletions outside the worktree. A spoke never self-lands.
