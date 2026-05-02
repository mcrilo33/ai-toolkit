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


# ── Generator functions ─────────────────────────────────────────────

def _script_ref(hook_name: str, tool: str) -> str:
    """Return the command string pointing to the hook script."""
    script = f"{hook_name}.sh"
    if tool == "copilot":
        return f"./.github/hooks/scripts/{script}"
    elif tool == "cursor":
        return f"./.cursor/hooks/scripts/{script}"
    elif tool == "claude":
        return f'"$CLAUDE_PROJECT_DIR"/.claude/hooks/scripts/{script}'
    return f"./hooks/scripts/{script}"


def generate_copilot(hooks: dict[str, dict]) -> dict:
    """Generate Copilot hooks JSON (version 1 format).

    Config path: .github/hooks/<name>.json
    Format: {"version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "..."}]}}
    """
    config: dict[str, list] = {}

    for name, data in hooks.items():
        merged = _merged(data, "copilot")
        event = merged.get("event", "")
        event = COPILOT_EVENT_MAP.get(event, event)
        if not event:
            continue

        entry: dict = {
            "type": "command",
            "bash": _script_ref(name, "copilot"),
            "timeoutSec": int(merged.get("timeout", 30)),
        }

        config.setdefault(event, []).append(entry)

    return {"version": 1, "hooks": config}


def generate_cursor(hooks: dict[str, dict]) -> dict:
    """Generate Cursor hooks JSON (version 1 format).

    Config path: .cursor/hooks.json
    Format: {"version": 1, "hooks": {"preToolUse": [{"command": "...", "matcher": "..."}]}}
    """
    config: dict[str, list] = {}

    for name, data in hooks.items():
        merged = _merged(data, "cursor")
        event = merged.get("event", "")
        event = CURSOR_EVENT_MAP.get(event, event)
        if not event:
            continue

        entry: dict = {
            "command": _script_ref(name, "cursor"),
        }

        matcher = merged.get("matcher")
        if matcher:
            entry["matcher"] = matcher

        timeout = merged.get("timeout")
        if timeout:
            entry["timeout"] = int(timeout)

        config.setdefault(event, []).append(entry)

    return {"version": 1, "hooks": config}


def generate_claude(hooks: dict[str, dict]) -> dict:
    """Generate Claude hooks JSON fragment.

    Config path: .claude/settings.json → hooks key
    Format: {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "..."}]}]}
    """
    config: dict[str, list] = {}

    for name, data in hooks.items():
        merged = _merged(data, "claude")
        event = merged.get("event", "")
        event = CLAUDE_EVENT_MAP.get(event, event)
        if not event:
            continue

        hook_handler: dict = {
            "type": "command",
            "command": _script_ref(name, "claude"),
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
    """CLI: python3 hooks_generator.py <shared-hooks-dir> <target-repo> <tool>

    Outputs JSON to stdout. The sync script captures and writes to the
    appropriate file.
    """
    if len(sys.argv) < 4:
        print(
            "Usage: hooks_generator.py <shared-hooks-dir> <target-repo> <tool>",
            file=sys.stderr,
        )
        sys.exit(1)

    hooks_dir = sys.argv[1]
    _target_dir = sys.argv[2]  # reserved for future use
    tool = sys.argv[3]

    if tool not in GENERATORS:
        print(f"Unknown tool: {tool}. Use: {', '.join(GENERATORS)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(hooks_dir, "metadata.yml")
    if not os.path.isfile(meta_path):
        print(f"metadata.yml not found: {meta_path}", file=sys.stderr)
        sys.exit(1)

    hooks = parse_hooks_metadata(meta_path)
    result = GENERATORS[tool](hooks)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
