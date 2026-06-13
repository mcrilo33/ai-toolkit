# Hooks Frontmatter Reference

Hook definitions live in `shared/hooks/metadata.yml`. The sync script reads
this file and generates platform-specific configurations for Copilot, Cursor,
and Claude.

## Metadata fields

| Field | Required | Description |
| --- | --- | --- |
| `event` | yes | Lifecycle event (canonical name, mapped per platform) |
| `matcher` | no | Tool name filter (`Bash`, `Edit\|Write`, etc.) |
| `if` | no | Claude-only fine-grained condition (permission rule syntax) |
| `description` | yes | Human-readable purpose |
| `tier` | no | Priority: 1 (high), 2 (medium), 3 (nice-to-have) |
| `timeout` | no | Max execution seconds (default: 30) |

## Hook inventory

All hooks defined in `shared/hooks/metadata.yml`:

| Hook | Event | Matcher | Tier | Description | Agent-scoped |
| ---- | ----- | ------- | :--: | ----------- | ------------ |
| `block-no-verify` | preToolUse | Bash | 1 | Block `git` commands with `--no-verify` or improper force pushes | — |
| `secrets-scan` | preToolUse | Write/Edit | 1 | Scan for hardcoded secrets (API keys, tokens) before writing | devops |
| `secrets-scan-revert` | postToolUse | Write/Edit | 1 | Revert a file whose edit introduced a hardcoded secret (Cursor `afterFileEdit`) | — |
| `git-push-review` | preToolUse | Bash | 2 | Show diff summary before `git push` for review | — |
| `push-scope-guard` | preToolUse | Bash | 1 | Spoke worktrees push only their own branch; the default branch is published by the hub (hard deny on Cursor, advisory elsewhere) | — |
| `config-protection` | preToolUse | Write/Edit | 2 | Block modification of linter, formatter, and CI config files | devops |
| `commit-quality` | preToolUse | Bash | 2 | Validate commit messages match conventional commits format | — |
| `commit-gauntlet` | preToolUse | Bash | 1 | Lint/typecheck staged files before commit; block on failure | — |
| `post-edit-format` | postToolUse | Edit/Write | 1 | Auto-format edited files using the project's configured formatter | debug, tdd-green, tdd-refactor, refactor |
| `quality-gate` | postToolUse | Edit/Write | 1 | Run linter and typechecker on edited files | debug, tdd-green, tdd-refactor, refactor |
| `console-log-warn` | postToolUse | Edit/Write | 2 | Warn when `console.log`, `print()`, or debug statements are added | debug |
| `red-proof-warn` | preToolUse | Bash | 2 | Flag pushed commits that add source with no `Tested-RED:` trailer | — |
| `reviewer-sep-warn` | preToolUse | Bash | 2 | On push: verify the signed APPROVE review artifact bound to the pushed diff (see [review-stamp.md](./review-stamp.md)) | — |
| `todo-ledger-warn` | preToolUse | Bash | 2 | On push: scan the session transcript for a ledger call (`TodoWrite`, `TaskCreate`, or `TaskUpdate`); warn (Claude) / hard-deny (Cursor) if absent. A `No-Ledger:` commit trailer bypasses the gate for single-step work | — |
| `todo-ledger-nudge` | sessionStart | — | 3 | At session start, nudge the agent to seed a task ledger (TodoWrite or Tasks) before the first commit (advisory; companion of `todo-ledger-warn`) | — |
| `delegation-gate-warn` | preToolUse | Bash | 2 | Delegation hints for specialist agents; enforced at the shipping gate | — |
| `review-stamp-guard` | beforeMCPExecution (Cursor-only) | — | 1 | Deny `approve_review` MCP calls outside an active code-review review-window (fail-closed) | — |
| `review-window-open` | subagentStart (Cursor-only) | — | 1 | Open the review window (`.review/.window`) when a code-review subagent starts | — |
| `review-window-close` | subagentStop (Cursor-only) | — | 1 | Close the review window when the code-review subagent stops | — |

The events/matchers above are the **canonical** (Claude/Copilot) wiring. On
Cursor these hooks are remapped onto **dedicated events** via per-hook `cursor:`
overrides — see [cursor-hooks-migration-plan.md](./cursor-hooks-migration-plan.md).

The **Agent-scoped** column shows which agents include the hook in their
frontmatter (runs only when that agent is active). Hooks without an agent scope
run globally via workspace-level hook files.

## Per-tool overrides

Nest overrides under `copilot:`, `cursor:`, or `claude:` to change any field
for a specific platform.

```yaml
secrets-scan:
  event: preToolUse
  matcher: "Write|Edit"
  description: "Scan for hardcoded secrets before writing files"
  copilot:
    matcher: "edit|create"   # Copilot uses lowercase tool names
```

## Event name mapping

The canonical event name in `metadata.yml` is mapped to each platform's
native event name by the sync script.

| Canonical | Copilot | Cursor | Claude |
| --- | --- | --- | --- |
| `preToolUse` | `preToolUse` | `preToolUse` | `PreToolUse` |
| `postToolUse` | `postToolUse` | `postToolUse` | `PostToolUse` |
| `sessionStart` | `sessionStart` | `sessionStart` | `SessionStart` |
| `sessionEnd` | `sessionEnd` | `sessionEnd` | `SessionEnd` |
| `stop` | `agentStop` | `stop` | `Stop` |
| `userPromptSubmit` | `userPromptSubmitted` | `beforeSubmitPrompt` | `UserPromptSubmit` |
| `preCompact` | *(unsupported)* | `preCompact` | `PreCompact` |
| `subagentStart` | *(unsupported)* | `subagentStart` | `SubagentStart` |
| `subagentStop` | `subagentStop` | `subagentStop` | `SubagentStop` |

### Cursor dedicated events

Cursor also exposes dedicated events that carry the real command/file payload
(the generic `preToolUse`/`postToolUse` path delivers an internal scratch
payload). A hook opts into one via a per-hook `cursor: event:` override; the
matcher is then a **command regex** (`beforeShellExecution`) or a dedicated tool
token (`afterFileEdit`), not a `preToolUse` tool name.

| Cursor event | Input (top-level) | Can block? | Agent-visible output |
| --- | --- | :--: | --- |
| `beforeShellExecution` | `command`, `cwd`, `sandbox`, `workspace_roots` | yes | `agent_message` on deny |
| `afterFileEdit` | `file_path`, `edits[]`, `workspace_roots` | no | none |
| `beforeReadFile` | `file_path`, `content`, `attachments` | yes (reads only) | `user_message` |
| `beforeMCPExecution` | `tool_name`, `tool_input`, `url`/`command` (server) | yes | `agent_message` on deny |

`beforeMCPExecution` hooks can set `failClosed: true` so that a hook error
denies the call instead of allowing it — used by `review-stamp-guard.sh`.
Note the payload carries **no agent identity**: a guard can gate on state
(e.g. an active review window) but cannot identify the calling agent.

The `subagentStart` / `subagentStop` events (already in the mapping table
above) are used by `review-window-open.sh` / `review-window-close.sh` to
maintain the code-review review-window state file (`.review/.window`,
30-minute TTL) — see [review-stamp.md](./review-stamp.md).

## Generated output per platform

### Copilot

**Location**: `.github/hooks/ai-toolkit.json`

```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [
      {
        "type": "command",
        "bash": "./.github/hooks/scripts/block-no-verify.sh",
        "timeoutSec": 30
      }
    ]
  }
}
```

- Scripts are copied to `.github/hooks/scripts/`
- JSON uses `{"type": "command", "bash": "..."}` format
- No matcher support in Copilot — filtering is done inside the script

### Cursor

**Location**: `.cursor/hooks.json`

```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [
      {
        "command": "./.cursor/hooks/scripts/block-no-verify.sh",
        "matcher": "Shell"
      }
    ]
  }
}
```

- Scripts are copied to `.cursor/hooks/scripts/`
- JSON uses `{"command": "...", "matcher": "..."}` format
- Matchers filter by tool name (`Shell`, `Read`, `Write`, etc.)

### Claude

**Location**: `.claude/settings.json` → `hooks` key

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/scripts/block-no-verify.sh",
            "if": "Bash(git * --no-verify *)"
          }
        ]
      }
    ]
  }
}
```

- Scripts are copied to `.claude/hooks/scripts/`
- JSON nests handlers inside matcher groups
- Supports `if` field for fine-grained matching (permission rule syntax)
- Uses `$CLAUDE_PROJECT_DIR` for path resolution

## Hook script contract

All hook scripts follow the same stdin/stdout protocol:

1. **Input**: JSON on stdin describing the event (tool name, args, etc.)
2. **Output** (preToolUse only): JSON on stdout with permission decision
3. **Exit codes**:
   - `0` — allow / success
   - `2` — block the action (Claude/Cursor preToolUse)
   - other — non-blocking error

### Input fields (preToolUse)

| Platform | Tool name field | Tool args field |
| --- | --- | --- |
| Copilot | `toolName` | `toolArgs` (JSON string) |
| Cursor | `tool_name` | `tool_input` (object) |
| Claude | `tool_name` | `tool_input` (object) |

### Output fields (preToolUse deny)

| Platform | Format |
| --- | --- |
| Copilot | `{"permissionDecision": "deny", "permissionDecisionReason": "..."}` |
| Cursor | `{"permission": "deny", "user_message": "...", "agent_message": "..."}` |
| Claude | exit code 2 + stderr message, or `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}` |

## Directory structure

```text
shared/hooks/
├── metadata.yml        # Hook registry (source of truth)
├── lib/
│   └── utils.sh        # Shared shell utilities
├── block-no-verify.sh  # PreToolUse: block --no-verify
├── secrets-scan.sh     # PreToolUse: detect hardcoded secrets
├── post-edit-format.sh # PostToolUse: auto-format
├── quality-gate.sh     # PostToolUse: lint + typecheck
├── desktop-notify.sh   # Stop: macOS notification
└── ...
```

After sync, scripts are copied to:

- `.github/hooks/scripts/` (Copilot)
- `.cursor/hooks/scripts/` (Cursor)
- `.claude/hooks/scripts/` (Claude)
