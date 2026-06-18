"""Smoke test for the v2 spoke render (Issue #35).

``dashboard/app.py`` is the thin Streamlit presentation layer; the data logic is
tested in ``test_dashboard_spoke_v2.py``. Streamlit cannot be imported in the
base test env, so we inject a ``MagicMock`` ``streamlit`` and drive the render to
prove it does not crash on real node shapes — locking in the no-nested-expander
and key-access guarantees against future Streamlit-API regressions.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries, store_v2

RUN = "feature/v2+1000"


def _streamlit_stub() -> MagicMock:
    """A MagicMock streamlit whose layout primitives return usable shapes."""
    st = MagicMock()
    st.columns.side_effect = lambda spec: [
        MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
    ]
    st.tabs.side_effect = lambda names: [MagicMock() for _ in names]
    st.selectbox.side_effect = lambda _label, options, **_kw: options[0]
    return st


def test_render_spoke_view_does_not_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", _streamlit_stub())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    app = load_app()

    # Renders the steps drill-down + meta tab over a real store without raising.
    app.render_spoke_view(store_v2())


def test_render_spoke_view_handles_empty_store(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", _streamlit_stub())
    queries = load_queries()
    monkeypatch.setitem(sys.modules, "queries", queries)
    app = load_app()

    app.render_spoke_view(queries.SpanStore.from_events([]))  # no spokes → info, no crash


def test_format_duration_is_compact_minutes_and_seconds():
    queries = load_queries()

    assert queries.format_duration(432_000) == "7m12s"  # 7 min 12 s
    assert queries.format_duration(45_000) == "45s"
    assert queries.format_duration(3_780_000) == "1h03m"


def test_idle_divider_renders_its_gap_duration(monkeypatch):
    st = _streamlit_stub()
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    app = load_app()

    # An idle gap is a duration-bearing line (Issue #79), not a zero-width divider.
    app._render_divider({"kind": "gap", "name": "idle", "duration_ms": 432_000})

    rendered = " ".join(str(call.args[0]) for call in st.markdown.call_args_list)
    assert "idle" in rendered
    assert "7m12s" in rendered
