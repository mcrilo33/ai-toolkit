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


def test_spoke_run_ids_are_unique_and_newest_first():
    # Ordered by latest activity (max ts_end), newest first — not by id string.
    # max(ts_end): d 06-11T13:00:05 > c 06-11T12:00:11 > b 06-10T13:00:10 > a 06-10T12:00:20.
    ids = store().spoke_run_ids()

    assert ids == [
        "feature/d+2100",
        "feature/c+2000",
        "feature/b+1100",
        "feature/a+1000",
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


def test_format_spoke_label_renders_branch_and_spawn_date():
    queries = load_queries()

    # id is "<branch>+<spawn-epoch>"; 1700000000 == 2023-11-14 UTC.
    label = queries.format_spoke_label("feature/22-demo+1700000000")

    assert label == "feature/22-demo · 2023-11-14"


def test_format_spoke_label_keeps_branch_with_internal_plus():
    queries = load_queries()

    # Only the final "+epoch" is the spawn stamp; earlier '+' stays in the branch.
    label = queries.format_spoke_label("feature/a+b+1700000000")

    assert label == "feature/a+b · 2023-11-14"


def test_format_spoke_label_falls_back_to_raw_id_when_malformed():
    queries = load_queries()

    assert queries.format_spoke_label("no-epoch") == "no-epoch"
    assert queries.format_spoke_label("branch+abc") == "branch+abc"


def test_format_spoke_label_falls_back_when_epoch_out_of_range():
    queries = load_queries()

    # All-digits but past time_t range — must not crash the selectbox format_func.
    assert queries.format_spoke_label("branch+99999999999999999999") == (
        "branch+99999999999999999999"
    )
    assert queries.format_spoke_label("branch+10000000000000") == "branch+10000000000000"


def test_spoke_run_ids_ordering_is_deterministic_on_tied_activity():
    queries = load_queries()
    # Two spokes with identical activity windows must order deterministically.
    spans = [
        {
            "span_id": "s1",
            "spoke_run_id": "feature/aaa+1000",
            "ts_start": "2026-01-01T00:00:00Z",
            "ts_end": "2026-01-01T00:00:10Z",
        },
        {
            "span_id": "s2",
            "spoke_run_id": "feature/bbb+1000",
            "ts_start": "2026-01-01T00:00:00Z",
            "ts_end": "2026-01-01T00:00:10Z",
        },
    ]

    ids = queries.SpanStore.from_events(spans).spoke_run_ids()

    assert ids == ["feature/bbb+1000", "feature/aaa+1000"]
