#!/usr/bin/env python3
"""Assemble a spoke's existing rich Langfuse observations into one nested trace.

Natively, each turn Claude Code runs lands as its own flat Langfuse trace, and the
marker (``step:``/``lifecycle:``/``spoke-push``) and hook (``*.sh``) emissions land as
yet more flat traces. Every one of those observations already carries the rich fields we
built — ``usageDetails``, ``costDetails``, ``input``/``output`` messages, ``metadata``
(including ``rollup`` and, on hooks, ``hook_event``/``tool_name``/``tool_use_id``/
``decision``/``duration_ms``), ``name``, ``type``, and ``startTime``/``endTime``. A spoke
therefore reads as dozens of disconnected traces.

This post-run script SOURCES FROM LANGFUSE — it does not rebuild from the causal store.
It fetches every trace in the session and every observation in those traces, then COPIES
each observation verbatim into ONE new trace, re-parenting across the original trace
boundaries so the whole spoke renders as a single tree with every field intact::

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \\
        python3 scripts/telemetry/langfuse_spoke_tree.py <spoke_run_id>

Re-parenting rules for each source observation:

- It had a ``parentObservationId`` -> the copy points at the copy of that parent.
- It was a trace-root interaction / marker / lifecycle / script -> the synthetic root.
- It was a trace-root hook (name ends ``.sh`` or ``metadata.attributes.workflow.kind ==
  hook``) -> the copy of the tool whose ``tool_use_id`` matches the hook's
  ``metadata.attributes.tool_use_id``; or the synthetic root when there is no id or no
  match. (Langfuse nests OTel span attributes under ``metadata["attributes"]``.)

All ids derive from the spoke run id and the source ``(trace_id, observation_id)`` pair,
so a rerun overwrites the same trace/observations instead of appending. This trace
DUPLICATES the native per-turn traces by design — it is the assembled, nested view.

Import-safe: no environment is read at import time, so :func:`build_batch` is unit-testable
with no network. The HTTP I/O happens only in :func:`main`. Stdlib only; reuses the
fetch/post helpers, env vars, and ingestion endpoint of ``langfuse_rollup``.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import urllib.parse
from typing import Any

from telemetry.langfuse_rollup import (
    GetFn,
    Observation,
    PostFn,
    all_observations,
    make_get,
    make_post,
)

logger = logging.getLogger("langfuse_spoke_tree")

IngestEvent = dict[str, Any]
# One source trace paired with all of its observations: ``(orig_trace_id, observations)``.
TraceObservations = tuple[str, list[Observation]]

# Deterministic id prefixes — a rerun resolves to the same trace/observation ids.
_TRACE_PREFIX = "spoketree-"
_ROOT_PREFIX = "spokeroot-"
_COPY_PREFIX = "tree-"
_TRACE_NAME_PREFIX = "spoke-tree:"
_ROOT_NAME_PREFIX = "spoke:"

# Langfuse ingestion requires a timestamp on every event; used for the trace/root and as a
# fallback when a source observation carries no ``startTime``. Fixed so reruns stay stable.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Max page size the Langfuse traces endpoint accepts.
_PAGE_LIMIT = 100
# Max ingestion events per POST, to keep each request small.
_CHUNK_SIZE = 100

# Observation fields copied verbatim into the assembled trace when present.
_COPIED_FIELDS = ("input", "output", "usageDetails", "costDetails", "metadata", "model", "level")
# Metadata keys that may carry a tool-call id, in priority order.
_TOOL_USE_ID_KEYS = ("tool_use_id", "gen_ai.tool.call.id")


def trace_id_for(spoke_run_id: str) -> str:
    """Return the deterministic trace id for a spoke's assembled tree.

    Args:
        spoke_run_id: The spoke run identifier.

    Returns:
        A stable ``spoketree-<sha1[:16]>`` id, identical across reruns.
    """
    return _TRACE_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def root_id_for(spoke_run_id: str) -> str:
    """Return the deterministic id of the synthetic root span for a spoke."""
    return _ROOT_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def _copy_id(orig_trace_id: str, orig_obs_id: str) -> str:
    """Return the deterministic copy id for a source observation in the assembled trace."""
    digest = hashlib.sha1(f"{orig_trace_id}:{orig_obs_id}".encode()).hexdigest()[:24]
    return _COPY_PREFIX + digest


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


def _build_tool_index(traces: list[TraceObservations]) -> dict[str, str]:
    """Map each tool-call id to the copy id of the (non-hook) observation that owns it.

    Hooks are skipped so a hook never indexes its own ``tool_use_id``; the surviving owner
    is the tool observation, which is the re-parent target for matching hooks.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        A mapping of ``tool_use_id`` to the assembled-trace copy id of its tool.
    """
    index: dict[str, str] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            if _is_hook(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid:
                index[tuid] = _copy_id(orig_trace_id, observation["id"])
    return index


def _resolve_parent(
    observation: Observation, *, orig_trace_id: str, root_id: str, tool_index: dict[str, str]
) -> str:
    """Resolve the assembled-trace parent id for one source observation.

    Args:
        observation: The source observation.
        orig_trace_id: The id of the trace the observation came from.
        root_id: The synthetic root span id (the single collapsed root).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.

    Returns:
        The copy id of the intra-trace parent, the matching tool, or the synthetic root.
    """
    parent = observation.get("parentObservationId")
    if parent:
        return _copy_id(orig_trace_id, parent)
    if _is_hook(observation):
        tuid = _tool_use_id(observation)
        if tuid and tuid in tool_index:
            return tool_index[tuid]
    return root_id


def _copy_event(
    observation: Observation, *, orig_trace_id: str, trace_id: str, parent_id: str
) -> IngestEvent:
    """Shape one ingestion event copying a source observation into the assembled trace.

    The type tracks the source: a ``GENERATION`` becomes a ``generation-create``, anything
    else a ``span-create``. ``usageDetails`` and ``model`` are re-passed so Langfuse
    recomputes ``costDetails`` identically; an explicit ``costDetails`` is forwarded too.

    Args:
        observation: The source observation to copy.
        orig_trace_id: The id of the trace the observation came from.
        trace_id: The assembled trace id every copy references.
        parent_id: The resolved parent id for this copy.

    Returns:
        A Langfuse ingestion batch event recreating the observation.
    """
    new_id = _copy_id(orig_trace_id, observation["id"])
    obs_type = observation.get("type") or "SPAN"
    event_type = "generation-create" if obs_type == "GENERATION" else "span-create"
    start = observation.get("startTime") or _INGEST_TIMESTAMP
    body: dict[str, Any] = {
        "id": new_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": observation.get("name"),
        "startTime": observation.get("startTime"),
        "endTime": observation.get("endTime"),
    }
    for field in _COPIED_FIELDS:
        if observation.get(field) is not None:
            body[field] = observation[field]
    return {"id": new_id, "type": event_type, "timestamp": start, "body": body}


def _earliest_start(traces: list[TraceObservations]) -> str:
    """Return the earliest ISO ``startTime`` across all observations, or the fixed base."""
    starts = [
        observation["startTime"]
        for _, observations in traces
        for observation in observations
        if observation.get("startTime")
    ]
    return min(starts) if starts else _INGEST_TIMESTAMP


def build_batch(traces: list[TraceObservations], spoke_run_id: str) -> list[IngestEvent]:
    """Assemble one nested trace from a spoke's source traces and their observations.

    Emits a ``trace-create``, a single synthetic root span, and one copy per source
    observation re-parented across the original trace boundaries (see module docstring).
    All ids derive from the spoke run id and the source ``(trace_id, observation_id)``
    pair, so the batch is idempotent.

    Args:
        traces: Each source trace paired with all of its observations, as fetched from
            Langfuse with full fields.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).

    Returns:
        The ingestion events: a ``trace-create``, the synthetic root, then the copies.
    """
    trace_id = trace_id_for(spoke_run_id)
    root_id = root_id_for(spoke_run_id)
    base_ts = _earliest_start(traces)
    trace_event: IngestEvent = {
        "id": trace_id,
        "type": "trace-create",
        "timestamp": base_ts,
        "body": {
            "id": trace_id,
            "name": _TRACE_NAME_PREFIX + spoke_run_id,
            "sessionId": spoke_run_id,
            "timestamp": base_ts,
        },
    }
    root_event: IngestEvent = {
        "id": root_id,
        "type": "span-create",
        "timestamp": base_ts,
        "body": {
            "id": root_id,
            "traceId": trace_id,
            "name": _ROOT_NAME_PREFIX + spoke_run_id,
            "startTime": base_ts,
        },
    }
    tool_index = _build_tool_index(traces)
    copies: list[IngestEvent] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            parent_id = _resolve_parent(
                observation, orig_trace_id=orig_trace_id, root_id=root_id, tool_index=tool_index
            )
            copies.append(
                _copy_event(
                    observation, orig_trace_id=orig_trace_id, trace_id=trace_id, parent_id=parent_id
                )
            )
    return [trace_event, root_event, *copies]


def all_traces(spoke_run_id: str, get: GetFn) -> list[dict[str, Any]]:
    """Fetch every trace in a session, walking all pages.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to fetch.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        The session's traces across all pages, in fetch order.
    """
    session = urllib.parse.quote(spoke_run_id)
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = get(f"/traces?sessionId={session}&limit={_PAGE_LIMIT}&page={page}")
        out.extend(resp.get("data") or [])
        total_pages = (resp.get("meta") or {}).get("totalPages") or 1
        if page >= total_pages:
            break
        page += 1
    return out


def fetch_session(spoke_run_id: str, get: GetFn) -> list[TraceObservations]:
    """Fetch every trace in a session paired with all of its observations.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to fetch.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        Each trace id paired with its observations (full fields), in fetch order.
    """
    traces = all_traces(spoke_run_id, get)
    return [(trace["id"], all_observations(trace["id"], get)) for trace in traces]


def post_in_chunks(
    batch: list[IngestEvent], post: PostFn, *, chunk_size: int = _CHUNK_SIZE
) -> None:
    """POST an ingestion batch in fixed-size chunks.

    Args:
        batch: The full ingestion batch.
        post: Ingestion batch sink (see :data:`telemetry.langfuse_rollup.PostFn`).
        chunk_size: Maximum events per request.
    """
    for start in range(0, len(batch), chunk_size):
        post(batch[start : start + chunk_size])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the spoke-tree assembler."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id (session id) to assemble.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Assemble a spoke's rich Langfuse observations into one nested trace.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[spoke-tree] %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    get, post = make_get(host, auth), make_post(host, auth)

    traces = fetch_session(args.spoke_run_id, get)
    batch = build_batch(traces, args.spoke_run_id)
    post_in_chunks(batch, post)

    trace_id = trace_id_for(args.spoke_run_id)
    print(f"{len(batch) - 2} observations assembled under trace {trace_id}, roots collapsed to 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
