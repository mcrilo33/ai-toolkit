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


# --- file mode: body_ref fallback when no inline body ------------------------


def test_request_body_ref_attaches_input_from_file(tmp_path: Path) -> None:
    # Arrange: file mode (OTEL_LOG_RAW_API_BODIES=file:<dir>) emits no inline `body`, only a
    # `body_ref` absolute path to the untruncated request JSON on disk.
    sink = _Sink()
    bridge = Bridge(sink)
    ref = tmp_path / "abc.request.json"
    ref.write_text(_body("turn-file"))

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body_ref": str(ref),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-f"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-f"))

    # Assert: the input is read from the file and patched as the last message.
    assert sink.calls == [("1122334455667788", "input", {"role": "user", "content": "turn-file"})]
    assert bridge.pending_count() == 0


def test_response_body_ref_attaches_output_from_file(tmp_path: Path) -> None:
    # Arrange: file mode for the response body too -- a body_ref instead of inline `body`.
    sink = _Sink()
    bridge = Bridge(sink)
    bridge.on_spans(_span_payload(span_id="aabbccddeeff0011", request_id="req-r"))
    ref = tmp_path / "abc.response.json"
    ref.write_text('{"content": [{"type": "text", "text": "from file"}]}')

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_response_body",
                    "request_id": "req-r",
                    "body_ref": str(ref),
                }
            )
        )
    )

    # Assert: output read from the file and patched onto the span.
    assert sink.calls == [("aabbccddeeff0011", "output", [{"type": "text", "text": "from file"}])]
    assert bridge.pending_count() == 0


def test_inline_body_preferred_over_body_ref(tmp_path: Path) -> None:
    # Arrange: both attributes present -- inline `body` must win, body_ref ignored.
    sink = _Sink()
    bridge = Bridge(sink)
    ref = tmp_path / "ignored.request.json"
    ref.write_text(_body("from-file"))

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body": _body("inline"),
                    "body_ref": str(ref),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-i"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-i"))

    # Assert: the inline body is used, not the file.
    assert sink.calls == [("1122334455667788", "input", {"role": "user", "content": "inline"})]


def test_missing_body_ref_skips_without_crashing(tmp_path: Path) -> None:
    # Arrange: a body_ref pointing at a nonexistent path (stale/hostile log event).
    sink = _Sink()
    bridge = Bridge(sink)
    missing = tmp_path / "does-not-exist.request.json"

    # Act: must not raise.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body_ref": str(missing),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-x"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-x"))

    # Assert: nothing patched, nothing buffered, no crash.
    assert sink.calls == []
    assert bridge.pending_count() == 0


def test_body_ref_pointing_at_directory_skips_without_crashing(tmp_path: Path) -> None:
    # Arrange: body_ref points at a directory, not a regular file.
    sink = _Sink()
    bridge = Bridge(sink)

    # Act: must not raise.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_response_body",
                    "request_id": "req-d",
                    "body_ref": str(tmp_path),
                }
            )
        )
    )
    bridge.on_spans(_span_payload(span_id="aabbccddeeff0011", request_id="req-d"))

    # Assert: a non-file path is skipped gracefully.
    assert sink.calls == []
    assert bridge.pending_count() == 0


def test_oversized_body_ref_skips_without_crashing(tmp_path: Path) -> None:
    # Arrange: a body_ref file beyond the read cap must be skipped, not loaded into memory.
    from telemetry.langfuse_message_bridge import _MAX_BODY_REF_BYTES

    sink = _Sink()
    bridge = Bridge(sink)
    ref = tmp_path / "huge.request.json"
    ref.write_bytes(b"x" * (_MAX_BODY_REF_BYTES + 1))

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body_ref": str(ref),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-o"}),
        )
    )
    bridge.on_spans(_span_payload(span_id="1122334455667788", request_id="req-o"))

    # Assert: skipped, nothing patched.
    assert sink.calls == []
    assert bridge.pending_count() == 0


def test_non_utf8_body_ref_does_not_crash_the_batch(tmp_path: Path) -> None:
    # Arrange: a body_ref with invalid UTF-8 bytes must not raise (UnicodeDecodeError is a
    # ValueError, not OSError) and abort the rest of the batch -- the record is skipped, the
    # following audit record in the same batch still processes.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    ref = tmp_path / "binary.request.json"
    ref.write_bytes(b"\xff\xfe\x00 not valid utf-8")

    # Act: must not raise.
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body_ref": str(ref),
                }
            ),
            _attrs(**{"event.name": "compaction", "event.sequence": "31"}),
        )
    )

    # Assert: the binary body produced no input patch, but the audit event still went through.
    assert patch.calls == []
    assert [event["type"] for event in create.batches[0]] == ["trace-create", "event-create"]


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


def test_audit_event_without_a_spoke_key_is_dropped() -> None:
    # Arrange: no spoke_run_id and no session.id — there is no trace to attach to.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {},
            _audit_record(**{"event.name": "compaction", "event.sequence": "1"}),
        )
    )

    # Assert: nothing created (and nothing patched).
    assert create.batches == []
    assert patch.calls == []


# --- hook-event nesting: resolve a tool_use_id for Pre/PostToolUse hooks --------


def _hook_events(create: _CreateSink) -> list[dict]:
    """Every hook_execution_complete event-create the bridge emitted, in order."""
    return [
        event
        for batch in create.batches
        for event in batch
        if event["type"] == "event-create"
        and event["body"]["name"].startswith("hook_execution_complete")
    ]


def test_pretooluse_hook_gets_tool_use_id_from_following_decision() -> None:
    # Arrange: a PreToolUse:Edit hook (seq 10) arrives BEFORE its tool_decision (seq 15), as
    # Claude Code emits them. The bridge emits the hook immediately at root (never dropped),
    # then re-emits it with the resolved tool_use_id once the decision is seen.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "hook_execution_complete",
                    "event.sequence": "10",
                    "hook_event": "PreToolUse",
                    "hook_name": "PreToolUse:Edit",
                }
            ),
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "15",
                    "tool_name": "Edit",
                    "tool_use_id": "toolu_1",
                    "decision": "accept",
                }
            ),
        )
    )

    # Assert: two emits of the SAME observation id -- first at root (no id), then enriched.
    hooks = _hook_events(create)
    assert len(hooks) == 2
    assert "tool_use_id" not in hooks[0]["body"]["metadata"]
    assert hooks[-1]["body"]["metadata"]["tool_use_id"] == "toolu_1"
    assert hooks[0]["id"] == hooks[-1]["id"]


def test_posttooluse_hook_gets_tool_use_id_from_preceding_decision() -> None:
    # Arrange: a PostToolUse:Read hook (seq 20) is preceded by its tool_decision (seq 15).
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "15",
                    "tool_name": "Read",
                    "tool_use_id": "toolu_2",
                    "decision": "accept",
                }
            ),
            _audit_record(
                **{
                    "event.name": "hook_execution_complete",
                    "event.sequence": "20",
                    "hook_event": "PostToolUse",
                    "hook_name": "PostToolUse:Read",
                }
            ),
        )
    )

    # Assert: the hook nests under the preceding Read decision's tool_use_id.
    hooks = _hook_events(create)
    assert hooks[-1]["body"]["metadata"]["tool_use_id"] == "toolu_2"


def test_hook_with_no_matching_decision_stays_at_root_once() -> None:
    # Arrange: a PreToolUse:Edit hook with only a Bash decision present. The tool_name must
    # match, so the hook never binds to Bash -- it is emitted once at root with no id.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "hook_execution_complete",
                    "event.sequence": "10",
                    "hook_event": "PreToolUse",
                    "hook_name": "PreToolUse:Edit",
                }
            ),
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "15",
                    "tool_name": "Bash",
                    "tool_use_id": "toolu_x",
                    "decision": "accept",
                }
            ),
        )
    )

    # Assert: emitted exactly once, never bound to the wrong tool.
    hooks = _hook_events(create)
    assert len(hooks) == 1
    assert "tool_use_id" not in hooks[0]["body"]["metadata"]


def test_sessionstart_hook_is_emitted_once_and_never_buffered() -> None:
    # Arrange: a SessionStart hook has no tool; it must stay at root, emitted once, even with
    # a following decision present (it is never a Pre/PostToolUse, so never buffered).
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "hook_execution_complete",
                    "event.sequence": "5",
                    "hook_event": "SessionStart",
                    "hook_name": "SessionStart",
                }
            ),
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "8",
                    "tool_name": "Edit",
                    "tool_use_id": "toolu_3",
                    "decision": "accept",
                }
            ),
        )
    )

    # Assert
    hooks = _hook_events(create)
    assert len(hooks) == 1
    assert "tool_use_id" not in hooks[0]["body"]["metadata"]


def test_pretooluse_hook_matches_decision_arriving_in_later_batch() -> None:
    # Arrange: the matching decision arrives in a SEPARATE later batch -- the buffered hook
    # must resolve across batches (order-independent, like the message join).
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act: hook first; only the immediate at-root emit exists so far.
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "hook_execution_complete",
                    "event.sequence": "10",
                    "hook_event": "PreToolUse",
                    "hook_name": "PreToolUse:Edit",
                }
            ),
        )
    )
    assert len(_hook_events(create)) == 1
    assert "tool_use_id" not in _hook_events(create)[0]["body"]["metadata"]

    # The decision lands later and resolves the buffered hook.
    bridge.on_logs(
        _audit_log_payload(
            {"spoke_run_id": "spoke-1"},
            _audit_record(
                **{
                    "event.name": "tool_decision",
                    "event.sequence": "15",
                    "tool_name": "Edit",
                    "tool_use_id": "toolu_1",
                    "decision": "accept",
                }
            ),
        )
    )

    # Assert: re-emitted exactly once with the id; not re-emitted again on later flushes.
    hooks = _hook_events(create)
    assert len(hooks) == 2
    assert hooks[-1]["body"]["metadata"]["tool_use_id"] == "toolu_1"


def _multi_session_log_payload(*sessions: tuple[dict[str, str], list[list[dict]]]) -> dict:
    """An OTLP/HTTP logs batch carrying several sessions, each its own resource + records.

    Mirrors the single bridge fanning in concurrent sessions (each ``resourceLogs`` entry is a
    distinct Claude Code process, keyed by its own ``session.id``). All records are ingested
    before the single end-of-batch hook flush, so a cross-session sequence collision is
    exercised deterministically.
    """
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attrs(**resource)},
                "scopeLogs": [{"logRecords": [{"attributes": attrs} for attrs in records]}],
            }
            for resource, records in sessions
        ]
    }


def test_pretooluse_hook_binds_only_a_same_session_decision() -> None:
    # Arrange: session A's PreToolUse:Edit hook (seq 10) precedes A's own Edit decision (seq
    # 20). A DIFFERENT session B emits an Edit decision at seq 12 -- nearer in the global
    # sequence namespace. event.sequence is per-session, so the join must be scoped to
    # session.id: A's hook must bind A's toolu_A, never B's nearer-but-foreign toolu_B.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _multi_session_log_payload(
            (
                {"spoke_run_id": "spoke-A", "session.id": "sess-A"},
                [
                    _audit_record(
                        **{
                            "event.name": "hook_execution_complete",
                            "event.sequence": "10",
                            "hook_event": "PreToolUse",
                            "hook_name": "PreToolUse:Edit",
                        }
                    ),
                    _audit_record(
                        **{
                            "event.name": "tool_decision",
                            "event.sequence": "20",
                            "tool_name": "Edit",
                            "tool_use_id": "toolu_A",
                            "decision": "accept",
                        }
                    ),
                ],
            ),
            (
                {"spoke_run_id": "spoke-B", "session.id": "sess-B"},
                [
                    _audit_record(
                        **{
                            "event.name": "tool_decision",
                            "event.sequence": "12",
                            "tool_name": "Edit",
                            "tool_use_id": "toolu_B",
                            "decision": "accept",
                        }
                    ),
                ],
            ),
        )
    )

    # Assert: session A's hook resolves to A's own tool, not B's nearer foreign decision.
    a_hooks = [
        event
        for event in _hook_events(create)
        if event["body"]["metadata"].get("session.id") == "sess-A"
    ]
    assert a_hooks[-1]["body"]["metadata"]["tool_use_id"] == "toolu_A"


def test_hook_with_only_a_foreign_session_decision_stays_unresolved() -> None:
    # Arrange: session A's PreToolUse:Edit hook has NO same-session Edit decision; only session
    # B offers one. A foreign decision must never resolve the hook -- it stays at root, emitted
    # once with no tool_use_id, rather than dangling on a foreign id in the assembled tree.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act
    bridge.on_logs(
        _multi_session_log_payload(
            (
                {"spoke_run_id": "spoke-A", "session.id": "sess-A"},
                [
                    _audit_record(
                        **{
                            "event.name": "hook_execution_complete",
                            "event.sequence": "10",
                            "hook_event": "PreToolUse",
                            "hook_name": "PreToolUse:Edit",
                        }
                    ),
                ],
            ),
            (
                {"spoke_run_id": "spoke-B", "session.id": "sess-B"},
                [
                    _audit_record(
                        **{
                            "event.name": "tool_decision",
                            "event.sequence": "15",
                            "tool_name": "Edit",
                            "tool_use_id": "toolu_B",
                            "decision": "accept",
                        }
                    ),
                ],
            ),
        )
    )

    # Assert: session A's hook is emitted once, never bound to B's foreign tool_use_id.
    a_hooks = [
        event
        for event in _hook_events(create)
        if event["body"]["metadata"].get("session.id") == "sess-A"
    ]
    assert len(a_hooks) == 1
    assert "tool_use_id" not in a_hooks[0]["body"]["metadata"]


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


# --- prompt.id stamping: recover the span-side causal key (#111) --------------
#
# Claude Code never emits prompt.id on the claude_code.interaction span (verified against
# real Langfuse data) -- it rides the logs signal only (api_request[_body].prompt.id). The
# bridge reconstructs it: prompt.id --(event.sequence / request_id)--> request_id
# --(llm_request span)--> span_id --(parentSpanId, up to the enclosing interaction)-->
# interaction span_id, then stamps flat metadata["prompt.id"] via a span-update so the
# spoke-tree's _build_interaction_index / _build_skill_index have a key to match.


def _tree_span(
    *,
    span_id: str,
    name: str | None = None,
    parent: str | None = None,
    request_id: str | None = None,
) -> dict:
    """A single OTLP span with optional name / parentSpanId / request_id attribute."""
    attrs = (
        [{"key": "request_id", "value": {"stringValue": request_id}}]
        if request_id is not None
        else []
    )
    span: dict = {"spanId": span_id, "attributes": attrs}
    if name is not None:
        span["name"] = name
    if parent is not None:
        span["parentSpanId"] = parent
    return span


def _spans(*spans: dict) -> dict:
    """An OTLP/HTTP traces batch carrying several spans (interaction + llm_request)."""
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def _prompt_stamps(create: _CreateSink) -> list[dict]:
    """Every span-update event the bridge emitted to stamp prompt.id, in order."""
    return [event for batch in create.batches for event in batch if event["type"] == "span-update"]


def test_interaction_stamped_with_prompt_id_from_request_body() -> None:
    # Arrange: an interaction with one child llm_request (request_id req-1); a request body
    # carries prompt.id p-1 keyed by its event.sequence, matched to the request by sequence.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000a1", request_id="req-1"),
        )
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "prompt.id": "p-1",
                    "body": _body("turn-30"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-1"}),
        )
    )

    # Assert: the interaction (not the llm_request) is stamped with flat metadata["prompt.id"].
    stamps = _prompt_stamps(create)
    assert len(stamps) == 1
    assert stamps[0]["body"] == {"id": "00000000000000a1", "metadata": {"prompt.id": "p-1"}}


def test_prompt_id_read_directly_from_api_request() -> None:
    # Arrange: when api_request itself carries request_id AND prompt.id, no body is needed.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000a1", request_id="req-1"),
        )
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "request_id": "req-1", "prompt.id": "p-9"})
        )
    )

    # Assert
    stamps = _prompt_stamps(create)
    assert stamps == [
        {
            "id": "00000000000000a1-prompt-id",
            "type": "span-update",
            "timestamp": stamps[0]["timestamp"],
            "body": {"id": "00000000000000a1", "metadata": {"prompt.id": "p-9"}},
        }
    ]


def test_prompt_id_stamp_flushes_regardless_of_arrival_order() -> None:
    # Arrange: the logs arrive BEFORE the spans -- the stamp must still resolve on the later
    # on_spans flush (same order-independence as the message join).
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)

    # Act: logs first (no spans yet -> nothing stamped), then the spans.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "prompt.id": "p-1",
                    "body": _body("turn-30"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-1"}),
        )
    )
    assert _prompt_stamps(create) == []  # interaction not seen yet -> buffered
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000a1", request_id="req-1"),
        )
    )

    # Assert
    stamps = _prompt_stamps(create)
    assert len(stamps) == 1
    assert stamps[0]["body"]["id"] == "00000000000000a1"


def test_interaction_stamped_once_when_turn_has_two_llm_requests() -> None:
    # Arrange: two llm_requests under the SAME interaction, both tied to prompt.id p-1 (one
    # turn). The interaction must be stamped exactly once, not per request.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000a1", request_id="req-1"),
            _tree_span(span_id="00000000000000b2", parent="00000000000000a1", request_id="req-2"),
        )
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "request_id": "req-1", "prompt.id": "p-1"}),
            _attrs(**{"event.name": "api_request", "request_id": "req-2", "prompt.id": "p-1"}),
        )
    )

    # Assert: a single span-update for the lone interaction.
    stamps = _prompt_stamps(create)
    assert len(stamps) == 1
    assert stamps[0]["body"]["id"] == "00000000000000a1"


def test_prompt_id_walks_up_to_nearest_interaction_ancestor() -> None:
    # Arrange: a sub-agent shape -- the llm_request's parent is a tool:Agent span whose parent
    # is the interaction. The walk must climb to the nearest interaction ancestor.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000c1", name="tool:Agent", parent="00000000000000a1"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000c1", request_id="req-1"),
        )
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(**{"event.name": "api_request", "request_id": "req-1", "prompt.id": "p-1"})
        )
    )

    # Assert: stamped onto the interaction, not the intermediate tool:Agent span.
    stamps = _prompt_stamps(create)
    assert [s["body"]["id"] for s in stamps] == ["00000000000000a1"]


def test_prompt_id_stamp_self_corrects_after_out_of_order_request() -> None:
    # Arrange: two turns. Turn A = interaction a1 + llm_request req-37; turn B = interaction a2 +
    # llm_request req-50. Body seq 30 carries turn A's prompt (p-a), body seq 45 carries turn B's
    # (p-b). The api_request for req-50 arrives BEFORE req-37 and BEFORE body 45 -- so on the first
    # flush body 30 is mis-matched (nearest-preceding) to req-50 and turn B is stamped p-a. The
    # later flush, once req-37 and body 45 settle, must RE-STAMP turn B with the correct p-b.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="000000000000000a", name="claude_code.interaction"),
            _tree_span(span_id="000000000000000b", parent="000000000000000a", request_id="req-37"),
            _tree_span(span_id="000000000000000c", name="claude_code.interaction"),
            _tree_span(span_id="000000000000000d", parent="000000000000000c", request_id="req-50"),
        )
    )

    # Act 1: only body 30 and req-50 so far -> body 30 mis-binds to req-50 (turn B stamped p-a).
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "prompt.id": "p-a",
                    "body": _body("turn-a"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "50", "request_id": "req-50"}),
        )
    )
    assert _prompt_stamps(create)[-1]["body"] == {
        "id": "000000000000000c",
        "metadata": {"prompt.id": "p-a"},
    }

    # Act 2: req-37 and body 45 settle the matching -> body 30 -> req-37, body 45 -> req-50.
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "45",
                    "prompt.id": "p-b",
                    "body": _body("turn-b"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-37"}),
        )
    )

    # Assert: turn A stamped p-a, and turn B CORRECTED from p-a to p-b (idempotent upsert id).
    final = {s["body"]["id"]: s["body"]["metadata"]["prompt.id"] for s in _prompt_stamps(create)}
    assert final == {"000000000000000a": "p-a", "000000000000000c": "p-b"}


def test_request_body_without_prompt_id_produces_no_stamp() -> None:
    # Arrange: a normal message-join body with no prompt.id attribute must not stamp anything.
    patch, create = _Sink(), _CreateSink()
    bridge = Bridge(patch, create=create)
    bridge.on_spans(
        _spans(
            _tree_span(span_id="00000000000000a1", name="claude_code.interaction"),
            _tree_span(span_id="00000000000000b1", parent="00000000000000a1", request_id="req-1"),
        )
    )

    # Act
    bridge.on_logs(
        _log_payload(
            _attrs(
                **{
                    "event.name": "api_request_body",
                    "event.sequence": "30",
                    "body": _body("turn-30"),
                }
            ),
            _attrs(**{"event.name": "api_request", "event.sequence": "37", "request_id": "req-1"}),
        )
    )

    # Assert: input still patched (message join), but no prompt.id stamp.
    assert _prompt_stamps(create) == []
    assert patch.calls == [("00000000000000b1", "input", {"role": "user", "content": "turn-30"})]
