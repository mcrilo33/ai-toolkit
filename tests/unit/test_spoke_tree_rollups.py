"""Unit tests for the token + duration rollups (:mod:`telemetry.spoke_tree.rollups`)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.rollups import (
    _apply_container_rollups,
    _duration_class,
    _strip_container_usage,
    _union_ms,
)


def _dt(second: int) -> datetime:
    return datetime(2026, 1, 2, 0, 0, second, tzinfo=UTC)


def _span(obs_id: str, *, parent: str | None, start: str, end: str, **body) -> dict:
    return {
        "id": obs_id,
        "type": "span-create",
        "body": {
            "id": obs_id,
            "parentObservationId": parent,
            "startTime": start,
            "endTime": end,
            **body,
        },
    }


class TestDurationClass:
    def test_generation_is_llm_request(self) -> None:
        assert _duration_class({"type": "generation-create", "body": {"name": "llm_request"}}) == (
            "llm_request"
        )

    def test_gate_span_is_wait(self) -> None:
        assert _duration_class({"type": "span-create", "body": {"name": "script:gate"}}) == "wait"

    def test_interaction_is_turn(self) -> None:
        event = {"type": "span-create", "body": {"name": "claude_code.interaction"}}
        assert _duration_class(event) == "turn"

    def test_tool_span_is_tool(self) -> None:
        assert _duration_class({"type": "span-create", "body": {"name": "tool:Bash"}}) == "tool"

    def test_unknown_is_other(self) -> None:
        assert _duration_class({"type": "span-create", "body": {"name": "mystery"}}) == "other"

    def test_sub_agent_generation_is_sub_agent(self) -> None:
        # #230: a sub-agent's LLM call (``sub-agent:llm``) books to the sub-agent bucket, not
        # llm_request — the name prefix is checked before the generation branch.
        assert (
            _duration_class({"type": "generation-create", "body": {"name": "sub-agent:llm"}})
            == "sub-agent"
        )

    def test_sub_agent_container_is_sub_agent(self) -> None:
        assert (
            _duration_class({"type": "span-create", "body": {"name": "sub-agent:code-review"}})
            == "sub-agent"
        )


class TestUnionMs:
    def test_overlapping_intervals_counted_once(self) -> None:
        intervals: list[tuple[datetime, datetime] | None] = [(_dt(0), _dt(4)), (_dt(2), _dt(6))]
        assert _union_ms(intervals, clip=(_dt(0), _dt(10))) == 6000

    def test_clip_bounds_the_union(self) -> None:
        intervals: list[tuple[datetime, datetime] | None] = [(_dt(0), _dt(10))]
        assert _union_ms(intervals, clip=(_dt(2), _dt(5))) == 3000


class TestApplyContainerRollups:
    def test_container_gets_token_and_duration_rollup(self) -> None:
        events = [
            _span(
                "root",
                parent=None,
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:10Z",
                name="spoke:x",
            ),
            {
                "id": "g1",
                "type": "generation-create",
                "body": {
                    "id": "g1",
                    "parentObservationId": "root",
                    "name": "llm_request",
                    "startTime": "2026-01-02T00:00:01Z",
                    "endTime": "2026-01-02T00:00:03Z",
                    "usageDetails": {"input": 10, "output": 5},
                },
            },
        ]
        _apply_container_rollups(events)
        rollup = events[0]["body"]["metadata"]["rollup"]
        assert rollup["duration"]["total_ms"] == 10000
        assert rollup["duration"]["components"]["llm_request"] == 2000

    def test_leaf_gets_no_rollup(self) -> None:
        events = [
            _span(
                "root",
                parent=None,
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:02Z",
                name="tool:Bash",
            ),
        ]
        _apply_container_rollups(events)
        assert "rollup" not in events[0]["body"].get("metadata", {})

    def test_sub_agent_subtree_books_the_sub_agent_bucket(self) -> None:
        # #230: a sub-agent container + its LLM call book their wall-clock to the ``sub-agent``
        # duration bucket, not ``llm_request``/``other``, so the step's ``self`` shrinks.
        events = [
            _span(
                "root",
                parent=None,
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:10Z",
                name="spoke:x",
            ),
            _span(
                "sa",
                parent="root",
                start="2026-01-02T00:00:01Z",
                end="2026-01-02T00:00:09Z",
                name="sub-agent:code-review",
            ),
            {
                "id": "sag",
                "type": "generation-create",
                "body": {
                    "id": "sag",
                    "parentObservationId": "sa",
                    "name": "sub-agent:llm",
                    "startTime": "2026-01-02T00:00:02Z",
                    "endTime": "2026-01-02T00:00:08Z",
                    "usageDetails": {"input": 10, "output": 5},
                },
            },
        ]
        _apply_container_rollups(events)
        components = events[0]["body"]["metadata"]["rollup"]["duration"]["components"]
        assert components["sub-agent"] == 8000
        assert components["llm_request"] == 0


class TestStripContainerUsage:
    def test_container_with_generation_child_loses_own_usage(self) -> None:
        events = [
            {
                "id": "c1",
                "type": "span-create",
                "body": {"id": "c1", "parentObservationId": None, "usageDetails": {"input": 1}},
            },
            {
                "id": "g1",
                "type": "generation-create",
                "body": {"id": "g1", "parentObservationId": "c1", "usageDetails": {"input": 1}},
            },
        ]
        _strip_container_usage(events)
        assert "usageDetails" not in events[0]["body"]
        assert events[1]["body"]["usageDetails"] == {"input": 1}
