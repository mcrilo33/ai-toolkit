# Agents frontmatter fields

Correspondence between `metadata.yml` fields and what each platform expects.

## Field reference

| Field | Copilot | Cursor | Claude Code | Type | Description |
| ----- | :-----: | :----: | :---------: | ---- | ----------- |
| `name` | ✅ Optional (defaults to filename) | ✅ Optional (inferred from filename) | ✅ Required | `string` | Display name / identifier. |
| `description` | ✅ Required | ✅ Optional | ✅ Required | `string` | Description for routing and auto-discovery. |
| `model` | ✅ Optional (`string \| string[]`) | ✅ Optional (`inherit` / `fast` / model-id) | ✅ Optional (`sonnet` / `opus` / `haiku` / model-id / `inherit`) | `string \| string[]` | Dedicated LLM model. Copilot supports a prioritized array. |
| `tools` | ✅ Optional (YAML array or aliases) | — | ✅ Optional (inherits all if omitted) | `string[]` | Accessible tools. |
| `disallowedTools` | ✅ Optional | — | ✅ Optional | `string[]` | Blocked tools. |
| `user-invocable` | ✅ Optional (default `true`) | — | — | `boolean` | Visible in the agent picker. |
| `disable-model-invocation` | ✅ Optional (default `false`) | — | — | `boolean` | Prevents auto-loading by the agent. |
| `target` | ✅ Optional (`vscode` / `github-copilot`) | — | — | `string` | Target environment. |
| `argument-hint` | ✅ Optional | — | — | `string` | Hint text in the chat input field. |
| `agents` | ✅ Optional (nested sub-agents, `*` = all) | — | — | `string[]` | Allowed sub-agents. |
| `handoffs` | ✅ Optional (`label`, `agent`, `prompt`, `send`, `model`) | — | — | `map[]` | Sequential chaining between agents. |
| `mcp-servers` | ✅ Optional (YAML, not used in VS Code IDE) | — | ✅ Optional (`mcpServers` — inline or reference) | `map` | Dedicated MCP servers. |
| `hooks` | ✅ Preview (`chat.useCustomAgentHooks`) | — | ✅ Optional (`PreToolUse`, `PostToolUse`, `Stop`) | `map` | Lifecycle hooks scoped to the agent. |
| `metadata` | ✅ Optional (name/value pairs) | — | — | `map` | Annotations (`author`, `version`…). |
| `effort` | — | — | ✅ Optional (`low` / `medium` / `high` / `xhigh` / `max`) | `string` | Sub-agent effort level. |
| `maxTurns` | — | — | ✅ Optional | `integer` | Execution turn limit. |
| `permissionMode` | — | — | ✅ Optional (`default` / `acceptEdits` / `auto` / `dontAsk` / `bypassPermissions` / `plan`) | `string` | Permission mode. |
| `memory` | — | — | ✅ Optional (`user` / `project` / `local`) | `string` | Persistent cross-session memory. |
| `background` | — | — | ✅ Optional (default `false`) | `boolean` | Background execution. |
| `isolation` | — | — | ✅ Optional (`worktree`) | `string` | Isolated git worktree. |
| `skills` | — | — | ✅ Optional | `string[]` | Skills preloaded into context at startup. |
| `color` | — | — | ✅ Optional (`red` / `blue` / `green` / `yellow` / `purple` / `orange` / `pink` / `cyan`) | `string` | Display color in the UI. |
| `initialPrompt` | — | — | ✅ Optional | `string` | Auto-submitted prompt at startup (`--agent`). |
| `readonly` | — | ✅ Optional (default `false`) | — | `boolean` | Restricts write permissions. |
| `is_background` | — | ✅ Optional (default `false`) | — | `boolean` | Background execution. |

## Handoffs

Handoffs enable sequential chaining between agents. After an agent completes,
handoff buttons appear so the user (or the system, with `send: true`) can
transition to the next agent with relevant context.

### Handoff fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `label` | `string` | Button text shown to the user. |
| `agent` | `string` | Target agent identifier. |
| `prompt` | `string` | Prompt sent to the target agent. |
| `send` | `boolean` | Auto-submit the prompt (default `false`). |
| `model` | `string` | Optional model override for the handoff. |

### Configured handoff chains

| From | To | `send` | Trigger |
| ---- | -- | :----: | ------- |
| **architect** | planner | `true` | Design approved → decompose into plan |
| **planner** | tdd-red | `false` | User chooses TDD path |
| **planner** | code-review | `false` | User wants plan reviewed |
| **tdd-red** | tdd-green | `true` | Failing tests written → implement |
| **tdd-green** | tdd-refactor | `true` | Tests pass → clean up |
| **tdd-refactor** | code-review | `true` | Refactor done → quality gate |
| **code-review** | debug | `false` | Bugs found |
| **code-review** | refactor | `false` | Quality issues found |
| **code-review** | security-reviewer | `false` | Security concerns flagged |
| **code-review** | documentation | `false` | Missing or outdated docs |
| **debug** | code-review | `true` | Fix applied → validate |
| **debug** | tdd-red | `false` | Optional regression test |
| **refactor** | code-review | `true` | Refactor done → validate |
| **security-reviewer** | debug | `false` | Vulnerabilities to fix |
| **devops** | code-review | `false` | Pipeline changes to review |

### Common workflow paths

```text
# TDD full cycle (auto)
architect → planner → tdd-red → tdd-green → tdd-refactor → code-review

# Code review with findings (manual)
code-review → { debug | refactor | security-reviewer } → code-review

# Bug fix (auto review)
debug → code-review

# Security audit (manual)
security-reviewer → debug → code-review
```

## Agent-scoped hooks

Hooks defined in agent frontmatter run **only** when that agent is active.
They complement workspace-level hooks with agent-specific guardrails.
Requires `chat.useCustomAgentHooks: true`.

### Configured hooks

| Agent | Event | Hook script | Purpose |
| ----- | ----- | ----------- | ------- |
| **debug** | PostToolUse | `post-edit-format.sh` | Auto-format edited files |
| **debug** | PostToolUse | `quality-gate.sh` | Lint + typecheck after edits |
| **debug** | PostToolUse | `console-log-warn.sh` | Warn if debug statements left behind |
| **tdd-green** | PostToolUse | `post-edit-format.sh` | Auto-format implementation code |
| **tdd-green** | PostToolUse | `quality-gate.sh` | Lint + typecheck as code is written |
| **tdd-refactor** | PostToolUse | `post-edit-format.sh` | Auto-format refactored code |
| **tdd-refactor** | PostToolUse | `quality-gate.sh` | Lint + typecheck after refactoring |
| **refactor** | PostToolUse | `post-edit-format.sh` | Auto-format across files |
| **refactor** | PostToolUse | `quality-gate.sh` | Lint + typecheck cross-cutting changes |
| **devops** | PreToolUse | `secrets-scan.sh` | Block secrets in infrastructure files |
| **devops** | PreToolUse | `config-protection.sh` | Alert when touching CI/linter configs |

### Agents without hooks

Read-only agents (`code-review`, `security-reviewer`, `architect`, `planner`)
have `disallowedTools` blocking file edits, so PostToolUse edit hooks would
never fire. `tdd-red` writes tests only. `documentation` writes prose, not
code — code linters don't apply.

## Emitted frontmatter per platform

| Platform | Fields injected | Output path | Extension |
| -------- | --------------- | ----------- | --------- |
| Copilot | `name`, `description`, `model`, `tools`, `disallowedTools`, `user-invocable`, `disable-model-invocation`, `target`, `argument-hint`, `agents`, `handoffs`, `mcp-servers`, `hooks`, `metadata` | `.github/agents/` | `.agent.md` |
| Cursor | `description`, `model`, `readonly`, `is_background` | `.cursor/agents/` | `.md` |
| Claude Code | `name`, `description`, `model`, `tools`, `disallowedTools`, `mcp-servers`, `hooks`, `effort`, `maxTurns`, `permissionMode`, `memory`, `background`, `isolation`, `skills`, `color`, `initialPrompt` | `.claude/agents/` | `.md` |
