"""Spoke-run grouping + per-invocation normalized metrics (Issue #22, subtask 3 — RED).

Spans are grouped into spoke-run lifetimes by ``spoke_run_id``. Pull spans
(parsed from session logs) carry a null ``spoke_run_id``; they are backfilled
from the push spans that share their ``session_id`` (a spoke run spans many
sessions, but within one session the spoke_run_id is constant). Spans with no
spoke_run_id and no session match are ad-hoc and group under ``None``.

Per spoke run, normalized per-invocation metrics (mean/median duration, cost,
human-interaction count per step key) let the dashboard compare fairly across
runs that invoked a step a different number of times.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spans import Span
from telemetry.spoke_runs import backfill_spoke_run_ids, group_spoke_runs

RUN_A = "feature/22-demo+1700000000"
RUN_B = "feature/22-other+1700009999"

_BASE = Span(
    span_id="s",
    kind="step",
    name="solo-cycle",
    phase="red",
    session_id="sess-1",
    spoke_run_id=RUN_A,
    ts_start="2026-06-13T12:00:00Z",
    ts_end="2026-06-13T12:00:01Z",
    duration_ms=1000,
    cost_usd=0.1,
)


def _span(**over) -> Span:
    return replace(_BASE, **over)


class TestBackfill:
    def test_pull_span_inherits_spoke_run_id_from_session_peer(self) -> None:
        push = _span(
            span_id="p", kind="hook", name="secrets-scan.sh", phase=None, session_id="sess-9"
        )
        pull = _span(
            span_id="u",
            kind="skill",
            name="source-task",
            phase=None,
            session_id="sess-9",
            spoke_run_id=None,
        )

        backfill_spoke_run_ids([push, pull])

        assert pull.spoke_run_id == RUN_A

    def test_span_with_unknown_session_stays_null(self) -> None:
        orphan = _span(
            span_id="o",
            kind="human",
            name="prompt",
            phase=None,
            session_id="sess-unknown",
            spoke_run_id=None,
        )

        backfill_spoke_run_ids([orphan])

        assert orphan.spoke_run_id is None


class TestGrouping:
    def test_groups_by_spoke_run_id(self) -> None:
        spans = [_span(span_id="a"), _span(span_id="b", spoke_run_id=RUN_B)]

        runs = group_spoke_runs(spans)

        assert {r.spoke_run_id for r in runs} == {RUN_A, RUN_B}

    def test_spoke_run_lifetime_is_min_start_to_max_end(self) -> None:
        spans = [
            _span(span_id="a", ts_start="2026-06-13T12:00:00Z", ts_end="2026-06-13T12:00:05Z"),
            _span(span_id="b", ts_start="2026-06-13T12:10:00Z", ts_end="2026-06-13T12:10:30Z"),
        ]

        run = group_spoke_runs(spans)[0]

        assert run.ts_start == "2026-06-13T12:00:00Z"
        assert run.ts_end == "2026-06-13T12:10:30Z"

    def test_total_cost_summed_over_spans(self) -> None:
        spans = [_span(span_id="a", cost_usd=0.1), _span(span_id="b", cost_usd=0.3)]

        run = group_spoke_runs(spans)[0]

        assert run.total_cost_usd == 0.4

    def test_ad_hoc_spans_group_under_none(self) -> None:
        spans = [_span(span_id="x", session_id="sess-x", spoke_run_id=None)]

        runs = group_spoke_runs(spans)

        assert [r.spoke_run_id for r in runs] == [None]


class TestPerInvocationMetrics:
    def test_mean_median_and_count_per_step_key(self) -> None:
        spans = [
            _span(span_id="r1", duration_ms=1000, cost_usd=0.1),
            _span(span_id="r2", duration_ms=3000, cost_usd=0.3),
        ]

        run = group_spoke_runs(spans)[0]
        red = run.metrics["step:solo-cycle:red"]

        assert red.count == 2
        assert red.mean_duration_ms == 2000
        assert red.median_duration_ms == 2000
        assert red.total_cost_usd == 0.4
        assert red.mean_cost_usd == 0.2

    def test_distinct_step_keys_are_separate_invocations(self) -> None:
        spans = [
            _span(span_id="red", phase="red"),
            _span(span_id="push", phase="push"),
        ]

        run = group_spoke_runs(spans)[0]

        assert set(run.metrics) == {"step:solo-cycle:red", "step:solo-cycle:push"}

    def test_human_interaction_count_per_step(self) -> None:
        spans = [
            _span(
                span_id="q1",
                kind="human",
                name="AskUserQuestion",
                phase=None,
                human={"type": "question", "wait_ms": 5000},
            ),
            _span(
                span_id="q2",
                kind="human",
                name="AskUserQuestion",
                phase=None,
                human={"type": "question", "wait_ms": 7000},
            ),
            _span(span_id="r1", duration_ms=1000),
        ]

        run = group_spoke_runs(spans)[0]

        assert run.metrics["human:AskUserQuestion"].human_count == 2
        assert run.metrics["step:solo-cycle:red"].human_count == 0
