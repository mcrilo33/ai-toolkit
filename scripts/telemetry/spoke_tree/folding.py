"""Fold sub-spans, collapse guard groups, and stamp failure levels (#100, #157).

The post-copy passes that reshape a tool's satellites: the three 1:1 sub-spans fold into the
tool's metadata (:func:`_fold_tool_subspans`), the ``.sh`` guard spans collapse under a synthetic
``guards`` group (:func:`_apply_guard_groups`), ``hook_execution_complete`` events get a derived
endTime (:func:`_stamp_hook_endtimes`), and WARNING/ERROR levels are stamped from the folded
failure data (:func:`_apply_levels`). Depends only on the foundation modules.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from telemetry.langfuse_rollup import Observation
from telemetry.spoke_tree.ids import _copy_id, _guards_id, _mcp_group_id
from telemetry.spoke_tree.observations import (
    _FOLD_BLOCKED_NAME,
    _FOLD_DECISION_PREFIX,
    _FOLD_EXECUTION_NAME,
    _GUARDS_NAME,
    _GUARDS_SESSION_NAME,
    _INGEST_TIMESTAMP,
    _MCP_GROUP_PREFIX,
    IngestEvent,
    TraceObservations,
    _attr,
    _duration_ms,
    _is_blocked_tool,
    _is_fold_subspan,
    _is_hook,
    _is_hook_event,
    _is_mcp_tool_span,
    _is_skill_span,
    _is_tool_span,
    _mcp_server,
    _obs_envelope,
    _parse_utc,
    _tool_use_id,
)

# A guard span with this shape is a droppable no-op (#157).
_GUARD_NOOP_MAX_MS = 1000
# A guard decision / status that flags its whole group WARNING.
_GUARD_WARN_DECISIONS = ("deny", "ask", "block")
_STATUS_SUCCESS = "success"
_NUM_BLOCKING_KEY = "num_blocking"
# Failure levels (#157): ERROR > WARNING > DEFAULT.
_LEVEL_ERROR = "ERROR"
_LEVEL_WARNING = "WARNING"
# hook_execution_complete endTime stamping (#157): derive width from total_duration_ms and flag it.
_TOTAL_DURATION_KEY = "total_duration_ms"
_TIME_SOURCE_KEY = "time_source"
_TIME_SOURCE_LAGGING = "lagging"


def _fold_attrs(observation: Observation) -> dict[str, Any]:
    """Return the metadata a fold sub-span contributes to its tool node (see :func:`_is_fold_subspan`).

    The ``*_ms`` values derive from the span's own duration and are robust; the ``success`` /
    ``error`` / ``decision`` / ``decision_source`` reads probe several candidate attribute keys
    (bare and ``claude_code.tool.*``-namespaced) since the exact native OTel names vary. When a
    tool has both a blocked-on-user and a tool_decision sub-span they both write ``decision``;
    last-writer-wins, and the two are expected to agree.

    UPGRADE: pin the success/error/decision/source attribute keys once confirmed against a real
    Claude Code OTel trace — the duration-derived ``*_ms`` already fold reliably regardless.
    """
    name = observation.get("name") or ""
    out: dict[str, Any] = {}
    if name == _FOLD_EXECUTION_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["execution_ms"] = ms
        success = _attr(observation, "success", "claude_code.tool.success", "gen_ai.tool.success")
        if success is not None:
            out["success"] = bool(success)
        error = _attr(observation, "error", "error.message", "claude_code.tool.error")
        if error:
            out["error"] = error
    elif name == _FOLD_BLOCKED_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["blocked_on_user_ms"] = ms
        out.update(_decision_attrs(observation))
    elif name.startswith(_FOLD_DECISION_PREFIX):
        suffix = name.split(":", 1)[1] if ":" in name else None
        out.update(_decision_attrs(observation, default_decision=suffix))
    return out


def _decision_attrs(
    observation: Observation, *, default_decision: str | None = None
) -> dict[str, Any]:
    """Return the ``decision`` / ``decision_source`` a blocked/decision sub-span contributes."""
    out: dict[str, Any] = {}
    decision = _attr(observation, "decision", "claude_code.tool.decision") or default_decision
    if decision:
        out["decision"] = decision
    source = _attr(observation, "decision_source", "source", "claude_code.tool.decision_source")
    if source:
        out["decision_source"] = source
    return out


def _fold_owner(
    observation: Observation,
    orig_trace_id: str,
    tool_index: dict[str, str],
    tool_span_ids: set[str],
) -> str | None:
    """Return the copy id of the tool a fold sub-span belongs to, or None.

    The audit ``tool_decision`` joins by ``tool_use_id``; the native execution / blocked spans
    are children of their tool, so they also fall back to the copy of their
    ``parentObservationId`` — but ONLY when that parent is itself a ``tool:`` span, so a sub-span
    whose parent is an interaction (or another sub-span, e.g. a resume nested under a
    ``tool.execution``) is never folded onto a non-tool node.
    """
    tuid = _tool_use_id(observation)
    if tuid and tuid in tool_index:
        return tool_index[tuid]
    parent = observation.get("parentObservationId")
    if parent:
        parent_copy = _copy_id(orig_trace_id, parent)
        if parent_copy in tool_span_ids:
            return parent_copy
    return None


def _fold_tool_subspans(
    copies: list[IngestEvent], traces: list[TraceObservations], tool_index: dict[str, str]
) -> list[IngestEvent]:
    """Fold the three 1:1 tool sub-spans into their tool's metadata, dropping their nodes (#100).

    Each execution / blocked-on-user / ``tool_decision`` sub-span's fields are merged onto the
    owning ``tool:`` node's metadata and the sub-span copy is removed. A sub-span whose tool is
    absent (an unmatched audit event) is left as-is — it keeps its node and collapses to the root.

    A folded sub-span can itself have children — a resume ``claude_code.interaction`` nests under
    the push command's ``tool.execution`` via TRACEPARENT — so any node parented on a folded
    sub-span is re-homed onto the fold owner (the tool) before the sub-span is dropped, so its
    subtree (and its tokens in the container rollups) survives rather than dangling on a deleted id.

    Args:
        copies: The source observation copies; owner tool bodies and orphaned children's parents
            are mutated in place.
        traces: The source traces (to walk every sub-span and resolve its owner).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.

    Returns:
        The copies with the folded sub-spans removed.
    """
    by_id = {event["body"]["id"]: event for event in copies}
    tool_span_ids = {
        _copy_id(orig_trace_id, observation["id"])
        for orig_trace_id, observations in traces
        for observation in observations
        if _is_tool_span(observation)
    }
    reparent: dict[str, str] = {}  # folded sub-span copy id -> its fold owner (the tool)
    for orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_fold_subspan(observation):
                continue
            owner = _fold_owner(observation, orig_trace_id, tool_index, tool_span_ids)
            if owner is None or owner not in by_id:
                continue  # no tool to fold into — leave the sub-span as a node
            attrs = _fold_attrs(observation)
            if attrs:
                by_id[owner]["body"].setdefault("metadata", {}).update(attrs)
            reparent[_copy_id(orig_trace_id, observation["id"])] = owner
    for event in copies:
        if event["body"]["id"] in reparent:
            continue  # this node is itself being dropped
        parent = event["body"].get("parentObservationId")
        if parent in reparent:
            while parent in reparent:  # resolve through any chain of folded ancestors
                parent = reparent[parent]
            event["body"]["parentObservationId"] = parent
    return [event for event in copies if event["body"]["id"] not in reparent]


def _guard_noop(body: Observation) -> bool:
    """Whether a guard span is a droppable no-op: ``decision=allow`` ∧ ``status=success`` ∧ <1s."""
    ms = _duration_ms(body)
    return (
        _attr(body, "decision") == "allow"
        and _attr(body, "status") == "success"
        and ms is not None
        and ms < _GUARD_NOOP_MAX_MS
    )


def _guard_group_metadata(members: list[IngestEvent]) -> dict[str, Any]:
    """Return a guards group's rollup over ALL its raw guard spans (before any are dropped).

    ``by_hook`` keys are sorted and ``decisions`` de-duplicated + sorted so the group body is
    byte-stable across reruns; ``count`` / ``total_ms`` / per-hook ``ms`` sum every member,
    including the no-op spans dropped from the tree (#157 AC1).
    """
    by_hook: dict[str, dict[str, int]] = {}
    total_ms = 0
    decisions: set[str] = set()
    for member in members:
        body = member["body"]
        name = body.get("name") or ""
        ms = _duration_ms(body) or 0
        entry = by_hook.setdefault(name, {"count": 0, "ms": 0})
        entry["count"] += 1
        entry["ms"] += ms
        total_ms += ms
        decision = _attr(body, "decision")
        if decision is not None:
            decisions.add(str(decision))
    return {
        "count": len(members),
        "total_ms": total_ms,
        "by_hook": {name: by_hook[name] for name in sorted(by_hook)},
        "decisions": sorted(decisions),
    }


def _guard_envelope(members: list[IngestEvent]) -> tuple[str | None, str | None]:
    """Return the (min start, max end) ISO bounds over the guard members, chronologically."""
    return _obs_envelope([member["body"] for member in members])


def _guard_group_event(
    parent_id: str, members: list[IngestEvent], *, trace_id: str, root_id: str
) -> IngestEvent:
    """Build the synthetic ``guards`` / ``guards:session`` group node for one parent's guards."""
    group_id = _guards_id(parent_id)
    name = _GUARDS_SESSION_NAME if parent_id == root_id else _GUARDS_NAME
    start, end = _guard_envelope(members)
    body: dict[str, Any] = {
        "id": group_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": name,
        "startTime": start or _INGEST_TIMESTAMP,
        "endTime": end,
        "metadata": _guard_group_metadata(members),
    }
    if any(_guard_warns(member["body"]) for member in members):
        body["level"] = _LEVEL_WARNING  # a non-allow / failed guard flags its whole group (#157)
    return {
        "id": group_id,
        "type": "span-create",
        "timestamp": start or _INGEST_TIMESTAMP,
        "body": body,
    }


def _apply_guard_groups(
    copies: list[IngestEvent],
    *,
    trace_id: str,
    root_id: str,
    tool_owner_ids: set[str],
    keep_noop_guards: bool,
) -> list[IngestEvent]:
    """Collapse each tool's (and the session's) ``.sh`` guard copies under a ``guards`` group (#157).

    A guard copy (:func:`_is_hook`) whose resolved parent is a tool owner or the synthetic root is
    re-homed under a synthesized ``guards`` group (``guards:session`` at the root) parented where
    the guard sat. No-op guards (:func:`_guard_noop`) are dropped unless ``keep_noop_guards``; the
    survivors keep their nodes under the group. Guards resolved under anything else (e.g. an
    interaction) and non-guard satellites are left untouched. The group's ``by_hook`` rollup counts
    every raw guard including the dropped ones (:func:`_guard_group_metadata`).

    Args:
        copies: The assembled copies; guard copies are re-parented or dropped in place.
        trace_id: The assembled trace id every group node references.
        root_id: The synthetic root id (host of the ``guards:session`` group).
        tool_owner_ids: Copy ids that own a tool call (real tool spans + synthesized blocked-tools).
        keep_noop_guards: When True, no-op guards are retained under their group instead of dropped.

    Returns:
        The copies with grouped guards re-parented, no-ops dropped, and group nodes appended.
    """
    grouped: dict[str, list[IngestEvent]] = {}
    for event in copies:
        body = event["body"]
        if not _is_hook(body):
            continue
        parent = body.get("parentObservationId")
        if parent in tool_owner_ids or parent == root_id:
            grouped.setdefault(parent, []).append(event)
    if not grouped:
        return copies
    dropped: set[str] = set()
    group_events: list[IngestEvent] = []
    for parent_id, members in grouped.items():
        group = _guard_group_event(parent_id, members, trace_id=trace_id, root_id=root_id)
        group_events.append(group)
        for member in members:
            if not keep_noop_guards and _guard_noop(member["body"]):
                dropped.add(member["body"]["id"])
            else:
                member["body"]["parentObservationId"] = group["body"]["id"]
    kept = [event for event in copies if event["body"]["id"] not in dropped]
    return kept + group_events


def _mcp_member_failed(body: Observation) -> bool:
    """Whether an MCP tool member's folded result is error-shaped (#234).

    The #100 fold stamps ``success`` / ``error`` from the tool's ``claude_code.tool.execution``
    sub-span onto the tool node, so an error-shaped MCP result surfaces as ``success is False`` or a
    non-empty ``error`` — the same signal :func:`_level_for` reads to flag a failed tool ERROR.
    """
    metadata = body.get("metadata") or {}
    return metadata.get("success") is False or bool(metadata.get("error"))


def _mcp_group_metadata(server: str, members: list[IngestEvent]) -> dict[str, Any]:
    """Return an ``mcp:<server>`` group's rollup: the server, its call count, and its failures."""
    failures = sum(1 for member in members if _mcp_member_failed(member["body"]))
    return {"server": server, "calls": len(members), "failures": failures}


def _mcp_group_event(
    parent_id: str, server: str, members: list[IngestEvent], *, trace_id: str
) -> IngestEvent:
    """Build the synthetic ``mcp:<server>`` group node for one parent's MCP calls (#234)."""
    group_id = _mcp_group_id(parent_id, server)
    start, end = _obs_envelope([member["body"] for member in members])
    metadata = _mcp_group_metadata(server, members)
    body: dict[str, Any] = {
        "id": group_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": _MCP_GROUP_PREFIX + server,
        "startTime": start or _INGEST_TIMESTAMP,
        "endTime": end,
        "metadata": metadata,
    }
    if metadata["failures"]:
        body["level"] = _LEVEL_WARNING  # an error-shaped call flags its whole server group (#234)
    return {
        "id": group_id,
        "type": "span-create",
        "timestamp": start or _INGEST_TIMESTAMP,
        "body": body,
    }


def _apply_mcp_groups(copies: list[IngestEvent], *, trace_id: str) -> list[IngestEvent]:
    """Fold each turn's ``tool:mcp__<server>__<tool>`` copies under one ``mcp:<server>`` group (#234).

    Every MCP tool span (:func:`_is_mcp_tool_span`) is re-homed under a synthesized ``mcp:<server>``
    group parented where the calls sat (their common ``parentObservationId``), so a server's calls
    read as one unit with a duration rollup (the group is a container, stamped by
    :func:`~telemetry.spoke_tree.rollups._apply_container_rollups`) and a success signal
    (:func:`_mcp_group_metadata`; an error-shaped member flags the group WARNING). Grouping is keyed
    by ``(parent, server)`` so calls to one server across different turns stay in their own turn's
    group. Runs AFTER guard grouping so an MCP tool's guards already nest under the tool and ride it
    under the group. Non-MCP copies are untouched; when the spoke made no MCP call, ``copies`` is
    returned unchanged.

    Args:
        copies: The assembled copies; MCP tool copies are re-parented in place.
        trace_id: The assembled trace id every group node references.

    Returns:
        The copies with grouped MCP tools re-parented and the group nodes appended.
    """
    grouped: dict[tuple[str, str], list[IngestEvent]] = {}
    for event in copies:
        body = event["body"]
        if not _is_mcp_tool_span(body):
            continue
        server = _mcp_server(body.get("name") or "")
        if not server:
            continue
        parent_id = body.get("parentObservationId") or ""
        grouped.setdefault((parent_id, server), []).append(event)
    if not grouped:
        return copies
    group_events: list[IngestEvent] = []
    for (parent_id, server), members in grouped.items():
        group = _mcp_group_event(parent_id, server, members, trace_id=trace_id)
        group_events.append(group)
        for member in members:
            member["body"]["parentObservationId"] = group["body"]["id"]
    return copies + group_events


def _stamp_hook_endtimes(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Give each ``hook_execution_complete`` copy a derived endTime from ``total_duration_ms`` (#157).

    A hook event carries ``total_duration_ms`` but no ``endTime``; set ``endTime = startTime +
    total_duration_ms`` and ``metadata.time_source = "lagging"`` so the timeline can render its
    width while flagging it as derived. Events missing a start or ``total_duration_ms`` are left
    untouched. Mutates the bodies in place and returns ``copies``.
    """
    for event in copies:
        body = event["body"]
        if not _is_hook_event(body) or body.get("endTime"):
            continue
        start = body.get("startTime")
        total = _attr(body, _TOTAL_DURATION_KEY)
        # UPGRADE: accept only a native numeric counter; a numeric-string total_duration_ms would
        # be skipped (left zero-width). Coerce here if a future emission path ever stringifies it.
        if not start or not isinstance(total, (int, float)):
            continue
        parsed = _parse_utc(start)
        if parsed is None:
            continue
        end = parsed + timedelta(milliseconds=total)
        # metadata is aliased from the source observation (copied via _COPIED_FIELDS), so this
        # writes time_source back onto the source dict too — intentional and inert (time_source is
        # never read; the write is idempotent), matching the module's other in-place patterns.
        body["endTime"] = end.isoformat().replace("+00:00", "Z")
        body.setdefault("metadata", {})[_TIME_SOURCE_KEY] = _TIME_SOURCE_LAGGING
    return copies


def _hook_event_exclude(events: list[IngestEvent]) -> set[str]:
    """Return the ids of ``hook_execution_complete`` nodes to drop from duration attribution (#157).

    Their stamped width (:func:`_stamp_hook_endtimes`) duplicates the ``.sh`` guard durations
    already booked in the ``hook`` bucket, so they must contribute nothing to ``rollup.duration``.
    Name-based, so it resolves the same ids in either view's id namespace.
    """
    return {
        event["body"]["id"]
        for event in events
        if event["type"] != "trace-create" and _is_hook_event(event["body"])
    }


def _guard_warns(body: Observation) -> bool:
    """Whether a guard span is failure-worthy: a deny/ask/block decision or a non-success status."""
    decision = _attr(body, "decision")
    status = _attr(body, "status")
    return decision in _GUARD_WARN_DECISIONS or (status is not None and status != _STATUS_SUCCESS)


def _level_for(body: Observation) -> str | None:
    """Return the failure level (:data:`_LEVEL_ERROR` / :data:`_LEVEL_WARNING`) for a node, or None.

    ERROR for a tool OR a relabeled ``skill:<name>`` span (#234) whose folded metadata shows
    ``success is False`` or an ``error`` — the #100 fold keys on ``tool_use_id`` and stamps the same
    ``success``/``error`` onto a skill copy, so a failed skill must earn ERROR too (an MCP tool keeps
    its ``tool:`` name, so it is already covered). WARNING for a failure-worthy guard span
    (:func:`_guard_warns`), a synthesized blocked-tool node, or a ``hook_execution_complete`` with
    ``num_blocking > 0``. Each node matches at most one rule, so the ERROR > WARNING precedence needs
    no explicit tie-break. The guards GROUP's level is set at build time
    (:func:`_apply_guard_groups`) from its raw members, not here.
    """
    if _is_tool_span(body) or _is_skill_span(body):
        metadata = body.get("metadata") or {}
        if metadata.get("success") is False or metadata.get("error"):
            return _LEVEL_ERROR
        return None
    if _is_blocked_tool(body):
        return _LEVEL_WARNING
    if _is_hook(body):
        return _LEVEL_WARNING if _guard_warns(body) else None
    if _is_hook_event(body):
        num = _attr(body, _NUM_BLOCKING_KEY)
        return _LEVEL_WARNING if isinstance(num, (int, float)) and num > 0 else None
    return None


def _apply_levels(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Stamp WARNING/ERROR failure levels onto the assembled nodes in place (#157, :func:`_level_for`)."""
    for event in copies:
        level = _level_for(event["body"])
        if level:
            event["body"]["level"] = level
    return copies


def _guards_total_ms(body: Observation) -> int:
    """Return a guards group's summed raw guard duration from its metadata (0 if malformed)."""
    total = (body.get("metadata") or {}).get("total_ms")
    return total if isinstance(total, int) else 0
