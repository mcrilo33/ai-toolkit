"""Lazy per-spoke build + cache (Issue #53 track D).

The v3 cross-cutting requirement (docs/dashboard-spoke-trace-scope.md): startup
loads only the spoke index for the selectbox; the selected spoke's tree is built
on demand and memoized on ``(spoke_id, log-mtime)``, so a re-select is instant and
a drill toggle reuses cached rows rather than rebuilding. A changed log mtime
invalidates the entry.

These tests drive ``dashboard/app.py`` with a MagicMock streamlit and a fake store
that counts ``spoke_steps`` calls — the observable proxy for "did it rebuild?".
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries


def _streamlit_stub() -> MagicMock:
    st = MagicMock()
    st.columns.side_effect = lambda spec: [
        MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
    ]
    st.tabs.side_effect = lambda names: [MagicMock() for _ in names]
    st.selectbox.side_effect = lambda _label, options, **_kw: options[0]
    st.checkbox.side_effect = lambda *_a, **_kw: False
    return st


def _app(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", _streamlit_stub())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def _interval(name: str) -> dict:
    return {
        "span_id": None,
        "parent_id": None,
        "kind": "interval",
        "name": name,
        "summary": None,
        "phase": None,
        "status": "success",
        "ts_start": None,
        "ts_end": None,
        "duration_ms": None,
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "agent": "main",
        "human_count": 0,
        "children": [],
    }


class _FakeStore:
    """A store recording each ``spoke_steps`` build, returning a one-node forest."""

    def __init__(self, spoke_ids: list[str]):
        self._ids = spoke_ids
        self.built: list[str] = []

    def spoke_run_ids(self, _real_repo_prefix=None) -> list[str]:
        return self._ids

    def spoke_steps(self, spoke_run_id: str) -> list[dict]:
        self.built.append(spoke_run_id)
        return [_interval(spoke_run_id)]

    def spoke_meta_by_kind(self, _spoke_run_id: str) -> list[dict]:
        return []


def test_spoke_forest_builds_once_per_spoke_and_source(monkeypatch):
    app = _app(monkeypatch)
    store = _FakeStore(["feature/x+1"])

    first = app._spoke_forest(store, "feature/x+1", "correlated:100")
    again = app._spoke_forest(store, "feature/x+1", "correlated:100")

    assert store.built == ["feature/x+1"]  # built once
    assert again is first  # cached object reused — a drill toggle never rebuilds


def test_spoke_forest_rebuilds_when_source_key_changes(monkeypatch):
    app = _app(monkeypatch)
    store = _FakeStore(["feature/x+1"])

    app._spoke_forest(store, "feature/x+1", "correlated:100")
    app._spoke_forest(store, "feature/x+1", "correlated:200")  # the log changed under us

    assert store.built == ["feature/x+1", "feature/x+1"]


def test_render_reuses_cached_forest_across_reruns(monkeypatch):
    app = _app(monkeypatch)
    store = _FakeStore(["feature/a+1", "feature/b+2"])

    app.render_spoke_view(store, source_key="correlated:100")
    app.render_spoke_view(store, source_key="correlated:100")  # a Streamlit rerun / re-select

    # Only the SELECTED spoke is built (not all), and only once across reruns.
    assert store.built == ["feature/a+1"]


def test_render_rebuilds_when_correlation_toggled(monkeypatch):
    app = _app(monkeypatch)
    store = _FakeStore(["feature/a+1"])

    # Toggling the correlation source swaps the underlying store at the same log
    # mtime; the source_key must change so the cache does not serve the other tree.
    app.render_spoke_view(store, source_key="correlated:100")
    app.render_spoke_view(store, source_key="raw:100")

    assert store.built == ["feature/a+1", "feature/a+1"]


def test_resolve_store_keys_correlated_store_on_log_mtime(monkeypatch, tmp_path):
    app = _app(monkeypatch)
    calls = []
    monkeypatch.setattr(app, "resolve_projects_dir", lambda: tmp_path)  # exists → correlated
    monkeypatch.setattr(
        app,
        "load_correlated_store",
        lambda _span_log, _projects_dir, mtime: calls.append(mtime) or object(),
    )
    span_log = tmp_path / "events.jsonl"
    span_log.write_text("")

    store, mode = app._resolve_store(span_log, 1234.5)

    # The push-log mtime keys the correlated store so a fresh log rebuilds it, not
    # just the forest — otherwise the (spoke_id, source_key) cache serves stale data.
    assert mode == "correlated"
    assert calls == [1234.5]
    assert store is not None
