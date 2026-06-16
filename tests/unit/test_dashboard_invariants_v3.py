"""Forest invariants for the v3 spoke trace (Issue #52 Track C — RED).

Subtask 4 (invariants) enforces the spec's two cross-cutting guarantees over the
``feature/47`` golden fixture, plus the ``(unresolved)`` ~empty acceptance bar:

- **Conservation** — ``Σ owned == Σ turns``: the once-per-turn cost summed across the
  whole forest equals the raw turns total, with no double-count under any
  re-parenting (workflow/phase containers, scope-bands, dividers all own ``$0``).
- **Status rollup** — a container's status is *last-event-wins* (Issue #57): the status
  of the chronologically last leaf in its subtree (the step's closing marker), NOT the
  worst severity — so a recovered deny/failure stays at its leaf and never reddens an
  ancestor that completed.
- **No bogus ``(unresolved)``** — every fixture span/turn is claimed; the forest has
  no ``(unresolved)`` root once the spine + nesting fallbacks have drained it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
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
        n = stack.pop()
        yield n
        stack.extend(n["children"])


def _ts(iso: str | None) -> float:
    """Epoch seconds for an ISO timestamp (0.0 when absent), for last-event ordering."""
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


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


def test_status_rolls_up_to_last_event_not_worst_child() -> None:
    # last-event-wins (Issue #57): a container takes the status of the chronologically
    # last leaf in its subtree (the step's closing marker), never the worst severity.
    # So the rolled-up status must always be a status carried by one of the latest
    # leaves — a recovered deny deeper/earlier in the subtree must not surface.
    forest = _forest()
    for node in _walk(forest):
        if not node["children"]:
            continue
        leaves = [n for n in _walk([node]) if not n["children"]]
        last_ts = max(_ts(leaf["ts_start"]) for leaf in leaves)
        last_statuses = {leaf["status"] for leaf in leaves if _ts(leaf["ts_start"]) == last_ts}
        assert node["rollup"]["status"] in last_statuses, (
            f"{node['kind']} status is not its last-event status"
        )


def test_recovered_deny_stays_at_its_leaf_and_does_not_propagate() -> None:
    # The denied approval (g_approval) is followed by later successful activity in its
    # bucket (the closing marker), so the deny must NOT redden the container — it stays
    # only at the leaf. Inverts the old worst-child propagation.
    forest = _forest()
    bucket = next(r for r in forest if "g_approval" in {n.get("span_id") for n in _walk([r])})
    assert bucket["rollup"]["status"] != "deny", "recovered deny wrongly reddened its container"
    approval = next(n for n in _walk([bucket]) if n.get("span_id") == "g_approval")
    blocked = next(n for n in _walk([bucket]) if n.get("span_id") == "g_tool_blocked")
    assert blocked["status"] == "deny", "deny is no longer visible at its own leaf"
    assert approval["rollup"]["status"] == "deny", "the deny subtree must still surface deny"


def test_no_unresolved_root_on_the_golden_fixture() -> None:
    forest = _forest()
    assert not any(r["kind"] == "unresolved" for r in forest), "(unresolved) is not empty"


def _load_tree():
    import importlib.util

    from _dashboard_helpers import DASHBOARD_DIR

    spec = importlib.util.spec_from_file_location("tree", DASHBOARD_DIR / "tree.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load tree module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_node(kind: str, status: str, children: list[dict], ts: str | None = None) -> dict:
    return {
        "kind": kind,
        "status": status,
        "ts_start": ts,
        "human_count": 0,
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "children": children,
    }


def test_status_is_last_event_not_max_severity() -> None:
    # last-event-wins (Issue #57): a container takes the status of the chronologically
    # LAST leaf in its subtree, never the worst. A deny followed by a later success
    # leaf must NOT escalate the container — the run recovered. This fails under the
    # old worst-child rollup, which would surface the deny.
    tree = _load_tree()
    early_deny = _raw_node("tool", "deny", [], ts="2026-06-12T23:00:00Z")
    later_ok = _raw_node("tool", "success", [], ts="2026-06-12T23:00:09Z")
    recovered = _raw_node("agent", "success", [early_deny, later_ok])
    tree._roll_up_steps(recovered)
    assert recovered["rollup"]["status"] == "success"

    # A subtree whose last leaf is a terminal failure (no recovery after) rolls up red
    # even when earlier activity succeeded.
    early_ok = _raw_node("tool", "success", [], ts="2026-06-12T23:00:00Z")
    last_fail = _raw_node("tool", "failure", [], ts="2026-06-12T23:00:09Z")
    failed = _raw_node("agent", "success", [early_ok, last_fail])
    tree._roll_up_steps(failed)
    assert failed["rollup"]["status"] == "failure"
