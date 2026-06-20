#!/usr/bin/env python3
"""Map Claude Code's OTel audit/lifecycle log events onto a per-spoke Langfuse trace.

Claude Code emits an audit/lifecycle layer on the OTel *logs* signal — ``tool_decision``
(incl. rejections), ``permission_mode_changed``, ``mcp_server_connection``,
``plugin_loaded``, ``skill_activated``, ``hook_execution_complete``, ``compaction``,
``api_error``, ``api_refusal`` — that the message bridge previously dropped (it consumed
only ``api_request_body``/``api_response_body``). None of these events has a pre-existing
trace span, so they cannot be PATCHed onto one the way the message bodies are; they are
CREATED as Langfuse ``event-create`` observations instead.

Each event attaches to a per-spoke *synthetic audit trace* keyed by the spoke run id
(``sessionId == spoke_run_id``), so all of a spoke's audit events group under its Langfuse
session — the same way :mod:`telemetry.langfuse_spoke_tree` mints a deterministic trace.
The join needs no buffering: unlike the message-body join (which waits for the matching
span), the audit trace is minted here, so every event is emittable on arrival. Because the
events become real session observations, the post-run :mod:`telemetry.langfuse_spoke_tree`
pass folds them into the single assembled spoke tree with no change to that script.

The join keys are ``spoke_run_id`` (the resource attribute the otelcol re-homes sessions
onto, fallback ``session.id``) to select the trace, with ``session.id`` and ``prompt.id``
carried into each observation's metadata for cross-reference back to the per-turn traces.

This module is pure and import-safe: it takes an already-merged attribute dict (the bridge
flattens the OTLP resource + record envelopes) and returns an ingestion event, so it is
unit-testable with no network and no OTLP knowledge. The bridge owns the HTTP I/O.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Langfuse ingestion requires a timestamp on every event; used when an event carries no
# ``event.timestamp`` (it normally does, ISO 8601). Fixed so reruns stay stable.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Deterministic id prefix + trace-name prefix for the per-spoke audit trace.
_TRACE_PREFIX = "audit-"
_TRACE_NAME_PREFIX = "spoke-audit:"

# Identifier attributes copied into every audit observation's metadata for cross-reference
# back to the per-turn native traces (the spoke trace itself is keyed by spoke_run_id).
_CROSS_REF_KEYS = ("session.id", "prompt.id")


class AuditSpec:
    """How to render one ``event.name`` as a Langfuse ``event-create`` observation."""

    __slots__ = ("discriminator", "level", "level_key", "level_map", "metadata")

    def __init__(
        self,
        *,
        metadata: tuple[str, ...],
        discriminator: str | None = None,
        level: str = "DEFAULT",
        level_key: str | None = None,
        level_map: dict[str, str] | None = None,
    ) -> None:
        self.discriminator = discriminator  # attr whose value is appended to the name
        self.metadata = metadata  # attr keys copied into observation metadata (if present)
        self.level = level  # base Langfuse observation level
        self.level_key = level_key  # attr whose value may raise the level
        self.level_map = level_map or {}  # value -> level override


# The audit/lifecycle events to ingest, keyed by their bare ``event.name`` (the value the
# bridge already matches for ``api_request`` & co.; Claude Code's full metric name is
# ``claude_code.<event.name>``). Rejections, MCP failures, and API errors are raised to
# WARNING/ERROR so they surface in Langfuse's level filters.
AUDIT_SPECS: dict[str, AuditSpec] = {
    "tool_decision": AuditSpec(
        discriminator="decision",
        metadata=("tool_name", "tool_use_id", "decision", "source"),
        level_key="decision",
        level_map={"reject": "WARNING"},
    ),
    "permission_mode_changed": AuditSpec(
        discriminator="to_mode",
        metadata=("from_mode", "to_mode", "trigger"),
    ),
    "mcp_server_connection": AuditSpec(
        discriminator="status",
        metadata=(
            "status",
            "transport_type",
            "server_scope",
            "duration_ms",
            "error_code",
            "server_name",
            "error",
            "plugin.name",
        ),
        level_key="status",
        level_map={"failed": "ERROR"},
    ),
    "plugin_loaded": AuditSpec(
        metadata=(
            "plugin.name",
            "marketplace.name",
            "plugin.version",
            "plugin.scope",
            "enabled_via",
        ),
    ),
    "skill_activated": AuditSpec(
        metadata=("skill.name", "invocation_trigger", "skill.source", "skill.kind", "plugin.name"),
    ),
    "hook_execution_complete": AuditSpec(
        discriminator="hook_event",
        metadata=(
            "hook_event",
            "hook_name",
            "num_hooks",
            "num_success",
            "num_blocking",
            "num_cancelled",
            "total_duration_ms",
            "hook_source",
        ),
    ),
    "compaction": AuditSpec(
        metadata=(
            "trigger",
            "success",
            "duration_ms",
            "pre_tokens",
            "post_tokens",
            "error",
            "precompute_reuse",
        ),
    ),
    "api_error": AuditSpec(
        metadata=("error", "status_code", "request_id", "model", "attempt", "query_source"),
        level="ERROR",
    ),
    "api_refusal": AuditSpec(
        metadata=("request_id", "model", "category", "has_explanation", "query_source"),
        level="WARNING",
    ),
}


def audit_trace_id(trace_key: str) -> str:
    """Return the deterministic Langfuse trace id for a spoke's audit trace.

    Args:
        trace_key: The spoke run id (or ``session.id`` fallback) the trace groups under.

    Returns:
        A stable ``audit-<sha1[:16]>`` id, identical across reruns so re-ingest overwrites.
    """
    return _TRACE_PREFIX + hashlib.sha1(trace_key.encode()).hexdigest()[:16]


def trace_create(trace_key: str, timestamp: str) -> dict[str, Any]:
    """Build the once-per-spoke ``trace-create`` event the audit observations hang off.

    Args:
        trace_key: The spoke run id (or ``session.id`` fallback) for the trace.
        timestamp: ISO 8601 timestamp stamped on the trace.

    Returns:
        A Langfuse ingestion ``trace-create`` event homed on the spoke session.
    """
    trace_id = audit_trace_id(trace_key)
    return {
        "id": trace_id,
        "type": "trace-create",
        "timestamp": timestamp,
        "body": {
            "id": trace_id,
            "name": _TRACE_NAME_PREFIX + trace_key,
            "sessionId": trace_key,
            "timestamp": timestamp,
        },
    }


def _resolve_level(spec: AuditSpec, attrs: dict[str, str]) -> str:
    """Return the observation level, raised above the base when the event warrants it."""
    if spec.level_key is not None:
        value = attrs.get(spec.level_key)
        if value is not None and value in spec.level_map:
            return spec.level_map[value]
    return spec.level


def _build_metadata(spec: AuditSpec, attrs: dict[str, str]) -> dict[str, str]:
    """Copy the spec's present metadata keys plus the cross-reference ids into metadata."""
    keys = (*spec.metadata, *_CROSS_REF_KEYS)
    return {key: attrs[key] for key in keys if attrs.get(key) is not None}


def _observation_id(attrs: dict[str, str], trace_key: str) -> str:
    """Return a deterministic, collision-free id for one audit observation.

    Keyed on ``(trace_key, session.id, sequence)`` so it is idempotent on re-ingest yet
    unique across a spoke's resumed sessions: ``event.sequence`` is monotonic only *within*
    a session, so two resumes under one ``spoke_run_id`` can repeat a sequence number, and
    ``session.id`` disambiguates them. When ``event.sequence`` is absent the discriminator
    falls back to ``event.timestamp`` then a content fingerprint, so distinct same-named
    events never silently overwrite one another. Hashed for a stable id like the sibling
    :func:`telemetry.langfuse_spoke_tree._copy_id`.

    Args:
        attrs: The merged OTLP resource + log-record attributes.
        trace_key: The spoke run id (or ``session.id`` fallback) selecting the audit trace.

    Returns:
        A stable ``audit-<sha1[:24]>`` observation id.
    """
    session = attrs.get("session.id", "")
    discriminator = (
        attrs.get("event.sequence")
        or attrs.get("event.timestamp")
        or hashlib.sha1(repr(sorted(attrs.items())).encode()).hexdigest()[:16]
    )
    digest = hashlib.sha1(f"{trace_key}\x1f{session}\x1f{discriminator}".encode()).hexdigest()
    return f"{_TRACE_PREFIX}{digest[:24]}"


def build_audit_event(attrs: dict[str, str], *, trace_key: str) -> dict[str, Any] | None:
    """Map one merged log-event attribute dict to a Langfuse ``event-create``, or None.

    Args:
        attrs: The merged OTLP resource + log-record attributes (string-valued).
        trace_key: The spoke run id (or ``session.id`` fallback) selecting the audit trace.

    Returns:
        A Langfuse ingestion ``event-create`` event, or None when ``event.name`` is not an
        audit/lifecycle event this module maps.
    """
    spec = AUDIT_SPECS.get(attrs.get("event.name", ""))
    if spec is None:
        return None
    timestamp = attrs.get("event.timestamp") or _INGEST_TIMESTAMP
    event_id = _observation_id(attrs, trace_key)
    name = attrs["event.name"]
    if spec.discriminator and attrs.get(spec.discriminator):
        name = f"{name}:{attrs[spec.discriminator]}"
    return {
        "id": event_id,
        "type": "event-create",
        "timestamp": timestamp,
        "body": {
            "id": event_id,
            "traceId": audit_trace_id(trace_key),
            "name": name,
            "startTime": timestamp,
            "level": _resolve_level(spec, attrs),
            "metadata": _build_metadata(spec, attrs),
        },
    }
