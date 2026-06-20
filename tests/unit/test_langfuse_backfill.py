"""Unit tests for the transcript→Langfuse backfill translator (Issue #92, S3 — RED).

The backfill reuses the parser + causal builder to assemble one spoke's causal forest
from the local transcript, then translates that forest into Langfuse ingestion events —
the SECOND sink for the same forest the dashboard renders. These tests run with NO
network: :func:`forest_to_events` is pure, fed a forest built by ``build_causal_forest``.

They assert one ``trace-create`` + one synthetic root + one event per causal node,
turn/agent leaves becoming ``generation-create`` with usage details, tool/context/
reasoning nodes becoming spans, deterministic ids keyed on ``(spoke_run_id, node_id)``,
parent re-wiring across the tree, container token rollups, and the thinking body joined
onto reasoning nodes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal_tree import build_causal_forest
from telemetry.langfuse_backfill import (
    backfill_node_id,
    backfill_root_id,
    backfill_trace_id,
    forest_to_events,
)

SPOKE = "feature/92-demo+1700000000"


def _turn(**kw: Any) -> dict:
    row = {
        "uuid": "m1",
        "parent_uuid": "u1",
        "session_id": "s",
        "ts": "2026-06-12T23:00:10Z",
        "source": "main",
        "agent_id": None,
        "is_sidechain": False,
        "model": "claude-opus-4-8",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cache_read": 500,
        "cache_creation": 300,
        "cost_usd": 0.10,
        "reasoning": "weighing the reuse",
    }
    row.update(kw)
    return row


def _forest() -> list[Any]:
    """One main turn with a tool child and a thinking body (so a reasoning node forms)."""
    spans = [
        {
            "span_id": "sp_read",
            "parent_id": None,
            "kind": "tool",
            "name": "Read",
            "phase": None,
            "ts_start": "2026-06-12T23:00:11Z",
            "ts_end": "2026-06-12T23:00:11Z",
            "duration_ms": 0,
            "status": "success",
            "summary": "queries.py",
        }
    ]
    return build_causal_forest([_turn()], spans, {"sp_read": "m1"}, thinking={"m1": "BODY_THINK"})


def _events() -> list[dict]:
    return forest_to_events(_forest(), SPOKE, thinking={"m1": "BODY_THINK"})


def _by_type(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e["type"] == event_type]


def _body_by_id(events: list[dict]) -> dict[str, dict]:
    return {e["body"]["id"]: e["body"] for e in events if e["type"] != "trace-create"}


class TestTraceAndRoot:
    def test_exactly_one_trace_create(self) -> None:
        traces = _by_type(_events(), "trace-create")
        assert len(traces) == 1

    def test_trace_carries_the_session_id(self) -> None:
        trace = _by_type(_events(), "trace-create")[0]
        assert trace["body"]["sessionId"] == SPOKE
        assert trace["body"]["id"] == backfill_trace_id(SPOKE)

    def test_one_synthetic_root_span(self) -> None:
        bodies = _body_by_id(_events())
        assert backfill_root_id(SPOKE) in bodies

    def test_one_event_per_node_plus_trace_and_root(self) -> None:
        # 1 interval + 1 turn + 1 context + 1 reasoning + 1 tool = 5 nodes, + trace + root.
        assert len(_events()) == 5 + 2


class TestNodeTranslation:
    def test_turn_becomes_a_generation_with_usage(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "m1")]
        gen_ids = {e["body"]["id"] for e in _by_type(_events(), "generation-create")}
        assert backfill_node_id(SPOKE, "m1") in gen_ids
        assert body["usageDetails"] == {
            "input": 1000,
            "output": 200,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 300,
        }

    def test_turn_cost_is_preserved(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "m1")]
        assert body["costDetails"]["total"] == 0.10

    def test_tool_becomes_a_span_under_its_turn(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "sp_read")]
        span_ids = {e["body"]["id"] for e in _by_type(_events(), "span-create")}
        assert backfill_node_id(SPOKE, "sp_read") in span_ids
        assert body["parentObservationId"] == backfill_node_id(SPOKE, "m1")

    def test_reasoning_node_carries_the_thinking_body_as_output(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "reasoning:m1")]
        assert body["output"] == "BODY_THINK"

    def test_node_metadata_carries_kind(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "sp_read")]
        assert body["metadata"]["kind"] == "tool"


class TestTopLevelParenting:
    def test_top_level_node_parents_under_the_root(self) -> None:
        # The single synthetic interval (run) sits directly under the spoke root.
        events = _events()
        intervals = [
            e for e in events if e["type"] == "span-create" and e["body"].get("name") == "run"
        ]
        assert intervals
        assert intervals[0]["body"]["parentObservationId"] == backfill_root_id(SPOKE)


class TestContainerRollups:
    def test_container_node_carries_subtree_token_rollup(self) -> None:
        # The run interval contains the whole subtree; its rollup sums the turn's usage.
        run = next(
            e["body"]
            for e in _events()
            if e["type"] == "span-create" and e["body"].get("name") == "run"
        )
        assert run["metadata"]["rollup"] == {
            "reused": 500,
            "written": 300,
            "input": 1000,
            "output": 200,
        }


class TestDeterminism:
    def test_ids_are_stable_across_runs(self) -> None:
        first = {e["body"]["id"] for e in _events()}
        second = {e["body"]["id"] for e in _events()}
        assert first == second

    def test_node_id_is_derived_from_spoke_and_node(self) -> None:
        assert backfill_node_id(SPOKE, "m1") == backfill_node_id(SPOKE, "m1")
        assert backfill_node_id(SPOKE, "m1") != backfill_node_id("other", "m1")
        assert backfill_node_id(SPOKE, "m1") != backfill_node_id(SPOKE, "m2")
