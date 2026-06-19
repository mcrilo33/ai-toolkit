#!/usr/bin/env python3
"""Langfuse message bridge: attach Claude Code LLM message bodies to llm_request spans.

Claude Code emits the full API request/response (the conversation messages) on the OTel
*logs* signal (``api_request_body`` / ``api_response_body``), NOT on the trace spans, and
the log records carry no trace_id/span_id. Langfuse therefore cannot attach them to the
``llm_request`` observation on its own.

This bridge receives both signals (the collector forwards them here as OTLP/HTTP JSON) and
joins them to patch the matching Langfuse observation's input/output (Langfuse observation
id == OTel span_id) via a ``generation-update`` ingestion event.

The input join is *per call*. One ``prompt.id`` spans an entire user turn, so it cannot key
a single API call -- every call of an agent loop shares it. The per-call key is
``event.sequence`` instead. An ``api_request`` log carries ``request_id`` + ``event.sequence``
(no body); an ``api_request_body`` log carries the body + ``event.sequence`` but no
``request_id``.

The body-to-request pairing is *one-to-one nearest-preceding*. A body is always emitted just
before its ``api_request``, so each request (taken in ascending sequence) claims its nearest
PRECEDING still-unused body -- the body whose ``event.sequence`` is the largest strictly less
than the request's -- and each body is consumed once. A plain "next ``api_request``" rule
double-assigns when bodies cluster (e.g. bodies at seqs 30 and 36 both precede request 37,
leaving request 40 with no body); one-to-one consumption removes both the double-assignment
and the resulting missing input. The recovered ``request_id`` maps to a span_id via the
``llm_request`` span. The join chain, derived from the actual telemetry, is::

    output: api_response_body.request_id ---------------(llm_request span)--> span_id
    input : api_request_body.event.sequence --(nearest-preceding api_request, 1:1)-->
                                             request_id --(llm_request span)--> span_id

HARD LIMIT (attribution is purely temporal): the body carries no per-call and no per-agent
id -- only ``prompt.id`` (shared by the whole session) and ``event.sequence``. So when
main-agent and sub-agent API calls INTERLEAVE, an input can still land on an adjacent
(wrong-agent) span at the boundary. This one-to-one match removes missing inputs and
double-assignment, but it does NOT fully resolve interleaved-agent swaps; that would require
a ``request_id`` or ``agent_id`` on the body, which Claude Code does not emit.

The input value is *only the last message* of the request body's ``messages`` array (the new
user/tool_result turn). Every call resends the whole growing conversation, and Claude Code
truncates the body at 60KB from the front, so the full request is near-identical noise across
calls; the last message is the only distinguishing, useful part. :func:`_last_message` is
tolerant of the truncation: it full-parses the JSON when possible, else scans the array
bracket-/quote-aware and returns the last element that closed before the cut.

Everything is buffered and re-resolved as the pieces arrive, so signal ordering is
irrelevant (an ``api_request_body`` may arrive before its ``api_request`` or its span).
Stdlib only; runs on the host, reached by the collector at ``host.docker.internal:4319``.

Import-safe: no environment is read at import time, so the resolver can be unit-tested
without any Langfuse credentials. Configuration is read in :func:`main`.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import logging
import os
import threading
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

logger = logging.getLogger("langfuse_message_bridge")

# Langfuse ingestion requires a timestamp on every event; the value is not meaningful for
# a generation-update (it only patches an existing observation), so a fixed stamp is fine.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Patches a single Langfuse observation field: (span_id, field, value) -> None.
PatchFn = Callable[[str, str, object], None]


class PendingItem(TypedDict):
    """A buffered input/output patch awaiting its span to resolve."""

    key: int | str  # join key: an event.sequence (ktype "seq") or request_id (ktype "req")
    ktype: str  # "seq" or "req"
    field: str  # Langfuse observation field: "input" or "output"
    value: object  # the last request message, or the response content


def _attr(attrs: list[dict[str, Any]], key: str) -> str | None:
    """Return the string value of OTLP attribute ``key``, or None if absent.

    OTLP/JSON wraps each value in a typed envelope. Every attribute this bridge reads
    (``event.name``, ``request_id``, ``event.sequence``, ``body``) is read as a string;
    int/bool values are coerced to ``str`` for defensiveness so callers always get a
    key-usable value.

    Args:
        attrs: The OTLP attribute list from a span or log record.
        key: The attribute key to look up.

    Returns:
        The attribute's value as a string, or None when the key is missing.
    """
    for a in attrs or []:
        if a.get("key") != key:
            continue
        value = a.get("value") or {}
        if "stringValue" in value:
            return value["stringValue"]
        for alt in ("intValue", "boolValue"):
            if alt in value:
                return str(value[alt])
    return None


def _hexid(raw: str) -> str:
    """Normalize an OTLP span id to the lowercase hex Langfuse uses for observation ids.

    OTLP/JSON may encode a span id as 16 hex chars or as base64; Langfuse keys observations
    on lowercase hex.

    Args:
        raw: The span id as it appears in the OTLP payload.

    Returns:
        The span id as lowercase hex, or the input unchanged if it is neither form.
    """
    s = str(raw or "")
    if len(s) == 16 and all(c in "0123456789abcdefABCDEF" for c in s):
        return s.lower()
    try:
        return base64.b64decode(s + "==").hex()
    except ValueError:
        return s


def _last_message(raw: str) -> object | None:
    """Return the last (complete) element of the request body's ``messages`` array.

    The last message is the new user/tool_result turn -- the only part that distinguishes
    one API call from the next, since each call resends the whole conversation. Full-parses
    the JSON when possible; on the 60KB truncation (which yields invalid JSON) it falls back
    to a tolerant scan of the array.

    Args:
        raw: The raw ``api_request_body`` text, possibly truncated to invalid JSON.

    Returns:
        The last complete message object, or None when no complete message is present.
    """
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict):
        msgs = doc.get("messages")
        if isinstance(msgs, list) and msgs:
            return msgs[-1]
    return _scan_last_message(raw)


def _scan_last_message(raw: str) -> object | None:
    """Scan a (possibly truncated) request body for the last complete ``messages`` element.

    The body may be cut mid-element by the 60KB cap, leaving invalid JSON. Walk the
    ``messages`` array tracking bracket depth and string state, parsing each top-level
    element as it closes; the last one that closed before the cut is the newest complete
    message.

    Args:
        raw: The raw (possibly truncated) ``api_request_body`` text.

    Returns:
        The last complete message object, or None when none closed before the cut.
    """
    marker = raw.find('"messages"')
    if marker < 0:
        return None
    start = raw.find("[", marker)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    elem_start: int | None = None
    last: object | None = None
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            if depth == 1 and c == "{":
                elem_start = i
            depth += 1
        elif c in "]}":
            depth -= 1
            if c == "}" and depth == 1 and elem_start is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    last = json.loads(raw[elem_start : i + 1])
                elem_start = None
            if depth == 0:
                break
    return last


def make_langfuse_patch(host: str, auth: str) -> PatchFn:
    """Build a patch function that PATCHes a Langfuse observation via ingestion.

    Args:
        host: Base Langfuse URL, e.g. ``http://localhost:3000``.
        auth: The ``Authorization`` header value, ``Basic <base64(pk:sk)>``.

    Returns:
        A callable ``(span_id, field, value) -> None`` posting a generation-update event.
    """

    def patch(span_id: str, field: str, value: object) -> None:
        event = {
            "batch": [
                {
                    "id": f"{span_id}-{field}",
                    "type": "generation-update",
                    "timestamp": _INGEST_TIMESTAMP,
                    "body": {"id": span_id, field: value},
                }
            ]
        }
        request = urllib.request.Request(
            f"{host}/api/public/ingestion",
            data=json.dumps(event).encode(),
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                resp.read()
        except OSError as e:
            logger.warning("patch failed span=%s %s: %s", span_id, field, e)

    return patch


class Bridge:
    """Per-call, order-independent join of LLM message logs onto ``llm_request`` spans.

    Spans supply ``request_id -> span_id``; ``api_request`` logs supply
    ``event.sequence -> request_id``; ``api_request_body`` carries the input (keyed by its
    own ``event.sequence``, matched one-to-one to its nearest-preceding ``api_request``) and
    ``api_response_body`` carries the output (keyed directly by ``request_id``). Items whose
    span has not arrived yet -- or whose ``api_request`` pairing is not yet known -- are
    buffered and re-resolved on every subsequent span or log batch, so the signals may arrive
    in any order. The body-to-request matching is recomputed from scratch on every flush, so
    a body and the request that should claim it can arrive in either order.
    """

    def __init__(self, patch: PatchFn) -> None:
        """Initialize the bridge.

        Args:
            patch: Sink applied to each resolved patch; the production sink posts to
                Langfuse, tests pass a recording stub so no network is touched.
        """
        self._lock = threading.Lock()
        self._patch = patch
        self._span_by_req: dict[str, str] = {}  # request_id -> span_id (hex)
        self._req_seq: list[tuple[int, str]] = []  # (api_request event.sequence, request_id)
        self._body_seqs: list[int] = []  # every api_request_body event.sequence ever seen
        self._pending: list[PendingItem] = []

    def pending_count(self) -> int:
        """Return the number of items still buffered awaiting their span."""
        with self._lock:
            return len(self._pending)

    def on_spans(self, payload: dict[str, Any]) -> None:
        """Record ``request_id -> span_id`` from a traces batch, then flush.

        Args:
            payload: A decoded OTLP/HTTP JSON ``ExportTraceServiceRequest``.
        """
        with self._lock:
            for rs in payload.get("resourceSpans", []):
                for ss in rs.get("scopeSpans", []):
                    for sp in ss.get("spans", []):
                        request_id = _attr(sp.get("attributes", []), "request_id")
                        if request_id:
                            self._span_by_req[request_id] = _hexid(sp.get("spanId", ""))
            self._try_flush()

    def on_logs(self, payload: dict[str, Any]) -> None:
        """Ingest message-body log records from a logs batch, then flush.

        Args:
            payload: A decoded OTLP/HTTP JSON ``ExportLogsServiceRequest``.
        """
        with self._lock:
            for rl in payload.get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    for lr in sl.get("logRecords", []):
                        self._ingest_log(lr.get("attributes", []))
            self._try_flush()

    def _ingest_log(self, attrs: list[dict[str, Any]]) -> None:
        event = _attr(attrs, "event.name")
        if event == "api_request":
            request_id, seq = _attr(attrs, "request_id"), _attr(attrs, "event.sequence")
            if request_id and seq is not None:
                self._req_seq.append((int(seq), request_id))  # matched 1:1 to a body
            return
        raw = _attr(attrs, "body")
        if not raw:
            return
        if event == "api_request_body":
            seq = _attr(attrs, "event.sequence")
            if seq is None:
                return
            self._body_seqs.append(int(seq))  # kept past flush so the match stays stable
            self._pending.append(
                {"key": int(seq), "ktype": "seq", "field": "input", "value": _last_message(raw)}
            )
        elif event == "api_response_body":
            request_id = _attr(attrs, "request_id")
            if not request_id:
                return
            try:
                doc = json.loads(raw)
            except json.JSONDecodeError:
                doc = {"raw": raw}
            self._pending.append(
                {
                    "key": request_id,
                    "ktype": "req",
                    "field": "output",
                    "value": doc.get("content", doc),
                }
            )

    def _match_bodies(self) -> dict[int, str]:
        """Match each buffered input body's ``event.sequence`` to a ``request_id``, 1:1.

        An ``api_request_body`` carries no per-call id, only ``event.sequence``, and a body
        is always emitted just before its ``api_request``. So each request (in ascending
        sequence) claims its nearest PRECEDING still-unused body -- the one whose sequence is
        the largest strictly less than the request's -- and each body is consumed once.
        One-to-one consumption avoids the double-assignment a plain "next request" rule causes
        when bodies cluster (e.g. bodies at seqs 30 and 36 both precede request 37).

        The match runs over *every* body sequence ever seen (``self._body_seqs``), not just
        the bodies still buffered: a body is dropped from ``self._pending`` once its span
        arrives and it is patched, so matching off the buffer would let a later flush re-grant
        its request to a different (older) body. Matching off the full set keeps each
        assignment stable no matter which spans have arrived.

        Returns:
            A mapping of body ``event.sequence`` to the ``request_id`` that claimed it.
        """
        bodies = sorted(set(self._body_seqs))
        used: set[int] = set()
        matched: dict[int, str] = {}
        for seq, request_id in sorted(self._req_seq):
            candidates = [b for b in bodies if b < seq and b not in used]
            if candidates:
                body_seq = max(candidates)
                used.add(body_seq)
                matched[body_seq] = request_id
        return matched

    def _resolve(self, item: PendingItem, matched: dict[int, str]) -> str | None:
        """Map a pending item's join key to a span_id, following the chain when needed.

        Args:
            item: The buffered patch awaiting its span.
            matched: The body-``event.sequence`` -> ``request_id`` map from
                :meth:`_match_bodies`, used for input items.

        Returns:
            The resolved span_id, or None when the join cannot complete yet.
        """
        key = item["key"]
        if item["ktype"] == "req":
            return self._span_by_req.get(key) if isinstance(key, str) else None
        request_id = matched.get(key) if isinstance(key, int) else None
        return self._span_by_req.get(request_id) if request_id else None

    def _try_flush(self) -> None:
        """Patch every pending item whose join key now resolves; drop it once patched.

        The body-to-request matching is recomputed every flush so arrival order is
        irrelevant: a body and the request that claims it may arrive in either order.
        """
        matched = self._match_bodies()
        still: list[PendingItem] = []
        for item in self._pending:
            span_id = self._resolve(item, matched)
            if span_id:
                self._patch(span_id, item["field"], item["value"])
            else:
                still.append(item)
        self._pending[:] = still


def make_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    """Build an OTLP/HTTP JSON receiver bound to ``bridge``.

    Args:
        bridge: The join target the receiver routes traces and logs into.

    Returns:
        A :class:`BaseHTTPRequestHandler` subclass the server instantiates per request.
    """

    class Handler(BaseHTTPRequestHandler):
        """Minimal OTLP/HTTP JSON receiver for the traces and logs the collector forwards."""

        def log_message(self, format: str, *args: Any) -> None:  # silence access logging
            pass

        def do_POST(self) -> None:
            """Decode one OTLP/HTTP JSON batch and route it to the span or log handler."""
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            try:
                payload = json.loads(raw)
                if self.path.endswith("/v1/traces"):
                    bridge.on_spans(payload)
                elif self.path.endswith("/v1/logs"):
                    bridge.on_logs(payload)
            except (ValueError, KeyError) as e:
                logger.warning("handler error %s: %s", self.path, e)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    return Handler


def main() -> None:
    """Read configuration from the environment and serve the bridge until interrupted.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s")
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    port = int(os.environ.get("BRIDGE_PORT", "4319"))

    bridge = Bridge(make_langfuse_patch(host, auth))
    logger.info("listening on :%d -> %s", port, host)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(bridge)).serve_forever()


if __name__ == "__main__":
    main()
