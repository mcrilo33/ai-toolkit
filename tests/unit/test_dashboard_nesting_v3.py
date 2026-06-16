"""Nesting for the v3 spoke trace (Issue #52 Track C — RED).

Subtask 2 (nesting) covers, against the ``feature/47`` golden fixture:

- **Hard agent recursion** — ``agent → agent → agent`` nests by ``parent_id`` and a
  leaf tool sits under the deepest agent.
- **Workflow fan-out** — ``workflow → workflow_phase → agent`` nests; the workflow
  and phase containers own ``$0`` (cost lives on the agent/turn leaves).
- **Approval → tool** — a denied approval brackets the tool it blocked (never ran).
- **Subagent interval-fallback** — a subagent turn whose timestamp sits inside a
  phase interval but outside every ``agent`` window falls back to that phase bucket
  rather than orphaning to ``(unresolved)`` (the defect-#3 fix).
- **Actor column** — agent rows carry their sub-agent name; workflow/script/hooks/
  sidecar rows carry their actor; the Actor column is no longer a bare ``main``.
- **Soft skill scope-band** — a skill renders as a ``scope-band`` holding the turns
  it influenced (option A), carrying only its load cost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import store_from

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
V3_SPANS = _FIXTURES / "dashboard_golden_spoke.jsonl"
V3_TURNS = _FIXTURES / "dashboard_golden_spoke_turns.jsonl"
SPOKE_RUN_ID = "feature/47+1700000000"


def _forest() -> list[dict]:
    return store_from(V3_SPANS, V3_TURNS).spoke_steps(SPOKE_RUN_ID)


def _walk(forest: list[dict]):
    stack = list(forest)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node["children"])


def _find(forest: list[dict], span_id: str) -> dict | None:
    return next((n for n in _walk(forest) if n.get("span_id") == span_id), None)


def _child_ids(node: dict) -> set[str]:
    ids: set[str] = set()
    stack = list(node["children"])
    while stack:
        n = stack.pop()
        if n.get("span_id") is not None:
            ids.add(n["span_id"])
        stack.extend(n["children"])
    return ids


def test_agent_recursion_nests_three_deep() -> None:
    forest = _forest()
    rec1 = _find(forest, "g_rec1")
    assert rec1 is not None
    assert "g_rec2" in _child_ids(rec1) and "g_rec3" in _child_ids(rec1)
    rec3 = _find(forest, "g_rec3")
    assert rec3 is not None and "g_rec3_tool" in _child_ids(rec3)


def test_workflow_chain_nests_and_containers_own_nothing() -> None:
    forest = _forest()
    wf = _find(forest, "g_wf")
    assert wf is not None
    ids = _child_ids(wf)
    assert {"g_wf_p1", "g_wf_p2", "g_wf_a1", "g_wf_a2", "g_wf_a3"} <= ids
    # Workflow + phase are display-only containers: cost lives on the agent/turn
    # leaves, never on the container itself.
    for cid in ("g_wf", "g_wf_p1", "g_wf_p2"):
        node = _find(forest, cid)
        assert node["own_cost_usd"] == 0.0, f"{cid} container must own $0"


def test_denied_approval_brackets_the_blocked_tool() -> None:
    forest = _forest()
    approval = _find(forest, "g_approval")
    assert approval is not None
    assert "g_tool_blocked" in _child_ids(approval)


def test_subagent_turn_inside_phase_falls_back_to_its_bucket(tmp_path: Path) -> None:
    # A subagent turn whose ts sits inside the design interval but outside every
    # agent window must land in the design bucket, never (unresolved).
    turns = [json.loads(line) for line in V3_TURNS.read_text().splitlines() if line.strip()]
    turns.append(
        {
            "session_id": "sess-47a",
            "ts": "2026-06-12T23:01:30Z",  # inside design [23:01:01, 23:05:05]
            "model": "claude-opus-4-8",
            "source": "subagent",
            "agent_id": "ghost",
            "tokens_in": 10,
            "tokens_out": 5,
            "tokens_total": 15,
            "cost_usd": 0.01,
        }
    )
    crafted = tmp_path / "turns.jsonl"
    crafted.write_text("\n".join(json.dumps(t) for t in turns) + "\n")
    forest = store_from(V3_SPANS, crafted).spoke_steps(SPOKE_RUN_ID)

    unresolved = next((r for r in forest if r["kind"] == "unresolved"), None)
    assert unresolved is None, "the off-agent subagent turn orphaned to (unresolved)"
    design = next((r for r in forest if r["name"] == "design"), None)
    assert design is not None
    ghost_cost = sum(
        n["own_cost_usd"]
        for n in _walk([design])
        if n["kind"] == "turn" and n["own_cost_usd"] == 0.01
    )
    assert ghost_cost == 0.01, "the fallback turn did not land in the design bucket"


def test_actor_column_carries_real_actors() -> None:
    forest = _forest()
    assert _find(forest, "g_rec1")["actor"] == "Explore"
    assert _find(forest, "g_wf_a1")["actor"] == "Plan"
    assert _find(forest, "g_wf")["actor"] == "workflow"
    assert _find(forest, "g_script_red")["actor"] == "script"
    assert _find(forest, "g_hook_sidecar")["actor"] == "sidecar"
    hooks = next(n for n in _walk(forest) if n["kind"] == "hooks")
    assert hooks["actor"] == "hooks"


def test_skill_renders_as_a_scope_band() -> None:
    forest = _forest()
    band = next((n for n in _walk(forest) if n["kind"] == "scope-band"), None)
    assert band is not None, "the skill did not render as a scope-band"
    assert "tdd-red" in band["name"]
    # The band carries only its load cost (the agent/turn leaves under it own theirs).
    assert band["own_cost_usd"] == 0.0
    # It influenced the RED-phase agent that ran under it.
    assert "g_agent_tddred" in _child_ids(band)
