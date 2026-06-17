"""Render the per-turn causal context node in the v3 spoke trace (Issue #67, A — RED).

Phase 1 (#65) builds ONE ``context`` node per main turn carrying ``input_context`` —
the named rules / CLAUDE.md / memory / tool-schemas that composed the prompt plus the
real cached-prefix total. Phase 3 renders it as the spec's ``📐 context`` row: the
Tokens column is the real total, the row drills into the named items, each with its own
token estimate and a cost slot.

The defect this kills (confirmed empirically): ``format_step_label`` crashed with
``KeyError 'collapsed_count'`` on the causal context node — it only handled the *v2*
collapsed ``rule xN`` group — so the live causal trace blew up on any spoke with a main
turn, and the named items never rendered (they live in ``input_context``, not children).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import load_app, load_queries
from telemetry.causal import causal_node


def _input_context(**over) -> dict:
    ctx = {
        "rules": [
            {"name": "code-quality", "tokens": 1800},
            {"name": "python-style", "tokens": 1200},
        ],
        "claude_md": {"name": "CLAUDE.md", "tokens": 1100},
        "memory": [{"name": "dashboard-v3-roadmap", "tokens": 80}],
        "schemas": {"count": 2, "tokens": 600},
        "history_tokens": 7220,
        "total_tokens": 12000,
    }
    ctx.update(over)
    return ctx


def _ctx_node(**over) -> dict:
    """A per-turn causal context node (carries ``input_context``, no ``collapsed_count``)."""
    return causal_node(
        node_id="ctx:m1",
        kind="context",
        name="context",
        parent_id="m1",
        input_context=_input_context(**over),
    )


def _recording_streamlit() -> MagicMock:
    """A streamlit stub recording every rendered markdown string, with toggles open."""
    st = MagicMock()
    recorded: list[str] = []

    def _columns(spec):
        count = spec if isinstance(spec, int) else len(spec)
        cols = []
        for _ in range(count):
            col = MagicMock()
            col.markdown.side_effect = lambda text, *a, **k: recorded.append(str(text))
            cols.append(col)
        return cols

    st.columns.side_effect = _columns
    st.markdown.side_effect = lambda text, *a, **k: recorded.append(str(text))
    st.toggle.return_value = True  # force every drill open so the items render
    st._recorded = recorded
    return st


def _app_with_stub(monkeypatch):
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


# --- queries: label + Tokens column for the causal context node ----------------


class TestContextLabelAndMetrics:
    def test_label_does_not_crash_and_names_context(self) -> None:
        # The empirical defect: this raised KeyError 'collapsed_count' before the fix.
        label = load_queries().format_step_label(_ctx_node())
        assert "context" in label
        assert "None" not in label

    def test_label_summarizes_loaded_item_count(self) -> None:
        # 2 rules + CLAUDE.md + 1 memory + 2 tool-schemas = 6 named items loaded.
        assert load_queries().format_step_label(_ctx_node()) == "context · 6 loaded"

    def test_empty_context_label_is_bare_context(self) -> None:
        ctx = _ctx_node(
            rules=[],
            claude_md=None,
            memory=[],
            schemas={"count": 0, "tokens": 0},
            history_tokens=500,
            total_tokens=500,
        )
        assert load_queries().format_step_label(ctx) == "context"

    def test_tokens_column_is_the_real_total_prefix(self) -> None:
        # Tokens column shows the real cached-prefix total, not the (zero) own/rollup.
        assert load_queries().format_step_metrics(_ctx_node())["tokens"] == "12,000"

    def test_v2_collapsed_context_group_still_reads_rule_xN(self) -> None:
        # Regression: the *v2* collapsed group (collapsed_count + phase, no input_context)
        # must keep its "rule x3" label — the new branch only owns the causal node.
        v2_group = {"kind": "context", "phase": "rule", "collapsed_count": 3}
        assert load_queries().format_step_label(v2_group) == "rule x3"


# --- app: 📐 row glyph + drill rows for the named items ------------------------


class TestContextRowGlyph:
    def test_context_row_glyph_is_compass(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "streamlit", _recording_streamlit())
        app = _app_with_stub(monkeypatch)
        assert app._row_glyph(_ctx_node()) == "📐"

    def test_non_context_row_keeps_status_icon(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "streamlit", _recording_streamlit())
        app = _app_with_stub(monkeypatch)
        tool = causal_node(node_id="t1", kind="tool", name="Read", status="success")
        assert app._row_glyph(tool) == "✅"


class TestContextItemRows:
    def test_names_each_item_with_its_tokens(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "streamlit", _recording_streamlit())
        app = _app_with_stub(monkeypatch)
        rows = app._context_item_rows(_input_context())
        by_label = {r["label"]: r["tokens"] for r in rows}
        assert by_label["rule · code-quality"] == 1800
        assert by_label["rule · python-style"] == 1200
        assert by_label["CLAUDE.md"] == 1100
        assert by_label["memory · dashboard-v3-roadmap"] == 80
        assert by_label["tool-schemas x2"] == 600
        assert by_label["history"] == 7220

    def test_drill_rows_reconcile_to_the_total(self, monkeypatch) -> None:
        # Conservation: the drilled items + history sum to the context total.
        monkeypatch.setitem(sys.modules, "streamlit", _recording_streamlit())
        app = _app_with_stub(monkeypatch)
        rows = app._context_item_rows(_input_context())
        assert sum(r["tokens"] for r in rows) == 12000

    def test_empty_context_is_history_only(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "streamlit", _recording_streamlit())
        app = _app_with_stub(monkeypatch)
        rows = app._context_item_rows(
            {
                "rules": [],
                "claude_md": None,
                "memory": [],
                "schemas": {"count": 0, "tokens": 0},
                "history_tokens": 500,
                "total_tokens": 500,
            }
        )
        assert rows == [{"label": "history", "tokens": 500, "cost": 0.0}]


class TestContextDrillRender:
    def test_drilling_a_context_node_renders_its_named_items(self, monkeypatch) -> None:
        # The named items live in input_context (not children), so the renderer must
        # surface them on drill — the gap before Phase 3 left the drill empty.
        st = _recording_streamlit()
        monkeypatch.setitem(sys.modules, "streamlit", st)
        app = _app_with_stub(monkeypatch)
        app._render_node(_ctx_node(), depth=0, path="0")
        rendered = " ".join(st._recorded)
        assert "code-quality" in rendered
        assert "history" in rendered

    def test_render_context_node_does_not_crash(self, monkeypatch) -> None:
        st = _recording_streamlit()
        monkeypatch.setitem(sys.modules, "streamlit", st)
        app = _app_with_stub(monkeypatch)
        app._render_node(_ctx_node(), depth=0, path="0")  # must not raise
