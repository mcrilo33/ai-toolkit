"""Approvals end-to-end — PreToolUse approve/deny/ask as L3 nodes (Issue #60).

Real spokes never carry ``kind='approval'`` spans: a tool-permission decision
lives only in the push-layer PreToolUse **hook** span as its ``status``
(``allow`` / ``deny`` / ``warn``). This module locks the derivation that turns
those gating hooks into first-class ``approval`` nodes and links each to the tool
it gated:

- an **allow** / **warn** gate renders *under* the tool it let through;
- a **deny** gate blocks the tool, which reparents *under* the approval and
  renders as never-run (matching the golden fixture's ``approval → blocked tool``
  shape);
- every approval carries ``human={type: 'approval', wait_ms}`` so it routes into
  the Automatability view, and rolls up at ``$0`` cost in meta-by-kind.

The store is built straight from span dicts (the ``from_events`` path that
``from_jsonl`` and the live ``from_telemetry`` correlation both funnel through),
so the derivation is exercised wherever a ``SpanStore`` is constructed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from _dashboard_helpers import load_app, load_queries

RUN = "feature/60-approvals+1700000000"


def _span(span_id: str, kind: str, name: str, ts_start: str, ts_end: str, **over) -> dict:
    """A fully-formed span dict (every ``_COLUMNS`` key present, then overrides)."""
    base = {
        "span_id": span_id,
        "parent_id": None,
        "spoke_run_id": RUN,
        "session_id": "sess-a",
        "workflow_rev": "rev60",
        "repo": "ai-toolkit",
        "branch": "feature/60",
        "kind": kind,
        "name": name,
        "phase": None,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": 0,
        "status": "success",
        "human": None,
        "summary": None,
        "emits": None,
        "sidecar_session": None,
        "agent_link": None,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
    }
    base.update(over)
    return base


def _spans() -> list[dict]:
    """A spoke with one allowed Bash, one denied (blocked) Bash, one ungated tool."""
    t = "2026-06-16T12:00:"
    return [
        _span("life", "lifecycle", "spoke", f"{t}00Z", f"{t}30Z"),
        # An allow gate immediately precedes the Bash it lets through.
        _span("h_allow", "hook", "bash", f"{t}01Z", f"{t}01Z", status="allow", duration_ms=60),
        _span("t_allow", "tool", "Bash", f"{t}02Z", f"{t}03Z", summary="ls -la"),
        # A deny gate blocks the Bash that follows; the blocked tool still parses
        # (the tool_use block exists, its result is an error) as a failure span.
        _span(
            "h_deny",
            "hook",
            "push-scope-guard.sh",
            f"{t}10Z",
            f"{t}10Z",
            status="deny",
            duration_ms=90,
        ),
        _span("t_deny", "tool", "Bash", f"{t}11Z", f"{t}11Z", status="failure", summary="git push"),
        # An ungated tool: its PreToolUse hook returned no decision (success), so
        # it must NOT acquire an approval node.
        _span("h_ok", "hook", "post-edit-format.sh", f"{t}20Z", f"{t}20Z", status="success"),
        _span("t_ok", "tool", "Read", f"{t}21Z", f"{t}22Z", summary="/etc/hosts"),
    ]


def _store():
    queries = load_queries()
    return queries.SpanStore.from_events(_spans())


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


def _approvals(store) -> list[dict]:
    return store._query("SELECT * FROM spans WHERE kind = 'approval' ORDER BY ts_start")


class TestApprovalDerivation:
    def test_each_gating_hook_yields_one_approval(self) -> None:
        approvals = _approvals(_store())
        statuses = sorted(a["status"] for a in approvals)
        assert statuses == ["allow", "deny"], (
            f"expected one allow + one deny approval, got {statuses}"
        )

    def test_ungated_tool_gets_no_approval(self) -> None:
        # Only allow/deny/warn gates become approvals — a status='success' hook does not.
        approvals = _approvals(_store())
        assert len(approvals) == 2, "a no-decision (success) hook must not yield an approval"

    def test_approval_carries_human_block_and_wait(self) -> None:
        allow = next(a for a in _approvals(_store()) if a["status"] == "allow")
        assert allow["human_type"] == "approval"
        assert allow["human_wait_ms"] == 60, "wait derives from the gate hook's duration"

    def test_approval_owns_no_cost(self) -> None:
        assert all(a["cost_usd"] in (None, 0, 0.0) for a in _approvals(_store()))


class TestLinkageToGatedTool:
    def test_allowed_tool_gains_an_approval_child(self) -> None:
        forest = _store().spoke_tree(RUN)
        allowed = next(
            n
            for n in _walk(forest)
            if n["kind"] == "tool" and n["name"] == "Bash" and n["span_id"] == "t_allow"
        )
        kinds = [c["kind"] for c in allowed["children"]]
        assert "approval" in kinds, "an allowed tool must render its gate as a child approval"

    def test_denied_tool_nests_under_its_approval_as_never_run(self) -> None:
        forest = _store().spoke_tree(RUN)
        deny_approval = next(
            n for n in _walk(forest) if n["kind"] == "approval" and n["status"] == "deny"
        )
        blocked = [c for c in deny_approval["children"] if c["kind"] == "tool"]
        assert blocked, "the blocked tool must reparent under the deny approval"
        tool = blocked[0]
        assert tool["status"] == "deny", "a blocked tool renders as never-run (deny)"

    def test_blocked_tool_summary_marks_it_never_ran(self) -> None:
        # The never-run marker is a materialised data fact (the lean spoke_tree node
        # projection drops summary); assert it on the spans table directly.
        store = _store()
        rows = store._query("SELECT summary FROM spans WHERE kind = 'tool' AND status = 'deny'")
        assert rows, "the deny gate must leave a never-run tool"
        assert all("never ran" in (r["summary"] or "").lower() for r in rows)


class TestDenyHeuristicGuards:
    """A deny must never relabel a tool that actually ran (review finding #1)."""

    def _store_with(self, spans):
        queries = load_queries()
        return queries.SpanStore.from_events(spans)

    def test_deny_does_not_relabel_a_successful_intervening_tool(self) -> None:
        # deny gate, then a *different* allow gate, then a tool that SUCCEEDED. A hook
        # deny forces the blocked tool's result to an error, so a success tool was
        # never the one blocked — the deny must leave it alone and stand up a
        # synthetic never-run placeholder instead.
        t = "2026-06-16T12:00:"
        spans = [
            _span("life", "lifecycle", "spoke", f"{t}00Z", f"{t}30Z"),
            _span(
                "h_deny",
                "hook",
                "push-scope-guard.sh",
                f"{t}10Z",
                f"{t}10Z",
                status="deny",
                duration_ms=90,
            ),
            _span("h_allow", "hook", "bash", f"{t}11Z", f"{t}11Z", status="allow", duration_ms=40),
            _span("t_ok", "tool", "Bash", f"{t}12Z", f"{t}13Z", status="success", summary="ls"),
        ]
        store = self._store_with(spans)
        ok = store._query("SELECT status, summary FROM spans WHERE span_id = 't_ok'")[0]
        assert ok["status"] == "success", "a successful tool must not be relabeled never-run"
        assert "never ran" not in (ok["summary"] or "").lower()
        # The deny still produces a never-run node — a synthetic one.
        synth = store._query(
            "SELECT 1 FROM spans WHERE kind = 'tool' AND status = 'deny' AND span_id != 't_ok'"
        )
        assert synth, "the deny must stand up a synthetic never-run tool"

    def test_deny_with_no_following_tool_synthesizes_never_run(self) -> None:
        t = "2026-06-16T12:00:"
        spans = [
            _span("life", "lifecycle", "spoke", f"{t}00Z", f"{t}30Z"),
            _span(
                "h_deny",
                "hook",
                "secrets-scan.sh",
                f"{t}10Z",
                f"{t}10Z",
                status="deny",
                duration_ms=70,
            ),
        ]
        forest = self._store_with(spans).spoke_tree(RUN)
        deny = next(n for n in _walk(forest) if n["kind"] == "approval" and n["status"] == "deny")
        blocked = [c for c in deny["children"] if c["kind"] == "tool" and c["status"] == "deny"]
        assert blocked, "a deny with no parsable tool must synthesize a never-run tool"

    def test_warn_gate_nests_under_its_tool(self) -> None:
        t = "2026-06-16T12:00:"
        spans = [
            _span("life", "lifecycle", "spoke", f"{t}00Z", f"{t}30Z"),
            _span(
                "h_warn",
                "hook",
                "console-log-warn.sh",
                f"{t}05Z",
                f"{t}05Z",
                status="warn",
                duration_ms=30,
            ),
            _span("t_warn", "tool", "Edit", f"{t}06Z", f"{t}07Z", summary="app.py"),
        ]
        forest = self._store_with(spans).spoke_tree(RUN)
        tool = next(n for n in _walk(forest) if n["span_id"] == "t_warn")
        kinds = [(c["kind"], c["status"]) for c in tool["children"]]
        assert ("approval", "warn") in kinds, "a warn gate nests under the tool it flagged"


class TestApprovalsInRollups:
    def test_meta_by_kind_has_a_zero_cost_approval_row(self) -> None:
        rows = _store().spoke_meta_by_kind(RUN)
        approval = next((r for r in rows if r["kind"] == "approval"), None)
        assert approval is not None, "meta-by-kind must carry an approval row"
        assert approval["count"] == 2
        assert approval["total_cost_usd"] == 0

    def test_approvals_surface_in_automatability_with_wait(self) -> None:
        rows = _store().automatability_candidates(min_frequency=1)
        approval = next((r for r in rows if r["human_type"] == "approval"), None)
        assert approval is not None, "approvals must route into the Automatability view"
        assert approval["frequency"] == 2
        assert approval["mean_wait_ms"] == 75, "mean wait across the 60ms + 90ms gates"

    def test_automatability_row_carries_a_decision_breakdown(self) -> None:
        approval = next(
            r
            for r in _store().automatability_candidates(min_frequency=1)
            if r["human_type"] == "approval"
        )
        # allow + deny gates → an allow/ask/deny breakdown (warn maps to ask).
        assert approval["decisions"] == {"allow": 1, "ask": 0, "deny": 1}

    def test_meta_by_kind_approval_row_carries_mean_wait(self) -> None:
        rows = _store().spoke_meta_by_kind(RUN)
        approval = next(r for r in rows if r["kind"] == "approval")
        assert approval["mean_wait_ms"] == 75
        # A kind with no human wait reports None, never a bogus 0.
        tool = next(r for r in rows if r["kind"] == "tool")
        assert tool["mean_wait_ms"] is None


class TestLabelFormatting:
    def test_approval_label_shows_lock_and_decision(self) -> None:
        queries = load_queries()
        allow = {"kind": "approval", "name": "tool-permission", "status": "allow", "summary": None}
        deny = {"kind": "approval", "name": "tool-permission", "status": "deny", "summary": None}
        assert "🔐" in queries.format_step_label(allow)
        assert "ask→allow" in queries.format_step_label(allow)
        assert "ask→deny" in queries.format_step_label(deny)


# ── app render layer ────────────────────────────────────────────────────────


def _ctx() -> MagicMock:
    m = MagicMock()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


def _recording_st():
    """A streamlit stub capturing column markdown (rows/texts) and dataframe tables."""
    rec = SimpleNamespace(texts=[], rows=[], tables=[])

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

    st = MagicMock()
    st.columns.side_effect = _columns
    st.markdown.side_effect = lambda text, *_a, **_kw: rec.texts.append(str(text))
    st.dataframe.side_effect = lambda data, *_a, **_kw: rec.tables.append(data)
    st.caption.side_effect = lambda *_a, **_kw: None
    st.header.side_effect = lambda *_a, **_kw: None
    st.info.side_effect = lambda *_a, **_kw: None
    st.slider.side_effect = lambda *_a, **_kw: 1
    st.toggle.side_effect = lambda *_a, **_kw: False
    st.expander.side_effect = lambda *_a, **_kw: _ctx()
    return st, rec


def _app(monkeypatch, st):
    monkeypatch.setitem(sys.modules, "streamlit", st)
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def _label_node(kind: str, name: str, **over) -> dict:
    base = {
        "span_id": "s",
        "parent_id": None,
        "kind": kind,
        "name": name,
        "summary": None,
        "phase": None,
        "status": "success",
        "ts_start": "2026-06-16T12:00:00Z",
        "ts_end": "2026-06-16T12:00:00Z",
        "duration_ms": 0,
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


class TestAppRender:
    def test_approval_node_row_shows_lock_and_decision(self, monkeypatch) -> None:
        st, rec = _recording_st()
        app = _app(monkeypatch, st)
        app._render_spine([_label_node("approval", "tool-permission", status="allow")])
        cell = next(r[0] for r in rec.rows if r[0] and "approval" in r[0])
        assert "🔐" in cell and "ask→allow" in cell

    def test_blocked_tool_renders_as_never_run(self, monkeypatch) -> None:
        st, rec = _recording_st()
        app = _app(monkeypatch, st)
        app._render_spine(
            [_label_node("tool", "Bash", status="deny", summary="git push (blocked, never ran)")]
        )
        cell = next(r[0] for r in rec.rows if r[0] and "Bash" in r[0])
        assert "never-run" in cell.lower()

    def test_meta_table_has_a_mean_wait_column(self, monkeypatch) -> None:
        st, rec = _recording_st()
        app = _app(monkeypatch, st)
        store = load_queries().SpanStore.from_events(_spans())
        app._render_meta(store, RUN)
        table = rec.tables[0]
        assert all("Mean wait" in row for row in table), "meta table must carry a Mean wait column"
        approval = next(r for r in table if r["Kind"] == "approval")
        assert approval["Mean wait"] == "0.1s"

    def test_automatability_table_shows_decision_breakdown(self, monkeypatch) -> None:
        st, rec = _recording_st()
        app = _app(monkeypatch, st)
        store = load_queries().SpanStore.from_events(_spans())
        app.render_automatability_view(store)
        table = rec.tables[0]
        approval = next(r for r in table if "tool-permission" in r["Interaction"])
        assert "allow" in str(approval["Decisions"]) and "deny" in str(approval["Decisions"])
