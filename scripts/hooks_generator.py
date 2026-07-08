#!/usr/bin/env python3
"""Generate platform-specific hook configuration files from shared/hooks/.

Reads shared/hooks/metadata.yml and generates:
  - Copilot:  .github/hooks/ai-toolkit.json   (version 1 JSON)
  - Cursor:   .cursor/hooks.json               (version 1 JSON)
  - Claude:   .claude/settings.json hooks merge (JSON fragment)

Called by sync-to-repo.sh or standalone:
    python3 scripts/hooks_generator.py <shared-hooks-dir> <target-repo> <tool>

Can also be imported as a module for unit testing.
"""

from __future__ import annotations

import json
import os
import re
import sys

TOOL_NAMES = {"copilot", "cursor", "claude"}

# ── Event name mapping per platform ─────────────────────────────────
# Shared metadata uses a canonical event name; each platform may differ.

COPILOT_EVENT_MAP: dict[str, str] = {
    "preToolUse": "preToolUse",
    "postToolUse": "postToolUse",
    "sessionStart": "sessionStart",
    "sessionEnd": "sessionEnd",
    "stop": "agentStop",
    "userPromptSubmit": "userPromptSubmitted",
    "errorOccurred": "errorOccurred",
}

CURSOR_EVENT_MAP: dict[str, str] = {
    "preToolUse": "preToolUse",
    "postToolUse": "postToolUse",
    "sessionStart": "sessionStart",
    "sessionEnd": "sessionEnd",
    "stop": "stop",
    "userPromptSubmit": "beforeSubmitPrompt",
    "preCompact": "preCompact",
    "subagentStart": "subagentStart",
    "subagentStop": "subagentStop",
    "afterFileEdit": "afterFileEdit",
    # Dedicated events (Cursor 3.7.21). Unlike the generic preToolUse/postToolUse
    # path, these carry the real command/file payload at the top level. A hook
    # selects one via a per-hook ``cursor: event:`` override in metadata.yml.
    "beforeShellExecution": "beforeShellExecution",
    "afterShellExecution": "afterShellExecution",
    "beforeReadFile": "beforeReadFile",
    "beforeMCPExecution": "beforeMCPExecution",
    "afterMCPExecution": "afterMCPExecution",
}

# Cursor dedicated events whose ``matcher`` is NOT a tool-type name. On
# beforeShellExecution/afterShellExecution the matcher is a COMMAND REGEX; on
# afterFileEdit/beforeReadFile it filters by the dedicated event's own tool
# token (e.g. "Write|TabWrite"). In neither case should the
# preToolUse/postToolUse tool-name translation (Bash->Shell, Edit->Write) run.
CURSOR_DEDICATED_EVENTS: frozenset[str] = frozenset(
    {
        "beforeShellExecution",
        "afterShellExecution",
        "afterFileEdit",
        "beforeReadFile",
        # MCP events: the matcher is an MCP tool-name filter, never run
        # through the Bash->Shell token translation.
        "beforeMCPExecution",
        "afterMCPExecution",
    }
)

# Cursor uses different tool-type names in preToolUse/postToolUse matchers than
# the Claude/Copilot naming used in metadata.yml. A matcher referencing a tool
# name Cursor never emits (e.g. "Bash") silently never fires. Translate each
# alternation token to Cursor's real tool name: shell tool is "Shell" (not
# "Bash"); the file-write tool is "Write" (there is no "Edit" tool).
CURSOR_MATCHER_TOKEN_MAP: dict[str, str] = {
    "Bash": "Shell",
    "Edit": "Write",
    "Write": "Write",
    "Read": "Read",
}

CLAUDE_EVENT_MAP: dict[str, str] = {
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
    "stop": "Stop",
    "userPromptSubmit": "UserPromptSubmit",
    "preCompact": "PreCompact",
    "subagentStart": "SubagentStart",
    "subagentStop": "SubagentStop",
    # Notification (issue #176): fires when Claude Code surfaces a permission /
    # question prompt. No Copilot/Cursor equivalent, so it maps for Claude ONLY and
    # the other generators skip it (see the skip-unmapped note in generate_copilot).
    "notification": "Notification",
}


# ── Metadata parser (reuses the lightweight YAML parser pattern) ────


def parse_hooks_metadata(path: str) -> dict[str, dict]:
    """Parse hooks/metadata.yml into {hook_name: {defaults, overrides}}."""
    items: dict[str, dict] = {}
    cur_item: str | None = None
    cur_sub: str | None = None

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            s = line.lstrip()
            if not s or s.startswith("#"):
                continue
            indent = len(line) - len(s)

            if indent == 0 and s.endswith(":"):
                cur_item = s[:-1].strip()
                items[cur_item] = {"__defaults": {}, "__overrides": {}}
                cur_sub = None
            elif indent == 2 and cur_item is not None:
                m = re.match(r"^(\S+):\s*(.*)", s)
                if m:
                    k, v = m.group(1), m.group(2)
                    if v == "" and k in TOOL_NAMES:
                        cur_sub = k
                        items[cur_item]["__overrides"].setdefault(cur_sub, {})
                    else:
                        cur_sub = None
                        v = _unquote(v)
                        items[cur_item]["__defaults"][k] = v
            elif indent >= 4 and cur_item and cur_sub:
                m = re.match(r"^(\S+):\s*(.*)", s)
                if m:
                    k, v = m.group(1), m.group(2)
                    v = _unquote(v)
                    items[cur_item]["__overrides"][cur_sub][k] = v

    return items


def _unquote(v: str) -> str:
    if v and len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        return v[1:-1]
    return v


def _merged(data: dict, tool: str) -> dict[str, str]:
    """Merge defaults with tool-specific overrides."""
    return {**data["__defaults"], **data["__overrides"].get(tool, {})}


# Hooks forced to the end of their event bucket regardless of tier, so the
# slow/expensive gates emit last and diffs stay stable across runs.
_TRAILING_HOOKS = ("commit-gauntlet",)


def _ordered_hooks(hooks: dict[str, dict]) -> list[tuple[str, dict]]:
    """Return (name, data) pairs in deterministic emission order.

    Ordering keys, in priority:
      1. Blocking/high-value gates first (lower tier number first; tier 1 < 2 < 3).
      2. Trailing hooks (e.g. commit-gauntlet, with its longer timeout) pinned last.
      3. Hook name, alphabetically, as a stable tiebreaker.

    A deterministic order keeps generated configs byte-identical across runs.
    """

    def sort_key(item: tuple[str, dict]) -> tuple[int, int, str]:
        name, data = item
        defaults = data.get("__defaults", {})
        try:
            tier = int(defaults.get("tier", 99))
        except (TypeError, ValueError):
            tier = 99
        trailing = 1 if name in _TRAILING_HOOKS else 0
        return (trailing, tier, name)

    return sorted(hooks.items(), key=sort_key)


# ── Generator functions ─────────────────────────────────────────────


def _script_ref(hook_name: str, tool: str, script_prefix: str | None = None) -> str:
    """Return the command string pointing to the hook script.

    Cursor project hooks run from the project root and must use a root-relative
    path WITHOUT a leading "./" (".cursor/hooks/..."). The "./.cursor/..." form
    still executes but trips a false-positive warning icon in the Hooks settings
    UI, so the canonical form is emitted instead.

    Args:
        hook_name: Hook key from metadata.yml (script basename without .sh).
        tool: Target platform (copilot/cursor/claude).
        script_prefix: Optional path prefix overriding the per-tool default
            (used by the plugin build, e.g. "./scripts").
    """
    script = f"{hook_name}.sh"
    if script_prefix is not None:
        return f"{script_prefix.rstrip('/')}/{script}"
    refs = {
        "copilot": f"./.github/hooks/scripts/{script}",
        "cursor": f".cursor/hooks/scripts/{script}",
        "claude": f'"$CLAUDE_PROJECT_DIR"/.claude/hooks/scripts/{script}',
    }
    return refs.get(tool, f"./hooks/scripts/{script}")


def _cursor_matcher(matcher: str) -> str:
    """Translate a metadata matcher to Cursor's tool-type names.

    Matchers are pipe-alternations (e.g. ``Write|Edit``). Each token is mapped
    to Cursor's real tool name; unknown tokens pass through unchanged. Duplicate
    tokens that collapse after mapping (``Write|Edit`` -> ``Write|Write``) are
    de-duplicated while preserving first-seen order.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in matcher.split("|"):
        token = CURSOR_MATCHER_TOKEN_MAP.get(raw, raw)
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return "|".join(tokens)


def generate_copilot(hooks: dict[str, dict], script_prefix: str | None = None) -> dict:
    """Generate Copilot hooks JSON (version 1 format).

    Config path: .github/hooks/<name>.json
    Format: {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "..."}]}}
    """
    config: dict[str, list] = {}

    for name, data in _ordered_hooks(hooks):
        merged = _merged(data, "copilot")
        event = merged.get("event", "")
        # Skip a CANONICAL event with no Copilot mapping — a Claude-only lifecycle like
        # `notification` (#176) — rather than passthrough-emitting a bucket Copilot never
        # fires. A non-canonical value is a native per-platform override (e.g. `agentStop`
        # from a `copilot: event:` block) and still passes through below.
        if event not in COPILOT_EVENT_MAP and event in CLAUDE_EVENT_MAP:
            continue
        event = COPILOT_EVENT_MAP.get(event, event)
        if not event:
            continue

        entry: dict = {
            "type": "command",
            "bash": _script_ref(name, "copilot", script_prefix),
            "timeoutSec": int(merged.get("timeout", 30)),
        }

        config.setdefault(event, []).append(entry)

    return {"version": 1, "hooks": config}


def generate_cursor(hooks: dict[str, dict], script_prefix: str | None = None) -> dict:
    """Generate Cursor hooks JSON (version 1 format).

    Config path: .cursor/hooks.json
    Format: {"version": 1, "hooks": {"preToolUse": [{"command": "...", "matcher": "..."}]}}
    """
    config: dict[str, list] = {}

    for name, data in _ordered_hooks(hooks):
        merged = _merged(data, "cursor")
        event = merged.get("event", "")
        # Skip a CANONICAL event with no Cursor mapping (the Claude-only `notification`
        # event, #176) — same reason as generate_copilot; native overrides pass through.
        if event not in CURSOR_EVENT_MAP and event in CLAUDE_EVENT_MAP:
            continue
        event = CURSOR_EVENT_MAP.get(event, event)
        if not event:
            continue

        entry: dict = {
            "command": _script_ref(name, "cursor", script_prefix),
        }

        matcher = merged.get("matcher")
        if matcher:
            # Dedicated events use a command regex / dedicated tool token, not a
            # preToolUse/postToolUse tool name — pass the matcher through verbatim.
            if event in CURSOR_DEDICATED_EVENTS:
                entry["matcher"] = matcher
            else:
                entry["matcher"] = _cursor_matcher(matcher)

        timeout = merged.get("timeout")
        if timeout:
            entry["timeout"] = int(timeout)

        # failClosed: "true" in metadata -> boolean true in the entry (used by
        # fail-closed MCP guards); any other value omits the field.
        if str(merged.get("failClosed", "")).lower() == "true":
            entry["failClosed"] = True

        config.setdefault(event, []).append(entry)

    return {"version": 1, "hooks": config}


def generate_claude(hooks: dict[str, dict], script_prefix: str | None = None) -> dict:
    """Generate Claude hooks JSON fragment.

    Config path: .claude/settings.json → hooks key
    Format: {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "..."}]}]}
    """
    config: dict[str, list] = {}

    for name, data in _ordered_hooks(hooks):
        merged = _merged(data, "claude")
        event = merged.get("event", "")
        event = CLAUDE_EVENT_MAP.get(event, event)
        if not event:
            continue

        hook_handler: dict = {
            "type": "command",
            "command": _script_ref(name, "claude", script_prefix),
        }

        if_cond = merged.get("if")
        if if_cond:
            hook_handler["if"] = if_cond

        timeout = merged.get("timeout")
        if timeout:
            hook_handler["timeout"] = int(timeout)

        matcher = merged.get("matcher")

        # Claude groups hooks by matcher within an event
        # Find or create a matcher group
        event_hooks = config.setdefault(event, [])
        group = None
        for existing in event_hooks:
            if existing.get("matcher") == matcher:
                group = existing
                break
        if group is None:
            group = {"hooks": []}
            if matcher:
                group["matcher"] = matcher
            event_hooks.append(group)

        group["hooks"].append(hook_handler)

    return config


# ── CLI entry point ─────────────────────────────────────────────────

GENERATORS = {
    "copilot": generate_copilot,
    "cursor": generate_cursor,
    "claude": generate_claude,
}


def main() -> None:
    """CLI: python3 hooks_generator.py <shared-hooks-dir> <target-repo> <tool> [--script-prefix=<prefix>]

    Outputs JSON to stdout. The sync script captures and writes to the
    appropriate file. The optional ``--script-prefix`` overrides the per-tool
    script path prefix (used by the plugin build, e.g. ``--script-prefix=./scripts``).
    """
    if len(sys.argv) < 4:
        print(
            "Usage: hooks_generator.py <shared-hooks-dir> <target-repo> <tool> "
            "[--script-prefix=<prefix>]",
            file=sys.stderr,
        )
        sys.exit(1)

    hooks_dir = sys.argv[1]
    _target_dir = sys.argv[2]  # reserved for future use
    tool = sys.argv[3]

    script_prefix: str | None = None
    for extra in sys.argv[4:]:
        if extra.startswith("--script-prefix="):
            script_prefix = extra.split("=", 1)[1]
        else:
            print(f"Unknown argument: {extra}", file=sys.stderr)
            sys.exit(1)

    if tool not in GENERATORS:
        print(f"Unknown tool: {tool}. Use: {', '.join(GENERATORS)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(hooks_dir, "metadata.yml")
    if not os.path.isfile(meta_path):
        print(f"metadata.yml not found: {meta_path}", file=sys.stderr)
        sys.exit(1)

    hooks = parse_hooks_metadata(meta_path)
    result = GENERATORS[tool](hooks, script_prefix)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
