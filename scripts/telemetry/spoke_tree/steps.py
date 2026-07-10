"""View A step lens: derive solo-cycle step windows from the todo ledger and wrap siblings (#113).

Each ``TaskCreate`` subject is a step whose ``in_progress`` → ``completed`` ``TaskUpdate`` bounds a
window (:func:`build_step_windows`); :func:`_apply_step_grouping` inserts a ``step:<subject>`` node
INSIDE each interaction that holds the task's markers, wrapping the contiguous run of same-parent
siblings in the window. :func:`_collapse_startup_instants` demotes session-startup instants to the
root's metadata (#104).

For the View B cycle spine, :func:`build_cycle_windows` PREFERS the mechanical ``step:<phase>``
marker spans (``cycle-step-mark.sh``, #235) over the ledger — a crashed/relaunched spoke that never
touches its ledger still yields a complete RED/GREEN/REVIEW/PUSH axis, with the ledger consulted for
labels only; it falls back to :func:`build_step_windows` when no markers were stamped. Depends only
on the foundation modules.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, NamedTuple

from telemetry.spoke_tree.ids import _copy_id
from telemetry.spoke_tree.observations import (
    IngestEvent,
    ToolContent,
    TraceObservations,
    _cycle_marker_phase,
    _is_audit_instant,
    _is_cycle_step_marker,
    _is_startup_instant,
    _latest_time,
    _tool_use_id,
)

# Deterministic id prefix for the synthetic cycle-step nodes (#100, derived from the ledger).
_STEP_PREFIX = "tree-step-"
# Root metadata field collecting the demoted session-startup instants (#104).
_SESSION_INIT_FIELD = "session_init"
# Matches the numeric task id in a TaskCreate result ("Task #1 created successfully: …"); the
# matching TaskUpdate carries the same id (bare digits) in its ``taskId`` input.
_TASK_ID_RE = re.compile(r"#(\d+)")


class StepWindow(NamedTuple):
    """One solo-cycle step derived from the todo ledger (#100).

    The ``subject`` is the ``TaskCreate`` title (``S1 RED: …``); the window spans the task's
    ``in_progress`` ``TaskUpdate`` start to its ``completed`` ``TaskUpdate`` end. In View A the
    same-parent interaction siblings whose ``startTime`` falls in ``[start, end]`` re-home under a
    local step node (:func:`_apply_step_grouping`); in View B every reliably-timestamped span in
    the window re-homes under the cycle step.
    """

    task_id: str
    subject: str
    start: str
    end: str
    status: str


def _step_id(spoke_run_id: str, task_id: str, parent_id: str) -> str:
    """Return the deterministic id of one cycle-step node for a spoke.

    Keyed by the wrap's parent as well as the task, so a cross-turn task that produces a
    partial wrap in more than one interaction gets a distinct, stable id per interaction (#113).
    """
    digest = hashlib.sha1(f"{spoke_run_id}:step:{task_id}:{parent_id}".encode()).hexdigest()[:24]
    return _STEP_PREFIX + digest


def _task_id_from_create(output: object | None) -> str | None:
    """Extract the created task id from a ``TaskCreate`` result, or None.

    The transcript ``tool_result`` content is usually the ``"Task #N created…"`` string but can
    arrive as a list of content blocks (``[{"type": "text", "text": …}]``); both are searched by
    serializing non-string output, since the only ``#N`` in the result is the task id.
    """
    if output is None:
        return None
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    match = _TASK_ID_RE.search(text)
    return match.group(1) if match else None


def _ledger_subjects(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, str]:
    """Map each created task id to its ``TaskCreate`` subject (the step title)."""
    subjects: dict[str, str] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskCreate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            subject = content.input.get("subject")
            task_id = _task_id_from_create(content.output)
            if subject and task_id:
                subjects[task_id] = str(subject)
    return subjects


def _ledger_bounds(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, dict[str, str]]:
    """Map each task id to its window bounds from ``TaskUpdate`` status transitions.

    ``start`` is the earliest ``in_progress`` update's ``startTime``; ``end`` is the latest
    ``completed`` update's ``endTime`` (resumes can re-mark a task, so the extremes win).
    """
    bounds: dict[str, dict[str, str]] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskUpdate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            task_id = str(content.input.get("taskId") or "")
            if not task_id:
                continue
            entry = bounds.setdefault(task_id, {})
            status = content.input.get("status")
            start = observation.get("startTime")
            if status == "in_progress" and start:
                entry["start"] = start if "start" not in entry else min(entry["start"], start)
            if status == "completed":
                end = observation.get("endTime") or observation.get("startTime") or ""
                if end:
                    entry["end"] = end if "end" not in entry else max(entry["end"], end)
                entry["status"] = "completed"
    return bounds


def build_step_windows(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> list[StepWindow]:
    """Derive the solo-cycle step windows from the todo ledger (#100).

    Each ``TaskCreate`` subject is a step; its ``in_progress`` → ``completed`` ``TaskUpdate``
    timestamps bound the window. A task created but never started (no ``in_progress``) has no
    window and is skipped. An in-flight task (no ``completed``) clamps its end to the spoke's
    last observation. Non-ledger spokes (no ``TaskCreate``) yield ``[]`` — no step grouping.

    Args:
        traces: The source traces paired with their observations.
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).

    Returns:
        The step windows in chronological start order.
    """
    subjects = _ledger_subjects(traces, tool_content)
    if not subjects:
        return []
    bounds = _ledger_bounds(traces, tool_content)
    fallback_end = _latest_time(traces)
    windows: list[StepWindow] = []
    for task_id, subject in subjects.items():
        bound = bounds.get(task_id)
        if not bound or "start" not in bound:
            continue
        windows.append(
            StepWindow(
                task_id=task_id,
                subject=subject,
                start=bound["start"],
                end=bound.get("end") or fallback_end,
                status=bound.get("status", "in_progress"),
            )
        )
    windows.sort(key=lambda window: window.start)
    return windows


def _marker_spans(traces: list[TraceObservations]) -> list[dict[str, Any]]:
    """Return the timestamped solo-cycle marker spans across all traces, sorted by start (#235)."""
    markers = [
        observation
        for _orig_trace_id, observations in traces
        for observation in observations
        if _is_cycle_step_marker(observation) and observation.get("startTime")
    ]
    markers.sort(key=lambda observation: observation["startTime"])
    return markers


def _subject_names_phase(subject: str, phase: str) -> bool:
    """Whether ``subject`` names the marker ``phase`` as a whole word (e.g. ``"S1 RED: …"`` / RED)."""
    return re.search(rf"\b{re.escape(phase.upper())}\b", subject.upper()) is not None


def _marker_label(phase: str, start: str, end: str, ledger: list[StepWindow]) -> str:
    """Borrow an overlapping same-phase ledger subject for the marker window, else the PHASE (#235).

    The mechanical marker owns the window bounds; the ledger only supplies a human label. A ledger
    window of the same phase that overlaps ``[start, end]`` lends its subject; otherwise the bare
    uppercase phase (``GREEN``) is used — still parseable by the closed-set phase scorer.
    """
    for window in ledger:
        if (
            window.start <= end
            and start <= window.end
            and _subject_names_phase(window.subject, phase)
        ):
            return window.subject
    return phase.upper()


def build_cycle_windows(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> list[StepWindow]:
    """Derive the View B cycle spine, preferring the mechanical marker spans (#235).

    ``cycle-step-mark.sh`` stamps one ``step:<phase>`` marker span per solo-cycle boundary from the
    mechanical witness of each transition (a ``Tested-RED:`` commit -> red, a plain commit -> green,
    a ``.review/*.json`` write -> review, a branch push -> push). This spine is LLM-independent, so a
    crashed/relaunched spoke that never touches its ledger still yields a complete step axis — unlike
    :func:`build_step_windows`, whose windows vanish when the ledger is empty.

    Each marker opens a window that runs to the next marker's start (the last clamps to the spoke's
    latest activity). The ledger is consulted for LABELS only: an overlapping same-phase ledger
    subject is borrowed (so a step reads ``step:S1 RED: …``), otherwise the bare uppercase phase.
    When no markers were stamped (legacy traces, or a telemetry-off run) it falls back to the
    ledger-derived windows unchanged.

    Args:
        traces: The source traces paired with their observations.
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output, for labels).

    Returns:
        The cycle-step windows in chronological start order.
    """
    markers = _marker_spans(traces)
    ledger = build_step_windows(traces, tool_content)
    if not markers:
        return ledger
    latest = _latest_time(traces)
    windows: list[StepWindow] = []
    for index, marker in enumerate(markers):
        start = marker["startTime"]
        end = markers[index + 1]["startTime"] if index + 1 < len(markers) else latest
        if end < start:
            end = start
        phase = _cycle_marker_phase(marker)
        windows.append(
            StepWindow(
                task_id=f"marker{index}",
                subject=_marker_label(phase, start, end, ledger),
                start=start,
                end=end,
                status="completed",
            )
        )
    return windows


def _containing_window(start: str, windows: list[StepWindow]) -> StepWindow | None:
    """Return the innermost step window containing ``start`` (latest start wins), or None.

    ``windows`` is ordered by start, so iterating and overwriting yields the latest-starting
    window that contains the timestamp — the innermost on an overlap.
    """
    chosen: StepWindow | None = None
    for window in windows:
        if window.start <= start <= window.end:
            chosen = window
    return chosen


def _step_node_name(window: StepWindow) -> str:
    """Return the shared ``step:<subject>`` node name for a ledger window (both views)."""
    return f"step:{window.subject}"


def _step_node_metadata(window: StepWindow) -> dict[str, Any]:
    """Return the shared step-node metadata for a ledger window (both views)."""
    return {
        "subject": window.subject,
        "status": window.status,
        "started": window.start,
        "completed": window.end,
    }


def _step_event(window: StepWindow, step_id: str, parent_id: str, trace_id: str) -> IngestEvent:
    """Shape one cycle-step span-create event nested under ``parent_id`` (the local wrap parent)."""
    return {
        "id": step_id,
        "type": "span-create",
        "timestamp": window.start,
        "body": {
            "id": step_id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "name": _step_node_name(window),
            "startTime": window.start,
            "endTime": window.end,
            "metadata": _step_node_metadata(window),
        },
    }


def _collapse_startup_instants(
    copies: list[IngestEvent], root_event: IngestEvent
) -> list[IngestEvent]:
    """Demote session-startup instants to the root's ``session_init`` metadata, dropping nodes (#104).

    Each ``mcp_server_connection`` / ``plugin_loaded`` copy is summarised as ``{"name", …metadata}``
    onto the synthetic root's ``session_init`` list (preserving fetch order) and its node is removed,
    so a spoke's startup events read as one metadata field instead of N sibling spans placed by the
    lagging log timestamp. No ``session_init`` key is written when the spoke has no startup instants.

    Args:
        copies: The source observation copies; startup-instant copies are removed.
        root_event: The synthetic root event; its metadata is mutated in place.

    Returns:
        The copies with the startup-instant nodes removed.
    """
    init: list[dict[str, Any]] = []
    kept: list[IngestEvent] = []
    for event in copies:
        body = event["body"]
        if not _is_startup_instant(body):
            kept.append(event)
            continue
        init.append({"name": body.get("name"), **(body.get("metadata") or {})})
    if init:
        root_event["body"].setdefault("metadata", {})[_SESSION_INIT_FIELD] = init
    return kept


class _LedgerMarkers(NamedTuple):
    """Copy ids of one task's ledger markers (#113).

    ``create`` is the ``TaskCreate`` copy id(s); ``anchors`` is the ``in_progress`` / ``completed``
    ``TaskUpdate`` copy ids whose parent locates the local wrap. Both are absorbed under the step.
    """

    create: set[str]
    anchors: set[str]


def _ledger_marker_ids(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, _LedgerMarkers]:
    """Map each task id to the copy ids of its ``TaskCreate`` + ``in_progress``/``completed`` markers."""
    markers: dict[str, _LedgerMarkers] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            name = observation.get("name") or ""
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            copy_id = _copy_id(orig_trace_id, observation["id"])
            if name == "tool:TaskCreate":
                task_id = _task_id_from_create(content.output)
                if task_id:
                    markers.setdefault(task_id, _LedgerMarkers(set(), set())).create.add(copy_id)
            elif name == "tool:TaskUpdate" and content.input.get("status") in (
                "in_progress",
                "completed",
            ):
                task_id = str(content.input.get("taskId") or "")
                if task_id:
                    markers.setdefault(task_id, _LedgerMarkers(set(), set())).anchors.add(copy_id)
    return markers


def _anchor_parents(
    windows: list[StepWindow],
    markers: dict[str, _LedgerMarkers],
    by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[StepWindow]], dict[str, set[str]]]:
    """Resolve each task's anchor parents (where its anchor markers sit) and group windows by them.

    Returns ``(windows_by_parent, parents_by_task)``: the first maps a parent copy id to the
    windows anchored under it (in start order, for the innermost-wins tie-break); the second maps
    a task id to the set of parents that hold its ``in_progress`` / ``completed`` markers.
    """
    windows_by_parent: dict[str, list[StepWindow]] = {}
    parents_by_task: dict[str, set[str]] = {}
    for window in windows:
        slots = markers.get(window.task_id)
        if slots is None:
            continue
        parents = {by_id[c]["parentObservationId"] for c in slots.anchors if c in by_id}
        parents_by_task[window.task_id] = parents
        for parent in parents:
            windows_by_parent.setdefault(parent, []).append(window)
    return windows_by_parent, parents_by_task


def _wrap_members(
    parent: str,
    window: StepWindow,
    children: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]],
    windows_by_parent: dict[str, list[StepWindow]],
    all_marker_ids: set[str],
) -> list[str]:
    """Return ``parent``'s non-marker children whose innermost in-window step is ``window``.

    A child qualifies when it is not a ledger marker, not a lagging-timestamped audit instant
    (#104), and its ``startTime`` falls in ``window`` — with the innermost (latest-starting)
    window among those anchored at ``parent`` deciding ties on overlap.
    """
    members: list[str] = []
    for child in children.get(parent, []):
        if child in all_marker_ids:
            continue
        body = by_id[child]
        if _is_audit_instant(body):
            continue
        start = body.get("startTime")
        if not start or not (window.start <= start <= window.end):
            continue
        if _containing_window(start, windows_by_parent[parent]) is window:
            members.append(child)
    return members


def _task_marker_ids(task_id: str, markers: dict[str, _LedgerMarkers]) -> set[str]:
    """Return the task's ledger marker copy ids (``TaskCreate`` + the two ``TaskUpdate`` anchors)."""
    slots = markers[task_id]
    return slots.create | slots.anchors


def _apply_step_grouping(
    copies: list[IngestEvent],
    traces: list[TraceObservations],
    tool_content: dict[str, ToolContent],
    *,
    spoke_run_id: str,
    trace_id: str,
) -> list[IngestEvent]:
    """Wrap each ledger step's local same-parent siblings in a ``step:`` node, in place (#113).

    For every step window, a ``step:<subject>`` node is inserted INSIDE the interaction(s) that
    hold the task's ``in_progress`` / ``completed`` markers, wrapping the contiguous run of
    same-parent siblings whose ``startTime`` falls in the window and absorbing the task's three
    ledger markers (``TaskCreate`` + the two ``TaskUpdate`` anchors). The wrap never crosses an
    interaction boundary, so a cross-turn task yields one partial wrap per anchor-holding
    interaction; a wrap with zero non-marker siblings is suppressed (no empty steps). Audit
    instants are excluded — their lagging timestamp must never window-place them (#104). The
    ``claude_code.interaction`` subtrees and their W3C-TRACEPARENT nesting are otherwise left
    untouched; root-level satellites are no longer grouped here (View B is the cycle lens).

    Args:
        copies: The re-parented source observation copies; wrapped children are mutated in place.
        traces: The source traces (for ledger windows + marker copy ids).
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).
        spoke_run_id: The spoke run identifier (for deterministic step ids).
        trace_id: The assembled trace id every step node references.

    Returns:
        The new step span events (empty when the spoke has no ledger windows).
    """
    windows = build_step_windows(traces, tool_content)
    if not windows:
        return []
    by_id = {event["body"]["id"]: event["body"] for event in copies}
    markers = _ledger_marker_ids(traces, tool_content)
    all_marker_ids = {c for m in markers.values() for c in (m.create | m.anchors)}
    children: dict[str, list[str]] = {}
    for body in by_id.values():
        children.setdefault(body.get("parentObservationId"), []).append(body["id"])
    windows_by_parent, parents_by_task = _anchor_parents(windows, markers, by_id)
    step_events: list[IngestEvent] = []
    for window in windows:
        for parent in sorted(parents_by_task.get(window.task_id, set())):
            members = _wrap_members(
                parent, window, children, by_id, windows_by_parent, all_marker_ids
            )
            if not members:
                continue  # suppress a wrap with zero non-marker siblings
            step_id = _step_id(spoke_run_id, window.task_id, parent)
            step_events.append(_step_event(window, step_id, parent, trace_id))
            absorbed = members + [
                c
                for c in _task_marker_ids(window.task_id, markers)
                if c in by_id and by_id[c]["parentObservationId"] == parent
            ]
            for cid in absorbed:
                by_id[cid]["parentObservationId"] = step_id
    return step_events
