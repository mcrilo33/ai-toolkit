# Rules frontmatter fields

Correspondence between `metadata.yml` fields and what each platform expects.

## Field reference

| Field | Copilot | Cursor | Claude Code | Type | Description |
| ----- | :-----: | :----: | :---------: | ---- | ----------- |
| `name` | ✅ | — | — | `string` | Display name in the UI. Defaults to the filename. |
| `description` | ✅ | ✅ | — | `string` | Semantic matching: the agent decides to load the rule when the description matches the task. Also used as tooltip in the Copilot UI. |
| `applyTo` | ✅ | — | — | `string \| string[]` | Glob pattern(s) of targeted files. The rule auto-attaches when an open file matches. Without `applyTo`, no auto-attach. |
| `globs` | — | ✅ | — | `string[]` | Targeted files. The rule auto-attaches when a matching file is referenced in context. |
| `alwaysApply` | — | ✅ | — | `boolean` | `true` = always-on (ignores `globs` and `description`). `false` = agent-decided or auto-attached. |
| `excludeAgent` | ✅ | — | — | `string` | Exclude a server-side agent: `"code-review"` or `"cloud-agent"`. Does not affect IDE Chat. |
| `paths` | — | — | ✅ | `string[]` | Glob pattern(s) of targeted files. Without `paths`, the rule is always-on. |

> [!NOTE]
> All fields are optional. None is required by any platform.

## Emitted frontmatter per platform

| Platform | Fields injected | Output path | Extension |
| -------- | --------------- | ----------- | --------- |
| Copilot | `name`, `description`, `applyTo`, `excludeAgent` | `.github/instructions/` | `.instructions.md` |
| Cursor | `description`, `globs`, `alwaysApply` | `.cursor/rules/` | `.md` or `.mdc` |
| Claude Code | `paths` | `.claude/rules/` | `.md` |
