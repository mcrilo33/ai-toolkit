"""per_turn_rows now lives in causal_tree (Issue #91 — RED).

ccusage and the pull-cost layer are retired: #90 removed their only consumer, and
the otelcol remaps tokens to ``gen_ai.usage.*`` so Langfuse computes cost itself.
So ``per_turn_rows`` no longer takes a ccusage cost map and no longer attributes a
per-turn ``cost_usd`` — it emits token counts, the cache breakdown, the causal ids,
and the reasoning gist. ``causal_forest_from_parsed`` likewise drops its
``ccusage_costs`` parameter, and ``telemetry.cost`` is gone.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal_tree import causal_forest_from_parsed, per_turn_rows
from telemetry.session_parser import UsageEvent, parse_session_file

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
SESSION = FIXTURES / "-Users-demo-Repos-proj" / "11111111-1111-1111-1111-111111111111.jsonl"


def test_rows_carry_tokens_without_a_cost_field() -> None:
    parsed = parse_session_file(SESSION)

    rows = per_turn_rows(parsed.usage_events)

    assert rows  # turns still emitted
    assert all(int(r["tokens_total"]) > 0 for r in rows)  # type: ignore[arg-type]
    assert all("cost_usd" not in r for r in rows)  # cost is Langfuse's job now


def test_rows_carry_cache_breakdown() -> None:
    # Each turn row still exposes cache_read vs cache_creation so the renderer can
    # frame cheap reuse against expensive cold-cache writes.
    event = UsageEvent(
        session_id="sess-a",
        ts="2026-06-13T12:00:01.000Z",
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read=900,
        cache_creation=300,
        source="main",
    )

    rows = per_turn_rows([event])

    assert rows[0]["cache_read"] == 900
    assert rows[0]["cache_creation"] == 300


def test_rows_carry_turn_causal_ids() -> None:
    # The per-turn row carries the turn's causal ids (uuid / parent_uuid /
    # is_sidechain) so the causal builder keys turns by id, never a shared timestamp.
    event = UsageEvent(
        session_id="sess-a",
        ts="2026-06-13T12:00:01.000Z",
        model="claude-opus-4-8",
        input_tokens=10,
        output_tokens=5,
        cache_read=0,
        cache_creation=0,
        source="subagent",
        agent_id="AG1",
        uuid="s2",
        parent_uuid="s1",
        is_sidechain=True,
    )

    row = per_turn_rows([event])[0]

    assert row["uuid"] == "s2"
    assert row["parent_uuid"] == "s1"
    assert row["is_sidechain"] is True


def test_reasoning_gist_joins_onto_its_turn() -> None:
    parsed = parse_session_file(SESSION)

    rows = per_turn_rows(parsed.usage_events, reasoning_refs=parsed.reasoning_refs)

    assert any(r["reasoning"] == "Running skill" for r in rows)
    # A turn with no reasoning ref carries reasoning=None — never a stray gist.
    assert any(r["reasoning"] is None for r in rows)


def _walk(forest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in forest:
        out.append(node)
        out.extend(_walk(node.get("children", [])))
    return out


def test_forest_builds_without_a_ccusage_costs_arg() -> None:
    parsed = parse_session_file(SESSION)

    forest = causal_forest_from_parsed(parsed, [])

    assert forest
    turns = [n for n in _walk(forest) if n["kind"] == "turn"]
    assert turns
    assert all(n["own_cost_usd"] == 0.0 for n in turns)  # cost no longer attributed


def test_cost_module_is_retired() -> None:
    # #91: scripts/telemetry/cost.py (the ccusage loader + pull-cost attribution)
    # is deleted; nothing imports it after #90 removed the pull path.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("telemetry.cost")
