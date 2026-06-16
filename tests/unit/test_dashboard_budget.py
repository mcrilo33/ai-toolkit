"""Budget panel: real cache_creation on resume + cache framing (Issue #59).

The session-resume divider must carry the *real* per-resume ``cache_creation`` (the
prompt re-read paid on resume), replacing the static note, and the composition panel
must frame ``cache_read`` (cheap reuse) against ``cache_creation`` (cold writes).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def _load_tree():
    path = _REPO_ROOT / "dashboard" / "tree.py"
    spec = importlib.util.spec_from_file_location("dashboard_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tree = _load_tree()


def _row(session_id: str, ts: str):
    return {"session_id": session_id, "ts_start": ts, "ts_end": ts}


def _turn(session_id: str, ts: str, cache_creation: int):
    return {
        "session_id": session_id,
        "ts": ts,
        "cache_read": 0,
        "cache_creation": cache_creation,
    }


def test_session_divider_carries_real_cache_creation() -> None:
    # Two sessions → one resume; the resumed session's first turn paid 4096 cold tokens.
    rows = [_row("sessA", "2026-06-13T09:00:00Z"), _row("sessB", "2026-06-13T10:00:00Z")]
    turns = [
        _turn("sessA", "2026-06-13T09:00:01Z", 100),
        _turn("sessB", "2026-06-13T10:00:01Z", 4096),
        _turn("sessB", "2026-06-13T10:05:00Z", 50),
    ]

    dividers = tree.build_dividers(rows, turns)
    sessions = [d for d in dividers if d["kind"] == "session"]

    assert len(sessions) == 1
    assert sessions[0]["resume_cache_creation"] == 4096  # the real cold re-read, not static
    # The magnitude must NOT ride on own_tokens_in — that would fold into the
    # once-per-turn rollup and inflate the "Tokens in (exact)" composition metric.
    assert sessions[0]["own_tokens_in"] == 0


def test_session_divider_summary_reflects_the_real_number() -> None:
    rows = [_row("sessA", "2026-06-13T09:00:00Z"), _row("sessB", "2026-06-13T10:00:00Z")]
    turns = [_turn("sessB", "2026-06-13T10:00:01Z", 4096)]

    sessions = [d for d in tree.build_dividers(rows, turns) if d["kind"] == "session"]

    assert "4,096" in (sessions[0]["summary"] or "")


def test_resume_cache_does_not_leak_into_exact_tokens_in(monkeypatch) -> None:
    # Conservation: the resume divider's cache_creation must NOT inflate the panel's
    # "Tokens in (exact)" metric — cache_creation is disjoint from input_tokens, and
    # synthetic display nodes never enter the once-per-turn rollup.
    monkeypatch.setitem(sys.modules, "streamlit", MagicMock())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    app = load_app()

    divider = tree._session_divider("2026-06-13T10:00:00Z", 4096)
    tree._roll_up_steps(divider)  # the post-order pass spoke_steps applies to every root
    turn = _node("turn", "turn", own_tokens_in=10, own_tokens_out=5)
    interval = _node("interval", "S1", children=[turn])
    tree._roll_up_steps(interval)

    totals = app._composition_totals([interval, divider])

    assert totals["tokens_in"] == 10  # only the turn's input tokens, not the 4096 cache


def test_turn_node_carries_cache_breakdown() -> None:
    node = tree._turn_node({"ts": "2026-06-13T12:00:00Z", "cache_read": 900, "cache_creation": 300})
    assert node["cache_read"] == 900
    assert node["cache_creation"] == 300


def _recording_streamlit():
    rec = SimpleNamespace(texts=[], captions=[])

    def _columns(spec):
        n = spec if isinstance(spec, int) else len(spec)
        cols = []
        for _ in range(n):
            c = MagicMock()
            c.metric.side_effect = lambda label, value, *_a, **_k: rec.texts.append(
                f"{label}={value}"
            )
            c.markdown.side_effect = lambda text, *_a, **_k: rec.texts.append(str(text))
            cols.append(c)
        return cols

    st = MagicMock()
    st.columns.side_effect = _columns
    st.caption.side_effect = lambda text, *_a, **_k: rec.captions.append(str(text))
    st.metric.side_effect = lambda label, value, *_a, **_k: rec.texts.append(f"{label}={value}")
    return st, rec


def _node(kind, name, **over):
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
        "cache_read": 0,
        "cache_creation": 0,
        "models": [],
        "actor": "main",
        "human_count": 0,
        "children": [],
        "rollup": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0},
    }
    base.update(over)
    return base


def test_composition_frames_cache_read_vs_cache_creation(monkeypatch):
    st, rec = _recording_streamlit()
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    app = load_app()
    turn = _node("turn", "turn", cache_read=5000, cache_creation=1200)
    forest = [
        _node(
            "interval",
            "S1",
            children=[turn],
            rollup={"cost_usd": 0.0, "tokens_in": 10, "tokens_out": 5},
        )
    ]

    app._render_composition(forest)

    blob = " ".join(rec.texts + rec.captions)
    assert "5,000" in blob  # cache_read surfaced
    assert "1,200" in blob  # cache_creation surfaced
    assert "cache" in blob.lower()
