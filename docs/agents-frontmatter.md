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

## Per-agent MCP servers (review-stamp)

The `code-review` agent declares the `review-stamp` MCP server (the signed
review-approval authenticator — see [review-stamp.md](./review-stamp.md))
under its `copilot:` and `claude:` override blocks in
`shared/agents/metadata.yml`:

```yaml
claude:
  mcpServers:
    review-stamp:
      type: stdio
      command: "./.ai-toolkit/mcp/review-stamp/run.sh"
copilot:
  mcp-servers:
    review-stamp:
      command: "./.ai-toolkit/mcp/review-stamp/run.sh"
```

The field name differs per platform: Claude Code's subagent frontmatter expects
the literal key `mcpServers` (inline stdio definition, scoped to the subagent —
connected at start, disconnected at finish), while Copilot uses `mcp-servers`.
Both names are whitelisted in `scripts/sync-to-repo.sh`, and the sync emits
each key as-is into the generated frontmatter. Wiring the server only into
`code-review` gives Claude and Copilot **positive control**: no other agent has
the `approve_review` tool.

The command path is repo-relative and resolves because the sync installs the
server itself into every target at `.ai-toolkit/mcp/review-stamp/` (source:
`mcp/review-stamp/` at the toolkit repo root). Both files are tracked in the
sync manifest, so removal from the toolkit garbage-collects them downstream.

> [!NOTE]
> Claude Code has known issues initializing MCP connections for custom
> subagents spawned via the Task tool (anthropic/claude-code#24841 and
> duplicates). If `approve_review` is unavailable inside the subagent, the
> push gate's signature fallback still warns rather than silently passing.

**Cursor is deliberately excluded.** Cursor agent frontmatter has no MCP field
(MCP servers are workspace-global), and Cursor's `readonly` mode blocks MCP
access entirely — so `code-review` is not marked readonly on Cursor either.
Cursor enforcement comes instead from the `beforeMCPExecution` review-window
guard plus signature verification at push (see
[review-stamp.md](./review-stamp.md)).

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

## Model and effort assignment

Each agent is matched to a model by **role type**, treating Fable as the scarce
currency (issue #141): only the design/plan roles whose output gates everything
downstream get `claude-fable-5`; reasoning-heavy execution roles get
`claude-opus-4-8` (plentiful); capable-but-routine roles get `claude-sonnet-5`;
Haiku is reserved — no current role is provably trivial. Effort is `max`
everywhere.

| Agent | Model | Effort | Rationale |
| ----- | ----- | ------ | --------- |
| `architect` | `claude-fable-5` | `max` | System design needs the strongest reasoning |
| `planner` | `claude-fable-5` | `max` | Decomposition quality gates all downstream work |
| `code-review` | `claude-opus-4-8` | `max` | Catching subtle bugs needs strong reasoning |
| `security-reviewer` | `claude-opus-4-8` | `max` | Highest stakes; assumes hostile code |
| `debug` | `claude-opus-4-8` | `max` | Root-cause investigation is hard |
| `tdd-red` | `claude-opus-4-8` | `max` | Behaviour specification gates the implementation |
| `devops` | `claude-opus-4-8` | `max` | Careful but bounded |
| `refactor` | `claude-sonnet-5` | `max` | Wide but mechanical; needs care, not genius |
| `tdd-green` | `claude-sonnet-5` | `max` | Deliberately minimal — over-thinking is a bug |
| `tdd-refactor` | `claude-sonnet-5` | `max` | Quality pass with tests already green |
| `documentation` | `claude-sonnet-5` | `max` | Reads code, writes prose |

These are declared **only under the `claude:` override block** in
`shared/agents/metadata.yml`. Full model IDs (`claude-fable-5`,
`claude-sonnet-5`) and the `effort` field are Claude-specific — Cursor's
`model` accepts only `inherit`/`fast`/model-id, and `effort` is not a Cursor or
Copilot field. Scoping them to `claude:` keeps invalid values out of the other
platforms' frontmatter. The values are a tunable policy choice; adjust them per
repository as needed.

## Preloaded skills (Claude-scoped)

The `skills` field injects the full content of each listed skill into the
subagent's context at startup, giving it domain knowledge without discovery
overhead. Like `model`/`effort`, it is declared **only under the `claude:`
override block** — neither Copilot nor Cursor supports the field.

| Agent | Preloaded skills | Rationale |
| ----- | ---------------- | --------- |
| `code-review` | `verification-loop`, `security-review` | Quality-gate pipeline + security checklist guide the review |
| `planner` | `context-map`, `brainstorming` | Blast-radius analysis and spec refinement shape the plan |
| `tdd-red` | `tdd-workflow`, `generate-tests` | Red-phase rules + test conventions |
| `tdd-green` | `tdd-workflow` | Minimal-implementation discipline |
| `tdd-refactor` | `tdd-workflow` | Refactor-phase rules with tests green |
| `devops` | `ci-cd-review`, `docker-patterns`, `deployment-patterns` | Pipeline, container, and deployment references |

Preloading controls startup context only — agents can still discover other
skills at runtime through the Skill tool.

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
