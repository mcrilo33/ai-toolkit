"""Forest invariants for the v3 spoke trace (Issue #52 Track C — RED).

Subtask 4 (invariants) enforces the spec's two cross-cutting guarantees over the
``feature/47`` golden fixture, plus the ``(unresolved)`` ~empty acceptance bar:

- **Conservation** — ``Σ owned == Σ turns``: the once-per-turn cost summed across the
  whole forest equals the raw turns total, with no double-count under any
  re-parenting (workflow/phase containers, scope-bands, dividers all own ``$0``).
- **Status rollup** — a container's status is the worst status among its children,
  propagated once and uniformly (so a denied approval surfaces up its ancestors).
- **No bogus ``(unresolved)``** — every fixture span/turn is claimed; the forest has
  no ``(unresolved)`` root once the spine + nesting fallbacks have drained it.
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

_SEVERITY = {"deny": 4, "failure": 3, "warn": 2, "skipped": 1, "success": 0}


def _forest() -> list[dict]:
    return store_from(V3_SPANS, V3_TURNS).spoke_steps(SPOKE_RUN_ID)


def _walk(forest: list[dict]):
    stack = list(forest)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n["children"])


def _turns_total() -> float:
    turns = [json.loads(line) for line in V3_TURNS.read_text().splitlines() if line.strip()]
    return round(sum(t.get("cost_usd") or 0.0 for t in turns), 6)


def test_conservation_forest_cost_equals_turns_total() -> None:
    forest = _forest()
    rolled = round(sum((r["rollup"]["cost_usd"] or 0.0) for r in forest), 6)
    assert rolled == _turns_total(), f"forest {rolled} != turns {_turns_total()}"


def test_no_node_double_counts_owned_cost() -> None:
    # Cost lives only on turn leaves; every container (interval/agent/workflow/phase/
    # scope-band/divider) owns $0, so summing own_cost over the whole forest — not
    # just leaves — still equals the turns total (no re-parenting double-count).
    forest = _forest()
    owned = round(sum(n["own_cost_usd"] for n in _walk(forest)), 6)
    assert owned == _turns_total()
    for n in _walk(forest):
        if n["children"] and n["kind"] in ("interval", "workflow", "workflow_phase", "scope-band"):
            assert n["own_cost_usd"] == 0.0, f"{n['kind']} container owns cost"


def test_status_rolls_up_worst_child() -> None:
    forest = _forest()
    for node in _walk(forest):
        children = node["children"]
        if not children:
            continue
        worst = max(
            [_SEVERITY.get(node["status"], 0)]
            + [_SEVERITY.get(c["rollup"]["status"], 0) for c in children]
        )
        assert _SEVERITY.get(node["rollup"]["status"], 0) == worst, (
            f"{node['kind']} status rollup is not worst-child"
        )


def test_denied_approval_propagates_deny_to_its_ancestors() -> None:
    forest = _forest()
    teardown = next(r for r in forest if "g_approval" in {n.get("span_id") for n in _walk([r])})
    assert teardown["rollup"]["status"] == "deny", "deny did not roll up to the bucket"


def test_no_unresolved_root_on_the_golden_fixture() -> None:
    forest = _forest()
    assert not any(r["kind"] == "unresolved" for r in forest), "(unresolved) is not empty"
