#!/usr/bin/env python3
"""Langfuse message bridge: attach Claude Code LLM message bodies to llm_request spans.

Claude Code emits the full API request/response (the conversation messages) on the OTel
*logs* signal (``api_request_body`` / ``api_response_body``), NOT on the trace spans, and
the log records carry no trace_id/span_id. Langfuse therefore cannot attach them to the
``llm_request`` observation on its own.

This bridge receives both signals (the collector forwards them here as OTLP/HTTP JSON) and
joins them to patch the matching Langfuse observation's input/output (Langfuse observation
id == OTel span_id) via a ``generation-update`` ingestion event.

The same logs signal also carries Claude Code's audit/lifecycle layer (``tool_decision``
incl. rejections, ``mcp_server_connection``, ``compaction``, and the rest -- see
:mod:`telemetry.langfuse_audit_events`). Those events have no pre-existing span to patch, so
the bridge CREATES them as ``event-create`` observations on a per-spoke synthetic audit
trace keyed by ``spoke_run_id`` (issue #93). That path needs no buffering -- the trace is
minted here -- and is independent of the message join below.

One audit sub-case DOES buffer: a ``PreToolUse``/``PostToolUse`` ``hook_execution_complete``
event carries no ``tool_use_id`` (only ``hook_name``/``hook_event``/``event.sequence``), so
the assembler cannot nest it under the tool that triggered it. The bridge recovers the id by
joining on ``event.sequence`` to the triggering ``tool_decision`` (nearest-FOLLOWING for a
Pre hook, nearest-PRECEDING for a Post hook, requiring a ``tool_name`` match), reusing the
same kept-across-flushes nearest-by-sequence machinery as the body join -- decisions are
retained in ``self._tool_decisions`` like body sequences are in ``self._body_seqs``. The
match is *deferred*, not the creation: the hook is emitted immediately at the root (so it is
never dropped) and, once its decision is seen, RE-EMITTED with the same observation id and
the ``tool_use_id`` stamped into metadata (Langfuse upserts by id). A hook with no matching
decision (e.g. a ``SessionStart`` hook, or a tool that emits no decision) keeps no id and
stays at the root.

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

The body itself arrives one of two ways depending on ``OTEL_LOG_RAW_API_BODIES``. Inline mode
(``=1``) puts the (60KB-truncated) body on the ``body`` attribute. File mode
(``=file:<dir>``, what the auto-wired worktree uses so #87 gets UNTRUNCATED bodies) emits no
inline ``body`` and instead a ``body_ref`` absolute path to the body on disk.
:func:`_read_body` prefers inline ``body`` and falls back to reading ``body_ref`` (defensively
-- regular file only, size-capped, never raising), so file-mode spokes get fuller input/output
than the old inline cap allowed.

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

try:
    from telemetry.langfuse_audit_events import build_audit_event, trace_create
except ModuleNotFoundError:  # direct `python3 scripts/telemetry/...` run: add the package root
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from telemetry.langfuse_audit_events import build_audit_event, trace_create

logger = logging.getLogger("langfuse_message_bridge")

# Langfuse ingestion requires a timestamp on every event; the value is not meaningful for
# a generation-update (it only patches an existing observation), so a fixed stamp is fine.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Cap a body_ref file read so a stale/hostile log event can never make the bridge load an
# unbounded file into memory; the largest legitimate untruncated body is well under this.
_MAX_BODY_REF_BYTES = 8 * 1024 * 1024

# Patches a single Langfuse observation field: (span_id, field, value) -> None.
PatchFn = Callable[[str, str, object], None]

# Creates Langfuse observations from an ingestion batch (audit/event layer): batch -> None.
CreateFn = Callable[[list[dict[str, Any]]], None]


def _noop_create(batch: list[dict[str, Any]]) -> None:
    """Default create sink: drop the batch (used when no audit ingestion is wired)."""


class PendingItem(TypedDict):
    """A buffered input/output patch awaiting its span to resolve."""

    key: int | str  # join key: an event.sequence (ktype "seq") or request_id (ktype "req")
    ktype: str  # "seq" or "req"
    field: str  # Langfuse observation field: "input" or "output"
    value: object  # the last request message, or the response content


# Maps a hook_execution_complete's hook_event to the join direction against tool_decision.
# Only Pre/PostToolUse hooks name a tool; the rest (SessionStart, Stop, PreCompact, ...) have
# none and stay at the synthetic root.
_TOOLUSE_HOOK_DIRECTIONS = {"PreToolUse": "pre", "PostToolUse": "post"}


class BufferedHook(TypedDict):
    """A Pre/PostToolUse hook event awaiting its tool_use_id from a tool_decision."""

    attrs: dict[str, str]  # the merged event attrs, re-rendered with the id once resolved
    trace_key: str  # the spoke audit trace the hook was emitted on
    seq: int  # the hook's event.sequence, the join key against tool_decision
    tool: str  # the tool named by hook_name, matched against the decision's tool_name
    direction: str  # "pre" (nearest-following decision) or "post" (nearest-preceding)


def _tooluse_hook(attrs: dict[str, str]) -> tuple[str, str] | None:
    """Return ``(tool_name, direction)`` for a Pre/PostToolUse hook event, else None.

    The tool is the suffix of ``hook_name`` (``"PreToolUse:Edit"`` -> ``"Edit"``) and the
    direction is keyed off ``hook_event``. Returns None for any non-hook event, a non-Pre/Post
    hook, or a ``hook_name`` with no tool suffix -- those keep no ``tool_use_id`` and stay at
    the synthetic root.

    Args:
        attrs: The merged OTLP resource + log-record attributes.

    Returns:
        The matched tool name and join direction, or None when the event names no tool.
    """
    if attrs.get("event.name") != "hook_execution_complete":
        return None
    direction = _TOOLUSE_HOOK_DIRECTIONS.get(attrs.get("hook_event", ""))
    if direction is None:
        return None
    tool = attrs.get("hook_name", "").partition(":")[2]
    return (tool, direction) if tool else None


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


def _attrs_dict(attrs: list[dict[str, Any]]) -> dict[str, str]:
    """Flatten an OTLP attribute list to a plain ``key -> string`` dict.

    The audit mapper works on a merged resource+record attribute dict rather than the OTLP
    envelope, so the bridge flattens both here (string/int/bool coerced to ``str`` like
    :func:`_attr`).

    Args:
        attrs: The OTLP attribute list from a log record's resource or its attributes.

    Returns:
        A mapping of every attribute key to its string value.
    """
    out: dict[str, str] = {}
    for a in attrs or []:
        key = a.get("key")
        if key is None:
            continue
        value = a.get("value") or {}
        if "stringValue" in value:
            out[key] = value["stringValue"]
            continue
        for alt in ("intValue", "boolValue"):
            if alt in value:
                out[key] = str(value[alt])
                break
    return out


def _read_body(record_attrs: list[dict[str, Any]]) -> str | None:
    """Return the raw API body for a body log, inline when present else read from ``body_ref``.

    In inline mode (``OTEL_LOG_RAW_API_BODIES=1``) Claude Code emits the body on the ``body``
    attribute. In file mode (``=file:<dir>``, required so #87 captures UNTRUNCATED bodies) it
    emits no inline ``body`` and instead a ``body_ref`` absolute path to the body on disk. This
    prefers the inline value and falls back to reading the file only when inline is absent.

    The bridge is a long-running server and ``body_ref`` arrives on an untrusted log event, so
    the file read is defensive: it reads only an existing regular file under
    :data:`_MAX_BODY_REF_BYTES`, and returns None (never raises) on any missing, non-file,
    oversized, or unreadable path so a bad event cannot crash the bridge.

    Args:
        record_attrs: The OTLP log-record attributes carrying ``body`` and/or ``body_ref``.

    Returns:
        The raw body text, or None when neither an inline body nor a readable file is present.
    """
    inline = _attr(record_attrs, "body")
    if inline:
        return inline
    ref = _attr(record_attrs, "body_ref")
    if not ref:
        return None
    try:
        if not os.path.isfile(ref) or os.path.getsize(ref) > _MAX_BODY_REF_BYTES:
            logger.warning("skipping body_ref (missing, non-file, or too large): %s", ref)
            return None
        # errors="replace": a non-UTF-8 byte must not raise UnicodeDecodeError (a ValueError,
        # not OSError) and abort the whole log batch; downstream JSON parsing is already tolerant.
        with open(ref, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        logger.warning("body_ref read failed %s: %s", ref, e)
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


def make_langfuse_create(host: str, auth: str) -> CreateFn:
    """Build a create function that POSTs an ingestion batch of CREATE events to Langfuse.

    Unlike :func:`make_langfuse_patch` (which updates an existing observation), this posts
    ``trace-create``/``event-create`` bodies that materialize new audit observations.

    Args:
        host: Base Langfuse URL, e.g. ``http://localhost:3000``.
        auth: The ``Authorization`` header value, ``Basic <base64(pk:sk)>``.

    Returns:
        A callable ``(batch) -> None`` posting the batch to the ingestion endpoint.
    """

    def create(batch: list[dict[str, Any]]) -> None:
        request = urllib.request.Request(
            f"{host}/api/public/ingestion",
            data=json.dumps({"batch": batch}).encode(),
            headers={"Authorization": auth, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                resp.read()
        except OSError as e:
            logger.warning("create failed (%d events): %s", len(batch), e)

    return create


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

    def __init__(self, patch: PatchFn, create: CreateFn = _noop_create) -> None:
        """Initialize the bridge.

        Args:
            patch: Sink applied to each resolved message patch; the production sink posts to
                Langfuse, tests pass a recording stub so no network is touched.
            create: Sink applied to each audit ingestion batch; defaults to a no-op so the
                message join works unchanged when the audit layer is not wired.
        """
        self._lock = threading.Lock()
        self._patch = patch
        self._create = create
        self._span_by_req: dict[str, str] = {}  # request_id -> span_id (hex)
        self._req_seq: list[tuple[int, str]] = []  # (api_request event.sequence, request_id)
        self._body_seqs: list[int] = []  # every api_request_body event.sequence ever seen
        self._pending: list[PendingItem] = []
        self._audit_traces: set[str] = set()  # spoke keys whose audit trace-create was sent
        # (event.sequence, tool_name, tool_use_id) of every tool_decision seen; kept across
        # flushes (like _body_seqs) as the shared join references for Pre/PostToolUse hooks.
        self._tool_decisions: list[tuple[int, str, str]] = []
        self._pending_hooks: list[BufferedHook] = []  # hooks awaiting their tool_use_id

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
                resource_attrs = (rl.get("resource") or {}).get("attributes", [])
                for sl in rl.get("scopeLogs", []):
                    for lr in sl.get("logRecords", []):
                        self._ingest_log(resource_attrs, lr.get("attributes", []))
            self._flush_hooks()
            self._try_flush()

    def _ingest_log(
        self, resource_attrs: list[dict[str, Any]], record_attrs: list[dict[str, Any]]
    ) -> None:
        event = _attr(record_attrs, "event.name")
        if event == "api_request":
            request_id, seq = (
                _attr(record_attrs, "request_id"),
                _attr(record_attrs, "event.sequence"),
            )
            if request_id and seq is not None:
                self._req_seq.append((int(seq), request_id))  # matched 1:1 to a body
            return
        if event not in ("api_request_body", "api_response_body"):
            self._ingest_audit(resource_attrs, record_attrs)  # the audit/lifecycle layer
            return
        raw = _read_body(record_attrs)
        if not raw:
            return
        if event == "api_request_body":
            seq = _attr(record_attrs, "event.sequence")
            if seq is None:
                return
            self._body_seqs.append(int(seq))  # kept past flush so the match stays stable
            self._pending.append(
                {"key": int(seq), "ktype": "seq", "field": "input", "value": _last_message(raw)}
            )
        else:  # api_response_body
            request_id = _attr(record_attrs, "request_id")
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

    def _ingest_audit(
        self, resource_attrs: list[dict[str, Any]], record_attrs: list[dict[str, Any]]
    ) -> None:
        """Create a Langfuse observation for one audit/lifecycle event, or drop it.

        The event is keyed onto a per-spoke synthetic audit trace by ``spoke_run_id``
        (resource attr, fallback ``session.id``); the trace's ``trace-create`` is emitted once
        per spoke, then the ``event-create`` for this event. Events with no audit mapping or
        no spoke key are dropped.

        Args:
            resource_attrs: The OTLP resource attributes (carry ``spoke_run_id``/``session.id``).
            record_attrs: The OTLP log-record attributes (carry ``event.name`` and the payload).
        """
        attrs = {**_attrs_dict(resource_attrs), **_attrs_dict(record_attrs)}
        trace_key = attrs.get("spoke_run_id") or attrs.get("session.id")
        if not trace_key:
            return
        self._note_tool_decision(attrs)
        event = build_audit_event(attrs, trace_key=trace_key)
        if event is None:
            return
        self._emit_audit(event, trace_key)
        self._buffer_hook(attrs, trace_key)

    def _emit_audit(self, event: dict[str, Any], trace_key: str) -> None:
        """Emit one audit ``event-create``, prefixed once per spoke by its ``trace-create``."""
        batch: list[dict[str, Any]] = []
        if trace_key not in self._audit_traces:
            self._audit_traces.add(trace_key)
            batch.append(trace_create(trace_key, event["timestamp"]))
        batch.append(event)
        self._create(batch)

    def _note_tool_decision(self, attrs: dict[str, str]) -> None:
        """Record a ``tool_decision``'s ``(sequence, tool_name, tool_use_id)`` join reference.

        Kept across flushes (like :attr:`_body_seqs` for the message join) so a Pre hook
        buffered before its decision still resolves once the decision arrives. A decision is a
        SHARED reference -- the same one anchors the PreToolUse hook before it and the
        PostToolUse hook after it -- so it is never consumed.
        """
        if attrs.get("event.name") != "tool_decision":
            return
        seq, tool, tuid = (
            attrs.get("event.sequence"),
            attrs.get("tool_name"),
            attrs.get("tool_use_id"),
        )
        if seq is not None and tool and tuid:
            self._tool_decisions.append((int(seq), tool, tuid))

    def _buffer_hook(self, attrs: dict[str, str], trace_key: str) -> None:
        """Buffer a Pre/PostToolUse hook so its tool_use_id resolves on a later flush.

        The hook has already been emitted at the root by the caller; buffering only defers the
        ``tool_use_id`` match. A hook naming no tool, or carrying no ``event.sequence`` to join
        on, is left at the root and not buffered.
        """
        hook = _tooluse_hook(attrs)
        seq = attrs.get("event.sequence")
        if hook is None or seq is None:
            return
        tool, direction = hook
        self._pending_hooks.append(
            {
                "attrs": attrs,
                "trace_key": trace_key,
                "seq": int(seq),
                "tool": tool,
                "direction": direction,
            }
        )

    def _resolve_hook_tuid(self, seq: int, tool: str, direction: str) -> str | None:
        """Return the ``tool_use_id`` of the nearest matching ``tool_decision``, or None.

        A Pre hook takes the nearest-FOLLOWING decision (smallest sequence greater than the
        hook's), a Post hook the nearest-PRECEDING (largest sequence less than the hook's);
        both require an exact ``tool_name`` match, so a hook never binds to a different tool
        *type*. Like the body join, the attribution is purely temporal: two calls of the SAME
        tool are told apart only by sequence, so if one of them emits no ``tool_decision``
        (e.g. an aborted call) the surviving decision is the nearest match for both hooks and
        one can bind to the adjacent same-tool call. There is no per-call id on the hook event
        to resolve this, exactly as for the interleaved-agent body case.
        """
        matches = [(s, tuid) for (s, t, tuid) in self._tool_decisions if t == tool]
        if direction == "pre":
            following = [m for m in matches if m[0] > seq]
            return min(following)[1] if following else None
        preceding = [m for m in matches if m[0] < seq]
        return max(preceding)[1] if preceding else None

    def _flush_hooks(self) -> None:
        """Re-emit each buffered hook whose ``tool_use_id`` now resolves; drop it once enriched.

        Recomputed every flush so a decision and the hook it anchors may arrive in either
        order. An unresolved hook stays buffered -- it was already emitted once at the root, so
        it is never dropped.
        """
        batch: list[dict[str, Any]] = []
        still: list[BufferedHook] = []
        for hook in self._pending_hooks:
            tuid = self._resolve_hook_tuid(hook["seq"], hook["tool"], hook["direction"])
            if tuid is None:
                still.append(hook)
                continue
            enriched = build_audit_event(
                {**hook["attrs"], "tool_use_id": tuid}, trace_key=hook["trace_key"]
            )
            if enriched is not None:
                batch.append(enriched)
        self._pending_hooks[:] = still
        if batch:
            self._create(batch)

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

    bridge = Bridge(make_langfuse_patch(host, auth), make_langfuse_create(host, auth))
    logger.info("listening on :%d -> %s", port, host)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(bridge)).serve_forever()


if __name__ == "__main__":
    main()
