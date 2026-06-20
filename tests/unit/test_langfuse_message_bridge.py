"""Unit tests for the Langfuse message bridge (Issue #83).

The bridge joins Claude Code's LLM message bodies (carried on the OTel *logs*
signal, with no trace ids) onto the matching ``llm_request`` span so Langfuse can
render the conversation as the observation's input/output.

The input join is *per call*: an ``api_request_body`` is keyed by its
``event.sequence`` and matched one-to-one to its nearest-preceding ``api_request``
(the largest sequence strictly less than the request's, consuming each body once)
to recover the ``request_id`` the span carries. The stored input is only the
*last* message of the request -- the new turn that distinguishes one call from the
next. The output join stays direct via ``request_id``. Everything is
order-independent: a log may arrive before its ``api_request`` or its span. These
AAA tests exercise the pure helpers and the buffering resolver with a stubbed patch
sink -- no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_message_bridge import Bridge, _attr, _hexid, _last_message

# --- OTLP/HTTP JSON payload builders ----------------------------------------


def _span_payload(*, span_id: str, request_id: str) -> dict:
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanId": span_id,
                                "attributes": [
                                    {"key": "request_id", "value": {"stringValue": request_id}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def _log_payload(*records: list[dict]) -> dict:
    return {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [{"attributes": attrs} for attrs in records]}]}
        ]
    }


def _attrs(**pairs: str) -> list[dict]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in pairs.items()]


def _audit_log_payload(resource: dict[str, str], *records: list[dict]) -> dict:
    """An OTLP/HTTP logs batch with resource-level attributes (e.g. spoke_run_id)."""
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attrs(**resource)},
                "scopeLogs": [{"logRecords": [{"attributes": attrs} for attrs in records]}],
            }
        ]
    }


class _Sink:
    """Records every patch the bridge would send to Langfuse, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, span_id: str, field: str, value: object) -> None:
        self.calls.append((span_id, field, value))


class _CreateSink:
    """Records every ingestion batch the bridge would CREATE in Langfuse, in order."""

    def __init__(self) -> None:
        self.batches: list[list[dict]] = []

    def __call__(self, batch: list[dict]) -> None:
        self.batches.append(batch)


# --- _attr -------------------------------------------------------------------


def test_attr_extracts_string_value() -> None:
    # Arrange
    attrs = _attrs(event_name="api_request", request_id="req-1")

    # Act / Assert
    assert _attr(attrs, "request_id") == "req-1"
    assert _attr(attrs, "missing") is None
    assert _attr([], "anything") is None


# --- _hexid ------------------------------------------------------------------


def test_hexid_passes_hex_through_lowercased() -> None:
    # Arrange / Act / Assert: a 16-char hex span id is returned lowercased.
    assert _hexid("00AABBCCDDEEFF11") == "00aabbccddeeff11"


def test_hexid_decodes_base64_to_hex() -> None:
    # Arrange: an 8-byte span id base64-encoded the way OTLP/JSON may emit it.
    raw = bytes.fromhex("0011223344556677")
    import base64

    encoded = base64.b64encode(raw).decode()

    # Act / Assert
    assert _hexid(encoded) == "0011223344556677"


# --- _last_message: full parse, truncated scan, missing ----------------------


def test_last_message_returns_final_element_of_full_parse() -> None:
    # Arrange: a well-formed body with two messages.
    raw = (
        '{"system": "sys", "messages": ['
        '{"role": "user", "content": "first"},'
        '{"role": "user", "content": "newest"}]}'
    )

    # Act / Assert: only the newest (last) message is returned.
    assert _last_message(raw) == {"role": "user", "content": "newest"}


def test_last_message_scans_last_complete_message_when_truncated() -> None:
    # Arrange: a >60KB body cut mid-final-element, leaving invalid JSON.
    raw = (
        '{"system": "sys", "messages": ['
        '{"role": "user", "content": "complete"},'
        '{"role": "user", "content": "cut off her'
    )

    # Act / Assert: the last element that closed before the cut is recovered.
    assert _last_message(raw) == {"role": "user", "content": "complete"}


def test_last_message_returns_none_when_no_messages() -> None:
    # Arrange / Act / Assert: no messages array -> None (full parse and scan).
    assert _last_message('{"system": "sys"}') is None
    assert _last_message('{"system": "sys", "messages": []}') is None
    assert _last_message("not json at all") is None


# --- resolver: direct request_id (api_response_body -> output) ---------------


def test_response_body_patches_output_when_span_present() -> None:
    # Arrange: the span has already arrived, then the response body log lands.
    sink = _Sink()
    bridge = Bridge(sink)
    bridge.on_spans(_span_payload(span_id="aabbccddeeff0011", request_id="req-9"))

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_response_body",
                    "request_id": "req-9",
                    "body": '{"content": [{"type": "text", "text": "hi"}]}',
                }
            )
        )
    )

    # Assert: output patched onto the span's hex id; nothing left buffered.
    assert sink.calls == [("aabbccddeeff0011", "output", [{"type": "text", "text": "hi"}])]
    assert bridge.pending_count() == 0


# --- resolver: per-call input via one-to-one nearest-preceding matching ------


def _body(content: str) -> str:
    return (
        '{"system": "sys", "messages": ['
        '{"role": "user", "content": "old"},'
        f'{{"role": "user", "content": "{content}"}}]}}'
    )


def test_bodies_match_one_to_one_to_nearest_preceding_request() -> None:
    # Arrange: bodies at seq 30 and 36 both precede request 37; one-to-one matching must give
    # the nearer body (36) to req-37 and leave body 30 for the later req-40 -- a plain "next
    # request" rule would assign BOTH to req-37 and leave req-40 with no input.
    sink = _Sink()
    bridge = Bridge(sink)

    # Act: two bodies, then two api_requests, then both spans.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body": _body("turn-30"),
                }
            ),
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "36",
                    "body": _body("turn-36"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-37"}),
            _attrs(**{"event.name": "api_request", "event.sequence": "40", "request_id": "req-40"}),
        )
    )
    assert sink.calls == []  # spans not seen yet -> buffered
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-37"))
    bridge.on_spans(_span_payload(span_id="8877665544332211", request_id="req-40"))

    # Assert: nearest-preceding, one-to-one -- body 36 -> req-37 (patched first, once its span
    # arrives), body 30 -> req-40 (patched after). Neither request double-assigns body 36.
    assert sink.calls == [
        ("1122334455667788", "input", {"role": "user", "content": "turn-36"}),
        ("8877665544332211", "input", {"role": "user", "content": "turn-30"}),
    ]
    assert bridge.pending_count() == 0


def test_request_with_no_preceding_body_gets_no_input() -> None:
    # Arrange: a single body at seq 36 precedes only req-37; req-30 (earlier) has no body
    # before it, so it must stay unmatched rather than steal the later body.
    sink = _Sink()
    bridge = Bridge(sink)

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "event.sequence": "30", "request_id": "req-30"}),
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "36",
                    "body": _body("turn-36"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-37"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-37"))
    bridge.on_spans(_span_payload(span_id="8877665544332211", request_id="req-30"))

    # Assert: only req-37 (with a preceding body) is patched; req-30 gets nothing.
    assert sink.calls == [("1122334455667788", "input", {"role": "user", "content": "turn-36"})]
    assert bridge.pending_count() == 0


def test_request_body_truncated_stores_last_complete_message() -> None:
    # Arrange: a >60KB request truncated by Claude Code into invalid JSON.
    sink = _Sink()
    bridge = Bridge(sink)
    truncated = (
        '{"system": "sys", "messages": ['
        '{"role": "user", "content": "complete turn"},'
        '{"role": "user", "content": "cut o'
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request_body", "event.sequence": "30", "body": truncated}),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-2"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="99aabbccddeeff00", request_id="req-2"))

    # Assert: the last message that closed before the cut is stored as the input.
    assert sink.calls == [
        ("99aabbccddeeff00", "input", {"role": "user", "content": "complete turn"})
    ]


def test_input_flushes_regardless_of_arrival_order() -> None:
    # Arrange: span and api_request arrive BEFORE the api_request_body this time.
    sink = _Sink()
    bridge = Bridge(sink)
    body = '{"messages": [{"role": "user", "content": "q"}]}'
    bridge.on_spans(_span_payload(span_id="ffeeddccbbaa9988", request_id="req-5"))
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "event.sequence": "10", "request_id": "req-5"}),
        )
    )

    # Act: the body (seq 8) lands last and resolves immediately to req-5's span.
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request_body", "event.sequence": "8", "body": body}),
        )
    )

    # Assert: patched exactly once with the last message; buffer cleared.
    assert sink.calls == [("ffeeddccbbaa9988", "input", {"role": "user", "content": "q"})]
    assert bridge.pending_count() == 0


# --- order independence: span first, output later ----------------------------


def test_buffered_output_flushes_once_regardless_of_arrival_order() -> None:
    # Arrange: the span arrives BEFORE any matching log this time.
    sink = _Sink()
    bridge = Bridge(sink)
    bridge.on_spans(_span_payload(span_id="ffeeddccbbaa9988", request_id="req-3"))

    # Act: response body for the already-seen request resolves immediately.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_response_body",
                    "request_id": "req-3",
                    "body": '{"content": "ok"}',
                }
            )
        )
    )

    # Assert: patched exactly once, buffer cleared, and a later flush re-sends nothing.
    assert sink.calls == [("ffeeddccbbaa9988", "output", "ok")]
    assert bridge.pending_count() == 0
    bridge.on_spans(_span_payload(span_id="ffeeddccbbaa9988", request_id="req-3"))
    assert len(sink.calls) == 1


# --- audit/event layer: CREATE observations onto the spoke audit trace -------


def _audit_record(**pairs: str) -> list[dict]:
    return _attrs(**pairs)


def test_audit_event_creates_trace_then_event_keyed_by_spoke_run_id() -> None:
    # Arrange: a rejected tool_decision with spoke_run_id at the resource level.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1", "session.id": "sess-1"},
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "42",
                    "decision": "reject",
                    "tool_name": "Bash",
                }
            ),
        )
    )

    # Assert: one batch = [trace-create, event-create]; the patch sink is untouched.
    assert patch.calls == []
    assert len(create.batches) == 1
    types = [event["type"] for event in create.batches[0]]
    assert types == ["trace-create", "event-create"]
    event_body = create.batches[0][1]["body"]
    assert event_body["name"] == "tool_decision:reject"
    assert event_body["level"] == "WARNING"


def test_audit_trace_create_emitted_once_per_spoke() -> None:
    # Arrange: two audit events for the SAME spoke.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    resource = {"spoke_run_id": "spoke-1"}

    # Act
    bridge.on_logs(
        _audit_log_payload(
            resource,
            _audit_record(**{"event.name": "compaction", "event.sequence": "1"}),
        )
    )
    bridge.on_logs(
        _audit_log_payload(
            resource,
            _audit_record(
                **{"event.name": "mcp_server_connection", "event.sequence": "2", "status": "failed"}
            ),
        )
    )

    # Assert: the trace-create lands only on the first batch; the second is event-only.
    assert [event["type"] for event in create.batches[0]] == ["trace-create", "event-create"]
    assert [event["type"] for event in create.batches[1]] == ["event-create"]


def test_audit_event_falls_back_to_session_id_when_no_spoke_run_id() -> None:
    # Arrange: no spoke_run_id; session.id is the only spoke key available.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"session.id": "sess-9"},
            _audit_record(**{"event.name": "compaction", "event.sequence": "1"}),
        )
    )

    # Assert: the trace is keyed by session.id.
    assert create.batches[0][0]["body"]["sessionId"] == "sess-9"


def test_response_body_does_not_reach_the_create_sink() -> None:
    # Arrange: an api_response_body (existing patch path) must not create observations.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(_span_payload(span_id="aabbccddeeff0011", request_id="req-9"))

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "api_response_body",
                    "request_id": "req-9",
                    "body": '{"content": "hi"}',
                }
            ),
        )
    )

    # Assert: patched as output, nothing created.
    assert patch.calls == [("aabbccddeeff0011", "output", "hi")]
    assert create.batches == []
