"""Unit tests for the re-parent + copy core (:mod:`telemetry.spoke_tree.assembly`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.assembly import (
    _MAX_CONTENT_CHARS,
    _capped,
    _copy_event,
    _resolve_parent,
    _tool_additions,
    _tool_result_size,
    _tool_span_ids,
)
from telemetry.spoke_tree.ids import _copy_id
from telemetry.spoke_tree.indices import InteractionIndex
from telemetry.spoke_tree.observations import ToolContent

_EMPTY_INDEX = InteractionIndex({}, [])


def _obs(obs_id: str, name: str, *, parent: str | None = None, **extra) -> dict:
    return {"id": obs_id, "name": name, "parentObservationId": parent, **extra}


def _resolve(obs: dict, *, root_id: str = "ROOT", **indices) -> str:
    kwargs = {
        "tool_index": {},
        "request_index": {},
        "interaction_index": _EMPTY_INDEX,
        "skill_index": {},
    }
    kwargs.update(indices)
    return _resolve_parent(obs, orig_trace_id="tr", root_id=root_id, **kwargs)


class TestResolveParent:
    def test_intra_trace_parent_is_remapped(self) -> None:
        obs = _obs("t1", "tool:Bash", parent="i1")
        assert _resolve(obs) == _copy_id("tr", "i1")

    def test_root_when_nothing_matches(self) -> None:
        assert _resolve(_obs("m1", "step:green")) == "ROOT"

    def test_hook_joins_its_tool_by_use_id(self) -> None:
        hook = _obs("h1", "PreToolUse.sh", metadata={"attributes": {"tool_use_id": "tu-1"}})
        assert _resolve(hook, tool_index={"tu-1": "tool-copy"}) == "tool-copy"


class TestCapped:
    def test_small_value_passes_through(self) -> None:
        assert _capped({"a": 1}) == {"a": 1}

    def test_oversized_text_is_truncated(self) -> None:
        big = "x" * (_MAX_CONTENT_CHARS + 10)
        out = _capped(big)
        assert isinstance(out, str) and out.endswith("...[truncated]")
        assert len(out) == _MAX_CONTENT_CHARS + len("...[truncated]")


class TestToolAdditions:
    def test_grafts_missing_input_output(self) -> None:
        obs = _obs("t1", "tool:Read", metadata={"attributes": {"tool_use_id": "tu-1"}})
        content = {"tu-1": ToolContent(input={"file": "x"}, output="data")}
        adds = _tool_additions(obs, content)
        assert adds == {"input": {"file": "x"}, "output": "data"}

    def test_never_overwrites_present_field(self) -> None:
        obs = _obs(
            "t1", "tool:Bash", input="native", metadata={"attributes": {"tool_use_id": "tu"}}
        )
        content = {"tu": ToolContent(input="transcript", output=None)}
        assert "input" not in _tool_additions(obs, content)

    def test_non_graftable_span_gets_nothing(self) -> None:
        assert _tool_additions(_obs("i1", "claude_code.interaction"), {}) == {}


class TestToolResultSize:
    def test_sizes_reconstructed_output_bytes(self) -> None:
        obs = _obs("t1", "tool:Read", metadata={"attributes": {"tool_use_id": "tu-1"}})
        content = {"tu-1": ToolContent(input=None, output="abc")}
        assert _tool_result_size(obs, content) == 3

    def test_none_for_non_tool_span(self) -> None:
        obs = _obs("s1", "sub-agent:x", metadata={"attributes": {"tool_use_id": "tu-1"}})
        assert _tool_result_size(obs, {"tu-1": ToolContent(input=None, output="abc")}) is None


class TestCopyEvent:
    def test_generation_becomes_generation_create(self) -> None:
        obs = {
            "id": "g1",
            "name": "llm_request",
            "type": "GENERATION",
            "usageDetails": {"input": 1},
        }
        event = _copy_event(obs, orig_trace_id="tr", trace_id="T", parent_id="P", tool_content={})
        assert event["type"] == "generation-create"
        assert event["body"]["usageDetails"] == {"input": 1}
        assert event["body"]["parentObservationId"] == "P"

    def test_tool_span_ids_collects_graftable_ids(self) -> None:
        tool = _obs("t1", "tool:Bash", metadata={"attributes": {"tool_use_id": "tu-1"}})
        agent = _obs("a1", "sub-agent:x", metadata={"attributes": {"tool_use_id": "tu-2"}})
        assert _tool_span_ids([("tr", [tool, agent])]) == {"tu-1", "tu-2"}
