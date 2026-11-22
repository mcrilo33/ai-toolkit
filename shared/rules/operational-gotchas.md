---
description: "Recurring mechanical traps a spoke hits on this host: merge origin/main before pushing, locale-sensitive system-tool output (LC_ALL=C), scratch files kept inside the worktree, restarting an expired gcloud or aws sso login yourself, landing inline work through land.sh --review when no coordinator runs, and ship discipline. Teaches how to satisfy the guard hooks, never to bypass them."
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

`commit-msg` also rejects a commit whose type is not `test` that adds, changes, deletes or renames a test file (`TEST_PATHS`),
naming the paths. Satisfy it by splitting: stage the implementation by path (never `git add .`) and put the test change in its
own `test(<scope>): ... #<n>` commit. A cross-cutting rename that must change src and tests together may carry a
`Test-Change: <why>` line in the message. `land.sh` rechecks every pushed commit and refuses (exit 2) one that bypassed the hook.

## Landing inline work when no coordinator runs

When an urgent toolkit fix is done inline because no coordinator terminal runs (`dispatch.sh` needs `ORCA_TERMINAL_HANDLE`),
`land.sh --review <n>` still lands it with every gate, provided the work sits in an Orca worktree linked to the issue:

- Do the work in `orca worktree create --name <n>-<slug> --issue <n> --setup skip`, not on a branch of the main checkout. If a branch
  already exists, put the main checkout back on the base and attach it with `orca worktree create --name <n>-<slug> --branch <name> --issue <n> --setup skip` (it checks out the
  existing branch; `land.sh` needs the issue link).
- Commit with the usual split (`test:` commits for test files), push the branch, and let CI run.
- From the main checkout, on the base and clean, run `bash .ai-toolkit/scripts/land.sh --review <n>`. Exit 6 whose only warning is
  `no dispatch found ... worker-release skipped` is expected: there was no worker to release.
- Never push the base directly and never `gh pr merge`: both skip the recorded review, the commit recheck and the refresh of the installed copies.

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

Write probe output, repro scripts, and temp dirs under your own worktree (a fixed-name dir `./tmp.<name>/` such as `mkdir -p ./tmp.home`, a single file as
`./tmp.<name>` such as `tmp.suite.out`, not the `/tmp` default) or in the session scratchpad the system prompt names, never a hard-coded `/tmp/<name>`, `$HOME/...`,
or a sibling worktree. When removing a scratch dir with `rm -r`, write the path literally (`rm -r ./tmp.home`, or the scratchpad's path copied
from the system prompt), never through a shell variable, `$(...)` or `$PWD/...` (`S=...; rm -rf "$S"`): `danger-guard` asks on an unexpanded
`rm -r` target, and in auto mode that ask blocks the issue; removing a `./tmp.*` dir is optional, since `/tmp.*` is git-ignored and goes away with the worktree. In pytest, use the `tmp_path` fixture, not a hard-coded
`/tmp/<name>` path.

For a read-only look at another revision, use `git show <rev>:<path>`, `git diff <a> <b> -- <path>` or `git archive <rev> | tar -x -C <scratch>`,
never `git worktree add`: it registers in the main repo, shows in Orca as a project worktree, and outlives the session. If a full checkout is
unavoidable, `git worktree remove --force <literal path>` it in the same step that used it.

## Expired cloud logins: gcloud and aws sso

Where interactive 2-step reauthentication is enforced, gcloud tokens expire and a refresh fails in a non-interactive shell with `Reauthentication failed. cannot prompt during non-interactive execution`. When that happens, or when `gcloud auth print-access-token --quiet` fails before a GCP task: run `gcloud auth login --update-adc` yourself in the background, poll `gcloud auth print-access-token --quiet` until it succeeds, then carry on. The user validates the browser prompt; do not ask them to run the command and do not stop to ask; a spoke has no attended chat, so its `ask` only names the URL or device code to validate (`solo-cycle`). `--update-adc` refreshes the Python ADC in the same flow. If the token is still invalid after about 5 minutes, stop polling and say so.

AWS SSO is the same pattern: when `aws sts get-caller-identity --profile <p>` reports an expired token, start `aws sso login --profile <p> --no-browser` in the background, let the human validate the URL and device code, and poll `aws sts get-caller-identity --profile <p>` until it succeeds. This says how to start the flow; `solo-cycle` says how to reach the human (through its `ask`).

## Ship discipline

Push every subtask's branch without asking; send `worker_done` when the acceptance criteria are all met. Ask first
before genuinely irreversible operations: force-push, history rewrites, anything touching the base branch, or
deletions outside the worktree. A spoke never self-lands.
