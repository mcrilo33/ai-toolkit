"""Unit tests for the Langfuse message bridge (Issue #83).

The bridge joins Claude Code's LLM message bodies (carried on the OTel *logs*
signal, with no trace ids) onto the matching ``llm_request`` span so Langfuse can
render the conversation as the observation's input/output.

The input join is *per call*: an ``api_request_body`` is keyed by its
``event.sequence`` and paired with the next ``api_request`` (smallest sequence
strictly greater than the body's) to recover the ``request_id`` the span carries.
The stored input is only the *last* message of the request -- the new turn that
distinguishes one call from the next. The output join stays direct via
``request_id``. Everything is order-independent: a log may arrive before its
``api_request`` or its span. These AAA tests exercise the pure helpers and the
buffering resolver with a stubbed patch sink -- no network.
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


class _Sink:
    """Records every patch the bridge would send to Langfuse, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, span_id: str, field: str, value: object) -> None:
        self.calls.append((span_id, field, value))


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


# --- resolver: per-call input via event.sequence pairing ---------------------


def test_request_body_resolves_to_next_api_request_by_sequence() -> None:
    # Arrange: a body at seq 30 must pair with the next api_request (seq 37), not 46.
    sink = _Sink()
    bridge = Bridge(sink)
    body = (
        '{"system": "sys", "messages": ['
        '{"role": "user", "content": "old"},'
        '{"role": "user", "content": "newest turn"}]}'
    )

    # Act: body (seq 30), then two api_requests at seq 37 and 46, then the span.
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request_body", "event.sequence": "30", "body": body}),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-37"}),
            _attrs(**{"event.name": "api_request", "event.sequence": "46", "request_id": "req-46"}),
        )
    )
    assert sink.calls == []  # span for req-37 not seen yet -> buffered
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-37"))

    # Assert: input is ONLY the last message, patched onto req-37's span.
    assert sink.calls == [("1122334455667788", "input", {"role": "user", "content": "newest turn"})]
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
