"""Span-source resolution tests (Issue #44 — correlate-by-default).

The dashboard now defaults the correlation toggle on and falls back to raw mode
when the Claude session-logs (projects) dir is absent, instead of blanking the
view. ``app.resolve_mode`` is the pure, streamlit-free decision that drives that
fallback; these tests pin it without rendering anything.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries


def _load_app(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", MagicMock())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def test_resolve_mode_correlated_when_projects_dir_present(monkeypatch, tmp_path):
    app = _load_app(monkeypatch)

    assert app.resolve_mode(True, tmp_path) == "correlated"


def test_resolve_mode_falls_back_to_raw_when_projects_dir_absent(monkeypatch, tmp_path):
    app = _load_app(monkeypatch)
    absent = tmp_path / "does-not-exist"

    assert app.resolve_mode(True, absent) == "raw"


def test_resolve_mode_raw_when_correlation_off(monkeypatch, tmp_path):
    app = _load_app(monkeypatch)

    assert app.resolve_mode(False, tmp_path) == "raw"
