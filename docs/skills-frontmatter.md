# Skills frontmatter fields

Correspondence between `metadata.yml` fields and what each platform expects.

## Directory structure

```text
.github/skills/source-task/       # Copilot (folder name = skill name)
├── SKILL.md                      # Main file (required)
├── scripts/                      # Executable helpers (optional)
│   ├── validate.sh
│   └── process.py
├── references/                   # On-demand docs (optional)
│   └── api-guide.md
├── templates/                    # Scaffolds (optional)
│   └── pr-template.md
└── assets/                       # Static files (optional)
```

## Field reference

| Field | Required | Copilot | Cursor | Claude Code | Type | Description |
| ----- | :------: | :-----: | :----: | :---------: | ---- | ----------- |
| `name` | Yes | ✅ | ✅ | ✅ | `string` | Unique identifier and `/` command. Must match the parent folder name. 1–64 chars, kebab-case. |
| `description` | Yes | ✅ | ✅ | ✅ | `string` | WHAT + WHEN + keywords. The agent uses this to decide when to load the skill. ≤ 1024 chars. |
| `allowed-tools` | No | ✅ | — | ✅ | `string` | Space-separated pre-approved tools (e.g. `shell git`). The agent uses them without confirmation. |
| `license` | No | ✅ | ✅ | — | `string` | License name or reference to a bundled license file. |
| `compatibility` | No | — | ✅ | — | `string` | Environment prerequisites (system packages, network access…). ≤ 500 chars. |
| `metadata` | No | — | ✅ | — | `map` | Arbitrary key-value map (`author`, `version`…). |
| `disable-model-invocation` | No | ✅ | ✅ | ✅ | `boolean` | `true` = manual invocation only via `/skill-name`, no auto-activation. |
| `user-invocable` | No | ✅ | — | ✅ | `boolean` | `false` = hidden from the `/` menu, only for agent use. |
| `argument-hint` | No | ✅ | — | ✅ | `string` | Placeholder in the chat input field (e.g. `[test file] [options]`). |
| `paths` | No | — | — | ✅ | `string \| string[]` | Glob patterns limiting auto-activation to matched files. |
| `context` | No | — | — | ✅ | `string` | `fork` to execute in an isolated sub-agent. SKILL.md becomes the sub-agent prompt. |
| `agent` | No | — | — | ✅ | `string` | Sub-agent type when `context: fork` (`Explore`, `Plan`, `general-purpose`, custom). |
| `when_to_use` | No | — | — | ✅ | `string` | Additional trigger context. Appended to `description`. Combined cap of 1536 chars. |
| `arguments` | No | — | — | ✅ | `string \| string[]` | Named positional arguments for `$name` substitution in content. |
| `model` | No | — | — | ✅ | `string` | LLM model override for the current turn when the skill is active. |
| `effort` | No | — | — | ✅ | `string` | Effort level: `low`, `medium`, `high`, `xhigh`, `max`. |
| `hooks` | No | — | — | ✅ | `map` | Hooks scoped to the skill lifecycle. |
| `shell` | No | — | — | ✅ | `string` | Shell for inline commands: `bash` (default) or `powershell`. |

## Emitted frontmatter per platform

| Platform | Fields injected | Output path | Extension |
| -------- | --------------- | ----------- | --------- |
| Copilot | `name`, `description`, `allowed-tools`, `license`, `disable-model-invocation`, `user-invocable`, `argument-hint` | `.github/skills/<name>/SKILL.md` | `.md` |
| Cursor | `name`, `description`, `license`, `compatibility`, `metadata`, `disable-model-invocation` | `.cursor/skills/<name>/SKILL.md` | `.md` |
| Claude Code | `name`, `description`, `allowed-tools`, `disable-model-invocation`, `user-invocable`, `argument-hint`, `paths`, `context`, `agent`, `when_to_use`, `arguments`, `model`, `effort`, `hooks`, `shell` | `.claude/skills/<name>/SKILL.md` | `.md` |
