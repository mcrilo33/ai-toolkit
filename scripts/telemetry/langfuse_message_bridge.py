#!/usr/bin/env python3
"""Langfuse message bridge: attach Claude Code LLM message bodies to llm_request spans.

Claude Code emits the full API request/response (the conversation messages) on the OTel
*logs* signal (``api_request_body`` / ``api_response_body``), NOT on the trace spans, and
the log records carry no trace_id/span_id. Langfuse therefore cannot attach them to the
``llm_request`` observation on its own.

This bridge receives both signals (the collector forwards them here as OTLP/HTTP JSON) and
joins them to patch the matching Langfuse observation's input/output (Langfuse observation
id == OTel span_id) via a ``generation-update`` ingestion event. The join chain, derived
from the actual telemetry, is::

    api_request_body.prompt.id --(api_request)--> request_id --(llm_request span)--> span_id
    api_response_body.request_id --------------------------------------------------> span_id

Everything is buffered and re-resolved as the pieces arrive, so signal ordering is
irrelevant. Stdlib only; runs on the host, reached by the collector at
``host.docker.internal:4319``.

Caveats preserved from the real telemetry:

* Request bodies over ~60KB hit Claude Code's body cap and are truncated to invalid JSON;
  the raw partial text is stored as the input so the request start is still visible.
* Response bodies parse cleanly.
* ``thinking`` blocks arrive as ``<REDACTED>``.

Import-safe: no environment is read at import time, so the resolver can be unit-tested
without any Langfuse credentials. Configuration is read in :func:`main`.
"""

from __future__ import annotations

import base64
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

    key: str  # join key: a prompt.id (ktype "prompt") or request_id (ktype "req")
    ktype: str  # "prompt" or "req"
    field: str  # Langfuse observation field: "input" or "output"
    value: object  # the structured messages/content, or raw truncated text


def _attr(attrs: list[dict[str, Any]], key: str) -> str | None:
    """Return the string value of OTLP attribute ``key``, or None if absent.

    OTLP/JSON wraps each value in a typed envelope. Every attribute this bridge reads
    (``event.name``, ``request_id``, ``prompt.id``, ``body``) is a string; int/bool values
    are coerced to ``str`` for defensiveness so callers always get a key-usable value.

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
    """Order-independent join of LLM message logs onto their ``llm_request`` spans.

    Spans supply ``request_id -> span_id``; ``api_request`` logs supply
    ``prompt.id -> request_id``; ``api_request_body`` / ``api_response_body`` logs carry the
    input/output to patch. Items whose span has not arrived yet are buffered and re-resolved
    on every subsequent span or log batch, so the two signals may arrive in any order.
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
        self._req_by_prompt: dict[str, str] = {}  # prompt.id -> request_id
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
        request_id, prompt_id = _attr(attrs, "request_id"), _attr(attrs, "prompt.id")
        if event == "api_request" and prompt_id and request_id:
            self._req_by_prompt[prompt_id] = request_id  # link prompt.id <-> request_id
            return
        raw = _attr(attrs, "body")
        if not raw:
            return
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            doc = {"raw": raw}
        if event == "api_request_body" and prompt_id:
            # Large requests hit Claude Code's 60KB body cap -> truncated, invalid JSON;
            # fall back to the raw (partial) text so the request start is still visible.
            value: object = (
                {"system": doc.get("system"), "messages": doc["messages"]}
                if "messages" in doc
                else raw
            )
            self._pending.append(
                {"key": prompt_id, "ktype": "prompt", "field": "input", "value": value}
            )
        elif event == "api_response_body" and request_id:
            self._pending.append(
                {
                    "key": request_id,
                    "ktype": "req",
                    "field": "output",
                    "value": doc.get("content", doc),
                }
            )

    def _resolve(self, key: str, ktype: str) -> str | None:
        """Map a pending item's join key to a span_id, following the chain when needed."""
        if ktype == "req":
            return self._span_by_req.get(key)
        request_id = self._req_by_prompt.get(key)
        return self._span_by_req.get(request_id) if request_id else None

    def _try_flush(self) -> None:
        """Patch every pending item whose join key now resolves; drop it once patched."""
        still: list[PendingItem] = []
        for item in self._pending:
            span_id = self._resolve(item["key"], item["ktype"])
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
