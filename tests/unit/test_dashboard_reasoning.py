"""Reasoning surfaced in the v3 spoke trace (Issue #59).

The drill-down shows a turn's reasoning summary as a synthetic ``reasoning`` node, and
a phase ``step``/``interval`` that resolves no todo summary falls back to a
content-derived gist from its turns' reasoning — so a step never renders as a bare
phase (``DESIGN``) when content exists to describe what happened.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def _load_tree():
    path = _REPO_ROOT / "dashboard" / "tree.py"
    spec = importlib.util.spec_from_file_location("dashboard_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tree = _load_tree()


def _turn(ts: str, **over):
    base = {
        "ts": ts,
        "model": "claude-opus-4-8",
        "source": "main",
        "cost_usd": 0.0,
        "tokens_in": 10,
        "tokens_out": 5,
        "reasoning": None,
    }
    base.update(over)
    return base


def _marker(span_id: str, ts: str, phase: str):
    return {
        "span_id": span_id,
        "parent_id": None,
        "kind": "step",
        "name": phase,
        "summary": None,
        "phase": phase,
        "status": "success",
        "ts_start": ts,
        "ts_end": ts,
        "duration_ms": 0,
        "human_type": None,
        "human_wait_ms": None,
        "human_count": 0,
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "actor": "main",
        "children": [],
    }


def test_turn_node_attaches_a_reasoning_child() -> None:
    node = tree._turn_node(_turn("2026-06-12T23:00:05Z", reasoning="Investigating the parser"))

    reasoning = [c for c in node["children"] if c["kind"] == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["summary"] == "Investigating the parser"


def test_turn_node_without_reasoning_has_no_reasoning_child() -> None:
    node = tree._turn_node(_turn("2026-06-12T23:00:05Z"))

    assert not [c for c in node["children"] if c["kind"] == "reasoning"]


def test_interval_label_falls_back_to_reasoning_gist_when_no_todo() -> None:
    # Two step markers: marker[0] closes the setup bucket; marker[1] heads a real
    # phase bucket whose label, absent a todo, should derive from its turn's reasoning.
    m1 = _marker("m1", "2026-06-12T23:00:10Z", "RED")
    m2 = _marker("m2", "2026-06-12T23:00:20Z", "DESIGN")
    nodes = [m1, m2]
    intervals = tree._build_intervals(nodes)
    turn = _turn("2026-06-12T23:00:15Z", reasoning="Designing the attachment parser")
    turns_by_owner = {"m2": [turn]}

    forest = tree._interval_forest(nodes, intervals, turns_by_owner)
    names = [r["name"] for r in forest]

    assert "Designing the attachment parser" in names  # gist replaces the bare phase
    assert "DESIGN" not in names
