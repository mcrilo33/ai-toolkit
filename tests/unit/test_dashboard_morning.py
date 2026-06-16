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


# ── _is_real_spoke_repo: the defense-in-depth fixture-spoke filter (#55) ──────


def test_is_real_spoke_repo_accepts_the_toolkit_checkout() -> None:
    queries = load_queries()

    assert queries._is_real_spoke_repo("ai-toolkit") is True
    assert queries._is_real_spoke_repo("ai-toolkit-55") is True  # a worktree checkout


def test_is_real_spoke_repo_rejects_sandbox_and_missing_repos() -> None:
    queries = load_queries()

    # The fixture-pollution hub basenames called out in #55, plus a missing repo.
    for fake in ("hub-8", "proj", "proj-8", "spoke", "work", "test_gauntlet_x"):
        assert queries._is_real_spoke_repo(fake) is False, fake
    assert queries._is_real_spoke_repo(None) is False


def test_is_real_spoke_repo_honours_an_explicit_prefix() -> None:
    queries = load_queries()

    assert queries._is_real_spoke_repo("proj", "proj") is True
    assert queries._is_real_spoke_repo("ai-toolkit", "proj") is False


# ── spoke_run_ids: an OPT-IN filter; the bare primitive stays unfiltered ──────


def test_spoke_run_ids_unfiltered_by_default_keeps_fixture_runs() -> None:
    store = store_v2()

    ids = store.spoke_run_ids()

    # The bare primitive is the shared seam — it lists EVERY run (other views and
    # the parser-pinned telemetry fixture rely on this). No filtering by default.
    assert "feature/v2+1000" in ids
    assert "feature/99-pushguard+1000" in ids


def test_spoke_run_ids_filters_fixture_runs_when_prefix_given() -> None:
    queries = load_queries()
    store = store_v2()

    ids = store.spoke_run_ids(queries.REAL_REPO_PREFIX)

    assert "feature/v2+1000" in ids
    assert "feature/99-pushguard+1000" not in ids  # repo='hub-8' is not real


def test_spoke_run_ids_keeps_run_with_a_mixed_real_and_unknown_repo() -> None:
    queries = load_queries()
    # A real run's session spans fall back to repo='unknown' (no cwd); only the
    # push span carries 'ai-toolkit'. The filter must keep the run on ANY real span,
    # not be fooled by a single representative repo.
    spans = [
        {
            "span_id": "p",
            "spoke_run_id": "feature/7-mix+1000",
            "repo": "ai-toolkit",
            "ts_start": "2026-01-01T00:00:00Z",
            "ts_end": "2026-01-01T00:00:01Z",
        },
        {
            "span_id": "s",
            "spoke_run_id": "feature/7-mix+1000",
            "repo": "unknown",
            "ts_start": "2026-01-01T00:00:02Z",
            "ts_end": "2026-01-01T00:00:03Z",
        },
    ]
    store = queries.SpanStore.from_events(spans)

    ids = store.spoke_run_ids(queries.REAL_REPO_PREFIX)

    assert ids == ["feature/7-mix+1000"]


# ── morning_rows: a per-run cost lens reusing spoke_run_summary ───────────────


def test_morning_rows_excludes_fixture_repo_spokes() -> None:
    store = store_v2()

    rows = store.morning_rows()

    run_ids = {row["spoke_run_id"] for row in rows}
    assert "feature/v2+1000" in run_ids, "the real ai-toolkit spoke surfaces"
    assert "feature/99-pushguard+1000" not in run_ids, "the hub-8 fixture spoke is filtered"


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
        # The telemetry fixture's repo is pinned to 'proj' by the session-parser
        # tests, so opt that prefix in to exercise triage annotation over it.
        rows = store.morning_rows(triage={"22": "conflict"}, real_repo_prefix="proj")
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
