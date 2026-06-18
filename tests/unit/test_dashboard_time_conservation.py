"""Time conservation: the time analog of cost conservation (Issue #79 — RED).

Every chunk of wall-clock must be a duration-bearing slice attributed to exactly one
node, so summing time by kind recovers the WHOLE spoke duration (spawn→teardown). The
partition rule is **deepest-active leaf**: at each instant the innermost running work
span owns the slice (a hook nested in a tool beats the tool; a sub-turn nested in an
agent beats the agent; a parent's wait while a sub-agent runs is the sub-agent's), and
any instant no work span covers is ``idle``. So nested hooks/tools and concurrent
sub-agents never double-count — Σ leaf-slice duration by kind == total wall-clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import load_queries
from telemetry.causal import CausalNode, causal_node, validate_causal_tree
from telemetry.causal_tree import build_causal_forest, leaf_time_slices
from telemetry.spans import derive_span_id

BASE = "2026-06-12T12:00:00Z"


def _at(seconds: int) -> str:
    """``seconds`` past 12:00:00 as an ISO timestamp (whole-minute aware)."""
    minute, second = divmod(seconds, 60)
    return f"2026-06-12T12:{minute:02d}:{second:02d}Z"


def _node(node_id: str, kind: str, start: int, end: int, children: list[CausalNode]) -> CausalNode:
    node = causal_node(
        node_id=node_id,
        kind=kind,
        name=kind,
        ts_start=_at(start),
        ts_end=_at(end),
        duration_ms=(end - start) * 1000,
    )
    for child in children:
        child["parent_id"] = node_id
        node["children"].append(child)
    return node


def _nested_spoke() -> list[CausalNode]:
    """A 600s spoke whose work spans nest hooks-in-tools and a concurrent sub-agent.

    Timeline (s past 12:00:00), deepest-active leaf per slice:
      000–010 idle · 010–040 turn1 · 040–050 tool1 · 050–070 hook1 · 070–100 tool1 ·
      100–115 idle · 115–120 toolTask · 120–130 agent · 130–150 sub-turn ·
      150–300 agent · 300–305 toolTask · 305–600 idle
    """
    hook1 = _node("hook1", "hook", 50, 70, [])
    tool1 = _node("tool1", "tool", 40, 100, [hook1])
    sub_turn = _node("subturn", "turn", 130, 150, [])
    agent1 = _node("agent1", "agent", 120, 300, [sub_turn])
    tool_task = _node("toolTask", "tool", 115, 305, [agent1])
    turn1 = _node("turn1", "turn", 10, 40, [tool1, tool_task])
    step = _node("step", "step", 0, 600, [turn1])
    return [step]


def test_leaf_slices_partition_the_whole_spoke_by_kind() -> None:
    forest = _nested_spoke()
    validate_causal_tree(forest)

    slices = leaf_time_slices(forest)

    assert slices == {
        "idle": 320_000,
        "turn": 50_000,
        "tool": 50_000,
        "hook": 20_000,
        "agent": 160_000,
    }


def test_leaf_slices_conserve_the_total_wall_clock() -> None:
    forest = _nested_spoke()

    slices = leaf_time_slices(forest)

    assert sum(slices.values()) == 600_000  # spawn (12:00:00) → teardown (12:10:00)


SID = "sess-t"
HREC = "hrec-uuid"


def _by_id(forest: list[CausalNode]) -> dict[str, CausalNode]:
    flat: dict[str, CausalNode] = {}

    def walk(nodes: list[CausalNode]) -> None:
        for node in nodes:
            flat[node["node_id"]] = node
            walk(node["children"])

    walk(forest)
    return flat


def _turn_with_latency_scenario() -> list[CausalNode]:
    """A prompt at :50 triggers t1 (inference ends :52); a continuation t2 ends :54.

    t1's trigger is the prompt → its inference window is prompt→t1 (2s); t2's trigger
    is the prior turn's tool_result, proxied by t1's end → its window is t1→t2 (2s).
    Before #79 both turns are zero-width, so their inference time is lost.
    """
    turns = [
        {
            "uuid": "t1",
            "parent_uuid": HREC,
            "session_id": SID,
            "ts": "2026-06-12T20:57:52Z",
            "source": "main",
            "agent_id": None,
            "is_sidechain": False,
            "cost_usd": 0.1,
            "tokens_in": 100,
            "tokens_out": 20,
        },
        {
            "uuid": "t2",
            "parent_uuid": "tr1",
            "session_id": SID,
            "ts": "2026-06-12T20:57:54Z",
            "source": "main",
            "agent_id": None,
            "is_sidechain": False,
            "cost_usd": 0.2,
            "tokens_in": 100,
            "tokens_out": 20,
        },
    ]
    spans = [
        {
            "span_id": derive_span_id(SID, HREC),
            "parent_id": None,
            "kind": "human",
            "name": "prompt",
            "phase": None,
            "ts_start": "2026-06-12T20:57:50Z",
            "ts_end": "2026-06-12T20:57:50Z",
            "duration_ms": 0,
            "status": "success",
            "human_type": "prompt",
            "human_wait_ms": None,
        },
        {
            "span_id": "st_spawn",
            "parent_id": None,
            "kind": "step",
            "name": "solo-cycle",
            "phase": "spawn",
            "ts_start": "2026-06-12T20:57:05Z",
            "ts_end": "2026-06-12T20:58:00Z",
            "duration_ms": 0,
            "status": "success",
        },
    ]
    return build_causal_forest(turns, spans, {})


def test_main_turn_duration_is_inference_latency_from_its_prompt() -> None:
    nodes = _by_id(_turn_with_latency_scenario())

    t1 = nodes["t1"]

    assert t1["ts_start"] == "2026-06-12T20:57:50Z"
    assert t1["duration_ms"] == 2000


def test_continuation_turn_duration_runs_from_the_prior_turn() -> None:
    nodes = _by_id(_turn_with_latency_scenario())

    t2 = nodes["t2"]

    assert t2["ts_start"] == "2026-06-12T20:57:52Z"
    assert t2["duration_ms"] == 2000


SPAWN = "2026-06-12T12:00:00Z"
TEARDOWN = "2026-06-12T12:10:00Z"
PROMPT = "prec"


def _spoke_forest() -> list[CausalNode]:
    """A 600s spoke: a lifecycle interval spans spawn→teardown; a prompt fires a turn
    (30s inference) that issues a tool (60s); the rest is idle."""
    turns = [
        {
            "uuid": "tn",
            "parent_uuid": PROMPT,
            "session_id": SID,
            "ts": "2026-06-12T12:00:35Z",
            "source": "main",
            "agent_id": None,
            "is_sidechain": False,
            "cost_usd": 0.1,
            "tokens_in": 100,
            "tokens_out": 20,
        },
    ]
    spans = [
        {
            "span_id": "life",
            "parent_id": None,
            "kind": "lifecycle",
            "name": "solo-cycle",
            "phase": "spawn",
            "ts_start": SPAWN,
            "ts_end": TEARDOWN,
            "duration_ms": 600_000,
            "status": "success",
        },
        {
            "span_id": derive_span_id(SID, PROMPT),
            "parent_id": None,
            "kind": "human",
            "name": "prompt",
            "phase": None,
            "ts_start": "2026-06-12T12:00:05Z",
            "ts_end": "2026-06-12T12:00:05Z",
            "duration_ms": 0,
            "status": "success",
            "human_type": "prompt",
            "human_wait_ms": None,
        },
        {
            "span_id": "tool1",
            "parent_id": None,
            "kind": "tool",
            "name": "Read",
            "phase": None,
            "ts_start": "2026-06-12T12:00:35Z",
            "ts_end": "2026-06-12T12:01:35Z",
            "duration_ms": 60_000,
            "status": "success",
        },
    ]
    return build_causal_forest(turns, spans, {"tool1": "tn"})


def test_leaf_slices_reconcile_to_the_marker_spawn_teardown_total() -> None:
    forest = _spoke_forest()

    slices = leaf_time_slices(forest)

    total = 600_000  # 12:00:00 → 12:10:00, the lifecycle marker span
    assert sum(slices.values()) == total
    assert slices["turn"] == 30_000  # prompt(:05) → turn(:35)
    assert slices["tool"] == 60_000  # tool [:35, 1:35]
    assert slices["idle"] == total - 90_000


def test_time_by_kind_rows_reconcile_and_carry_shares() -> None:
    queries = load_queries()
    slices = leaf_time_slices(_spoke_forest())

    rows = queries.time_by_kind_rows(slices)

    assert sum(r["total_duration_ms"] for r in rows) == 600_000
    assert sum(r["share"] for r in rows) == pytest.approx(1.0)
    assert [r["kind"] for r in rows][0] == "idle"  # the dominant slice, sorted first
