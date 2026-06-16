"""Per-view rollup behaviour of the v3 span kinds (Issue #61).

Issues #50-54 added ``script``, ``workflow``, and ``workflow_phase`` spans that
flow through the Spoke view; the OTHER views (Aggregate / Meta-by-kind / A-B)
grouped blindly by ``(kind, name, phase)`` and applied no special handling.
These tests pin the contract from ``docs/dashboard-spoke-trace-scope.md``
(*Per-view behaviour of new kinds*):

- ``script`` — own time+freq row, **$0 cost** (a script bills no LLM).
- ``workflow`` — own count+time row, **$0 own cost** (cost lives on its ``agent``
  children; counting it here too would double-count, breaking conservation).
- ``workflow_phase`` — **excluded** from every rollup (display-only grouping).

Spans are built inline via ``from_events`` so these never perturb the shared
JSONL fixture the other view tests pin against.
"""

from __future__ import annotations

from _dashboard_helpers import load_queries


def _span(span_id: str, kind: str, name: str, phase: str | None, **over):
    """A minimal span dict; ``over`` sets cost/tokens/duration/rev/etc."""
    base = {
        "span_id": span_id,
        "parent_id": None,
        "spoke_run_id": "feature/x+1000",
        "session_id": "sess-x",
        "workflow_rev": "rev_a",
        "repo": "ai-toolkit",
        "branch": "feature/x",
        "kind": kind,
        "name": name,
        "phase": phase,
        "ts_start": "2026-06-12T12:00:00Z",
        "ts_end": "2026-06-12T12:00:01Z",
        "duration_ms": 1000,
        "status": "success",
        "human": None,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
    }
    base.update(over)
    return base


def _store(events):
    return load_queries().SpanStore.from_events(events)


# A spoke whose script/workflow spans DO carry a cost (the user confirmed they
# can): the rollup must force those to $0, not pass the span value through.
_SPANS = [
    _span(
        "scr",
        "script",
        "spoke-push",
        "push",
        duration_ms=1200,
        cost_usd=0.50,
        tokens_in=10,
        tokens_out=5,
    ),
    _span(
        "wf",
        "workflow",
        "review",
        "design",
        duration_ms=3000,
        cost_usd=0.90,
        tokens_in=100,
        tokens_out=80,
    ),
    _span("wfp", "workflow_phase", "phase-1", "design", duration_ms=500, cost_usd=0.10),
    _span(
        "ag",
        "agent",
        "code-review",
        "design",
        parent_id="wf",
        duration_ms=2000,
        cost_usd=0.90,
        tokens_in=100,
        tokens_out=80,
    ),
]


def _group(rows, kind):
    for row in rows:
        if row["kind"] == kind:
            return row
    raise KeyError(kind)


# --- Aggregate -------------------------------------------------------------


def test_aggregate_script_is_own_row_with_zero_cost():
    row = _group(_store(_SPANS).aggregate(), "script")

    assert row["invocations"] == 1
    assert row["total_duration_ms"] == 1200  # time still rolls up
    assert row["total_cost_usd"] == 0.0  # but cost is forced to $0
    assert row["total_tokens"] == 0
    assert row["mean_tokens"] == 0  # the per-invocation token CASE is zeroed too


def test_aggregate_workflow_and_agent_child_coexist_without_double_count():
    # The workflow row reads $0 while its agent child keeps its real cost in the
    # SAME rollup — a direct demonstration of the no-double-count invariant.
    rows = _store(_SPANS).aggregate()
    assert _group(rows, "workflow")["total_cost_usd"] == 0.0
    assert round(_group(rows, "agent")["total_cost_usd"], 2) == 0.90


def test_aggregate_workflow_keeps_time_but_zeroes_own_cost():
    row = _group(_store(_SPANS).aggregate(), "workflow")

    assert row["invocations"] == 1
    assert row["total_duration_ms"] == 3000
    # cost lives on the workflow's agent children, never on the workflow row
    assert row["total_cost_usd"] == 0.0
    assert row["mean_cost_usd"] == 0.0
    assert row["total_tokens"] == 0


def test_aggregate_agent_child_keeps_its_cost():
    # conservation: zeroing the workflow must NOT zero its agent's real cost
    row = _group(_store(_SPANS).aggregate(), "agent")
    assert round(row["total_cost_usd"], 2) == 0.90


def test_aggregate_excludes_workflow_phase():
    rows = _store(_SPANS).aggregate()
    assert all(row["kind"] != "workflow_phase" for row in rows)


# --- Meta-by-kind ----------------------------------------------------------


def test_meta_by_kind_shows_script_and_workflow_at_zero_cost():
    rows = _store(_SPANS).spoke_meta_by_kind("feature/x+1000")
    kinds = {row["kind"] for row in rows}

    assert {"script", "workflow"} <= kinds
    assert _group(rows, "script")["total_cost_usd"] == 0.0
    assert _group(rows, "workflow")["total_cost_usd"] == 0.0


def test_meta_by_kind_excludes_workflow_phase():
    rows = _store(_SPANS).spoke_meta_by_kind("feature/x+1000")
    assert all(row["kind"] != "workflow_phase" for row in rows)


# --- A-B compare -----------------------------------------------------------

_AB_SPANS = [
    # rev_a: a slower push script + workflow
    _span(
        "a_scr",
        "script",
        "spoke-push",
        "push",
        workflow_rev="rev_a",
        duration_ms=2000,
        cost_usd=0.5,
    ),
    _span(
        "a_wf", "workflow", "review", "design", workflow_rev="rev_a", duration_ms=4000, cost_usd=0.9
    ),
    _span("a_wfp", "workflow_phase", "phase-1", "design", workflow_rev="rev_a", duration_ms=500),
    # rev_b: the same steps, faster (a real regression signal if reversed)
    _span(
        "b_scr",
        "script",
        "spoke-push",
        "push",
        workflow_rev="rev_b",
        duration_ms=1000,
        cost_usd=0.5,
    ),
    _span(
        "b_wf", "workflow", "review", "design", workflow_rev="rev_b", duration_ms=3000, cost_usd=0.9
    ),
    _span("b_wfp", "workflow_phase", "phase-1", "design", workflow_rev="rev_b", duration_ms=400),
]


def test_ab_compare_surfaces_script_and_workflow_time_deltas():
    rows = _store(_AB_SPANS).ab_compare("rev_a", "rev_b")

    script = _group(rows, "script")
    assert script["delta_duration_ms"] == -1000  # 1000 - 2000
    workflow = _group(rows, "workflow")
    assert workflow["delta_duration_ms"] == -1000  # 3000 - 4000


def test_ab_compare_zeroes_script_and_workflow_cost_delta():
    rows = _store(_AB_SPANS).ab_compare("rev_a", "rev_b")
    assert _group(rows, "script")["delta_cost_usd"] == 0.0
    assert _group(rows, "workflow")["delta_cost_usd"] == 0.0


def test_ab_compare_excludes_workflow_phase():
    rows = _store(_AB_SPANS).ab_compare("rev_a", "rev_b")
    assert all(row["kind"] != "workflow_phase" for row in rows)
