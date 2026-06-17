"""The causal forest is the dashboard's SOLE spoke-tree builder (Issue #80).

#80 removes the legacy timestamp-bucketed builder (``SpanStore.spoke_steps`` plus
``dashboard/tree.py``) and the silent fallback in ``app.py``: ``_build_spoke_forest``
must call ``spoke_causal_forest`` directly, and a build that *fails* must surface an
explicit ``st.error`` — never quietly render the old broken model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))  # queries.py imports telemetry.* at load

from _dashboard_helpers import DASHBOARD_DIR, load_app, load_queries


def _app(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", MagicMock())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


class _CausalStore:
    """Store stand-in: a scriptable causal forest plus a ``spoke_steps`` tripwire.

    The legacy fallback would call ``spoke_steps``; ``steps_called`` proves it never is.
    """

    def __init__(self, *, forest: list | None = None, error: Exception | None = None) -> None:
        self._forest = forest if forest is not None else []
        self._error = error
        self.steps_called = False

    def spoke_causal_forest(self, *_args, **_kw):
        if self._error is not None:
            raise self._error
        return self._forest

    def spoke_steps(self, *_args, **_kw):  # legacy tripwire — must never be reached
        self.steps_called = True
        return [{"kind": "legacy", "name": "FALLBACK", "children": []}]


def test_tree_module_is_gone() -> None:
    assert not (DASHBOARD_DIR / "tree.py").exists()


def test_spoke_steps_is_removed() -> None:
    queries = load_queries()
    assert not hasattr(queries.SpanStore, "spoke_steps")


def test_build_forest_uses_causal_only_no_fallback(monkeypatch) -> None:
    app = _app(monkeypatch)
    monkeypatch.setattr(app, "_ccusage_costs", lambda: {})
    monkeypatch.setattr(app, "resolve_projects_dir", lambda: _REPO_ROOT)
    store = _CausalStore(forest=[])

    result = app._build_spoke_forest(store, "feature/x+1")

    assert result == []  # the empty causal forest, NOT the legacy fallback
    assert store.steps_called is False


def test_build_failure_surfaces_explicit_error(monkeypatch) -> None:
    app = _app(monkeypatch)
    monkeypatch.setattr(app, "_ccusage_costs", lambda: {})
    monkeypatch.setattr(app, "resolve_projects_dir", lambda: _REPO_ROOT)
    errors: list[str] = []
    monkeypatch.setattr(app.st, "error", lambda msg: errors.append(msg))
    store = _CausalStore(error=RuntimeError("causal boom"))

    result = app._build_or_error(store, "feature/x+1", source_key="")

    assert result is None
    assert errors and "causal boom" in errors[0]
    assert store.steps_called is False
