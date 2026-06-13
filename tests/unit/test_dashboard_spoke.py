"""Spoke-view query tests (Issue #23, subtask 1 — RED).

The spoke view picks one ``spoke_run_id`` and renders the step/sub-step tree
(lifecycle -> cycle phase -> hook), each node showing time, token cost, status,
and human interactions, with per-subtree rollups. These tests pin the query
layer that feeds that view against the JSONL fixture.
"""

from __future__ import annotations

from _dashboard_helpers import load_queries, store


def _find(nodes, span_id):
    """Depth-first lookup of a node by span_id within a forest (raises if absent)."""
    for node in nodes:
        if node["span_id"] == span_id:
            return node
        try:
            return _find(node["children"], span_id)
        except KeyError:
            continue
    raise KeyError(span_id)


def test_load_jsonl_skips_blank_lines_and_parses_human():
    queries = load_queries()
    from _dashboard_helpers import FIXTURE

    events = queries.load_jsonl(FIXTURE)

    assert len(events) == 16
    green = next(e for e in events if e["span_id"] == "sa_green")
    assert green["human"] == {"type": "approval", "wait_ms": 4000}


def test_spoke_run_ids_are_unique_and_sorted():
    ids = store().spoke_run_ids()

    assert ids == [
        "feature/a+1000",
        "feature/b+1100",
        "feature/c+2000",
        "feature/d+2100",
    ]


def test_spoke_tree_roots_and_child_ordering():
    tree = store().spoke_tree("feature/a+1000")

    # Single lifecycle root for this spoke.
    assert [n["span_id"] for n in tree] == ["sa_root"]
    root = tree[0]
    # Children ordered by ts_start: red, green, review, push.
    assert [c["span_id"] for c in root["children"]] == [
        "sa_red",
        "sa_green",
        "sa_review",
        "sa_push",
    ]


def test_spoke_tree_node_carries_own_metrics():
    tree = store().spoke_tree("feature/a+1000")
    green = _find(tree, "sa_green")

    assert green["kind"] == "step"
    assert green["name"] == "solo-cycle"
    assert green["phase"] == "green"
    assert green["status"] == "success"
    assert green["duration_ms"] == 1000
    assert green["cost_usd"] == 0.10
    assert green["tokens_in"] == 300
    assert green["tokens_out"] == 150
    assert green["human_count"] == 1


def test_spoke_tree_subtree_rollup_sums_descendants():
    tree = store().spoke_tree("feature/a+1000")
    root = tree[0]

    # 3000 + 500 + 100 + 1000 + 200 + 500 + 700 + 300
    assert root["subtree"]["duration_ms"] == 6300
    # 0.02 + 0.10 + 0.05 (nulls treated as zero)
    assert round(root["subtree"]["cost_usd"], 2) == 0.17
    # 50 + 300 + 100
    assert root["subtree"]["tokens_in"] == 450
    # sa_green (approval) + sa_hook_human (prompt)
    assert root["subtree"]["human_count"] == 2


def test_spoke_tree_nests_hooks_under_steps():
    tree = store().spoke_tree("feature/a+1000")
    green = _find(tree, "sa_green")

    assert {c["span_id"] for c in green["children"]} == {
        "sa_green_hook",
        "sa_hook_human",
    }


def test_unknown_spoke_returns_empty_forest():
    assert store().spoke_tree("does/not+exist") == []
