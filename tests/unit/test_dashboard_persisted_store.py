"""Dashboard reads over the persisted store — byte-identical parity (Issue #62, RED).

The acceptance contract: a spoke ingested into the persisted ``store.duckdb`` renders
byte-identical to the old read-time parser (``SpanStore.from_telemetry``) for that
spoke. ``from_persisted_store`` attaches the store read-only, copies it into memory,
and answers every view exactly as before — this is a load-scoping change, not a
semantics change, so the spoke trace must match field-for-field.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _dashboard_helpers import load_queries

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.store import ingest_store

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
RUN = "feature/22-demo+1700000000"
CCUSAGE = {SESSION_ID: 2.80}


def _persisted_store(tmp_path: Path):
    queries = load_queries()
    store_path = tmp_path / "store.duckdb"
    # watermark=0 → every fixture session is post-watermark and ingests in full.
    ingest_store(
        store_path,
        events_path=EVENTS,
        projects_root=PROJECTS,
        ccusage_costs=CCUSAGE,
        watermark=0.0,
    )
    return queries.SpanStore.from_persisted_store(store_path)


def _parsed_store():
    queries = load_queries()
    return queries.SpanStore.from_telemetry(
        events_path=EVENTS,
        projects_root=PROJECTS,
        ccusage_costs=CCUSAGE,
        scripts_dir=_SCRIPTS,
    )


def test_persisted_store_lists_the_post_watermark_spoke(tmp_path: Path) -> None:
    store = _persisted_store(tmp_path)

    assert RUN in store.spoke_run_ids()


def test_spoke_causal_forest_byte_identical_to_parser(tmp_path: Path) -> None:
    persisted = _persisted_store(tmp_path)
    parsed = _parsed_store()

    assert persisted.spoke_causal_forest(RUN, PROJECTS, CCUSAGE) == parsed.spoke_causal_forest(
        RUN, PROJECTS, CCUSAGE
    )


def test_spoke_tree_byte_identical_to_parser(tmp_path: Path) -> None:
    persisted = _persisted_store(tmp_path)
    parsed = _parsed_store()

    assert persisted.spoke_tree(RUN) == parsed.spoke_tree(RUN)


def test_meta_by_kind_byte_identical_to_parser(tmp_path: Path) -> None:
    persisted = _persisted_store(tmp_path)
    parsed = _parsed_store()

    assert persisted.spoke_meta_by_kind(RUN) == parsed.spoke_meta_by_kind(RUN)


def test_aggregate_and_automatability_work_over_the_store(tmp_path: Path) -> None:
    store = _persisted_store(tmp_path)

    # The reduced views must run over the store (no exception) and surface the run.
    assert store.aggregate()
    # The run's turns flow through, so the budget/cost reconciles non-trivially.
    summary = store.morning_rows(real_repo_prefix=None)
    assert any(row["spoke_run_id"] == RUN for row in summary)
