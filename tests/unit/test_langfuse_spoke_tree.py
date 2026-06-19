"""Unit tests for the single nested spoke-tree Langfuse emitter (Issue #83).

The emitter (:mod:`telemetry.langfuse_spoke_tree`) re-assembles the strict causal
forest into ONE Langfuse trace. These tests exercise the pure batch builder with NO
network: a small hand-built forest fixture stands in for ``spoke_causal_forest`` and
the HTTP post is never called. They assert the DFS shape (one trace-create + one event
per node), parent linkage, generation-vs-span typing, usage mapping, and id determinism.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_spoke_tree import build_batch, trace_id_for

SPOKE = "feature/22-demo+1700000000"


def _node(kind: str, name: str, children: list[dict] | None = None, **extra) -> dict:
    """Build a minimal causal node carrying only the keys the emitter reads."""
    node = {
        "kind": kind,
        "name": name,
        "summary": extra.pop("summary", None),
        "status": extra.pop("status", "success"),
        "ts_start": extra.pop("ts_start", None),
        "ts_end": extra.pop("ts_end", None),
        "duration_ms": extra.pop("duration_ms", 0),
        "children": children or [],
    }
    node.update(extra)
    return node


def _forest() -> list[dict]:
    """A two-root forest: a token-bearing turn with a tool child, plus a marker step."""
    tool = _node("tool", "Bash", summary="run tests", duration_ms=10)
    turn = _node(
        "turn",
        "turn",
        children=[tool],
        own_tokens_in=120,
        own_tokens_out=45,
        own_cost_usd=0.01,
        cache_read=900,
        cache_creation=300,
        rollup={"cost_usd": 0.01, "status": "success"},
    )
    step = _node("step", "green", summary="green", status="success")
    return [turn, step]


def _events_by_id(batch: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in batch}


class TestBuildBatch:
    def test_emits_one_trace_create_plus_one_event_per_node(self) -> None:
        forest = _forest()

        batch = build_batch(forest, SPOKE)

        assert batch[0]["type"] == "trace-create"
        node_events = batch[1:]
        # turn + tool + step == 3 nodes.
        assert len(node_events) == 3
        assert all(e["type"] in {"generation-create", "span-create"} for e in node_events)

    def test_trace_carries_session_and_name(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        trace = batch[0]["body"]
        assert trace["sessionId"] == SPOKE
        assert trace["name"] == f"spoke-tree:{SPOKE}"
        assert trace["id"] == trace_id_for(SPOKE)

    def test_dfs_order_is_parent_before_child(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        names = [e["body"]["name"] for e in batch[1:]]
        # DFS: turn, then its Bash child, then the sibling step.
        assert names == ["turn", "tool:run tests", "step:green"]

    def test_parent_observation_ids_match_the_tree(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        turn, tool, step = batch[1], batch[2], batch[3]
        # Roots have no parent; the tool's parent is the turn.
        assert "parentObservationId" not in turn["body"]
        assert "parentObservationId" not in step["body"]
        assert tool["body"]["parentObservationId"] == turn["body"]["id"]

    def test_all_observations_reference_the_trace(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        trace_id = batch[0]["id"]
        assert all(e["body"]["traceId"] == trace_id for e in batch[1:])

    def test_token_bearing_node_becomes_a_generation_with_usage(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        turn = batch[1]
        assert turn["type"] == "generation-create"
        assert turn["body"]["usageDetails"] == {
            "input": 120,
            "output": 45,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
        }

    def test_non_token_node_becomes_a_span_without_usage(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        tool, step = batch[2], batch[3]
        assert tool["type"] == "span-create"
        assert step["type"] == "span-create"
        assert "usageDetails" not in tool["body"]
        assert "usageDetails" not in step["body"]

    def test_metadata_carries_kind_status_and_rollup(self) -> None:
        batch = build_batch(_forest(), SPOKE)

        turn_meta = batch[1]["body"]["metadata"]
        assert turn_meta["kind"] == "turn"
        assert turn_meta["status"] == "success"
        assert turn_meta["rollup"] == {"cost_usd": 0.01, "status": "success"}

    def test_ids_are_deterministic_for_the_same_forest(self) -> None:
        first = _events_by_id(build_batch(_forest(), SPOKE))

        second = _events_by_id(build_batch(_forest(), SPOKE))

        assert first.keys() == second.keys()

    def test_synthesized_windows_are_ordered(self) -> None:
        # No node carries absolute times, so windows are synthesized monotonically.
        batch = build_batch(_forest(), SPOKE)

        starts = [e["body"]["startTime"] for e in batch[1:]]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)
