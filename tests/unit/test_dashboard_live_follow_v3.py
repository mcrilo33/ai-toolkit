"""Live-follow a running spoke in the v3 spoke trace (Issue #67, B — RED).

When the selected spoke is still running, the view optionally auto-refreshes (~5–10s)
so the causal tree grows as the transcript grows — with NO live-emission hooks and NO
daemon: the per-spoke lazy parse re-reads the live-growing transcript directly. This
locks in the pieces that make that cheap and correct:

* ``SpanStore.spoke_transcript_mtime`` — the newest mtime across ONLY the spoke's own
  session transcripts (incl. their sub-agent files), the hook-free "still running" signal.
* ``_spoke_is_running`` — a transcript written within a freshness window is running.
* ``_forest_cache_key`` — folds the transcript mtime into the per-spoke forest cache key,
  so a grown transcript rebuilds the tree on refresh while a static one (an expand/collapse
  toggle, no new turns) reuses the cached tree instantly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import load_app, load_queries

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = _FIXTURES / "events.jsonl"
PROJECTS = _FIXTURES / "projects"
SPOKE = "feature/22-demo+1700000000"
SPOKE_SESSION = "11111111-1111-1111-1111-111111111111"


def _store():
    return load_queries().SpanStore.from_jsonl(EVENTS)


def _app(monkeypatch) -> object:
    monkeypatch.setitem(sys.modules, "streamlit", _stub_streamlit())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def _stub_streamlit() -> MagicMock:
    st = MagicMock()
    st.columns.side_effect = lambda spec: [
        MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))
    ]
    st.tabs.side_effect = lambda names: [MagicMock() for _ in names]
    st.selectbox.side_effect = lambda _label, options, **_kw: options[0]
    st.toggle.return_value = True
    # A fragment decorator that runs the body inline so the smoke exercises it.
    st.fragment.side_effect = lambda *a, **k: lambda fn: fn
    return st


# --- queries: the per-spoke transcript mtime signal ----------------------------


class TestLatestTranscriptMtime:
    def test_newest_file_including_subagents_wins(self, tmp_path) -> None:
        queries = load_queries()
        slug = tmp_path / "-Users-x-Repos-proj"
        sub = slug / "sess-1" / "subagents"
        sub.mkdir(parents=True)
        top = slug / "sess-1.jsonl"
        top.write_text("{}\n")
        agent = sub / "agent-aaaa.jsonl"
        agent.write_text("{}\n")
        os.utime(top, (1000.0, 1000.0))
        os.utime(agent, (2000.0, 2000.0))  # a sub-agent write is the newest

        assert queries._latest_transcript_mtime(tmp_path, ["sess-1"]) == 2000.0

    def test_no_transcript_on_disk_is_zero(self, tmp_path) -> None:
        queries = load_queries()
        assert queries._latest_transcript_mtime(tmp_path, ["absent"]) == 0.0


class TestSpokeTranscriptMtime:
    def test_returns_the_spokes_real_transcript_mtime(self) -> None:
        store = _store()
        top = next(PROJECTS.glob(f"*/{SPOKE_SESSION}.jsonl"))
        assert store.spoke_transcript_mtime(SPOKE, PROJECTS) >= top.stat().st_mtime > 0.0

    def test_unknown_spoke_has_zero_mtime(self) -> None:
        assert _store().spoke_transcript_mtime("nope/0+0", PROJECTS) == 0.0


# --- app: running signal, cache key, and the cache rebuild/reuse contract ------


class TestSpokeIsRunning:
    def test_recent_transcript_is_running(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._spoke_is_running(990.0, now=1000.0, window=120.0) is True

    def test_stale_transcript_is_not_running(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._spoke_is_running(500.0, now=1000.0, window=120.0) is False

    def test_absent_transcript_is_not_running(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._spoke_is_running(0.0, now=1000.0, window=120.0) is False

    def test_window_boundary_is_inclusive(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._spoke_is_running(880.0, now=1000.0, window=120.0) is True


class TestForestCacheKey:
    def test_grown_transcript_changes_the_key(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._forest_cache_key("correlated:v1", 100.0) != app._forest_cache_key(
            "correlated:v1", 200.0
        )

    def test_static_transcript_keeps_the_key(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        assert app._forest_cache_key("correlated:v1", 100.0) == app._forest_cache_key(
            "correlated:v1", 100.0
        )


class TestForestCacheRebuildOnGrowth:
    def test_grown_rebuilds_static_reuses(self, monkeypatch) -> None:
        app = _app(monkeypatch)
        app._FOREST_CACHE.clear()
        builds: list[str] = []
        monkeypatch.setattr(
            app, "_build_spoke_forest", lambda store, sid: builds.append(sid) or [{"id": sid}]
        )
        store = MagicMock()

        k1 = app._forest_cache_key("correlated:v1", 100.0)
        app._spoke_forest(store, SPOKE, k1)
        app._spoke_forest(store, SPOKE, k1)  # static refresh → cached, no rebuild
        assert len(builds) == 1

        k2 = app._forest_cache_key("correlated:v1", 200.0)
        app._spoke_forest(store, SPOKE, k2)  # grown transcript → rebuild
        assert len(builds) == 2

    def test_growth_evicts_the_stale_same_spoke_entry(self, monkeypatch) -> None:
        # Live-follow must not leak: each grown-transcript build supersedes the prior one,
        # so the cache keeps a single entry per spoke, not one per refresh (Issue #67).
        app = _app(monkeypatch)
        app._FOREST_CACHE.clear()
        monkeypatch.setattr(app, "_build_spoke_forest", lambda store, sid: [{"id": sid}])
        store = MagicMock()

        app._spoke_forest(store, SPOKE, app._forest_cache_key("correlated:v1", 100.0))
        app._spoke_forest(store, SPOKE, app._forest_cache_key("correlated:v1", 200.0))

        retained = [cached for cached in app._FOREST_CACHE if cached[0] == SPOKE]
        assert len(retained) == 1


# --- app: render wiring ---------------------------------------------------------


class TestLiveMtimeGuard:
    def test_zero_when_store_lacks_the_method(self, monkeypatch, tmp_path) -> None:
        app = _app(monkeypatch)
        bare = MagicMock(spec=[])  # no spoke_transcript_mtime attribute
        assert app._spoke_live_mtime(bare, SPOKE, tmp_path) == 0.0

    def test_zero_when_projects_dir_absent(self, monkeypatch, tmp_path) -> None:
        app = _app(monkeypatch)
        store = MagicMock()
        store.spoke_transcript_mtime.return_value = 1234.0
        missing = tmp_path / "nope"
        assert app._spoke_live_mtime(store, SPOKE, missing) == 0.0


class TestRenderOffersFollowForRunningSpoke:
    def test_running_spoke_offers_a_live_follow_toggle(self, monkeypatch, tmp_path) -> None:
        st = _stub_streamlit()
        monkeypatch.setitem(sys.modules, "streamlit", st)
        monkeypatch.setitem(sys.modules, "queries", load_queries())
        app = load_app()
        monkeypatch.setattr(app, "resolve_projects_dir", lambda: tmp_path)
        monkeypatch.setattr(app, "_build_spoke_forest", lambda store, sid: [])
        monkeypatch.setattr(app, "_spoke_live_mtime", lambda *a, **k: 1.0e12)  # very fresh
        monkeypatch.setattr(app, "_spoke_is_running", lambda *a, **k: True)

        store = MagicMock()
        store.spoke_run_ids.return_value = [SPOKE]
        store.spoke_meta_by_kind.return_value = []

        app.render_spoke_view(store, "correlated:v1")  # must not raise

        toggles = " ".join(str(c.args[0]) for c in st.toggle.call_args_list if c.args)
        assert "live" in toggles.lower() or "follow" in toggles.lower()
