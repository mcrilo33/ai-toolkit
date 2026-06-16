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

from _dashboard_helpers import load_queries

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
