#!/usr/bin/env python3
"""Parse metadata.yml files and emit per-tool frontmatter.

Extracted from sync-to-repo.sh for testability. Called by
sync-to-repo.sh via ``python3 scripts/metadata_parser.py <file> <tool> <fields>``.

Can also be imported as a module for unit testing.
"""

from __future__ import annotations

import re
import sys

TOOL_NAMES = {"copilot", "cursor", "claude"}


def parse(path: str) -> dict[str, dict]:
    """Parse a metadata.yml file into a nested dict.

    Returns ``{item_name: {"__defaults": {…}, "__overrides": {tool: {…}}}}``
    """
    items: dict[str, dict] = {}
    cur_item: str | None = None
    cur_sub: str | None = None

    with open(path) as f:
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
                        if v and v[0] in ('"', "'") and v[-1] == v[0]:
                            v = v[1:-1]
                        items[cur_item]["__defaults"][k] = v
            elif indent >= 4 and cur_item and cur_sub:
                m = re.match(r"^(\S+):\s*(.*)", s)
                if m:
                    k, v = m.group(1), m.group(2)
                    if v and v[0] in ('"', "'") and v[-1] == v[0]:
                        v = v[1:-1]
                    items[cur_item]["__overrides"][cur_sub][k] = v

    return items


def query(items: dict[str, dict], tool: str, fields: list[str]) -> list[tuple[str, str]]:
    """Return ``[(name, frontmatter_string), …]`` for the given tool and fields."""
    results: list[tuple[str, str]] = []
    for name, data in items.items():
        merged = {**data["__defaults"], **data["__overrides"].get(tool, {})}
        fm_lines = []
        for f in fields:
            if f in merged:
                v = merged[f]
                fm_lines.append(f"{f}: {v}" if v != "" else f"{f}:")
        if not fm_lines:
            continue
        fm = "\\n".join(fm_lines)
        results.append((name, fm))
    return results


def main() -> None:
    """CLI entry point — drop-in replacement for the heredoc in sync-to-repo.sh."""
    meta_file, tool, fields_str = sys.argv[1], sys.argv[2], sys.argv[3]
    wanted = [f.strip() for f in fields_str.split(",")]
    items = parse(meta_file)
    for name, fm in query(items, tool, wanted):
        print(f"{name}\t{fm}")


if __name__ == "__main__":
    main()
