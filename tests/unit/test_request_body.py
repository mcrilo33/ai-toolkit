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

import json
import re
import sys
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.measure_context_cost import CountTokensError
from telemetry.request_body import (
    CacheBoundary,
    ContextDelta,
    ContextItem,
    decompose_request_obj,
    diff_snapshots,
    first_real_request,
    measure_request_items,
    parse_request_body,
    parse_request_obj,
    snapshot_items,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SAMPLE = _FIXTURES / "sample.request.json"
_DEGENERATE = _FIXTURES / "degenerate.request.json"
# A REAL captured combined rules+memory+env reminder (sanitized from a raw-bodies dump):
# one ``<system-reminder>`` in ``messages[0]`` packing the claudeMd intro, the project
# CLAUDE.md and the memory index (each a ``Contents of <path>`` section), and the env
# headers. Pins the turn-0 section router on an authentic reminder shape (Issue #159).
_COMBINED = _FIXTURES / "combined_reminder.request.json"


def _reminder_full(path: Path) -> str:
    """Return the whole ``<system-reminder>…</system-reminder>`` block in ``messages[0]``."""
    obj = json.loads(path.read_text(encoding="utf-8"))
    text = obj["messages"][0]["content"][0]["text"]
    match = re.search(r"<system-reminder>.*?</system-reminder>", text, re.DOTALL)
    assert match is not None
    return match.group(0)


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

    # Assert: the whole-kept reminder kinds stay in ``context``, plus the residual prompt.
    # The rules+memory+env and skills reminders are routed to their own categories (Issue
    # #159), so they no longer appear here.
    assert "session-start-hook" in context_names
    assert "deferred-tools" in context_names
    assert "agent-types" in context_names
    assert "prompt" in context_names
    assert "rules+memory+env" not in context_names
    assert "skills" not in context_names


def test_turn0_routes_skills_reminder_to_skills_category() -> None:
    # Arrange / Act: the sample's skills reminder splits per skill on the turn-0 path (#159).
    parsed = parse_request_body(_SAMPLE)

    # Assert: the skill names land in the ``skills`` category, not a lumped ``context`` item.
    skill_names = {item.name for item in _by_category(parsed.items, "skills")}
    assert skill_names == {"afk", "hub"}


def test_turn0_routes_rules_reminder_to_rules_category() -> None:
    # Arrange / Act: the sample's rules+memory+env reminder splits per file on turn-0 (#159).
    parsed = parse_request_body(_SAMPLE)

    # Assert: the ``Contents of`` file becomes a ``rules`` item named by its basename.
    rule_names = {item.name for item in _by_category(parsed.items, "rules")}
    assert rule_names == {"CLAUDE.md"}


# --- turn-0 combined-block section router (Issue #159) ------------------------


def test_turn0_splits_combined_block_per_rule_file() -> None:
    # Arrange / Act: a REAL combined rules+memory+env reminder in messages[0].
    parsed = parse_request_obj(json.loads(_COMBINED.read_text(encoding="utf-8")))

    # Assert: each ``Contents of <path>`` section is its own ``rules`` item by basename;
    # the memory index rides inside its file's item and env residue is one item.
    rule_names = {item.name for item in _by_category(parsed.items, "rules")}
    assert rule_names == {"CLAUDE.md", "MEMORY.md"}
    assert _by_category(parsed.items, "environment")
    assert "rules+memory+env" not in {item.name for item in _by_category(parsed.items, "context")}


def test_turn0_combined_block_split_is_lossless_on_real_fixture() -> None:
    # Arrange: the split items derive wholly from the single combined reminder, so their
    # token sizes must sum to the WHOLE block — the ``<system-reminder>`` wrapper tags
    # included — with no residue dropped (AC #1). The turn-0 path folds in no remainder.
    parsed = parse_request_obj(json.loads(_COMBINED.read_text(encoding="utf-8")))
    split = [item for item in parsed.items if item.category in {"rules", "skills", "environment"}]

    # Act: measure with the character counter (token size == len under counter=len).
    rows = measure_request_items(split, counter=len, price=1.0)
    split_tokens = sum(cast(int, row["tokens"]) for row in rows)

    # Assert: Σ(split items) == token size of the original reminder block (wrapper and all).
    assert split_tokens == len(_reminder_full(_COMBINED))


def test_turn0_skills_split_conserves_header_and_wrapper() -> None:
    # Arrange: a lone skills reminder — the pre-skill header and the <system-reminder> tags
    # must be conserved (as environment), not dropped, since the turn-0 path has no remainder.
    block = _reminder(
        "The following skills are available for use with the Skill tool:",
        "- afk: Drain the backlog unattended.",
        "- hub: Orient a fresh planning-hub session.",
    )
    obj = _body([], [], [_msg("user", block)])

    # Act
    parsed = parse_request_obj(obj)
    from_block = [i for i in parsed.items if i.category in {"skills", "environment"}]
    total = sum(
        cast(int, r["tokens"]) for r in measure_request_items(from_block, counter=len, price=1.0)
    )

    # Assert: skills itemized per name and the split is lossless against the whole block.
    assert {i.name for i in parsed.items if i.category == "skills"} == {"afk", "hub"}
    assert total == len(block)


def test_turn0_split_leaves_cache_boundaries_and_effort_unaffected() -> None:
    # Arrange / Act: routing the reminder must not perturb boundaries/effort (AC #3).
    parsed = parse_request_obj(json.loads(_COMBINED.read_text(encoding="utf-8")))

    # Assert: the fixture's own breakpoints and effort are read exactly as before.
    assert parsed.cache_boundaries == [
        CacheBoundary("system", 2),
        CacheBoundary("system", 3),
        CacheBoundary("messages", 1),
    ]
    assert parsed.effort == "high"


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


# --- output_config.effort (Issue #101) ---------------------------------------


def test_effort_parsed_from_output_config() -> None:
    # Arrange: a request body carrying a reasoning effort in output_config.
    obj = {"output_config": {"effort": "high"}, "tools": [], "system": [], "messages": []}

    # Act
    parsed = parse_request_obj(obj)

    # Assert: the effort is read verbatim onto the RequestBody.
    assert parsed.effort == "high"


def test_effort_absent_is_none() -> None:
    # Arrange: a body with no output_config block at all.
    obj: dict[str, object] = {"tools": [], "system": [], "messages": []}

    # Act
    parsed = parse_request_obj(obj)

    # Assert: absent effort surfaces as None, not a crash.
    assert parsed.effort is None


def test_effort_read_verbatim_even_when_ultra() -> None:
    # Arrange: the parser does not editorialize — "ultra" (the harness mode, not a real
    # effort) is read through; the semantic split (tag vs ultracode) is enforced downstream.
    obj = {"output_config": {"effort": "ultra"}, "tools": [], "system": [], "messages": []}

    # Act
    parsed = parse_request_obj(obj)

    # Assert
    assert parsed.effort == "ultra"


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


# --- per-turn diff: snapshot itemization (full messages, Issue #98) -----------


def _tool(name: str, size: int = 1) -> dict[str, object]:
    """A minimal tool schema whose serialized size scales with ``size``."""
    return {"name": name, "description": "d" * size, "input_schema": {"type": "object"}}


def _msg(role: str, text: str) -> dict[str, object]:
    """A minimal Messages-API message with a single text content block."""
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _body(
    tools: list[dict[str, object]], system: list[str], messages: list[dict[str, object]]
) -> dict[str, object]:
    """Assemble a synthetic request body from tool, system, and message parts."""
    return {
        "tools": tools,
        "system": [{"type": "text", "text": block} for block in system],
        "messages": messages,
    }


def _by_cat(rows: list[dict[str, object]], category: str) -> list[dict[str, object]]:
    return [row for row in rows if row["category"] == category]


def test_snapshot_items_itemizes_every_message_not_just_the_first() -> None:
    # Arrange: a multi-message conversation (the #87 parser only reads messages[0]).
    obj = _body(
        [_tool("Bash")],
        ["billing header"],
        [_msg("user", "hello"), _msg("assistant", "hi there"), _msg("user", "more")],
    )

    # Act
    items = snapshot_items(obj)

    # Assert: one item per message under the ``messages`` category, in order.
    message_names = [item.name for item in items if item.category == "messages"]
    assert message_names == ["msg[0]:user", "msg[1]:assistant", "msg[2]:user"]


# --- per-turn diff: classification --------------------------------------------


def test_diff_detects_a_toolsearch_loaded_schema_as_added_by_name() -> None:
    # Arrange: turn N loads a deferred tool into the real ``tools`` array.
    prev = snapshot_items(_body([_tool("Bash")], ["sys"], [_msg("user", "hi")]))
    curr = snapshot_items(_body([_tool("Bash"), _tool("WebSearch")], ["sys"], [_msg("user", "hi")]))

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: the new schema is an ADDED tools item, named by the tool name.
    added_tools = {row["name"] for row in _by_cat(delta.added, "tools")}
    assert "WebSearch" in added_tools


def test_diff_detects_appended_message_as_added() -> None:
    # Arrange: a new assistant turn is appended.
    prev = snapshot_items(_body([_tool("Bash")], ["sys"], [_msg("user", "hi")]))
    curr = snapshot_items(
        _body([_tool("Bash")], ["sys"], [_msg("user", "hi"), _msg("assistant", "reply")])
    )

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: exactly the appended message is ADDED; nothing removed.
    added_messages = [row["name"] for row in _by_cat(delta.added, "messages")]
    assert added_messages == ["msg[1]:assistant"]
    assert delta.removed == []


def test_diff_detects_grown_system_block_as_size_changed() -> None:
    # Arrange: a system block's text grows between turns (same positional label).
    prev = snapshot_items(_body([_tool("Bash")], ["short"], [_msg("user", "hi")]))
    curr = snapshot_items(_body([_tool("Bash")], ["short" * 20], [_msg("user", "hi")]))

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: the block is a CHANGED item with a positive token delta, not add+remove.
    changed = _by_cat(delta.changed, "system")
    assert len(changed) == 1
    assert cast(int, changed[0]["delta_tokens"]) > 0
    assert delta.added == [] and delta.removed == []


def test_unchanged_snapshot_yields_empty_delta() -> None:
    # Arrange: two identical snapshots — there must be zero churn.
    obj = _body([_tool("Bash")], ["sys"], [_msg("user", "hi"), _msg("assistant", "yo")])
    items = snapshot_items(obj)

    # Act
    delta = diff_snapshots(items, snapshot_items(obj), counter=len, price=1.0)

    # Assert: nothing added/removed/changed, zero net, no compaction label.
    assert delta.added == [] and delta.removed == [] and delta.changed == []
    assert delta.net_tokens == 0
    assert delta.label is None


def test_compaction_drops_early_messages_without_false_churn_on_survivors() -> None:
    # Arrange: a big early message is dropped; a later message survives at a shifted index.
    big = "x" * 12000  # exceeds the compaction drop threshold under the len() counter
    prev = snapshot_items(
        _body(
            [_tool("Bash")],
            ["sys"],
            [_msg("user", big), _msg("assistant", "kept"), _msg("user", "kept2")],
        )
    )
    curr = snapshot_items(
        _body([_tool("Bash")], ["sys"], [_msg("assistant", "kept"), _msg("user", "kept2")])
    )

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: only the big message is REMOVED; the surviving messages are not re-churned,
    # the net is strongly negative, and the turn is labeled a compaction.
    removed_messages = _by_cat(delta.removed, "messages")
    assert len(removed_messages) == 1
    assert cast(int, removed_messages[0]["tokens"]) >= 12000
    assert delta.added == []
    assert delta.net_tokens < 0
    assert delta.label == "compaction"


def test_diff_net_reconciles_added_removed_and_changed_deltas() -> None:
    # Arrange: a turn that simultaneously adds a tool and grows a system block.
    prev = snapshot_items(_body([_tool("Bash")], ["short"], [_msg("user", "hi")]))
    curr = snapshot_items(
        _body([_tool("Bash"), _tool("WebSearch")], ["short" * 10], [_msg("user", "hi")])
    )

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: net == sum(added) - sum(removed) + sum(changed deltas).
    added = sum(cast(int, row["tokens"]) for row in delta.added)
    removed = sum(cast(int, row["tokens"]) for row in delta.removed)
    changed = sum(cast(int, row["delta_tokens"]) for row in delta.changed)
    assert delta.net_tokens == added - removed + changed


def test_duplicate_message_bucket_grows_without_phantom_removed_row() -> None:
    # Arrange: a message appears twice in the older snapshot and three times in the newer
    # (identical content) — a partially-overlapping multiset bucket. Only one copy is added.
    dup = _msg("user", "ping")
    prev = snapshot_items(_body([_tool("Bash")], ["sys"], [dup, dup]))
    curr = snapshot_items(_body([_tool("Bash")], ["sys"], [dup, dup, dup]))

    # Act
    delta = diff_snapshots(prev, curr, counter=len, price=1.0)

    # Assert: exactly one copy ADDED, and NO phantom REMOVED row from a negative slice.
    assert len(_by_cat(delta.added, "messages")) == 1
    assert delta.removed == []


def test_context_delta_is_frozen_dataclass() -> None:
    # Arrange / Act
    delta = ContextDelta(added=[], removed=[], changed=[], net_tokens=0, label=None)

    # Assert: the contract is an immutable container.
    with pytest.raises(AttributeError):
        delta.net_tokens = 5  # type: ignore[misc]


# --- full-body decomposition itemizer (Issue #99) -----------------------------


def _reminder(*lines: str) -> str:
    """Wrap lines in a single ``<system-reminder>`` block."""
    return "<system-reminder>\n" + "\n".join(lines) + "\n</system-reminder>"


def test_decompose_splits_rules_block_per_rule_file() -> None:
    # Arrange: a rules+memory+env reminder carrying two rule files plus env headers.
    block = _reminder(
        "# claudeMd",
        "Contents of /repo/.claude/rules/code-quality.md:",
        "quality body",
        "Contents of /repo/.claude/rules/python-style.md:",
        "style body",
        "# currentDate",
        "Today's date is 2026-06-20.",
    )
    obj = _body([], [], [_msg("user", block)])

    # Act
    items = decompose_request_obj(obj)

    # Assert: one ``rules`` item per file, named by basename.
    rule_names = {item.name for item in items if item.category == "rules"}
    assert rule_names == {"code-quality.md", "python-style.md"}


def test_decompose_keeps_markdown_headings_inside_the_rule_file_item() -> None:
    # Arrange: a rule file whose body has its own ``#``/``##`` headings (the real files do).
    block = _reminder(
        "# claudeMd",
        "Contents of /repo/.claude/rules/code-quality.md:",
        "# Code Quality",
        "## Clarity Over Cleverness",
        "Write code that reads like prose.",
        "# currentDate",
        "Today's date is 2026-06-20.",
    )
    obj = _body([], [], [_msg("user", block)])

    # Act
    items = decompose_request_obj(obj)

    # Assert: the file's own headings stay in its rules item, not split into env (#99 review).
    rule = next(item for item in items if item.category == "rules")
    assert rule.name == "code-quality.md"
    assert "## Clarity Over Cleverness" in rule.text
    assert "Write code that reads like prose." in rule.text


def test_decompose_splits_skills_block_per_skill() -> None:
    # Arrange: a skills reminder listing two skills.
    block = _reminder(
        "The following skills are available for use with the Skill tool:",
        "- afk: Drain the backlog unattended.",
        "- hub: Orient a fresh planning-hub session.",
    )
    obj = _body([], [], [_msg("user", block)])

    # Act
    items = decompose_request_obj(obj)

    # Assert: one ``skills`` item per skill, named by the skill name.
    skill_names = {item.name for item in items if item.category == "skills"}
    assert skill_names == {"afk", "hub"}


def test_decompose_itemizes_every_message_not_just_the_first() -> None:
    # Arrange: a multi-message conversation (the #87 parser only reads messages[0]).
    obj = _body(
        [_tool("Bash")],
        ["sys"],
        [_msg("user", "hi"), _msg("assistant", "reply"), _msg("user", "newest")],
    )

    # Act
    items = decompose_request_obj(obj)

    # Assert: messages[1:] each become their own ``messages`` item, named by index.
    message_names = [item.name for item in items if item.category == "messages"]
    assert message_names == ["msg[1]:assistant", "msg[2]:user"]


def test_decompose_marks_messages_after_last_marker_uncached() -> None:
    # Arrange: the cache_control marker sits on messages[1]; the newest message follows it.
    m0 = {
        "role": "user",
        "content": [
            {"type": "text", "text": _reminder("# claudeMd", "Contents of /r/CLAUDE.md:", "x")}
        ],
    }
    m1 = {
        "role": "assistant",
        "content": [{"type": "text", "text": "reply", "cache_control": {"type": "ephemeral"}}],
    }
    m2 = {"role": "user", "content": [{"type": "text", "text": "newest"}]}
    obj = {"tools": [], "system": [], "messages": [m0, m1, m2]}

    # Act
    items = decompose_request_obj(obj)
    by_name = {item.name: item for item in items}

    # Assert: the message at the marker is cached; the one after it is not (cache_creation).
    assert by_name["msg[1]:assistant"].cached is True
    assert by_name["msg[2]:user"].cached is False
