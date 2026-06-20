"""Unit tests for the raw-request-body loaded-context parser (Issue #87).

The parser reads an untruncated ``.request.json`` (Claude Code's
``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump) and itemizes every loaded-context
section by name and exact token size: each ``tools[*]`` entry (MCP tools split
out by their ``mcp__`` prefix), each ``system[*]`` block, and each
``<system-reminder>`` block in ``messages[0]`` classified by kind. It also records
the ``cache_control`` prefix boundaries and counts the deferred tools that are
named-only in a reminder (never sized).

These AAA tests run against a checked-in, sanitized fixture
``tests/fixtures/sample.request.json`` and stub the token counter — no network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.measure_context_cost import CountTokensError
from telemetry.request_body import (
    CacheBoundary,
    ContextItem,
    first_real_request,
    measure_request_items,
    parse_request_body,
    parse_request_obj,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SAMPLE = _FIXTURES / "sample.request.json"
_DEGENERATE = _FIXTURES / "degenerate.request.json"


def _by_category(items: list[ContextItem], category: str) -> list[ContextItem]:
    return [item for item in items if item.category == category]


def _named(items: list[ContextItem], name: str) -> ContextItem:
    return next(item for item in items if item.name == name)


# --- tools itemization -------------------------------------------------------


def test_resident_tools_itemized_by_name() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)

    # Assert: resident (non-MCP) tools are itemized by name, MCP excluded here.
    tool_names = {item.name for item in _by_category(parsed.items, "tools")}
    assert tool_names == {"Bash", "Workflow"}


def test_mcp_tool_split_into_its_own_category() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)

    # Assert: an ``mcp__server__tool`` name lands in the ``mcp`` category by name.
    mcp_names = {item.name for item in _by_category(parsed.items, "mcp")}
    assert mcp_names == {"mcp__claude_ai_Notion__notion-fetch"}


def test_per_tool_size_ordering_workflow_larger_than_bash() -> None:
    # Arrange: count tokens as serialized length so sizes are deterministic.
    parsed = parse_request_body(_SAMPLE)
    rows = measure_request_items(parsed.items, counter=len, price=1.0)
    sized = {row["name"]: cast(int, row["tokens"]) for row in rows}

    # Assert: the larger Workflow schema outsizes Bash (verified anatomy ordering).
    assert sized["Workflow"] > sized["Bash"] > 0


# --- system blocks -----------------------------------------------------------


def test_system_blocks_labeled_in_order() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)
    system = _by_category(parsed.items, "system")

    # Assert: four positional labels in order.
    assert [item.name for item in system] == [
        "billing header",
        "identity preamble",
        "base system prompt",
        "tool-use + output prompt",
    ]


def test_whole_system_cached_when_a_later_messages_breakpoint_exists() -> None:
    # Arrange / Act: the sample's prompt block carries a cache_control marker, so the
    # entire system array sits inside the cached prefix — even the un-marked early blocks.
    parsed = parse_request_body(_SAMPLE)
    system = _by_category(parsed.items, "system")

    # Assert: all four system blocks are flagged cached (prefix semantics, not per-marker).
    assert all(item.cached for item in system)


# --- reminder classification -------------------------------------------------


def test_reminder_blocks_classified_by_kind() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)
    context_names = [item.name for item in _by_category(parsed.items, "context")]

    # Assert: every injected reminder kind is recognized, plus the residual prompt.
    assert "session-start-hook" in context_names
    assert "deferred-tools" in context_names
    assert "agent-types" in context_names
    assert "skills" in context_names
    assert "rules+memory+env" in context_names
    assert "prompt" in context_names


def test_prompt_block_is_cache_flagged() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)

    # Assert: the actual prompt block carries the cache_control marker.
    assert _named(parsed.items, "prompt").cached is True


# --- cache boundaries --------------------------------------------------------


def test_cache_boundaries_extracted() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)

    # Assert: two system breakpoints + one messages breakpoint = three total.
    assert parsed.cache_boundaries == [
        CacheBoundary("system", 2),
        CacheBoundary("system", 3),
        CacheBoundary("messages", 1),
    ]


# --- deferred tools (named-only, counted not sized) --------------------------


def test_deferred_tools_counted_not_sized() -> None:
    # Arrange / Act
    parsed = parse_request_body(_SAMPLE)

    # Assert: the four names listed in the deferred-tools reminder are counted,
    # and none of them appear as a sized item.
    assert parsed.deferred_tool_count == 4
    sized_names = {item.name for item in parsed.items}
    assert "CronCreate" not in sized_names
    assert "WebFetch" not in sized_names


# --- char/4 fallback ---------------------------------------------------------


def test_char4_fallback_marks_estimated() -> None:
    # Arrange: a counter that always fails forces the len // 4 estimate.
    def failing_counter(_text: str) -> int:
        raise CountTokensError("unreachable")

    parsed = parse_request_body(_SAMPLE)

    # Act
    rows = measure_request_items(parsed.items, counter=failing_counter, price=2.0)

    # Assert: every row fell back to len // 4 and is flagged estimated, with cost.
    bash = next(row for row in rows if row["name"] == "Bash")
    tokens = cast(int, bash["tokens"])
    assert bash["estimated"] is True
    assert tokens >= 0
    assert bash["cost_usd"] == tokens * 2.0


# --- first real request selection --------------------------------------------


def test_first_real_request_skips_degenerate() -> None:
    # Arrange: the degenerate aux call (empty tools) precedes the real one.
    paths = [_DEGENERATE, _SAMPLE]

    # Act
    chosen = first_real_request(paths)

    # Assert: the empty-tools dump is skipped in favor of the full prefix.
    assert chosen == _SAMPLE


def test_first_real_request_none_when_all_degenerate() -> None:
    # Arrange / Act
    chosen = first_real_request([_DEGENERATE])

    # Assert: no real request found yields None for the caller's disk fallback.
    assert chosen is None


# --- cached-prefix semantics -------------------------------------------------


def test_block_after_last_breakpoint_is_uncached() -> None:
    # Arrange: a system block carries the only marker; a trailing block follows it and no
    # later messages breakpoint exists, so that trailing block is OUTSIDE the cached prefix.
    obj: dict[str, object] = {
        "system": [
            {"type": "text", "text": "cached one", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "trailing, uncached"},
        ],
        "messages": [{"role": "user", "content": "hello"}],
    }

    # Act
    parsed = parse_request_obj(obj)

    # Assert: block at the marker is cached, the block after it is not.
    system = _by_category(parsed.items, "system")
    assert [item.cached for item in system] == [True, False]


# --- deferred-tool counting robustness ---------------------------------------


def test_deferred_count_ignores_lowercase_prose_lines() -> None:
    # Arrange: a deferred-tools reminder whose tail has bare single-word prose lines that
    # are NOT tool names (lowercase, no mcp__ separator) — they must not be counted.
    reminder = (
        "<system-reminder>\n"
        "The following deferred tools are now available via ToolSearch:\n"
        "CronCreate\n"
        "mcp__srv__do\n"
        "keyword\n"  # prose, lowercase — not a tool
        "search\n"  # prose, lowercase — not a tool
        "</system-reminder>"
    )
    obj: dict[str, object] = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": reminder}]}]
    }

    # Act
    parsed = parse_request_obj(obj)

    # Assert: only the two identifier-shaped names count, not the prose words.
    assert parsed.deferred_tool_count == 2
