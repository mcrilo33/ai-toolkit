"""View B cycle-axis lens: re-home the shared copies onto a pure preStep/step/postStep axis (#113).

:func:`_apply_cycle_axis` remaps every copy into the View B id namespace, flattens each top-level
``claude_code.interaction`` from a container to a childless turn-marker (stamped with its
pre-flatten subtree ``rollup`` so per-turn cost survives, #114), and parents each copy onto the
cycle axis (:func:`_resolve_cycle_parent`) or lets it ride its surviving span by causal key. The
axis nodes (preStep + one ``step:<subject>`` per ledger task + postStep) come from
:func:`_cycle_step_events`. ``build_cycle_batch`` (the orchestrator) drives this after
``_assemble_copies``. Depends on the foundation, steps, folding, and rollups modules.
"""

from __future__ import annotations

from typing import Any

from telemetry.langfuse_rollup import build_tree
from telemetry.spoke_tree.folding import _hook_event_exclude
from telemetry.spoke_tree.ids import _copy_id, _cycle_copy_id, _cycle_step_id
from telemetry.spoke_tree.observations import (
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    IngestEvent,
    TraceObservations,
    _is_audit_instant,
    _is_blocked_tool,
    _is_interaction,
)
from telemetry.spoke_tree.rollups import (
    _container_rollup,
    _duration_class,
    _effective_intervals,
)
from telemetry.spoke_tree.steps import StepWindow, _step_node_metadata, _step_node_name

# The cycle-axis bookend keys that map to their synthetic ids.
_PRE_STEP_KEY = "pre"
_POST_STEP_KEY = "post"


def _cycle_step_for(start: str, windows: list[StepWindow]) -> str:
    """Return the cycle-axis key for a span starting at ``start`` (``pre`` / ``post`` / a task id).

    Before the first window's start -> ``preStep``; after the last ``completed`` -> ``postStep``;
    otherwise the latest-starting window at or before ``start`` (so an inter-step gap span attaches
    to its preceding step). ``windows`` is non-empty and ordered by start.
    """
    if start < windows[0].start:
        return _PRE_STEP_KEY
    if start > max(window.end for window in windows):
        return _POST_STEP_KEY
    chosen = windows[0]
    for window in windows:
        if window.start <= start:
            chosen = window
    return chosen.task_id


def _cycle_axis_event(
    node_id: str,
    name: str,
    start: str,
    end: str,
    parent_id: str,
    trace_id: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> IngestEvent:
    """Shape one View B cycle-axis span-create event (preStep / step:N / postStep)."""
    body: dict[str, Any] = {
        "id": node_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": name,
        "startTime": start,
        "endTime": end,
    }
    if metadata:
        body["metadata"] = metadata
    return {"id": node_id, "type": "span-create", "timestamp": start, "body": body}


def _cycle_step_ids(spoke_run_id: str, windows: list[StepWindow]) -> dict[str, str]:
    """Map each cycle-axis key (``pre`` / ``post`` / a task id) to its deterministic node id."""
    ids = {
        _PRE_STEP_KEY: _cycle_step_id(spoke_run_id, _PRE_STEP_KEY),
        _POST_STEP_KEY: _cycle_step_id(spoke_run_id, _POST_STEP_KEY),
    }
    for window in windows:
        ids[window.task_id] = _cycle_step_id(spoke_run_id, window.task_id)
    return ids


def _cycle_step_events(
    windows: list[StepWindow],
    step_id_for: dict[str, str],
    *,
    root_id: str,
    trace_id: str,
    base_ts: str,
    latest: str,
) -> list[IngestEvent]:
    """Build the preStep + step:N + postStep nodes that partition the cycle timeline under the root."""
    last_completed = max(window.end for window in windows)
    events = [
        _cycle_axis_event(
            step_id_for[_PRE_STEP_KEY], _PRE_STEP_NAME, base_ts, windows[0].start, root_id, trace_id
        )
    ]
    for window in windows:
        events.append(
            _cycle_axis_event(
                step_id_for[window.task_id],
                _step_node_name(window),
                window.start,
                window.end,
                root_id,
                trace_id,
                metadata=_step_node_metadata(window),
            )
        )
    events.append(
        _cycle_axis_event(
            step_id_for[_POST_STEP_KEY], _POST_STEP_NAME, last_completed, latest, root_id, trace_id
        )
    )
    return events


def _resolve_cycle_parent(
    body: dict[str, Any],
    parent_a: str,
    *,
    flattened: set[str],
    by_id_a: dict[str, dict[str, Any]],
    a_root_id: str,
    interaction_start: dict[str, str | None],
    windows: list[StepWindow],
    step_id_for: dict[str, str],
    root_id: str,
) -> str:
    """Resolve one copy's View B parent: ride a surviving span, else land on the cycle axis.

    A copy whose View A parent is a surviving span (a tool, llm_request, sub-agent, or nested
    interaction) keeps that parent (rides along by causal key). A copy left at the synthetic root
    or under a flattened top-level interaction lands on the cycle axis: a reliably-timestamped span
    by its own ``startTime``, an audit instant OR a synthesized ``blocked-tool`` node (whose own
    start is derived from lagging audit timestamps, #157) by its turn's start, falling back to
    ``preStep``. The flattened top-level interaction marker itself is just such a reliably-
    timestamped span (parent is the root), so it lands in the step window of its own start.
    """
    if parent_a != a_root_id and parent_a not in flattened and parent_a in by_id_a:
        return _cycle_copy_id(parent_a)
    if not windows:
        return root_id  # no ledger -> no cycle axis; copies hang flat under the cycle root
    if _is_audit_instant(body) or _is_blocked_tool(body) or not body.get("startTime"):
        anchor = interaction_start.get(parent_a)
        key = _cycle_step_for(anchor, windows) if anchor else _PRE_STEP_KEY
    else:
        key = _cycle_step_for(body["startTime"], windows)
    return step_id_for[key]


def _apply_cycle_axis(
    copies: list[IngestEvent],
    traces: list[TraceObservations],
    windows: list[StepWindow],
    *,
    spoke_run_id: str,
    a_root_id: str,
    root_id: str,
    trace_id: str,
    base_ts: str,
    latest: str,
) -> tuple[list[IngestEvent], list[IngestEvent], set[str]]:
    """Re-home the copies onto the cycle axis and build its nodes (#113 View B, #114 turn markers).

    Remaps every copy into the cycle id namespace, flattens the top-level interactions from
    containers to childless leaf markers (their children land on the axis by their own time; the
    marker itself lands in the step window of its own start), and parents each copy via
    :func:`_resolve_cycle_parent`. Each flattened marker is stamped with its turn's
    ``metadata.rollup`` — the token sum AND ``duration`` split of its pre-flatten View A subtree
    (#114, #128) — so the per-turn cost/latency total stays readable even though the marker is
    now childless. Returns ``(cycle_copies, step_events, marker_ids)``; the copies are mutated
    in place, and ``marker_ids`` (the flattened markers' cycle-namespace ids) must be excluded
    from the cycle-axis duration attribution — a marker's span overlaps its former children,
    now its step siblings.
    """
    interaction_start: dict[str, str | None] = {
        _copy_id(orig_trace_id, observation["id"]): observation.get("startTime")
        for orig_trace_id, observations in traces
        for observation in observations
        if _is_interaction(observation)
    }
    by_id_a = {event["body"]["id"]: event["body"] for event in copies}
    flattened = {
        iid
        for iid in interaction_start
        if iid in by_id_a and by_id_a[iid]["parentObservationId"] == a_root_id
    }
    step_id_for = _cycle_step_ids(spoke_run_id, windows)
    step_events = (
        _cycle_step_events(
            windows, step_id_for, root_id=root_id, trace_id=trace_id, base_ts=base_ts, latest=latest
        )
        if windows
        else []
    )
    # Each flattened marker becomes childless on the cycle axis, so neither _apply_container_rollups
    # (which skips childless nodes) nor Langfuse's descendant aggregation can recover the turn's
    # token/cost total once its generations re-home onto the steps. Precompute it from the still-
    # intact View A subtree and stamp it onto the marker so per-turn cost stays readable (#114). It
    # is kept as metadata.rollup, not usageDetails, so the marker's former children — now its step
    # siblings — are not double-counted in the step/root rollups (subtree_totals sums usageDetails).
    a_bodies = [event["body"] for event in copies]
    a_by_id, a_children = build_tree(a_bodies)
    a_class = {event["body"]["id"]: _duration_class(event) for event in copies}
    # Exclude the stamped hook events from the per-turn rollup too (#157) — their derived width
    # duplicates the guard time already in the ``hook`` bucket, exactly as for the container rollups.
    hook_exclude = _hook_event_exclude(copies)
    a_intervals = _effective_intervals(a_bodies, a_children, hook_exclude)
    turn_rollup = {
        iid: _container_rollup(
            iid,
            by_id=a_by_id,
            children=a_children,
            class_of=a_class,
            intervals=a_intervals,
            exclude=hook_exclude,
        )
        for iid in flattened
    }
    kept: list[IngestEvent] = []
    for event in copies:
        body = event["body"]
        orig_id = body["id"]
        parent_a = body["parentObservationId"]
        new_id = _cycle_copy_id(orig_id)
        body["id"] = new_id
        event["id"] = new_id
        body["traceId"] = trace_id
        body["parentObservationId"] = _resolve_cycle_parent(
            body,
            parent_a,
            flattened=flattened,
            by_id_a=by_id_a,
            a_root_id=a_root_id,
            interaction_start=interaction_start,
            windows=windows,
            step_id_for=step_id_for,
            root_id=root_id,
        )
        if orig_id in flattened:
            body.setdefault("metadata", {})["rollup"] = turn_rollup[orig_id]
        kept.append(event)
    return kept, step_events, {_cycle_copy_id(iid) for iid in flattened}
