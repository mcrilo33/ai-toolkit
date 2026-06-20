#!/usr/bin/env python3
"""Itemize a spoke's loaded context from an untruncated raw API request body.

Claude Code's ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` mode dumps each outgoing
Messages-API request verbatim to ``<dir>/<uuid>.request.json`` — the full
``tools`` array and the complete ``system`` / ``messages`` prefix, with no 60KB
inline cap. That makes every loaded-context section itemizable by name and exact
size, replacing the unreliable cache-arithmetic floor/mcp derivation.

This module parses one such file into named :class:`ContextItem` entries:

- ``tools[*]`` — one item per tool schema, named by the tool name; an
  ``mcp__server__tool`` name is split into the ``mcp`` category, every other tool
  into ``tools``. The serialized schema is the text whose tokens are measured.
- ``system[*]`` — one item per system block, labeled by position (billing header /
  identity preamble / base system prompt / tool-use + output prompt).
- ``messages[0]`` — each ``<system-reminder>`` block, classified by kind
  (session-start-hook / deferred-tools / agent-types / skills / rules+memory+env),
  plus the residual user prompt; all in the ``context`` category.

It also records every ``cache_control`` prefix boundary (read directly, not
inferred) and counts the deferred tools that are named-only in a reminder — their
schemas are NOT in ``tools``, so they are counted but never sized.

Token size is measured by :func:`measure_request_items`, reusing
``measure_context_cost``'s counter + char/4 fallback. Import-safe: no environment
or network at import; the HTTP token counter is injected by the caller.

Privacy: ``metadata.user_id`` is PII and is never read or logged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from telemetry.measure_context_cost import TokenCounter, _count

# Positional labels for the system blocks (verified anatomy: 4 blocks). Indices
# past the list fall back to a ``system[i]`` label so a longer array still itemizes.
_SYSTEM_LABELS = (
    "billing header",
    "identity preamble",
    "base system prompt",
    "tool-use + output prompt",
)

# Each ``<system-reminder>`` block is classified by the first kind whose signature
# substring appears in it; an unrecognized block falls back to ``other-reminder``.
_REMINDER_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("session-start-hook", ("SessionStart hook",)),
    ("deferred-tools", ("deferred tools are now available", "via ToolSearch")),
    ("agent-types", ("agent types for the Agent tool", "Available agent types")),
    ("skills", ("skills are available for use with the Skill tool",)),
    ("rules+memory+env", ("# claudeMd", "Memory Index", "currentDate", "CLAUDE.md")),
)

# Source label stamped on every measured row (distinguishes from the disk fallback).
_SOURCE = "request-body"

# One ``<system-reminder>…</system-reminder>`` block (DOTALL: spans newlines).
_REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.DOTALL)
# A bare identifier — a stripped reminder line that is exactly one tool name.
_TOOL_NAME_LINE_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One named, individually-sizable loaded-context entry from the request body.

    Attributes:
        category: Section key — ``tools`` / ``mcp`` / ``system`` / ``context``.
        name: Display name (tool name, system block label, or reminder kind).
        text: The exact source text whose tokens are measured.
        cached: Whether the entry lies within the cached prefix — at or before the last
            ``cache_control`` breakpoint, which is what the prompt cache actually reuses
            (the marker closes a prefix segment; everything ahead of it is cached too).
    """

    category: str
    name: str
    text: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class CacheBoundary:
    """A ``cache_control`` marker location: the ``system`` or ``messages`` block index."""

    location: str
    index: int


@dataclass(frozen=True, slots=True)
class RequestBody:
    """The parsed loaded-context view of one ``.request.json`` dump.

    Attributes:
        items: Every itemizable loaded-context entry (tools, system, context).
        cache_boundaries: The ``cache_control`` prefix breakpoints, in order.
        deferred_tool_count: Deferred tools named-only in a reminder (counted, not sized).
        model: The request's model id, when present.
    """

    items: list[ContextItem]
    cache_boundaries: list[CacheBoundary]
    deferred_tool_count: int
    model: str | None


def _tool_items(tools: object, *, cached: bool) -> list[ContextItem]:
    """Itemize the ``tools`` array, splitting ``mcp__`` tools into the ``mcp`` category.

    Tools precede ``system`` and ``messages`` in the request, so they are inside the cached
    prefix whenever any ``cache_control`` breakpoint exists at all (``cached``).
    """
    items: list[ContextItem] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        category = "mcp" if str(name).startswith("mcp__") else "tools"
        items.append(ContextItem(category, str(name), json.dumps(tool, ensure_ascii=False), cached))
    return items


def _block_text(block: object) -> str | None:
    """Return a content block's text whether it is a raw string or a ``{text: …}`` dict."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        text = block.get("text")
        return text if isinstance(text, str) else None
    return None


def _is_cached(block: object) -> bool:
    """Whether a content block carries a ``cache_control`` marker."""
    return isinstance(block, dict) and "cache_control" in block


def _last_marker_index(blocks: list[object]) -> int:
    """Return the index of the last ``cache_control``-marked block, or -1 if none."""
    return max((i for i, block in enumerate(blocks) if _is_cached(block)), default=-1)


def _system_items(
    system: object, *, any_later_boundary: bool
) -> tuple[list[ContextItem], list[CacheBoundary]]:
    """Itemize ``system`` blocks by positional label and collect their cache boundaries.

    A block is in the cached prefix when a later section (``messages``) holds a breakpoint
    (``any_later_boundary``) or it sits at/before this section's own last breakpoint.

    UPGRADE: labels are positional (the verified 4-block anatomy: billing header / identity
    preamble / base system prompt / tool-use + output prompt). If Claude Code reorders or
    drops a system block, every label shifts — switch to content-based detection then.
    """
    blocks = system if isinstance(system, list) else [system]
    last_marker = _last_marker_index(blocks)
    items: list[ContextItem] = []
    boundaries: list[CacheBoundary] = []
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if text is None:
            continue
        name = _SYSTEM_LABELS[index] if index < len(_SYSTEM_LABELS) else f"system[{index}]"
        if _is_cached(block):
            boundaries.append(CacheBoundary("system", index))
        cached = any_later_boundary or index <= last_marker
        items.append(ContextItem("system", name, text, cached))
    return items, boundaries


def _classify_reminder(text: str) -> str:
    """Classify one reminder block by the first matching kind signature."""
    for kind, signatures in _REMINDER_SIGNATURES:
        if any(signature in text for signature in signatures):
            return kind
    return "other-reminder"


def _is_deferred_tool_line(line: str) -> bool:
    """Whether a reminder line is a deferred-tool name rather than prose.

    Deferred-tool names are bare identifiers that carry an uppercase letter or an ``mcp__``
    separator (``CronCreate``, ``WebFetch``, ``mcp__srv__tool``); requiring one excludes the
    lowercase single-word prose lines that can otherwise share the reminder's tail.
    """
    stripped = line.strip()
    if not _TOOL_NAME_LINE_RE.fullmatch(stripped):
        return False
    return any(char.isupper() for char in stripped) or "__" in stripped


def _count_deferred(text: str) -> int:
    """Count the deferred-tool name lines (one per line) listed in a deferred-tools reminder."""
    return sum(_is_deferred_tool_line(line) for line in text.splitlines())


def _message_items(
    messages: object,
) -> tuple[list[ContextItem], list[CacheBoundary], int]:
    """Itemize ``messages[0]`` into reminder blocks + the residual prompt.

    Each ``<system-reminder>`` is split out and classified; whatever text remains in a
    block after the reminders are removed is the user prompt. A block (and every reminder /
    prompt within it) is in the cached prefix when it sits at/before the last ``messages``
    breakpoint. Deferred tools named in a deferred-tools reminder are tallied (their schemas
    are absent, so never sized).
    """
    if not isinstance(messages, list) or not messages:
        return [], [], 0
    content = messages[0].get("content") if isinstance(messages[0], dict) else None
    blocks: list[object] = content if isinstance(content, list) else [content]
    last_marker = _last_marker_index(blocks)
    items: list[ContextItem] = []
    boundaries: list[CacheBoundary] = []
    deferred = 0
    for index, block in enumerate(blocks):
        text = _block_text(block)
        if text is None:
            continue
        if _is_cached(block):
            boundaries.append(CacheBoundary("messages", index))
        cached = index <= last_marker
        for full, inner in ((m.group(0), m.group(1)) for m in _REMINDER_RE.finditer(text)):
            kind = _classify_reminder(inner)
            items.append(ContextItem("context", kind, full, cached))
            if kind == "deferred-tools":
                deferred += _count_deferred(inner)
        residual = _REMINDER_RE.sub("", text).strip()
        if residual:
            items.append(ContextItem("context", "prompt", residual, cached))
    return items, boundaries, deferred


def parse_request_obj(obj: dict[str, object]) -> RequestBody:
    """Parse an already-loaded request-body dict into its itemized loaded context.

    The cached-prefix flags are resolved across sections in request order (tools → system →
    messages): messages are parsed first to learn whether a later breakpoint exists, so the
    system and tool sections ahead of it inherit the cached flag correctly.
    """
    message_items, message_boundaries, deferred = _message_items(obj.get("messages"))
    system_items, system_boundaries = _system_items(
        obj.get("system"), any_later_boundary=bool(message_boundaries)
    )
    tool_items = _tool_items(obj.get("tools"), cached=bool(system_boundaries or message_boundaries))
    model = obj.get("model")
    return RequestBody(
        items=[*tool_items, *system_items, *message_items],
        cache_boundaries=[*system_boundaries, *message_boundaries],
        deferred_tool_count=deferred,
        model=model if isinstance(model, str) else None,
    )


def parse_request_body(path: Path) -> RequestBody:
    """Parse a ``.request.json`` dump at ``path`` into its itemized loaded context."""
    return parse_request_obj(_load(path))


def _load(path: Path) -> dict[str, object]:
    """Load and return the JSON object at ``path``."""
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def measure_request_items(
    items: list[ContextItem], *, counter: TokenCounter, price: float
) -> list[dict[str, object]]:
    """Measure tokens and cost for each loaded-context item.

    Args:
        items: Parsed items (see :func:`parse_request_obj`).
        counter: Token counter; raises ``CountTokensError`` when unreachable, which
            triggers the ``len(text) // 4`` fallback (the row is flagged ``estimated``).
        price: Cache-creation price in USD per token.

    Returns:
        One dict per item with keys ``category``, ``name``, ``tokens``, ``cost_usd``,
        ``source``, ``cached``, ``estimated``.
    """
    rows: list[dict[str, object]] = []
    for item in items:
        tokens, fell_back = _count(item.text, counter)
        rows.append(
            {
                "category": item.category,
                "name": item.name,
                "tokens": tokens,
                "cost_usd": tokens * price,
                "source": _SOURCE,
                "cached": item.cached,
                "estimated": fell_back,
            }
        )
    return rows


def first_real_request(paths: list[Path]) -> Path | None:
    """Return the first request file with a non-empty ``tools`` array, else None.

    The first ``llm_request`` of a session can be a degenerate aux call carrying a tiny
    prefix and no tools; it is skipped so the itemization reflects the full prefix.

    Args:
        paths: Candidate ``.request.json`` paths, oldest first (the caller sorts by mtime).

    Returns:
        The first path whose body has a non-empty ``tools`` array, or None when none do
        (the caller then falls back to disk measurement).
    """
    for path in paths:
        try:
            obj = _load(path)
        except (OSError, json.JSONDecodeError):
            continue
        tools = obj.get("tools")
        if isinstance(tools, list) and tools:
            return Path(path)
    return None
