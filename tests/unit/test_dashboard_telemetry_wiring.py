"""Wiring test: the dashboard query layer over Issue #22's real dataset (#23).

Subtask 1 built ``queries.py`` against fixtures with a ``from_connection`` seam
for "when Issue #22 lands". #22 has landed: its ``telemetry.queries.connect``
exposes a ``spans`` table whose columns match this module's exactly. This test
drives ``SpanStore.from_telemetry`` over #22's own fixtures to prove every view
runs unchanged against the correlated push+pull dataset — not just our JSONL.
"""

from __future__ import annotations

from pathlib import Path

from _dashboard_helpers import load_queries

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = _REPO_ROOT / "scripts"
TELEMETRY_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "telemetry"
SPOKE_RUN_ID = "feature/22-demo+1700000000"


def _telemetry_store():
    queries = load_queries()
    return queries.SpanStore.from_telemetry(
        events_path=TELEMETRY_FIXTURES / "events.jsonl",
        projects_root=TELEMETRY_FIXTURES / "projects",
        ccusage_costs={},
        scripts_dir=SCRIPTS_DIR,
    )


def test_from_telemetry_exposes_the_spoke_run():
    assert SPOKE_RUN_ID in _telemetry_store().spoke_run_ids()


def test_spoke_tree_builds_over_unified_push_pull_dataset():
    tree = _telemetry_store().spoke_tree(SPOKE_RUN_ID)

    assert tree  # non-empty forest
    node = tree[0]
    assert {"span_id", "kind", "duration_ms", "human_count", "subtree", "children"} <= (node.keys())


def test_aggregate_runs_over_unified_dataset():
    rows = _telemetry_store().aggregate()

    assert rows
    assert all("mean_duration_ms" in row and "frequency" in row for row in rows)
