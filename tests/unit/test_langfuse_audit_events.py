"""Unit tests for the Langfuse audit-event mapper (Issue #93).

Claude Code emits an audit/lifecycle layer on the OTel *logs* signal —
``tool_decision`` (incl. rejections), ``mcp_server_connection``, ``compaction``,
``permission_mode_changed``, ``plugin_loaded``, ``skill_activated``,
``hook_execution_complete``, ``api_error``, ``api_refusal`` — that the message
bridge previously dropped. None of these has a pre-existing trace span, so they are
CREATED as Langfuse ``event-create`` observations on a per-spoke synthetic audit
trace (``sessionId == spoke_run_id``), rather than patched onto an existing span.

These AAA tests exercise the pure mapper: a merged attribute dict in, an ingestion
event (or ``None``) out. No network, no OTLP envelope — the bridge owns those.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_audit_events import (
    audit_trace_id,
    build_audit_event,
    trace_create,
)

# --- audit_trace_id ----------------------------------------------------------


def test_audit_trace_id_is_deterministic_and_prefixed() -> None:
    # Arrange / Act
    first = audit_trace_id("spoke-abc")
    second = audit_trace_id("spoke-abc")

    # Assert: stable across calls (idempotent re-ingest) and prefixed.
    assert first == second
    assert first.startswith("audit-")
    assert audit_trace_id("spoke-xyz") != first


# --- trace_create ------------------------------------------------------------


def test_trace_create_groups_under_the_spoke_session() -> None:
    # Act
    event = trace_create("spoke-abc", "2026-06-20T00:00:00Z")

    # Assert: a trace-create homed on the spoke session, id == audit_trace_id.
    assert event["type"] == "trace-create"
    body = event["body"]
    assert body["id"] == audit_trace_id("spoke-abc")
    assert body["sessionId"] == "spoke-abc"
    assert body["name"] == "spoke-audit:spoke-abc"


# --- build_audit_event: the three acceptance-criteria events -----------------


def _merged(**pairs: str) -> dict[str, str]:
    return dict(pairs)


def test_tool_decision_reject_creates_a_warning_event() -> None:
    # Arrange: a rejected tool call (the AC's "rejected tool_decision").
    attrs = _merged(
        **{
            "event.name": "tool_decision",
            "event.sequence": "42",
            "event.timestamp": "2026-06-20T00:00:01Z",
            "session.id": "sess-1",
            "prompt.id": "prompt-1",
            "tool_name": "Bash",
            "tool_use_id": "toolu_1",
            "decision": "reject",
            "source": "user_reject",
        }
    )

    # Act
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert: an event-create on the spoke audit trace, WARNING, full metadata.
    assert event is not None
    assert event["type"] == "event-create"
    body = event["body"]
    assert event["id"] == body["id"]
    assert body["id"].startswith("audit-")
    assert body["traceId"] == audit_trace_id("spoke-abc")
    assert body["name"] == "tool_decision:reject"
    assert body["level"] == "WARNING"
    assert body["startTime"] == "2026-06-20T00:00:01Z"
    assert body["metadata"]["decision"] == "reject"
    assert body["metadata"]["source"] == "user_reject"
    assert body["metadata"]["tool_name"] == "Bash"
    assert body["metadata"]["tool_use_id"] == "toolu_1"
    assert body["metadata"]["session.id"] == "sess-1"
    assert body["metadata"]["prompt.id"] == "prompt-1"


def test_tool_decision_accept_stays_default_level() -> None:
    # Arrange / Act: an accepted decision must not be elevated.
    attrs = _merged(
        **{
            "event.name": "tool_decision",
            "event.sequence": "43",
            "tool_name": "Read",
            "decision": "accept",
            "source": "config",
        }
    )
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert
    assert event is not None
    assert event["body"]["name"] == "tool_decision:accept"
    assert event["body"]["level"] == "DEFAULT"


def test_mcp_connection_failure_creates_an_error_event() -> None:
    # Arrange: an MCP connect failure (the AC's "MCP connect failure").
    attrs = _merged(
        **{
            "event.name": "mcp_server_connection",
            "event.sequence": "7",
            "status": "failed",
            "transport_type": "stdio",
            "error_code": "ENOENT",
            "server_name": "telemetry-mcp",
        }
    )

    # Act
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert: ERROR level so it surfaces in Langfuse's error filters.
    assert event is not None
    assert event["body"]["name"] == "mcp_server_connection:failed"
    assert event["body"]["level"] == "ERROR"
    assert event["body"]["metadata"]["status"] == "failed"
    assert event["body"]["metadata"]["error_code"] == "ENOENT"
    assert event["body"]["metadata"]["server_name"] == "telemetry-mcp"


def test_mcp_connection_success_stays_default_level() -> None:
    # Arrange / Act
    attrs = _merged(
        **{"event.name": "mcp_server_connection", "event.sequence": "6", "status": "connected"}
    )
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert
    assert event is not None
    assert event["body"]["name"] == "mcp_server_connection:connected"
    assert event["body"]["level"] == "DEFAULT"


def test_compaction_carries_pre_and_post_tokens() -> None:
    # Arrange: a compaction event (the AC's "compaction event").
    attrs = _merged(
        **{
            "event.name": "compaction",
            "event.sequence": "99",
            "trigger": "auto",
            "pre_tokens": "150000",
            "post_tokens": "42000",
        }
    )

    # Act
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert
    assert event is not None
    assert event["body"]["name"] == "compaction"
    assert event["body"]["metadata"]["pre_tokens"] == "150000"
    assert event["body"]["metadata"]["post_tokens"] == "42000"
    assert event["body"]["metadata"]["trigger"] == "auto"


# --- build_audit_event: observation-id idempotency + collision safety --------


def _id(attrs: dict[str, str], trace_key: str = "spoke-abc") -> str:
    event = build_audit_event(attrs, trace_key=trace_key)
    assert event is not None
    return event["body"]["id"]


def test_observation_id_is_idempotent() -> None:
    # Arrange / Act: the same event twice (a re-ingest) must produce the same id.
    attrs = _merged(
        **{
            "event.name": "compaction",
            "event.sequence": "5",
            "session.id": "sess-1",
        }
    )

    # Assert
    assert _id(attrs) == _id(attrs)


def test_observation_id_disambiguates_resumed_sessions_with_same_sequence() -> None:
    # Arrange: event.sequence is monotonic only WITHIN a session, so a resumed spoke
    # (same spoke_run_id / trace_key) can repeat a sequence number across sessions. The id
    # must not collide, or the second event would silently overwrite the first.
    base = {"event.name": "compaction", "event.sequence": "5"}

    # Act
    first = _id(_merged(**base, **{"session.id": "sess-1"}))
    second = _id(_merged(**base, **{"session.id": "sess-2"}))

    # Assert
    assert first != second


def test_observation_id_disambiguates_missing_sequence_by_timestamp() -> None:
    # Arrange: with no event.sequence, two distinct same-named events must still differ —
    # the fallback keys on event.timestamp rather than the (shared) event.name.
    first = _id(_merged(**{"event.name": "compaction", "event.timestamp": "2026-06-20T00:00:01Z"}))
    second = _id(_merged(**{"event.name": "compaction", "event.timestamp": "2026-06-20T00:00:02Z"}))

    # Assert
    assert first != second


# --- build_audit_event: discrimination + skipping ----------------------------


def test_missing_discriminator_omits_the_suffix() -> None:
    # Arrange / Act: a tool_decision with no decision attr keeps the bare name.
    attrs = _merged(**{"event.name": "tool_decision", "event.sequence": "1", "tool_name": "Bash"})
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert
    assert event is not None
    assert event["body"]["name"] == "tool_decision"


def test_absent_metadata_keys_are_skipped_not_nulled() -> None:
    # Arrange / Act: only the present keys land in metadata (no None placeholders).
    attrs = _merged(**{"event.name": "tool_decision", "event.sequence": "1", "decision": "accept"})
    event = build_audit_event(attrs, trace_key="spoke-abc")

    # Assert
    assert event is not None
    assert "tool_use_id" not in event["body"]["metadata"]
    assert "source" not in event["body"]["metadata"]


def test_unrecognized_event_returns_none() -> None:
    # Arrange / Act / Assert: a non-audit event.name is not mapped.
    assert build_audit_event({"event.name": "api_request"}, trace_key="spoke-abc") is None
    assert build_audit_event({"event.name": "definitely_not_an_event"}, trace_key="s") is None


def test_api_error_and_refusal_are_mapped() -> None:
    # Arrange / Act: the error-shaped events map to ERROR / WARNING observations.
    err = build_audit_event(
        {"event.name": "api_error", "event.sequence": "2", "error": "503", "status_code": "503"},
        trace_key="spoke-abc",
    )
    refusal = build_audit_event(
        {"event.name": "api_refusal", "event.sequence": "3", "request_id": "req-1"},
        trace_key="spoke-abc",
    )

    # Assert
    assert err is not None and err["body"]["level"] == "ERROR"
    assert err["body"]["metadata"]["error"] == "503"
    assert refusal is not None and refusal["body"]["level"] == "WARNING"
