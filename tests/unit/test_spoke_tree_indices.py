"""Unit tests for the ownership / enclosing-turn / skill / blocked-tool indices module."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.ids import _blocked_tool_id, _copy_id
from telemetry.spoke_tree.indices import (
    _blocked_tool_name,
    _build_interaction_index,
    _build_request_index,
    _build_tool_index,
    _enclosing_turn,
    _synthesize_blocked_tools,
)


def _obs(
    obs_id: str, name: str, *, type_: str = "SPAN", parent: str | None = None, **extra
) -> dict:
    return {
        "id": obs_id,
        "name": name,
        "type": type_,
        "parentObservationId": parent,
        "startTime": extra.pop("startTime", None),
        "endTime": extra.pop("endTime", None),
        **extra,
    }


class TestToolIndex:
    def test_tool_span_owns_its_tool_use_id(self) -> None:
        tool = _obs("t1", "tool:Bash", metadata={"attributes": {"tool_use_id": "tu-1"}})
        index = _build_tool_index([("tr", [tool])])
        assert index == {"tu-1": _copy_id("tr", "t1")}

    def test_hook_satellite_never_owns_the_id(self) -> None:
        hook = _obs("h1", "PreToolUse.sh", metadata={"attributes": {"tool_use_id": "tu-1"}})
        assert _build_tool_index([("tr", [hook])]) == {}


class TestRequestIndex:
    def test_generation_owns_its_request_id(self) -> None:
        gen = _obs("g1", "llm_request", type_="GENERATION", metadata={"request_id": "r1"})
        assert _build_request_index([("tr", [gen])]) == {"r1": _copy_id("tr", "g1")}


class TestEnclosingTurn:
    def test_resolves_by_prompt_id(self) -> None:
        turn = _obs("i1", "claude_code.interaction", metadata={"attributes": {"prompt.id": "p1"}})
        index = _build_interaction_index([("tr", [turn])])
        event = {"name": "e", "metadata": {"prompt.id": "p1"}}
        assert _enclosing_turn(event, index) == _copy_id("tr", "i1")

    def test_none_when_no_turn_matches(self) -> None:
        index = _build_interaction_index([("tr", [])])
        assert _enclosing_turn({"name": "e", "metadata": {}}, index) is None


class TestBlockedToolName:
    def test_prefers_tool_name_attr(self) -> None:
        sat = {"name": "tool_result", "metadata": {"attributes": {"tool_name": "Edit"}}}
        assert _blocked_tool_name([sat]) == "Edit"

    def test_falls_back_to_hook_name_suffix(self) -> None:
        sat = {
            "name": "PreToolUse.sh",
            "metadata": {"attributes": {"hook_name": "PreToolUse:Write"}},
        }
        assert _blocked_tool_name([sat]) == "Write"

    def test_unknown_when_nothing_names_the_tool(self) -> None:
        assert _blocked_tool_name([{"name": "x", "metadata": {}}]) == "unknown"


class TestBlockedToolSynthesis:
    def test_orphaned_id_gets_one_blocked_node(self) -> None:
        hook = _obs("h1", "PreToolUse.sh", metadata={"attributes": {"tool_use_id": "tu-x"}})
        index = _build_interaction_index([("tr", [])])
        events, mapping = _synthesize_blocked_tools(
            [("tr", [hook])], tool_index={}, interaction_index=index, trace_id="T", root_id="R"
        )
        assert len(events) == 1
        assert events[0]["body"]["name"].startswith("blocked-tool:")
        assert mapping == {"tu-x": _blocked_tool_id("tu-x")}

    def test_owned_id_is_not_synthesized(self) -> None:
        hook = _obs("h1", "PreToolUse.sh", metadata={"attributes": {"tool_use_id": "tu-1"}})
        index = _build_interaction_index([("tr", [])])
        events, mapping = _synthesize_blocked_tools(
            [("tr", [hook])],
            tool_index={"tu-1": "owner"},
            interaction_index=index,
            trace_id="T",
            root_id="R",
        )
        assert events == [] and mapping == {}
