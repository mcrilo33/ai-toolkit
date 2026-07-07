"""Source-observation accessors and predicates (#166).

The read layer over a raw Langfuse observation: the small helpers that pull a tool-call id, a
prompt id, a request id, or a duration out of the OTel-nested metadata, and the ``_is_*``
predicates that classify an observation (hook, tool-scoped audit event, fold sub-span, startup /
audit instant, interaction container). Every other family module builds on these. It depends only
on :class:`~telemetry.langfuse_rollup.Observation`, so it stays a foundation leaf.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from telemetry.langfuse_rollup import Observation

# The native per-turn container span; kept nested in View A, flattened to a childless leaf
# turn-marker on the cycle axis in View B (#114).
_INTERACTION_NAME = "claude_code.interaction"
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
