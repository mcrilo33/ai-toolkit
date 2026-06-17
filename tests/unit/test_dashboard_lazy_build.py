"""Lazy per-spoke build + cache (Issue #53 track D).

The v3 cross-cutting requirement (docs/dashboard-spoke-trace-scope.md): startup
loads only the spoke index for the selectbox; the selected spoke's tree is built
on demand and memoized on ``(spoke_id, log-mtime)``, so a re-select is instant and
a drill toggle reuses cached rows rather than rebuilding. A changed log mtime
invalidates the entry.

These tests drive ``dashboard/app.py`` with a MagicMock streamlit and a fake store
that counts ``spoke_causal_forest`` calls — the observable proxy for "did it rebuild?".
"""

from __future__ import annotations

import sys
from pathlib import Path
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
    app = load_app()
    monkeypatch.setattr(app, "_ccusage_costs", lambda: {})  # never shell out to npx in tests
    return app


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
    """A store recording each causal-forest build, returning a one-node forest."""

    def __init__(self, spoke_ids: list[str]):
        self._ids = spoke_ids
        self.built: list[str] = []

    def spoke_run_ids(self, _real_repo_prefix=None) -> list[str]:
        return self._ids

    def spoke_causal_forest(
        self, spoke_run_id: str, _projects_dir=None, _ccusage=None
    ) -> list[dict]:
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


def test_build_uses_causal_trace_when_transcripts_present(monkeypatch):
    # Issue #65/#80: when the spoke's transcripts are on disk, _build_spoke_forest renders
    # the causal trace (a forest of node_id-bearing causal nodes) — the sole builder.
    app = _app(monkeypatch)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
    store = load_queries().SpanStore.from_jsonl(fixtures / "events.jsonl")
    monkeypatch.setattr(app, "resolve_projects_dir", lambda: fixtures / "projects")
    monkeypatch.setattr(app, "_ccusage_costs", lambda: {})

    forest = app._build_spoke_forest(store, "feature/22-demo+1700000000")

    assert forest and all("node_id" in root for root in forest)  # causal nodes


def test_resolve_store_keys_correlated_forest_on_store_version(monkeypatch, tmp_path):
    # Issue #62: the correlated source is the persisted store, ingested on open; its
    # content VERSION (not the log mtime) keys the forest cache, so a new spoke's delta
    # rebuilds the tree while an unchanged store serves the cached one.
    app = _app(monkeypatch)
    calls = []
    monkeypatch.setattr(app, "resolve_projects_dir", lambda: tmp_path)  # exists → correlated
    monkeypatch.setattr(app, "resolve_store_path", lambda: tmp_path / "store.duckdb")
    sentinel = object()
    monkeypatch.setattr(
        app,
        "load_correlated_store",
        lambda span_log, projects_dir, store_path: calls.append(store_path) or (sentinel, "v123"),
    )
    span_log = tmp_path / "events.jsonl"
    span_log.write_text("")

    store, source_key = app._resolve_store(span_log, 1234.5)

    assert store is sentinel
    assert source_key == "correlated:v123"
    assert calls == [str(tmp_path / "store.duckdb")]
