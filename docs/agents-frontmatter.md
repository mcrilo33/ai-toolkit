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

## Emitted frontmatter per platform

| Platform | Fields injected | Output path | Extension |
| -------- | --------------- | ----------- | --------- |
| Copilot | `name`, `description`, `model`, `tools`, `disallowedTools`, `user-invocable`, `disable-model-invocation`, `target`, `argument-hint`, `agents`, `handoffs`, `mcp-servers`, `hooks`, `metadata` | `.github/agents/` | `.agent.md` |
| Cursor | `description`, `model`, `readonly`, `is_background` | `.cursor/agents/` | `.md` |
| Claude Code | `name`, `description`, `model`, `tools`, `disallowedTools`, `mcp-servers`, `hooks`, `effort`, `maxTurns`, `permissionMode`, `memory`, `background`, `isolation`, `skills`, `color`, `initialPrompt` | `.claude/agents/` | `.md` |
