"""v3 panels — composition bar + cold-context lens (Issue #53 track D).

docs/dashboard-spoke-trace-scope.md, Drill panels / Loaded context:
- the per-turn context-composition bar shows **exact** usage totals (in/out/total)
  with a **modeled** prefix/skills/memory/history split labelled an estimate. The
  AC: the panel reconciles to the spoke's exact usage — the additive forest rollup
  (every turn counted once), NOT the per-kind meta sum (which omits main-agent /
  interval cost).
- the cold-context lens rolls up context loaded but never exercised (rules, tool
  schemas, memory recalls) — the trimming candidates.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from _dashboard_helpers import load_app, load_queries, store_v2

RUN = "feature/v2+1000"


def _recording_streamlit() -> tuple[MagicMock, SimpleNamespace]:
    rec = SimpleNamespace(texts=[], captions=[])

    def _columns(spec):
        n = spec if isinstance(spec, int) else len(spec)
        cols = []
        for _ in range(n):
            c = MagicMock()
            c.markdown.side_effect = lambda text, *_a, **_kw: rec.texts.append(str(text))
            c.metric.side_effect = lambda label, value, *_a, **_kw: rec.texts.append(
                f"{label}={value}"
            )
            cols.append(c)
        return cols

    st = MagicMock()
    st.columns.side_effect = _columns
    st.markdown.side_effect = lambda text, *_a, **_kw: rec.texts.append(str(text))
    st.caption.side_effect = lambda text, *_a, **_kw: rec.captions.append(str(text))
    st.metric.side_effect = lambda label, value, *_a, **_kw: rec.texts.append(f"{label}={value}")
    return st, rec


def _app(monkeypatch, st):
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def _node(kind: str, name: str, **over):
    base = {
        "span_id": None,
        "parent_id": None,
        "kind": kind,
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
    base.update(over)
    return base


# ── composition reconciliation ──────────────────────────────────────────────


def test_composition_totals_reconcile_to_forest_rollup(monkeypatch):
    st, _ = _recording_streamlit()
    app = _app(monkeypatch, st)
    forest = store_v2().spoke_steps(RUN)

    totals = app._composition_totals(forest)

    expected = sum((r.get("rollup") or {}).get("cost_usd", 0.0) for r in forest)
    assert totals["cost_usd"] == pytest.approx(expected)  # every turn counted once
    assert totals["tokens_in"] > 0
    assert totals["tokens_out"] > 0


def test_composition_total_exceeds_per_kind_meta_sum(monkeypatch):
    st, _ = _recording_streamlit()
    app = _app(monkeypatch, st)
    store = store_v2()
    forest = store.spoke_steps(RUN)

    exact = app._composition_totals(forest)["cost_usd"]
    per_kind = sum(row["total_cost_usd"] for row in store.spoke_meta_by_kind(RUN))

    # The per-kind meta view omits main-agent/interval cost; reconciliation matters.
    assert exact > per_kind


def test_render_composition_shows_exact_totals_and_estimate_label(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)
    forest = store_v2().spoke_steps(RUN)

    app._render_composition(forest)

    blob = " ".join(rec.texts + rec.captions).lower()
    assert "exact" in blob  # usage totals are surfaced as exact
    assert "estimate" in blob  # the modeled split is labelled an estimate, not measured


# ── cold-context lens ─────────────────────────────────────────────────────────


def test_cold_context_lists_only_unexercised_context(monkeypatch):
    st, _ = _recording_streamlit()
    app = _app(monkeypatch, st)

    cold = _node("context", "rule: markdown-style")  # loaded, never exercised
    warm_turn = _node("turn", "turn", own_tokens_in=10, own_tokens_out=5)
    warm = _node("context", "skill: solo-cycle", children=[warm_turn])  # exercised
    forest = [_node("interval", "S1", children=[cold, warm])]

    names = [n["name"] for n in app._cold_context(forest)]

    assert "rule: markdown-style" in names
    assert "skill: solo-cycle" not in names


def test_render_cold_context_lens_lists_candidates(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)
    forest = [_node("interval", "S1", children=[_node("context", "rule: mermaid")])]

    app._render_cold_context(forest)

    assert any("rule: mermaid" in t for t in rec.texts)
