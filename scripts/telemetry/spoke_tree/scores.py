"""Numeric Langfuse scores that make a spoke's metadata chartable (#100, #101, #158).

Langfuse dashboards can sum/aggregate numeric SCORES but not arbitrary observation metadata, so
signals already present as metadata are ALSO emitted as scores: :func:`build_score_events` emits
per-tool ``permission_wait_ms`` / ``tool_result_size`` and the trace-level ``gate_park_ms`` (from
:func:`~telemetry.spoke_tree.commits._gate_park_ms`); :func:`build_step_cost_scores` emits per-phase
``step_cache_write_usd`` / ``step_tokens_written`` from View B's step rollups; and
:func:`build_step_total_cost_scores` emits per-phase ``step_total_cost_usd`` — the true all-
generations cost windowed onto each step (#230). Depends on the foundation, ``ids``, ``steps``,
and ``commits``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from telemetry.spoke_tree.commits import _gate_park_ms
from telemetry.spoke_tree.cycle import _POST_STEP_KEY, _PRE_STEP_KEY
from telemetry.spoke_tree.ids import _CYCLE_STEP_PREFIX, cycle_trace_id_for, trace_id_for
from telemetry.spoke_tree.observations import (
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    IngestEvent,
    TraceObservations,
)

# Deterministic id prefix for the numeric Langfuse scores (#100 amendment: chartable time budget).
_SCORE_PREFIX = "tree-score-"
# Score names — Langfuse sums/charts numeric scores (it cannot chart arbitrary metadata).
_PERMISSION_WAIT_SCORE = "permission_wait_ms"  # per blocked tool observation
_GATE_PARK_SCORE = "gate_park_ms"  # trace-level PLAN-gate park wait
_TOOL_RESULT_SIZE_SCORE = "tool_result_size"  # bytes of a tool node's reconstructed tool_result
# Per-phase step cost/token scores (#158): the phase is the score-name suffix (a metrics
# dimension), so "what does RED cost across all spokes" is a one-widget Scores query.
_STEP_CACHE_WRITE_SCORE = (
    "step_cache_write_usd"  # per View B step observation, from rollup.written x cache-write price
)
_STEP_TOKENS_WRITTEN_SCORE = (
    "step_tokens_written"  # per View B step observation, from rollup.written
)
# True per-step cost (#230): sum EVERY generation's full Langfuse cost (main-loop +
# sub-agent:llm) into the cycle-step that contains it, so the per-phase scores reconcile to
# the trace totalCost rather than only the cache-write slice above.
_STEP_TOTAL_COST_SCORE = "step_total_cost_usd"  # per View B step observation, from costDetails
# The canonical solo-cycle phases parsed out of a step subject (e.g. "A-RED: …" → RED). Kept a
# closed set so a step subject can never mint a free-text score name (a metrics-cardinality guard).
_STEP_PHASES = ("ANCHOR", "RED", "GREEN", "REVIEW", "PUSH")
_STEP_PHASE_OTHER = "other"
_STEP_PHASE_RE = re.compile(rf"\b({'|'.join(_STEP_PHASES)})\b")


def _score_id(spoke_run_id: str, name: str, target: str) -> str:
    """Return the deterministic id of one score for a spoke (idempotent across reruns)."""
    digest = hashlib.sha1(f"{spoke_run_id}:score:{name}:{target}".encode()).hexdigest()[:24]
    return _SCORE_PREFIX + digest


def _score_event(
    spoke_run_id: str,
    *,
    name: str,
    value: float,
    trace_id: str,
    base_ts: str,
    observation_id: str | None = None,
) -> IngestEvent:
    """Shape one numeric ``score-create`` ingestion event (trace- or observation-level)."""
    target = observation_id or "trace"
    score_id = _score_id(spoke_run_id, name, target)
    body: dict[str, Any] = {
        "id": score_id,
        "traceId": trace_id,
        "name": name,
        "value": value,
        "dataType": "NUMERIC",
    }
    if observation_id is not None:
        body["observationId"] = observation_id
    return {"id": score_id, "type": "score-create", "timestamp": base_ts, "body": body}


def build_score_events(
    spoke_run_id: str,
    traces: list[TraceObservations],
    batch: list[IngestEvent],
    *,
    base_ts: str,
) -> list[IngestEvent]:
    """Build the numeric Langfuse scores that make a spoke's metadata chartable (#100, #101).

    Langfuse dashboards can sum/aggregate numeric SCORES but not arbitrary observation metadata,
    so three signals already present as metadata are ALSO emitted as scores:

    - ``permission_wait_ms`` — an observation-level score on every ``tool:`` node carrying a
      folded ``blocked_on_user_ms`` (Part 2), so permission-prompt wait sums across spokes.
    - ``tool_result_size`` — an observation-level score on every ``tool:`` node carrying a
      reconstructed ``tool_result_size`` (#101 part 4), so "which tool outputs bloat context"
      is a one-click chart.
    - ``gate_park_ms`` — a trace-level score for the PLAN-gate park (:func:`_gate_park_ms`),
      emitted only when the spoke parked at a gate.

    All ids derive from the spoke run id so a rerun overwrites the same scores.

    Args:
        spoke_run_id: The spoke run identifier.
        traces: The source traces (for the gate-park gap).
        batch: The assembled events (read for the folded ``blocked_on_user_ms`` /
            ``tool_result_size`` tool metadata).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` ingestion events (empty when no signal is present).
    """
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in batch:
        body = event["body"]
        metadata = body.get("metadata") or {}
        wait = metadata.get("blocked_on_user_ms")
        if wait is not None:
            events.append(
                _score_event(
                    spoke_run_id,
                    name=_PERMISSION_WAIT_SCORE,
                    value=int(wait),
                    trace_id=trace_id,
                    base_ts=base_ts,
                    observation_id=body["id"],
                )
            )
        size = metadata.get("tool_result_size")
        if size is not None:
            events.append(
                _score_event(
                    spoke_run_id,
                    name=_TOOL_RESULT_SIZE_SCORE,
                    value=int(size),
                    trace_id=trace_id,
                    base_ts=base_ts,
                    observation_id=body["id"],
                )
            )
    park = _gate_park_ms(traces)
    if park is not None:
        events.append(
            _score_event(
                spoke_run_id,
                name=_GATE_PARK_SCORE,
                value=park,
                trace_id=trace_id,
                base_ts=base_ts,
            )
        )
    return events


def _step_phase(subject: str) -> str:
    """Return the canonical solo-cycle phase named in a step subject, or ``other`` (#158).

    ``"A-RED: red first"`` → ``RED``; ``"ANCHOR #154 …"`` → ``ANCHOR``; a compound subject like
    ``"REVIEW + PUSH"`` takes the leftmost keyword (``REVIEW``). The result is always one of the
    closed :data:`_STEP_PHASES` set or ``other`` — never free text — so it is a safe score-name
    suffix.
    """
    match = _STEP_PHASE_RE.search(subject.upper())
    return match.group(1) if match else _STEP_PHASE_OTHER


def _step_phase_of(body: dict[str, Any]) -> str:
    """Return the phase of one View B step node: ``pre`` / ``post`` for the boundary partitions,
    else the phase parsed from its subject."""
    name = body.get("name") or ""
    if name == _PRE_STEP_NAME:
        return _PRE_STEP_KEY
    if name == _POST_STEP_NAME:
        return _POST_STEP_KEY
    subject = (body.get("metadata") or {}).get("subject") or name
    return _step_phase(subject)


def build_step_cost_scores(
    spoke_run_id: str, cycle_batch: list[IngestEvent], *, base_ts: str, price: float
) -> list[IngestEvent]:
    """Build per-phase step cost/token scores from View B's step rollups (#158).

    ``step:*`` nodes carry token rollups only in ``metadata.rollup`` (never ``usageDetails`` — the
    #114 double-count guard), so per-step cost is invisible to the Metrics API. Score NAMES are a
    metrics dimension, so each View B step emits ``step_cache_write_usd:<PHASE>`` and
    ``step_tokens_written:<PHASE>`` from its rollup's ``written`` tokens (cost = written x the
    cache-creation ``price``), observation-scoped to the step node with a deterministic id. This
    score is cache-WRITE cost only (its #230 rename made that explicit); the true
    all-generations per-step total is a separate score.

    Emitted on View B (the cycle lens) ONLY: a step lives on both views, but scoring both would
    double every phase in a Scores-view sum, so — like the other per-call enrichments — this is
    single-emit. A step with no rollup (a childless boundary partition) is skipped.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        cycle_batch: The assembled View B events (its step nodes' rollups are read).
        base_ts: ISO timestamp stamped on every score event.
        price: Cache-creation price in USD per written token.

    Returns:
        The ``score-create`` events (empty when View B has no step rollups).
    """
    trace_id = cycle_trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for event in cycle_batch:
        body = event["body"]
        if not body["id"].startswith(_CYCLE_STEP_PREFIX):
            continue
        written = ((body.get("metadata") or {}).get("rollup") or {}).get("written")
        if written is None:
            continue
        phase = _step_phase_of(body)
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_CACHE_WRITE_SCORE}:{phase}",
                value=written * price,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_TOKENS_WRITTEN_SCORE}:{phase}",
                value=written,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=body["id"],
            )
        )
    return events


def _generation_total_cost(body: dict[str, Any]) -> float:
    """Return one generation's full USD cost from its Langfuse ``costDetails`` (#230).

    Langfuse computes each generation's cost from token usage x its model price table and returns
    it as ``costDetails``. A reserved ``total`` key is the aggregate and wins when present;
    otherwise the per-usage-type components are summed. The sum over every generation therefore
    reconciles to the trace ``totalCost``. A generation with no ``costDetails`` contributes 0.
    """
    cost_details = body.get("costDetails") or {}
    total = cost_details.get("total")
    if isinstance(total, (int, float)):
        return float(total)
    return float(sum(v for v in cost_details.values() if isinstance(v, (int, float))))


def _nearest_step_ancestor(
    node_id: str, parent_of: dict[str, str | None], step_ids: set[str]
) -> str | None:
    """Walk ``parentObservationId`` up from ``node_id`` to the nearest cycle-step ancestor, or None.

    A generation may sit directly under a cycle-step node or under a ``sub-agent:<type>`` container
    that itself lands on a step, so the cost lands in the step whose window contains the work. The
    cycle-visited set guards against a malformed parent cycle.
    """
    seen: set[str] = set()
    current = parent_of.get(node_id)
    while current is not None and current not in seen:
        if current in step_ids:
            return current
        seen.add(current)
        current = parent_of.get(current)
    return None


def build_step_total_cost_scores(
    spoke_run_id: str, cycle_batch: list[IngestEvent], *, base_ts: str
) -> list[IngestEvent]:
    """Build per-phase ``step_total_cost_usd:<PHASE>`` scores windowing all generations (#230).

    Unlike :func:`build_step_cost_scores` (cache-WRITE cost only), this sums each generation's full
    Langfuse ``costDetails`` — main-loop ``claude_code.llm_request`` AND ``sub-agent:llm`` — into
    the cycle-step that contains it. Attribution keys off the authoritative View B spine: every
    generation copy is walked up ``parentObservationId`` to its nearest cycle-step ancestor
    (:func:`_nearest_step_ancestor`), so a sub-agent's calls land in the step its container sits
    under. The per-phase scores therefore reconcile to the trace ``totalCost``, with pre-first-step
    spend surfacing as a ``step_total_cost_usd:pre`` residual.

    Emitted on View B (the cycle lens) ONLY, observation-scoped to each step node with a
    deterministic id, mirroring :func:`build_step_cost_scores`. A step with no generation windowed
    into it emits nothing.

    Args:
        spoke_run_id: The spoke run identifier (keys the deterministic score ids).
        cycle_batch: The assembled View B events (its generation copies' ``costDetails`` are read).
        base_ts: ISO timestamp stamped on every score event.

    Returns:
        The ``score-create`` events (empty when View B has no generations under a step).
    """
    trace_id = cycle_trace_id_for(spoke_run_id)
    by_id = {event["body"]["id"]: event["body"] for event in cycle_batch}
    parent_of = {node_id: body.get("parentObservationId") for node_id, body in by_id.items()}
    step_ids = {node_id for node_id in by_id if node_id.startswith(_CYCLE_STEP_PREFIX)}
    cost_by_step: dict[str, float] = {}
    for event in cycle_batch:
        if event["type"] != "generation-create":
            continue
        step_id = _nearest_step_ancestor(event["body"]["id"], parent_of, step_ids)
        if step_id is None:
            continue
        cost_by_step[step_id] = cost_by_step.get(step_id, 0.0) + _generation_total_cost(
            event["body"]
        )
    events: list[IngestEvent] = []
    for step_id, cost in cost_by_step.items():
        phase = _step_phase_of(by_id[step_id])
        events.append(
            _score_event(
                spoke_run_id,
                name=f"{_STEP_TOTAL_COST_SCORE}:{phase}",
                value=cost,
                trace_id=trace_id,
                base_ts=base_ts,
                observation_id=step_id,
            )
        )
    return events
