# Cursor Hooks Migration Plan — dedicated events (Cursor-only)

> Hand this file to an agent working **in the `ai-toolkit` repo**. It is the
> single source of truth for migrating the Cursor hook wiring from the generic
> `preToolUse`/`postToolUse` events to Cursor's **dedicated** events. Claude and
> Copilot wiring stay unchanged.

## Why (root cause, empirically proven)

In Cursor `3.7.21`, hooks registered on the **generic** `preToolUse`/`postToolUse`
events with `Shell`/`Write` matchers receive the runtime's **internal scratch
payload**, not the user's real file/command:

- A `postToolUse`+`Write` hook saw `tool_input.file_path` =
  `~/.cursor/projects/<proj>/agent-tools/<uuid>.txt` (an internal scratch file
  containing unrelated content), NOT the `.py` the agent actually wrote. So
  `quality-gate`/`post-edit-format`/`console-log-warn` ran against the wrong file
  and no-op'd; the real edit was never formatted/linted.
- `preToolUse`+`Shell` hooks computed on a re-serialized/wrong command payload and
  silently no-op'd. Only **exit-2 blockers** (block-no-verify, secrets-scan,
  config-protection) appeared to work, because exit 2 blocks regardless of payload
  correctness.

A live probe (temporary audit hook dumping raw stdin) **confirmed the dedicated
events carry the real data**:

- `beforeShellExecution` stdin: `{"command":"<verbatim real command>","cwd":"","sandbox":true,"workspace_roots":["<repo>"],...}`
  - NOTE: `command` is **top-level** (no `tool_input` wrapper). `cwd` was **empty** —
    do NOT rely on it; use `workspace_roots[0]` / `$CURSOR_PROJECT_DIR`.
- `afterFileEdit` stdin: `{"file_path":"<real abs path>","edits":[{"old_string":"","new_string":"<real content>"}],"workspace_roots":["<repo>"],...}`
  - NOTE: `file_path` + `edits[]` are **top-level**. Real path (not scratch). Real content in `new_string`.

## Hard schema constraints (Cursor docs v3.7.21) — these drive the design

| Event | Input | Can block? | Agent-visible output |
| --- | --- | --- | --- |
| `beforeShellExecution` | `{command, cwd, sandbox}` | YES — `{"permission":"deny","user_message","agent_message"}` or exit 2 | `agent_message` **on deny only** |
| `afterFileEdit` | `{file_path, edits:[{old_string,new_string}]}` | **NO** | **None** ("No output fields currently supported") |
| `beforeReadFile` | `{file_path, content, attachments}` | YES (deny) | `user_message` only; gates **reads**, not writes |

Two capabilities have **no dedicated-event equivalent**:
1. **No pre-write block for agent edits.** `beforeReadFile` blocks reads only.
2. **`afterFileEdit` cannot message the agent.**

## What was implemented

This migration is **Cursor-only**. Claude and Copilot wiring (top-level
`event`/`matcher`/`if` in `shared/hooks/metadata.yml`) is unchanged; the Cursor
remap lives entirely in per-hook `cursor:` overrides. The same shell scripts run
on every platform and branch on `hook_event_name` to pick the right behavior.

### Event remap (per-hook `cursor:` override in `metadata.yml`)

| Hook | Canonical (Claude/Copilot) | Cursor event | Cursor matcher |
| --- | --- | --- | --- |
| `block-no-verify` | preToolUse / Bash | `beforeShellExecution` | *(none — self-filters)* |
| `commit-quality` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +commit( \|$)` |
| `commit-gauntlet` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +commit( \|$)` (timeout 60) |
| `secrets-scan` | preToolUse / Write\|Edit | `beforeShellExecution` | `git( +-… )* +(add\|commit)( \|$)` |
| `secrets-scan-revert` *(new)* | postToolUse / Write\|Edit | `afterFileEdit` | `Write\|TabWrite` |
| `config-protection` | preToolUse / Write\|Edit | `beforeShellExecution` | `git( +-… )* +(add\|commit)( \|$)` |
| `delegation-gate-warn` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +push( \|$)\|gh +pr( \|$)` |
| `git-push-review` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +push( \|$)\|gh +pr( \|$)` |
| `red-proof-warn` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +push( \|$)\|gh +pr( \|$)` |
| `reviewer-sep-warn` | preToolUse / Bash | `beforeShellExecution` | `git( +-… )* +push( \|$)\|gh +pr( \|$)` |
| `post-edit-format` | postToolUse / Edit\|Write | `afterFileEdit` | `Write\|TabWrite` |
| `quality-gate` | postToolUse / Edit\|Write | `afterFileEdit` | `Write\|TabWrite` |
| `console-log-warn` | postToolUse / Edit\|Write | `afterFileEdit` | `Write\|TabWrite` |

The `git( +-… )*` fragment above is shorthand for
`git( +-[^ ]+| +-C +[^ ]+)*` — it tolerates intervening git global options.

> [!IMPORTANT]
> On `beforeShellExecution` the `matcher` is a **command regex** that Cursor
> evaluates **before** invoking the script — if it does not match, the script
> never runs. So **correctness depends on the matcher**, contrary to a naive
> "the script rechecks anyway" assumption. A literal matcher like
> `git add|git commit` is silently bypassed by the very common `git -C <path>
> add` / `git --no-pager commit` forms (confirmed in live testing — those
> commands sailed past secrets-scan / config-protection / commit-quality). The
> matchers therefore tolerate intervening git global options:
> `git( +-[^ ]+| +-C +[^ ]+)* +(add|commit)( |$)` and the push/`gh pr`
> equivalent. The scripts *also* re-check internally (`is_git_commit_or_add`),
> but that is defense-in-depth, **not** a substitute for a correct matcher. A
> regression test (`TestCursorMatchersResistBypass`) locks this in.
>
> On `afterFileEdit` the matcher is the dedicated tool token (`Write|TabWrite`).
> The generator skips the `preToolUse`/`postToolUse` tool-name translation
> (`Bash->Shell`, `Edit->Write`) for these dedicated events.

### Cross-platform gating

The discriminator is the top-level `hook_event_name` field, which Cursor sets on
dedicated events and Claude/Copilot do not. `lib/utils.sh` adds:

- `get_hook_event` / `on_cursor_dedicated_event` — detect the dedicated event.
- `get_shell_command` — top-level `.command`, else `tool_input.command`.
- `get_edit_file_path` — top-level `.file_path`, else `tool_input.file_path`.
- `get_edit_new_content` — concat `.edits[].new_string`, else
  `tool_input.content // .new_string`.
- `project_root_from_payload` — `$CURSOR_PROJECT_DIR`, else
  `.workspace_roots[0]`, else `find_project_root` (never `cwd`, which is empty).
- `is_agent_tools_path` — true for the runtime's internal
  `*/.cursor/*/agent-tools/*` scratch path (scripts early-exit on it).
- `scan_for_secret` / `secret_patterns` — shared secret detection.
- `ship_gate_enforce` — DENY on `beforeShellExecution`, else `warn` (advisory).

### Behavior by hook

- **secrets-scan**: `beforeShellExecution` on `git add`/`git commit` scans staged
  content (`git diff --cached --text`, added lines only) and DENIES with the
  reason in `agent_message`. The `git add`/`commit` recognizer
  (`is_git_commit_or_add` in `lib/utils.sh`) matches the subcommand anywhere in
  the command, so chained/prefixed forms (`cd sub && git add`, `git -C path
  commit`) are not bypassed. `--text` forces a textual diff so a secret cannot
  hide behind a binary-diff suppression. The Claude/Copilot pre-write
  `tool_input` scan is retained.
- **secrets-scan-revert** *(new)*: `afterFileEdit` only — best-effort early
  containment. It is **fail-safe by design** (no data loss):
  1. It writes a timestamped backup (`<file>.secret-revert.<ts>.bak`) **before**
     any mutation, so the user can always recover.
  2. Containment is **surgical** — it removes only the on-disk line(s) that
     actually match a secret pattern, leaving all unrelated content untouched
     (it never reverses edits blindly, and never runs `git checkout`/`rm`).
  3. It only persists the redaction if the result is provably clean; otherwise
     it leaves the file as-is (backed up) and logs loudly — the commit-time
     `secrets-scan` deny is the backstop.
  It preserves the original trailing-newline state, logs to the Hooks output
  channel, and is a no-op on a clean edit, a scratch path, or any
  non-`afterFileEdit` event.
- **config-protection**: `beforeShellExecution` on `git add`/`git commit` checks
  staged files (`git diff --cached --name-only`) against the protected list and
  DENIES. The Write wiring is gone for Cursor; Claude/Copilot keep the pre-write
  check.
- **delegation / git-push-review / red-proof / reviewer-sep**: promoted to a hard
  DENY at the shipping gate (`git push` / `gh pr create|merge`) **on Cursor
  only**, via `ship_gate_enforce`. On Claude/Copilot and native git hooks they
  remain advisory (`warn`, exit 0). `git-push-review`'s gate is force-push
  without `--force-with-lease`; its `git` calls resolve the repo via
  `project_root_from_payload` (Cursor reports an empty `cwd`), not the ambient
  working directory.
- **post-edit-format / quality-gate / console-log-warn**: run on `afterFileEdit`
  using the real `file_path`/content, guarded by `is_agent_tools_path`. Because
  `afterFileEdit` has no output channel, on Cursor their findings are
  side-effects/logs only — the agent is not messaged. Enforcement the agent must
  act on stays at the blocking `commit-gauntlet`.

### Script path format (warning-free)

`hooks_generator.py` emits Cursor hook commands as **root-relative paths without
a leading `./`** (`.cursor/hooks/scripts/foo.sh`). Cursor's docs require this for
project hooks (their working directory is the project root). The previous
`./.cursor/...` form still executed, but tripped a **false-positive warning icon**
next to every hook in the Hooks settings UI. The reconciler's ownership marker is
the `hooks/scripts/` substring, so existing `./`-prefixed owned entries are still
recognized and cleaned up on the next sync.

### Reconciler migration cleanup

`scripts/hooks_reconciler.py` now purges ai-toolkit-**owned** entries from event
buckets it no longer emits into (`_purge_owned_from_unemitted`), so a hook that
moved `preToolUse -> beforeShellExecution` does not linger in the old bucket and
fire on both the scratch and the real event. User-authored hooks are preserved.

### `.gitignore` exception for the secrets-scan scripts

`shared/.gitignore` carries a broad `*secret*` rule (mandatory secrets guard).
That rule also matches the legitimate hook **source** scripts
`hooks/secrets-scan.sh` and `hooks/secrets-scan-revert.sh`, which would silently
prevent the new revert script from being committed/synced. Two scoped negations
(`!hooks/secrets-scan.sh`, `!hooks/secrets-scan-revert.sh`) un-ignore exactly
those two code files; the broad `*secret*` rule still guards every other path.

## Regenerate

```bash
# Cursor-only (this repo's own cage or any target):
./scripts/sync-to-repo.sh <target-repo> cursor
```

Claude/Copilot configs are only touched when `claude`/`copilot`/`all` is passed,
so a `cursor` sync provably cannot alter `.claude/settings.json` or
`.github/hooks/ai-toolkit.json`. Their generated event names are unchanged
(`PreToolUse`/`PostToolUse`, `preToolUse`/`postToolUse`).

## Lost / degraded behaviors (conscious sign-off)

1. **No pre-write block of secrets/config on Cursor.** The generic Write path
   delivered a scratch payload, so a reliable pre-write block was impossible.
   Enforcement moved to **commit time** (`git add`/`git commit` staged scan).
   `secrets-scan-revert` adds best-effort `afterFileEdit` containment.
2. **`quality-gate` / `console-log-warn` warnings are no longer agent-visible on
   Cursor.** `afterFileEdit` has no output channel; they log to the Hooks output
   only. The blocking `commit-gauntlet` remains the agent-facing enforcement.
3. **`red-proof` / `reviewer-sep` / `delegation` / `git-push-review` now BLOCK
   pushes/PRs on Cursor** when their conditions are unmet (missing `Tested-RED:`
   / `Reviewed-by:` trailers, unplanned multi-file change, force-without-lease).
   This is a deliberate behavior change from advisory-only. Claude/Copilot and
   native git hooks keep the advisory behavior.
4. **`afterFileEdit` hooks may not fire on subagent (Task-tool) edits.** Live
   testing confirmed `beforeShellExecution` hooks fire on a subagent's shell
   commands (secrets-scan, config-protection, commit-quality, block-no-verify,
   the push gates all denied real subagent `git` invocations). However, a
   subagent editing a file via the Write tool did **not** produce an observable
   `afterFileEdit` side effect (no reformat). It is unresolved whether Cursor
   delivers `afterFileEdit` to subagent edits at all, or whether it no-op'd. This
   only affects the best-effort side-effect hooks (post-edit-format, quality-gate,
   console-log-warn, secrets-scan-revert) which are already non-blocking and have
   no agent-visible output. **No blocking guarantee is lost**: a hardcoded secret
   is still caught by the commit-time `secrets-scan` deny before it can be
   committed (verified under a subagent). The scripts themselves are correct (they
   format/redact when invoked directly with a real `afterFileEdit` payload); the
   open question is purely Cursor's event delivery to subagents.

## Verification

```bash
# commit-quality denies a bad message on the dedicated event:
printf '%s' '{"hook_event_name":"beforeShellExecution","command":"git commit -m \"wip\"","workspace_roots":["'"$PWD"'"]}' \
  | shared/hooks/commit-quality.sh; echo "exit=$?"   # deny / exit 2

# post-edit-format reformats the real file on the dedicated event:
printf '%s' '{"hook_event_name":"afterFileEdit","file_path":"'"$PWD"'/t.py","edits":[{"old_string":"","new_string":"x=1\n"}]}' \
  | shared/hooks/post-edit-format.sh; echo "exit=$?"  # file reformatted to "x = 1"
```

Unit + integration tests: `pytest tests` (covers accessors, the agent-tools
guard, commit-time secrets/config deny, the revert, shipping-gate promotion, the
generator event override, and the reconciler purge).
