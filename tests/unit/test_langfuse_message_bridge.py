"""Unit tests for the Langfuse message bridge (Issue #83).

The bridge joins Claude Code's LLM message bodies (carried on the OTel *logs*
signal, with no trace ids) onto the matching ``llm_request`` span so Langfuse can
render the conversation as the observation's input/output. The join is
order-independent: a message log may arrive before or after the span that owns
its ``request_id``. These AAA tests exercise the pure helpers and the buffering
resolver with a stubbed patch sink — no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_message_bridge import Bridge, _attr, _hexid

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


# --- resolver: prompt.id -> request_id -> span_id chain (input) --------------


def test_request_body_resolves_through_prompt_chain() -> None:
    # Arrange
    sink = _Sink()
    bridge = Bridge(sink)
    body = '{"system": "sys", "messages": [{"role": "user", "content": "q"}]}'

    # Act: api_request links prompt->req; api_request_body carries the input;
    # the span supplying req->span arrives LAST.
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "prompt.id": "p-1", "request_id": "req-1"}),
            _attrs(**{"event.name": "api_request_body", "prompt.id": "p-1", "body": body}),
        )
    )
    assert sink.calls == []  # span not seen yet -> buffered
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-1"))

    # Assert: input is the structured {system, messages}; buffer drained.
    assert sink.calls == [
        (
            "1122334455667788",
            "input",
            {"system": "sys", "messages": [{"role": "user", "content": "q"}]},
        )
    ]
    assert bridge.pending_count() == 0


def test_request_body_truncated_falls_back_to_raw_text() -> None:
    # Arrange: a >60KB request is truncated by Claude Code into invalid JSON.
    sink = _Sink()
    bridge = Bridge(sink)
    truncated = '{"system": "sys", "messages": [{"role": "user", "content": "q'

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "prompt.id": "p-2", "request_id": "req-2"}),
            _attrs(**{"event.name": "api_request_body", "prompt.id": "p-2", "body": truncated}),
        )
    )
    bridge.on_spans(_span_payload(span_id="99aabbccddeeff00", request_id="req-2"))

    # Assert: the raw partial text is stored verbatim as the input.
    assert sink.calls == [("99aabbccddeeff00", "input", truncated)]


# --- order independence: span first, logs later ------------------------------


def test_buffered_item_flushes_once_regardless_of_arrival_order() -> None:
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
