"""Group spans into spoke-run lifetimes and compute per-invocation metrics.

A spoke run is one spoke's whole life (spawn → teardown), tied together by
``spoke_run_id`` across the many Claude sessions it spans. Push spans (hooks,
steps, lifecycle) carry the id; pull spans parsed from session logs do not, so
they are first backfilled from the push spans that share their ``session_id``.

Per spoke run, normalized per-invocation metrics (mean/median duration, total
and mean cost, human-interaction count) are computed per *step key* — the
toolkit construct identity (``kind:name[:phase]``) — so the dashboard can
compare runs that invoked a step a different number of times.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from telemetry.spans import Span


@dataclass(slots=True)
class InvocationMetrics:
    """Normalized metrics for repeated invocations of one step key in a run."""

    key: str
    count: int
    mean_duration_ms: float
    median_duration_ms: float
    total_cost_usd: float
    mean_cost_usd: float
    human_count: int


@dataclass(slots=True)
class SpokeRun:
    """All spans of one spoke run, with its lifetime and per-step metrics."""

    spoke_run_id: str | None
    spans: list[Span] = field(default_factory=list)
    ts_start: str | None = None
    ts_end: str | None = None
    total_cost_usd: float = 0.0
    metrics: dict[str, InvocationMetrics] = field(default_factory=dict)


def backfill_spoke_run_ids(spans: list[Span]) -> list[Span]:
    """Fill null ``spoke_run_id`` from a session peer that has one (in place).

    A spoke run spans many sessions, but within a single session every span
    belongs to the same run — so a pull span's null id can be recovered from any
    push span sharing its ``session_id``.

    Returns:
        The same ``spans`` list, mutated.
    """
    by_session: dict[str, str] = {}
    for span in spans:
        if span.spoke_run_id is not None and span.session_id is not None:
            by_session.setdefault(span.session_id, span.spoke_run_id)
    for span in spans:
        if span.spoke_run_id is None and span.session_id in by_session:
            span.spoke_run_id = by_session[span.session_id]
    return spans


def group_spoke_runs(spans: list[Span], *, backfill: bool = True) -> list[SpokeRun]:
    """Group spans into spoke runs and compute each run's normalized metrics.

    Args:
        spans: The unified push + pull spans.
        backfill: When True, recover null ``spoke_run_id`` from session peers
            before grouping. Spans still null afterwards group under ``None``
            (ad-hoc, non-spoke work).

    Returns:
        One :class:`SpokeRun` per distinct ``spoke_run_id`` (including ``None``).
    """
    if backfill:
        backfill_spoke_run_ids(spans)
    grouped: dict[str | None, list[Span]] = {}
    for span in spans:
        grouped.setdefault(span.spoke_run_id, []).append(span)
    return [_build_run(run_id, run_spans) for run_id, run_spans in grouped.items()]


def _build_run(run_id: str | None, spans: list[Span]) -> SpokeRun:
    starts = [s.ts_start for s in spans if s.ts_start]
    ends = [s.ts_end for s in spans if s.ts_end]
    return SpokeRun(
        spoke_run_id=run_id,
        spans=spans,
        ts_start=min(starts) if starts else None,
        ts_end=max(ends) if ends else None,
        total_cost_usd=sum(s.cost_usd or 0.0 for s in spans),
        metrics=_metrics_by_step(spans),
    )


def _metrics_by_step(spans: list[Span]) -> dict[str, InvocationMetrics]:
    by_key: dict[str, list[Span]] = {}
    for span in spans:
        by_key.setdefault(_step_key(span), []).append(span)
    return {key: _invocation_metrics(key, group) for key, group in by_key.items()}


def _invocation_metrics(key: str, spans: list[Span]) -> InvocationMetrics:
    durations = [s.duration_ms for s in spans]
    costs = [s.cost_usd or 0.0 for s in spans]
    total_cost = sum(costs)
    return InvocationMetrics(
        key=key,
        count=len(spans),
        mean_duration_ms=statistics.mean(durations),
        median_duration_ms=statistics.median(durations),
        total_cost_usd=total_cost,
        mean_cost_usd=total_cost / len(spans),
        human_count=sum(1 for s in spans if s.human is not None),
    )


def _step_key(span: Span) -> str:
    """Identity of a step for grouping invocations: ``kind:name[:phase]``."""
    if span.phase:
        return f"{span.kind}:{span.name}:{span.phase}"
    return f"{span.kind}:{span.name}"
