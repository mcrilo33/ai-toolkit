"""v3 spoke-trace rendering — columns/labels (Issue #53 track D).

``dashboard/app.py`` is the thin Streamlit presentation layer; the data layer is
``queries.py`` / ``tree.py``. Streamlit cannot be imported in the base test env,
so we inject a *recording* ``MagicMock`` streamlit that captures every ``markdown``
written across columns, then drive the render over contract-shaped forests built
with the #50 ``synthetic_node`` factory.

This module locks the v3 view spec from ``docs/dashboard-spoke-trace-scope.md``:
the ``Node · Time · Dur · Cost · Tokens · H · Actor`` columns (Time = start clock,
no Date column), actor names, date-dividers on day rollover, idle/session-resume
dividers, badges, and the xN drill to per-item rows.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries, store_v2

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spans import synthetic_node


def _ctx_mock() -> MagicMock:
    """A MagicMock usable as a ``with`` context manager (expander / tab)."""
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


def _recording_streamlit(checkbox_returns: dict[str, bool] | bool | None = None):
    """A streamlit stub recording markdown rows, flat texts, and captions.

    ``columns()`` returns recorder columns so a rendered row is captured as an
    ordered ``list[str]`` (one cell per column) in ``rec.rows``; every markdown
    string (top-level or per-column) also lands flat in ``rec.texts``.
    ``checkbox_returns`` controls drill toggles: a bool applies to all, a dict
    keys by label.
    """
    rec = SimpleNamespace(rows=[], texts=[], captions=[])
    checks = checkbox_returns if checkbox_returns is not None else False

    def _columns(spec):
        n = spec if isinstance(spec, int) else len(spec)
        row: list[str | None] = [None] * n
        rec.rows.append(row)
        cols = []
        for i in range(n):
            c = MagicMock()

            def _mk(text, *_a, _row=row, _i=i, **_kw):
                _row[_i] = str(text)
                rec.texts.append(str(text))

            c.markdown.side_effect = _mk
            c.write.side_effect = _mk
            cols.append(c)
        return cols

    def _checkbox(label, *_a, **_kw):
        return checks if isinstance(checks, bool) else checks.get(str(label), False)

    st = MagicMock()
    st.columns.side_effect = _columns
    st.markdown.side_effect = lambda text, *_a, **_kw: rec.texts.append(str(text))
    st.caption.side_effect = lambda text, *_a, **_kw: rec.captions.append(str(text))
    st.tabs.side_effect = lambda names: [_ctx_mock() for _ in names]
    st.expander.side_effect = lambda *_a, **_kw: _ctx_mock()
    st.selectbox.side_effect = lambda _label, options, **_kw: options[0]
    st.checkbox.side_effect = _checkbox
    st.toggle.side_effect = _checkbox
    return st, rec


def _app(monkeypatch, st):
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def _node(kind: str, name: str, **over):
    """A real-span-shaped forest node dict with all keys app.py may read."""
    base = {
        "span_id": "s",
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


def _row_for(rec, needle: str) -> list[str]:
    """The captured 7-cell row whose label cell (col 0) contains ``needle``."""
    return next(r for r in rec.rows if len(r) == 7 and r[0] and needle in r[0])


# ── columns ───────────────────────────────────────────────────────────────────


def test_spine_header_is_node_time_dur_h_actor(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    app.render_spoke_view(store_v2())

    header = next(r for r in rec.rows if r and all(c and c.startswith("**") for c in r))
    assert header == [
        "**Node**",
        "**Time**",
        "**Dur**",
        "**Cost**",
        "**Tokens**",
        "**H**",
        "**Actor**",
    ]


def test_time_cell_is_start_clock_and_dur_is_duration(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    node = _node("step", "S1·RED", ts_start="2026-06-12T12:01:05Z", duration_ms=2000)
    app._render_spine([node])

    row = _row_for(rec, "S1·RED")
    assert row[1] == "12:01:05"  # Time column = start clock, not duration
    assert row[2] == "2.0s"  # Dur column = wall-clock duration


# ── actor ───────────────────────────────────────────────────────────────────


def test_actor_label_resolves_name_kind_and_explicit_actor(monkeypatch):
    st, _ = _recording_streamlit()
    app = _app(monkeypatch, st)

    assert app._actor_label(_node("agent", "tdd-red")) == "tdd-red"
    assert app._actor_label(_node("workflow", "wf")) == "workflow"
    assert app._actor_label(_node("script", "spoke-push.sh")) == "script"
    assert app._actor_label(synthetic_node(kind="hooks", name="hooks")) == "hooks"
    assert app._actor_label(_node("tool", "Read", actor="sidecar")) == "sidecar"
    assert app._actor_label(_node("turn", "turn", agent="main")) == "main"


def test_subagent_tool_actor_inherits_agent_name(monkeypatch):
    # A tool/turn nested under a sub-agent is owned by that sub-agent, not main —
    # the tree tags the span's own `agent` as "main" (kind-based) and emits no #50
    # `actor` yet, so the renderer must inherit the enclosing agent's name.
    st, rec = _recording_streamlit(checkbox_returns=True)  # drill open to render children
    app = _app(monkeypatch, st)

    tool = _node("tool", "Read /repo/x.py", ts_start="2026-06-12T12:00:11Z", agent="main")
    agent_node = _node("agent", "tdd-red", ts_start="2026-06-12T12:00:10Z", children=[tool])
    step = _node("interval", "setup", ts_start="2026-06-12T12:00:00Z", children=[agent_node])
    app._render_spine([step])

    assert _row_for(rec, "tdd-red")[6] == "tdd-red"  # the agent itself
    assert _row_for(rec, "Read /repo/x.py")[6] == "tdd-red"  # its tool inherits the actor


def test_actor_column_shows_subagent_name(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    app._render_spine([_node("agent", "code-review", ts_start="2026-06-12T12:00:00Z")])

    assert _row_for(rec, "code-review")[6] == "code-review"


# ── dividers + badges ─────────────────────────────────────────────────────────


def test_date_divider_renders_only_on_day_rollover(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    day1 = _node("interval", "S1", ts_start="2026-06-12T23:59:00Z")
    day2 = _node("interval", "S2", ts_start="2026-06-13T00:01:00Z")
    app._render_spine([day1, day2])

    assert any("2026-06-13" in t for t in rec.texts)  # rollover divider
    assert not any("2026-06-12" in t for t in rec.texts)  # first day → no leading divider


def test_gap_node_renders_as_idle_divider_not_metric_row(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    gap = synthetic_node(kind="gap", name="idle 5m", duration_ms=300_000)
    app._render_spine([gap])

    assert any("idle" in t.lower() for t in rec.texts)
    assert not any(len(r) == 7 and r[0] and "idle" in r[0] for r in rec.rows)


def test_session_node_renders_resume_divider_with_cold_cache_note(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    sess = synthetic_node(kind="session", name="session resume", own_tokens_in=1200)
    app._render_spine([sess])

    blob = " ".join(rec.texts)
    # The cold-cache note is the point of the divider (scope doc): the surfaced
    # cache-creation token count must render, not just the word "resume".
    assert "session resume" in blob
    assert "cold cache" in blob
    assert "1,200" in blob


def test_status_icon_renders_in_node_cell(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    app._render_spine([_node("tool", "rm -rf", status="deny", ts_start="2026-06-12T12:00:00Z")])

    assert "🚫" in _row_for(rec, "rm -rf")[0]  # a denied node carries the deny icon


def test_badges_render_in_node_label(monkeypatch):
    st, rec = _recording_streamlit()
    app = _app(monkeypatch, st)

    synth = _node("interval", "S2·GREEN", badges=["⟨from todo — no marker⟩"])
    bust = _node("interval", "S3", badges=["ctx-bust"])
    app._render_spine([synth, bust])

    blob = " ".join(rec.texts)
    assert "⟨from todo — no marker⟩" in blob
    assert "ctx-bust" in blob


# ── xN drill ──────────────────────────────────────────────────────────────────


def test_drill_uses_toggle_not_nested_expander(monkeypatch):
    # Regression (visual gate): a checkbox inside an st.expander never reveals its
    # members because the expander re-collapses on the rerun. The drill must be a
    # uniform toggle with NO expander, so the same lazy GUI works at every depth.
    st, rec = _recording_streamlit(checkbox_returns=False)
    app = _app(monkeypatch, st)

    step = _node(
        "interval", "S1", ts_start="2026-06-12T12:00:00Z", children=[_node("tool", "Read /f0")]
    )
    app._render_spine([step])

    st.expander.assert_not_called()  # no nested-expander re-collapse trap
    st.toggle.assert_called()  # uniform drill control
    assert not any("/f0" in t for t in rec.texts)  # lazy: hidden until drilled


def test_drill_reveals_nested_members_when_open(monkeypatch):
    st, rec = _recording_streamlit(checkbox_returns=True)  # every drill open
    app = _app(monkeypatch, st)

    members = [_node("tool", f"Read /f{i}") for i in range(3)]
    group = _node("hooks", "hooks", collapsed=True, collapsed_count=3, children=members)
    step = _node("interval", "S1", ts_start="2026-06-12T12:00:00Z", children=[group])
    app._render_spine([step])

    st.expander.assert_not_called()  # the fix: no expander to re-collapse on rerun
    assert any("hooks x3" in t for t in rec.texts)  # group shows when the step is drilled
    assert any("/f0" in t for t in rec.texts)  # nested members reveal through the toggle
    assert any("/f2" in t for t in rec.texts)


def test_drill_toggle_keys_are_unique_and_path_shaped(monkeypatch):
    # The fix's load-bearing property: a stable, unique per-path toggle key (so it
    # persists across reruns and never DuplicateWidgetID-collides). Two sibling
    # groups with the SAME label must still get distinct keys.
    st, _rec = _recording_streamlit(checkbox_returns=True)
    app = _app(monkeypatch, st)

    g1 = _node(
        "hooks", "hooks", collapsed_count=2, children=[_node("hook", "a"), _node("hook", "b")]
    )
    g2 = _node(
        "hooks", "hooks", collapsed_count=2, children=[_node("hook", "c"), _node("hook", "d")]
    )
    step = _node("interval", "S1", ts_start="2026-06-12T12:00:00Z", children=[g1, g2])
    app._render_spine([step])

    keys = [call.kwargs.get("key") for call in st.toggle.call_args_list]
    assert keys, "expected drill toggles to be rendered"
    assert all(k and k.startswith("drill::") for k in keys)  # path-shaped
    assert len(keys) == len(set(keys))  # unique — no DuplicateWidgetID in real Streamlit


def test_non_hooks_collapsed_group_gets_times_n_label(monkeypatch):
    st, rec = _recording_streamlit(checkbox_returns=True)
    app = _app(monkeypatch, st)

    # A non-hooks collapsed group (e.g. parallel agents) must also get an xN label,
    # which the v2 format_step_label only produces for hooks.
    members = [_node("agent", f"worker-{i}") for i in range(3)]
    group = _node("agent", "agents", collapsed=True, collapsed_count=3, children=members)
    app._render_spine([_node("interval", "S1", ts_start="2026-06-12T12:00:00Z", children=[group])])

    assert any("agent x3" in t for t in rec.texts)
