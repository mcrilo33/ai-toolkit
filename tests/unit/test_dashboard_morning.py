"""Tests for the dashboard 'Morning' view (issue #40 ST7, Phase 4).

AC#6 asks for a morning view SHARED WITH #35: a filtered lens over the existing
spoke-run telemetry. ``SpanStore.morning_rows`` reuses ``spoke_run_summary`` (the
authoritative ccusage-sourced per-spoke cost), labels each run with its issue from
the ``spoke_run_id`` shape ``<type>/<issue>-<slug>+<epoch>``, and annotates land
readiness from the night's land-triage cache (the shell report's CONFLICTS/LAND
verdict). The render is a thin Streamlit table (smoke-tested with a stub, like the
other views).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries, store_v2

TELEMETRY_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _streamlit_stub() -> MagicMock:
    st = MagicMock()
    st.columns.side_effect = lambda spec: [
        MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
    ]
    st.tabs.side_effect = lambda names: [MagicMock() for _ in names]
    st.radio.side_effect = lambda _label, options, **_kw: options[0]
    return st


# ── _issue_from_spoke_run_id: parse the issue out of the run id ───────────────


def test_issue_from_spoke_run_id_parses_numbered_branch() -> None:
    queries = load_queries()

    assert queries._issue_from_spoke_run_id("feature/22-demo+1700000000") == "22"
    assert queries._issue_from_spoke_run_id("fix/103-x+99") == "103"


def test_issue_from_spoke_run_id_none_for_unnumbered() -> None:
    queries = load_queries()

    # An ad-hoc/express branch with no leading issue number -> None.
    assert queries._issue_from_spoke_run_id("feature/a+1000") is None
    assert queries._issue_from_spoke_run_id(None) is None


# ── morning_rows: a per-run cost lens reusing spoke_run_summary ───────────────


def test_morning_rows_returns_a_row_per_spoke_run_with_cost() -> None:
    store = store_v2()

    rows = store.morning_rows()

    assert rows, "expected at least one spoke run"
    for row in rows:
        assert "spoke_run_id" in row
        assert "total_cost_usd" in row
        assert "merge" in row  # land-triage annotation slot (None without a cache)


def test_morning_rows_annotates_triage_verdict() -> None:
    queries = load_queries()
    store = queries.SpanStore.from_telemetry(
        events_path=TELEMETRY_FIXTURES / "events.jsonl",
        projects_root=TELEMETRY_FIXTURES / "projects",
        ccusage_costs={SESSION_ID: 2.80},
        scripts_dir=SCRIPTS_DIR,
    )
    try:
        rows = store.morning_rows(triage={"22": "conflict"})
    finally:
        store.close()

    issue22 = [r for r in rows if r["issue"] == "22"]
    assert issue22, "the feature/22-demo run must be labelled with issue 22"
    assert issue22[0]["merge"] == "conflict", "the land-triage verdict annotates the row"
    assert issue22[0]["total_cost_usd"] is not None, "cost is reused from spoke_run_summary"


# ── render: the thin Streamlit view does not crash ───────────────────────────


def test_render_morning_view_does_not_crash(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "streamlit", _streamlit_stub())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    app = load_app()

    app.render_morning_view(store_v2())


def test_render_morning_view_handles_empty_store(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "streamlit", _streamlit_stub())
    queries = load_queries()
    monkeypatch.setitem(sys.modules, "queries", queries)
    app = load_app()

    app.render_morning_view(queries.SpanStore.from_events([]))  # no spokes -> info, no crash
