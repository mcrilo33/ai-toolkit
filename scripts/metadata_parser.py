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

_KEY_RE = re.compile(r"^(\S+):\s*(.*)")

# A parsed value: scalar string, list of values, or nested map.
Value = str | list["Value"] | dict[str, "Value"]

# Significant source lines as (indent, content) pairs.
Lines = list[tuple[int, str]]


def _read_lines(path: str) -> Lines:
    """Return (indent, content) for each significant line, skipping blanks and comments."""
    lines: Lines = []
    with open(path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            s = line.lstrip()
            if not s or s.startswith("#"):
                continue
            lines.append((len(line) - len(s), s))
    return lines


def _strip_quotes(v: str) -> str:
    if v and v[0] in ('"', "'") and v[-1] == v[0]:
        return v[1:-1]
    return v


def _parse_block(lines: Lines, pos: int) -> tuple[Value, int]:
    """Parse the block starting at ``pos``; dispatch on list vs map syntax."""
    indent = lines[pos][0]
    if lines[pos][1].startswith("- "):
        return _parse_list(lines, pos, indent)
    return _parse_map(lines, pos, indent)


def _parse_map(lines: Lines, pos: int, indent: int) -> tuple[dict[str, Value], int]:
    """Parse consecutive ``key: value`` lines at exactly ``indent`` into a dict."""
    result: dict[str, Value] = {}
    while pos < len(lines):
        ind, s = lines[pos]
        if ind != indent:
            break
        m = _KEY_RE.match(s)
        if not m:
            break
        k, v = m.group(1), m.group(2)
        pos += 1
        if v:
            result[k] = _strip_quotes(v)
        elif pos < len(lines) and lines[pos][0] > indent:
            result[k], pos = _parse_block(lines, pos)
        else:
            result[k] = ""
    return result, pos


def _parse_list(lines: Lines, pos: int, indent: int) -> tuple[list[Value], int]:
    """Parse consecutive ``- item`` lines at exactly ``indent`` into a list."""
    result: list[Value] = []
    while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
        rest = lines[pos][1][2:]
        pos += 1
        m = _KEY_RE.match(rest)
        if not m:
            result.append(_strip_quotes(rest))
            continue
        item: dict[str, Value] = {m.group(1): _strip_quotes(m.group(2))}
        if pos < len(lines) and lines[pos][0] > indent:
            cont, pos = _parse_map(lines, pos, lines[pos][0])
            item.update(cont)
        result.append(item)
    return result, pos


def _split_item(body: dict[str, Value]) -> dict[str, dict]:
    """Split an item body into shared defaults and per-tool override blocks."""
    defaults: dict[str, Value] = {}
    overrides: dict[str, dict[str, Value]] = {}
    for k, v in body.items():
        if k in TOOL_NAMES and isinstance(v, dict):
            overrides[k] = v
        elif k in TOOL_NAMES and v == "":
            overrides[k] = {}
        else:
            defaults[k] = v
    return {"__defaults": defaults, "__overrides": overrides}


def parse(path: str) -> dict[str, dict]:
    """Parse a metadata.yml file into a nested dict.

    Args:
        path: Path to the metadata.yml file.

    Returns:
        ``{item_name: {"__defaults": {…}, "__overrides": {tool: {…}}}}`` where
        values are scalar strings (surrounding quotes stripped), lists, or
        nested dicts.
    """
    lines = _read_lines(path)
    items: dict[str, dict] = {}
    pos = 0
    while pos < len(lines):
        ind, s = lines[pos]
        pos += 1
        if ind != 0 or not s.endswith(":"):
            continue
        body: dict[str, Value] = {}
        if pos < len(lines) and lines[pos][0] > 0:
            body, pos = _parse_map(lines, pos, lines[pos][0])
        items[s[:-1].strip()] = _split_item(body)
    return items


def read_mapping(path: str) -> dict[str, Value]:
    """Parse a whole file as one nested block-style YAML mapping.

    Unlike :func:`parse` (which treats each top-level key as a named item split
    into defaults/overrides), this returns the document as a plain nested dict —
    for config files such as ``settings/ai-toolkit.yml``. Flow-style collections
    (``{a: b}``) are not supported; use block style.

    Args:
        path: Path to a block-style YAML mapping file.

    Returns:
        The parsed mapping (empty dict for an empty/comment-only file).
    """
    lines = _read_lines(path)
    if not lines:
        return {}
    result, _ = _parse_map(lines, 0, 0)
    return result


# Characters that may not start a YAML plain scalar (c-indicators).
_UNSAFE_LEAD_CHARS = frozenset("!&*|>%@`\"'#,]}")

# Balanced flow collections (e.g. ``disallowedTools: [a, b]``) are intentional
# YAML in metadata.yml and must pass through unquoted to keep their type.
_FLOW_CLOSERS = {"[": "]", "{": "}"}


def _needs_quoting(value: str) -> bool:
    """Return True when ``value`` is unsafe as a YAML plain scalar."""
    if not value or value != value.strip():
        return True
    first = value[0]
    if first in _FLOW_CLOSERS:
        return not value.endswith(_FLOW_CLOSERS[first])
    if first in _UNSAFE_LEAD_CHARS:
        return True
    # '-', '?', ':' are indicators only when followed by a space (or alone).
    if first in "-?:" and (len(value) == 1 or value[1] == " "):
        return True
    return ": " in value or value.endswith(":") or " #" in value


def _emit_scalar(value: str) -> str:
    """Render a scalar for emission, double-quoting when plain style is unsafe.

    Escapes follow YAML double-quoted style (backslash, then double quote);
    the ``echo -e`` transport doubling is applied afterwards by :func:`_encode`.
    Safe scalars stay unquoted so existing outputs do not churn.
    """
    if not _needs_quoting(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_yaml_lines(key: str, value: Value, indent: int) -> list[str]:
    """Serialize one key/value pair as block-style YAML lines."""
    pad = " " * indent
    if isinstance(value, dict):
        lines = [f"{pad}{key}:"]
        for k, v in value.items():
            lines.extend(_to_yaml_lines(k, v, indent + 2))
        return lines
    if isinstance(value, list):
        lines = [f"{pad}{key}:"]
        for item in value:
            lines.extend(_item_lines(item, indent + 2))
        return lines
    if value == "":
        return [f"{pad}{key}:"]
    return [f"{pad}{key}: {_emit_scalar(value)}"]


def _item_lines(item: Value, indent: int) -> list[str]:
    """Serialize one list item (scalar or map) as block-style YAML lines."""
    pad = " " * indent
    if isinstance(item, str):
        return [f"{pad}- {_emit_scalar(item)}"]
    if not isinstance(item, dict):
        raise TypeError(f"unsupported list item (list-of-lists): {item!r}")
    lines: list[str] = []
    for k, v in item.items():
        lines.extend(_to_yaml_lines(k, v, indent + 2))
    lines[0] = f"{pad}- {lines[0][indent + 2 :]}"
    return lines


def _encode(fm_lines: list[str]) -> str:
    """Encode YAML lines for the ``echo -e`` transport in sync-to-repo.sh.

    Backslashes are doubled first so they survive ``echo -e`` expansion, then
    lines are joined with the literal two-character sequence backslash + n,
    which ``echo -e`` re-expands into real newlines.
    """
    return "\\n".join(line.replace("\\", "\\\\") for line in fm_lines)


def query(items: dict[str, dict], tool: str, fields: list[str]) -> list[tuple[str, str]]:
    """Return ``[(name, frontmatter_string), …]`` for the given tool and fields.

    Args:
        items: Output of :func:`parse`.
        tool: Tool name whose overrides take precedence over defaults.
        fields: Frontmatter field names to emit, in order.

    Returns:
        One entry per item that has at least one requested field. The
        frontmatter string is a single line with newlines encoded for the
        ``echo -e`` transport (see :func:`_encode`).
    """
    results: list[tuple[str, str]] = []
    for name, data in items.items():
        merged = {**data["__defaults"], **data["__overrides"].get(tool, {})}
        fm_lines: list[str] = []
        for f in fields:
            if f in merged:
                fm_lines.extend(_to_yaml_lines(f, merged[f], 0))
        if fm_lines:
            results.append((name, _encode(fm_lines)))
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
