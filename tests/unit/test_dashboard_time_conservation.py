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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal import CausalNode, causal_node, validate_causal_tree
from telemetry.causal_tree import leaf_time_slices

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
