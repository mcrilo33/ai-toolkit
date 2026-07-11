"""Source-observation accessors and predicates (#166).

The read layer over a raw Langfuse observation: the small helpers that pull a tool-call id, a
prompt id, a request id, or a duration out of the OTel-nested metadata, and the ``_is_*``
predicates that classify an observation (hook, tool-scoped audit event, fold sub-span, startup /
audit instant, interaction container). Every other family module builds on these. It depends only
on :class:`~telemetry.langfuse_rollup.Observation`, so it stays a foundation leaf.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NamedTuple

from telemetry.langfuse_rollup import Observation

# One Langfuse ingestion event (a ``*-create`` body); the assembled batches are lists of these.
IngestEvent = dict[str, Any]
# One source trace paired with all of its observations: ``(orig_trace_id, observations)``.
TraceObservations = tuple[str, list[Observation]]

# Langfuse ingestion requires a timestamp on every event; used for the trace/root and as a
# fallback when a source observation carries no ``startTime``. Fixed so reruns stay stable.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# The native per-turn container span; kept nested in View A, flattened to a childless leaf
# turn-marker on the cycle axis in View B (#114).
_INTERACTION_NAME = "claude_code.interaction"

# Node-name vocabulary of the ASSEMBLED tree. The predicates below classify a node by these
# names; the family modules that MINT such a node import the constant from here so the naming
# stays single-sourced.
_SUB_AGENT_PREFIX = "sub-agent:"  # the otelcol-renamed ``tool:Agent`` container (#161)
_GUARDS_NAME = "guards"  # per-tool guard group (#157)
_GUARDS_SESSION_NAME = "guards:session"  # the root's guard group (#157)
_BLOCKED_TOOL_NAME_PREFIX = "blocked-tool:"  # synthesized denied/never-run tool node (#157)
_HOOK_EXECUTION_PREFIX = "hook_execution_complete"  # a Pre/PostToolUse hook audit event (#157)
_GATE_OBSERVATION_NAME = "script:gate"  # the ``spoke-ready --gate`` PLAN-gate park span
_WAIT_PREFIX = "wait:"  # routes a node into the duration ``wait`` bucket (#162)
_PRE_STEP_NAME = "preStep"  # View B cycle-axis bookend nodes (#113)
_POST_STEP_NAME = "postStep"
_SKILL_ACTIVATED_NAME = "skill_activated"  # the span-less lifecycle event (#110 AC2)
_SKILL_NAME_KEY = "skill.name"
# The first-class skill invocation node (#234): a copied ``tool:Skill`` span relabeled to
# ``skill:<name>`` so a skill reads as a per-skill unit with its own cost rollup and success score.
_SKILL_SPAN_PREFIX = "skill:"
# MCP call grouping (#234): an MCP tool span is named ``tool:mcp__<server>__<tool>`` (the
# ``mcp__server__tool`` convention, ``__`` separating server from tool); its calls fold into one
# synthesized ``mcp:<server>`` group per server. Both prefixes are single-sourced here.
_MCP_TOOL_PREFIX = "tool:mcp__"
_MCP_GROUP_PREFIX = "mcp:"
# Metadata keys that may carry a tool-call id, in priority order.
_TOOL_USE_ID_KEYS = ("tool_use_id", "gen_ai.tool.call.id")
# The per-turn id Claude Code stamps on a ``claude_code.interaction`` and every event-layer
# satellite emitted inside that turn; the join key for re-homing an unmatched-tool satellite to
# its enclosing turn (#110, see :func:`_enclosing_turn`).
_PROMPT_ID_KEY = "prompt.id"
# Name prefixes of the audit observations scoped to a single tool call that carry its
# ``tool_use_id`` (``tool_decision:<decision>``, ``tool_result``, ``hook_execution_complete``).
# Like gate hooks, they join the tool sharing that id rather than the synthetic root.
_TOOL_AUDIT_EVENT_PREFIXES = ("tool_decision", "tool_result", "hook_execution_complete")

# The three native 1:1 sub-spans of a tool call that FOLD into their ``tool:`` node's metadata
# (#100 part 2) instead of nesting as child nodes: the execution span, the human-block span, and
# the #93 ``tool_decision:<d>`` audit event.
_FOLD_EXECUTION_NAME = "claude_code.tool.execution"
_FOLD_BLOCKED_NAME = "claude_code.tool.blocked_on_user"
_FOLD_DECISION_PREFIX = "tool_decision"

# Span-less session-startup audit instants (#104), demoted to the synthetic root's metadata
# rather than standing as sibling span nodes (their ``startTime`` is the lagging flush time).
_STARTUP_INSTANT_PREFIXES = ("mcp_server_connection", "plugin_loaded")

# Span-less audit/lifecycle instants that reach the step-grouping pass (#104); never placed by
# their lagging ``startTime``. ``api_error`` / ``api_refusal`` nest under their ``llm_request`` by
# ``request_id``; ``skill_activated`` under its ``tool:Skill``; the rest fall to the root.
_REQUEST_AUDIT_PREFIXES = ("api_error", "api_refusal")
_AUDIT_INSTANT_PREFIXES = (
    *_TOOL_AUDIT_EVENT_PREFIXES,
    *_REQUEST_AUDIT_PREFIXES,
    "skill_activated",
    "permission_mode_changed",
    "compaction",
)
# Metadata keys that may carry an LLM request id, in priority order: the audit event uses
# ``request_id``; the native ``llm_request`` span carries the same value as ``client_request_id``.
_REQUEST_ID_KEYS = ("request_id", "client_request_id")


class ToolContent(NamedTuple):
    """The transcript-sourced content of one tool call (either field may be absent)."""

    input: object | None  # the tool_use input args
    output: object | None  # the tool_result content


def _tool_use_id(observation: Observation) -> str | None:
    """Return the tool-call id from an observation's metadata, or None if absent.

    Langfuse stores OTel span attributes nested under ``metadata["attributes"]``, so each
    candidate key is read from there first and only then from the top level (a fallback for
    flatter shapes).
    """
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    for key in _TOOL_USE_ID_KEYS:
        value = attributes.get(key) or metadata.get(key)
        if value:
            return str(value)
    return None


def _prompt_id(observation: Observation) -> str | None:
    """Return the ``prompt.id`` (the per-turn id shared by a turn and its satellites), or None.

    Read from ``metadata["attributes"]`` first (where Langfuse nests OTel span attributes,
    e.g. on a ``claude_code.interaction``) and then from flat metadata (where the audit layer
    copies it as a cross-reference key, e.g. on a ``hook_execution_complete``).
    """
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    value = attributes.get(_PROMPT_ID_KEY) or metadata.get(_PROMPT_ID_KEY)
    return str(value) if value else None


def _is_hook(observation: Observation) -> bool:
    """Whether an observation is a hook emission.

    Detected by a ``*.sh`` name or a ``workflow.kind == "hook"`` span attribute (nested
    under ``metadata["attributes"]`` by Langfuse), with a top-level ``kind == "hook"`` kept
    as a fallback for flatter shapes.
    """
    name = observation.get("name") or ""
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    return (
        name.endswith(".sh")
        or attributes.get("workflow.kind") == "hook"
        or metadata.get("kind") == "hook"
    )


def _is_tool_audit_event(observation: Observation) -> bool:
    """Whether an observation is an audit event scoped to a single tool call.

    These (``tool_decision:<decision>``, ``tool_result``, and a Pre/PostToolUse
    ``hook_execution_complete``) are minted on the per-spoke audit trace and carry their
    ``tool_use_id`` in flat metadata; they are recognised by name prefix
    (:data:`_TOOL_AUDIT_EVENT_PREFIXES`). None of these prefixes collides with a visible
    ``tool:<Name>`` span or a bare tool name like ``Bash``.
    """
    name = observation.get("name") or ""
    return name.startswith(_TOOL_AUDIT_EVENT_PREFIXES)


def _joins_under_tool(observation: Observation) -> bool:
    """Whether an observation nests under the tool sharing its ``tool_use_id``.

    True for a gate hook or a tool-scoped audit event — the satellites of a tool call.
    Both are skipped as index owners (so the genuine tool span stays the re-parent target)
    and both join by ``tool_use_id`` in :func:`_resolve_parent`.
    """
    return _is_hook(observation) or _is_tool_audit_event(observation)


def _is_fold_subspan(observation: Observation) -> bool:
    """Whether an observation is one of the three 1:1 sub-spans that fold into their tool.

    The execution / blocked-on-user spans and the ``tool_decision:<d>`` audit event fold into
    the ``tool:`` node's metadata (:func:`_fold_attrs`); they are never re-parent targets, so
    they are also skipped as tool-index owners in :func:`_build_tool_index`.
    """
    name = observation.get("name") or ""
    return name in (_FOLD_EXECUTION_NAME, _FOLD_BLOCKED_NAME) or name.startswith(
        _FOLD_DECISION_PREFIX
    )


def _is_startup_instant(observation: Observation) -> bool:
    """Whether an observation is a session-startup audit instant demoted to root metadata (#104)."""
    return (observation.get("name") or "").startswith(_STARTUP_INSTANT_PREFIXES)


def _is_audit_instant(observation: Observation) -> bool:
    """Whether an observation is a span-less audit instant never placed by its lagging time (#104)."""
    return (observation.get("name") or "").startswith(_AUDIT_INSTANT_PREFIXES)


def _is_request_audit_event(observation: Observation) -> bool:
    """Whether an observation is an ``api_error`` / ``api_refusal`` joining its llm_request (#104)."""
    return (observation.get("name") or "").startswith(_REQUEST_AUDIT_PREFIXES)


def _is_interaction(observation: Observation) -> bool:
    """Whether an observation is a native per-turn ``claude_code.interaction`` container."""
    return (observation.get("name") or "") == _INTERACTION_NAME


def _is_cycle_step_marker(observation: Observation) -> bool:
    """Whether an observation is a solo-cycle step marker span (cycle-step-mark.sh, #178/#235).

    These carry ``workflow.kind == "step"`` (nested under ``metadata["attributes"]`` by Langfuse,
    or as flat ``kind`` in the audit/test shapes) and render with an OTLP label ``step:<phase>``.
    #235 reads them to build the View B cycle spine, then suppresses the raw node so a marker
    never surfaces as an orphan ``step:green`` sibling of the labelled ``step:GREEN`` step.
    """
    return _attr(observation, "workflow.kind", "kind") == "step"


def _cycle_marker_phase(observation: Observation) -> str:
    """Return a cycle-step marker's lowercase phase (``red``/``green``/``review``/``push``).

    From the ``workflow.phase`` attribute, falling back to the ``step:<phase>`` OTLP name label;
    ``""`` when neither is present.
    """
    phase = _attr(observation, "workflow.phase")
    if phase:
        return str(phase).lower()
    name = observation.get("name") or ""
    prefix = "step:"
    return name[len(prefix) :].lower() if name.startswith(prefix) else ""


def _request_id(observation: Observation) -> str | None:
    """Return the LLM request id from an observation's attributes/flat metadata, or None."""
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    for key in _REQUEST_ID_KEYS:
        value = attributes.get(key) or metadata.get(key)
        if value:
            return str(value)
    return None


def _elapsed_ms(start: str, end: str) -> int | None:
    """Return ``end - start`` in ms from two ISO timestamps, or None when uncomputable.

    Catches both a malformed timestamp (``ValueError``) and a mixed naive/aware pair
    (``TypeError`` — subtracting an offset-aware from an offset-naive datetime), so one odd
    pair never aborts the whole assembly; the value is simply omitted.
    """
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (ValueError, TypeError):
        return None
    return int(delta.total_seconds() * 1000)


def _duration_ms(observation: Observation) -> int | None:
    """Return a span's wall-clock duration in ms from its ISO start/end, or None."""
    start, end = observation.get("startTime"), observation.get("endTime")
    if not start or not end:
        return None
    return _elapsed_ms(start, end)


def _attr(observation: Observation, *keys: str) -> object | None:
    """Read the first present key from the span attributes, then flat metadata."""
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    for key in keys:
        value = attributes.get(key)
        if value is None:
            value = metadata.get(key)
        if value is not None:
            return value
    return None


def _is_tool_span(observation: Observation) -> bool:
    """Whether an observation is a visible ``tool:<Name>`` span (e.g. ``tool:TaskCreate``).

    Only these spans carry the tool call the user sees; the ``claude_code.tool.execution``,
    ``*.blocked_on_user``, and ``*.sh`` hook siblings that share a ``tool_use_id`` are not
    tool spans and are never filled with transcript content.
    """
    return (observation.get("name") or "").startswith("tool:")


def _is_graftable_span(observation: Observation) -> bool:
    """Whether transcript ``input``/``output`` may be grafted onto this span by ``tool_use_id``.

    Both a visible ``tool:`` span and a ``sub-agent:<type>`` container (the otelcol-renamed
    ``tool:Agent``) carry the invoking tool's call id, so each joins its transcript entry the
    same way. The ``claude_code.tool.execution`` / ``*.blocked_on_user`` siblings do not.
    """
    name = observation.get("name") or ""
    return name.startswith("tool:") or name.startswith(_SUB_AGENT_PREFIX)


def _is_guards_group(body: Observation | None) -> bool:
    """Whether a node is a synthesized ``guards`` / ``guards:session`` group (#157)."""
    return bool(body) and (body.get("name") in (_GUARDS_NAME, _GUARDS_SESSION_NAME))


def _is_blocked_tool(body: Observation | None) -> bool:
    """Whether a node is a synthesized ``blocked-tool:*`` node (#157)."""
    return bool(body) and (body.get("name") or "").startswith(_BLOCKED_TOOL_NAME_PREFIX)


def _is_hook_event(body: Observation | None) -> bool:
    """Whether a node is a ``hook_execution_complete`` audit event (#157)."""
    return bool(body) and (body.get("name") or "").startswith(_HOOK_EXECUTION_PREFIX)


def _is_skill_activated(observation: Observation) -> bool:
    """Whether an observation is a span-less ``skill_activated`` lifecycle event (#110 AC2)."""
    return (observation.get("name") or "") == _SKILL_ACTIVATED_NAME


def _is_skill_span(observation: Observation) -> bool:
    """Whether a node is a relabeled first-class ``skill:<name>`` invocation span (#234).

    Only the assembled copy carries this name (the source span is a generic ``tool:Skill``); the
    duration rollup books its exclusive time to the ``skill`` bucket and the success scorer keys off
    it.
    """
    return (observation.get("name") or "").startswith(_SKILL_SPAN_PREFIX)


def _is_mcp_tool_span(observation: Observation) -> bool:
    """Whether a node is an MCP tool call span (``tool:mcp__<server>__<tool>``, #234)."""
    return (observation.get("name") or "").startswith(_MCP_TOOL_PREFIX)


def _is_mcp_group(observation: Observation) -> bool:
    """Whether a node is a synthesized ``mcp:<server>`` per-server group (#234)."""
    return (observation.get("name") or "").startswith(_MCP_GROUP_PREFIX)


def _mcp_server(name: str) -> str | None:
    """Return the server segment of an MCP tool span name, or None when it is not one (#234).

    ``tool:mcp__<server>__<tool>`` -> ``<server>``; the ``__`` after the server delimits the tool
    (a tool name may itself contain ``__``, so only the first split matters). A server segment with
    single underscores (``my_server``) is preserved intact.
    """
    if not name.startswith(_MCP_TOOL_PREFIX):
        return None
    server = name[len(_MCP_TOOL_PREFIX) :].split("__", 1)[0]
    return server or None


def _skill_name(observation: Observation) -> str | None:
    """Return the ``skill.name`` carried by a ``skill_activated`` event, or None."""
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    value = attributes.get(_SKILL_NAME_KEY) or metadata.get(_SKILL_NAME_KEY)
    return str(value) if value else None


def _is_gate_observation(observation: Observation) -> bool:
    """Whether an observation is the ``spoke-ready --gate`` (PLAN-gate park) span.

    Matched by the OTel span label ``script:gate`` OR, robustly, by the workflow attributes
    (``workflow.kind == script`` and ``workflow.phase == gate``) so a label-format change does
    not silently drop the gate-park score.
    """
    if (observation.get("name") or "") == _GATE_OBSERVATION_NAME:
        return True
    attributes = (observation.get("metadata") or {}).get("attributes") or {}
    return (
        attributes.get("workflow.kind") == "script" and attributes.get("workflow.phase") == "gate"
    )


def _is_script_node(observation: Observation) -> bool:
    """Whether a node is a control-script run node — a ``script:`` label or ``workflow.kind==script``.

    The phased script spans keep a ``script:<phase>`` name; a phase-less one keeps its raw name, so
    the ``workflow.kind`` attribute is the reliable signal. Single-sourced here so the duration
    rollup's ``script`` bucket (:func:`~telemetry.spoke_tree.rollups._duration_class`) and the #233
    ``script_success`` scores classify scripts identically. NOTE: the PLAN-gate park span is ALSO a
    ``script:gate`` node, so a caller that means "real control script" must exclude
    :func:`_is_gate_observation` first (as ``_duration_class`` does — it buckets the gate as ``wait``).
    """
    name = observation.get("name") or ""
    return name.startswith("script:") or _attr(observation, "workflow.kind") == "script"


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp to a datetime, or None when malformed."""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO timestamp to an aware datetime (naive assumed UTC), or None."""
    parsed = _parse_ts(value) if value else None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _obs_envelope(observations: list[Observation]) -> tuple[str | None, str | None]:
    """Return the (min start, max end) ISO bounds over ``observations``, chronologically."""
    starts = [o["startTime"] for o in observations if o.get("startTime")]
    ends = [o["endTime"] for o in observations if o.get("endTime")]
    start = min(starts, key=lambda s: _parse_utc(s) or datetime.min) if starts else None
    end = max(ends, key=lambda s: _parse_utc(s) or datetime.min) if ends else None
    return start, end


def _earliest_start(traces: list[TraceObservations]) -> str:
    """Return the earliest ISO ``startTime`` across all observations, or the fixed base."""
    starts = [
        observation["startTime"]
        for _, observations in traces
        for observation in observations
        if observation.get("startTime")
    ]
    return min(starts) if starts else _INGEST_TIMESTAMP


def _latest_time(traces: list[TraceObservations]) -> str:
    """Return the latest ISO ``endTime``/``startTime`` across all observations, or the base."""
    times = [
        observation.get("endTime") or observation.get("startTime") or ""
        for _orig_trace_id, observations in traces
        for observation in observations
        if observation.get("endTime") or observation.get("startTime")
    ]
    return max(times) if times else _INGEST_TIMESTAMP


def _llm_requests_in_order(traces: list[TraceObservations]) -> list[tuple[str, Observation]]:
    """Return ``(orig_trace_id, observation)`` for each LLM call, oldest first by ``startTime``.

    An LLM call is any observation carrying ``cache_read_input_tokens`` or
    ``cache_creation_input_tokens`` usage — the same set the request-body dumps correspond to,
    so the two align positionally (the basis of the count gate in
    :func:`apply_llm_decomposition`).
    """
    calls: list[tuple[str, str, Observation]] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            if (
                usage.get("cache_read_input_tokens") is None
                and usage.get("cache_creation_input_tokens") is None
            ):
                continue
            calls.append((observation.get("startTime") or "", orig_trace_id, observation))
    calls.sort(key=lambda call: call[0])
    return [(orig_trace_id, observation) for _start, orig_trace_id, observation in calls]
