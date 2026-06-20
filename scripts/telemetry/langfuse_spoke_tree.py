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

Tool content from the transcript: Claude Code's native OTel surfaces the full
``full_command`` for Bash, but every other tool (TaskCreate/TaskUpdate, Read, Edit, ...)
arrives with ``input=None`` — only ``tool_name``/``tool_use_id``/``duration``. The real
content lives in the session TRANSCRIPT (``*.jsonl``): each assistant ``tool_use`` block
carries ``{id, name, input}`` and the matching user ``tool_result`` block carries
``{tool_use_id, content}``. Because the copy step CREATES fresh observations (one
``*-create`` event setting every field at once), it fills that content into the create
body at build time, keyed by ``tool_use_id`` — non-destructively, so collector-provided
input (Bash) is never overwritten. (A standalone UPDATE-based patcher used to do this, but
an update body that omits ``name``/``type`` makes Langfuse CLEAR them, so it was retired.)

Beyond copying, the build adds two CREATE-only enrichments (no patches):

- **Per-container token rollups.** Every container node (each interaction, ``tool:Agent``,
  sub-agent, and the synthetic root) gets ``metadata.rollup = {reused, written, input,
  output}`` summed over its subtree of the re-parented tree, reusing
  ``langfuse_rollup``'s sum logic but written into the create body.
- **An itemized loaded-context subtree.** A ``loaded-context`` node under the root with one
  category node per group and one item node per name (token size + cost). The primary
  source is the spoke's untruncated raw request body
  (``OTEL_LOG_RAW_API_BODIES=file:<dir>``, located via ``--request-bodies`` /
  ``$AI_TOOLKIT_OTEL_BODY_DIR``): ``request_body`` itemizes the WHOLE first-call prefix —
  every tool and MCP tool by name + exact size, each system block, and each
  ``messages[0]`` ``<system-reminder>`` by kind — so no reconciliation is needed. When no
  request body is available, it falls back to disk measurement of rules / memory / skills /
  sub-agents / environment (via ``measure_context_cost``) plus a single reconciled
  ``remainder`` node (``prefix - Σ measured disk``, clamped ≥ 0) absorbing the base system
  prompt, tool schemas, and MCP together — the full prefix being
  ``cache_read + cache_creation`` of the first LLM call.

Import-safe: no environment is read at import time, so :func:`build_batch`,
:func:`scan_transcripts`, and :func:`build_loaded_context_events` are unit-testable with no
network. The HTTP I/O happens only in :func:`main`. Stdlib only; reuses the fetch/post
helpers, env vars, and ingestion endpoint of ``langfuse_rollup``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, NamedTuple, cast

from telemetry.langfuse_rollup import (
    GetFn,
    Observation,
    PostFn,
    all_observations,
    build_tree,
    make_get,
    make_post,
    subtree_totals,
)
from telemetry.measure_context_cost import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    TokenCounter,
    assemble_items,
    make_counter,
    measure_items,
)
from telemetry.request_body import (
    first_real_request,
    measure_request_items,
    parse_request_body,
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

# Tool content (e.g. a large file Read) can be huge; cap the serialized text past this.
_MAX_CONTENT_CHARS = 20_000
_TRUNCATION_MARKER = "...[truncated]"

# Default root holding Claude Code session transcripts.
_DEFAULT_PROJECTS = Path("~/.claude/projects").expanduser()

# Deterministic id prefix for the synthetic loaded-context subtree nodes.
_LC_PREFIX = "tree-lc-"
# Source label for the single reconciled remainder used in the disk fallback path.
_REMAINDER_SOURCE = "reconciled-remainder"
# Note recorded on the disk-fallback remainder node, naming what it absorbs.
_REMAINDER_NOTE = "no request body; base system + tools + mcp reconciled as one"
# Default cache-creation price (USD per token), Opus tier — mirrors measure_context_cost.
_DEFAULT_PRICE = 0.00000625
# Category order for the request-body itemization (the primary, fully-itemized path).
_REQUEST_CATEGORY_ORDER = ("tools", "mcp", "system", "context")
# Category order for the disk fallback used when no request body is available.
_DISK_CATEGORY_ORDER = ("rules", "memory", "skills", "sub-agents", "environment")
# Env var naming the per-spoke dir of OTEL_LOG_RAW_API_BODIES=file:<dir> dumps.
_BODY_DIR_ENV = "AI_TOOLKIT_OTEL_BODY_DIR"


class ToolContent(NamedTuple):
    """The transcript-sourced content of one tool call (either field may be absent)."""

    input: object | None  # the tool_use input args
    output: object | None  # the tool_result content


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


def _is_tool_span(observation: Observation) -> bool:
    """Whether an observation is a visible ``tool:<Name>`` span (e.g. ``tool:TaskCreate``).

    Only these spans carry the tool call the user sees; the ``claude_code.tool.execution``,
    ``*.blocked_on_user``, and ``*.sh`` hook siblings that share a ``tool_use_id`` are not
    tool spans and are never filled with transcript content.
    """
    return (observation.get("name") or "").startswith("tool:")


def _tool_span_ids(traces: list[TraceObservations]) -> set[str]:
    """Collect the tool-call ids of every visible ``tool:`` span across the source traces."""
    ids: set[str] = set()
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_tool_span(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid:
                ids.add(tuid)
    return ids


def _capped(value: object) -> object:
    """Return ``value`` unchanged, or a truncated string when its serialized form is large.

    Small structured values are passed through so Langfuse renders them richly; only content
    whose serialized text exceeds :data:`_MAX_CONTENT_CHARS` (e.g. a large file Read) is
    flattened to a truncated string with a marker.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + _TRUNCATION_MARKER
    return value


def _tool_additions(
    observation: Observation, tool_content: dict[str, ToolContent]
) -> dict[str, Any]:
    """Return the input/output to graft onto a tool span's create body, empty when none.

    Only a visible ``tool:`` span with a matching transcript entry contributes, and only for
    a field the source span does not already carry — so collector-provided content (Bash's
    ``input``) is never overwritten and non-tool spans are untouched. Oversized values are
    truncated by :func:`_capped`.

    Args:
        observation: The source observation being copied.
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`.

    Returns:
        A mapping with ``input`` and/or ``output`` to merge into the body, or ``{}``.
    """
    if not _is_tool_span(observation):
        return {}
    content = tool_content.get(_tool_use_id(observation) or "")
    if content is None:
        return {}
    additions: dict[str, Any] = {}
    if not observation.get("input") and content.input is not None:
        additions["input"] = _capped(content.input)
    if not observation.get("output") and content.output is not None:
        additions["output"] = _capped(content.output)
    return additions


def _copy_event(
    observation: Observation,
    *,
    orig_trace_id: str,
    trace_id: str,
    parent_id: str,
    tool_content: dict[str, ToolContent],
) -> IngestEvent:
    """Shape one ingestion event copying a source observation into the assembled trace.

    The type tracks the source: a ``GENERATION`` becomes a ``generation-create``, anything
    else a ``span-create``. ``usageDetails`` and ``model`` are re-passed so Langfuse
    recomputes ``costDetails`` identically; an explicit ``costDetails`` is forwarded too.
    For a visible ``tool:`` span, transcript-sourced ``input``/``output`` is grafted into the
    create body (see :func:`_tool_additions`) so the fresh observation carries content the
    native span lacked, set in the same create event that fixes its name and type.

    Args:
        observation: The source observation to copy.
        orig_trace_id: The id of the trace the observation came from.
        trace_id: The assembled trace id every copy references.
        parent_id: The resolved parent id for this copy.
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`.

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
    body.update(_tool_additions(observation, tool_content))
    return {"id": new_id, "type": event_type, "timestamp": start, "body": body}


def _apply_container_rollups(events: list[IngestEvent]) -> None:
    """Set ``metadata.rollup`` on every container node of the assembled tree, in place.

    A container is any node with children once the tree is re-parented (the synthetic
    root, each ``interaction`` / ``tool:Agent`` / sub-agent). Its rollup is the subtree
    sum of the four usage components over itself and all descendants, computed from the
    create-body shapes (``id`` / ``parentObservationId`` / ``usageDetails``) — the same
    sum logic as :mod:`telemetry.langfuse_rollup`, but written into the create body
    rather than patched. Leaves (tools, single generations) are left untouched.

    Args:
        events: The assembled ingestion events; only ``*-create`` span/generation bodies
            participate (the ``trace-create`` is skipped). Mutated in place.
    """
    nodes = [event["body"] for event in events if event["type"] != "trace-create"]
    by_id, children = build_tree(nodes)
    for body in nodes:
        if not children.get(body["id"]):
            continue  # only containers (those with children) carry a rollup
        totals = subtree_totals(body["id"], by_id, children)
        body.setdefault("metadata", {})["rollup"] = {
            "reused": totals["cache_read_input_tokens"],
            "written": totals["cache_creation_input_tokens"],
            "input": totals["input"],
            "output": totals["output"],
        }


def _earliest_start(traces: list[TraceObservations]) -> str:
    """Return the earliest ISO ``startTime`` across all observations, or the fixed base."""
    starts = [
        observation["startTime"]
        for _, observations in traces
        for observation in observations
        if observation.get("startTime")
    ]
    return min(starts) if starts else _INGEST_TIMESTAMP


def build_batch(
    traces: list[TraceObservations],
    spoke_run_id: str,
    tool_content: dict[str, ToolContent] | None = None,
) -> list[IngestEvent]:
    """Assemble one nested trace from a spoke's source traces and their observations.

    Emits a ``trace-create``, a single synthetic root span, and one copy per source
    observation re-parented across the original trace boundaries (see module docstring).
    All ids derive from the spoke run id and the source ``(trace_id, observation_id)``
    pair, so the batch is idempotent. Visible ``tool:`` spans additionally have their
    transcript-sourced ``input``/``output`` grafted into the create body (see
    :func:`_tool_additions`).

    Args:
        traces: Each source trace paired with all of its observations, as fetched from
            Langfuse with full fields.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`;
            defaults to empty (no tool content filled).

    Returns:
        The ingestion events: a ``trace-create``, the synthetic root, then the copies.
    """
    tool_content = tool_content or {}
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
                    observation,
                    orig_trace_id=orig_trace_id,
                    trace_id=trace_id,
                    parent_id=parent_id,
                    tool_content=tool_content,
                )
            )
    events = [trace_event, root_event, *copies]
    _apply_container_rollups(events)
    return events


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


def _is_own_output(trace: dict[str, Any], target_trace_id: str) -> bool:
    """Whether a fetched session trace is this synthesizer's own assembled output.

    The assembled trace carries ``sessionId == spoke_run_id``, so on a re-run it reappears in
    the session listing; sourcing it would copy its spans again and multiply the tree. It is
    recognised by its deterministic id or, defensively for older ids, its ``spoke-tree:`` name.

    Args:
        trace: A trace dict as returned by the Langfuse traces endpoint.
        target_trace_id: The deterministic id of this spoke's assembled tree.

    Returns:
        True when the trace is the synthesizer's own output and must be excluded.
    """
    if trace.get("id") == target_trace_id:
        return True
    name = trace.get("name") or ""
    return name.startswith(_TRACE_NAME_PREFIX)


def fetch_session(spoke_run_id: str, get: GetFn) -> list[TraceObservations]:
    """Fetch every native trace in a session paired with all of its observations.

    The synthesizer's own prior output is excluded so re-runs stay idempotent (see
    :func:`_is_own_output`); only the real native traces are sourced, and the deterministic
    ids then overwrite the assembled trace cleanly instead of multiplying it.

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to fetch.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        Each native trace id paired with its observations (full fields), in fetch order.
    """
    target_trace_id = trace_id_for(spoke_run_id)
    traces = [
        trace
        for trace in all_traces(spoke_run_id, get)
        if not _is_own_output(trace, target_trace_id)
    ]
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


def _scan_blocks(content: list[Any], wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Collect ``tool_use`` inputs and ``tool_result`` contents for the wanted ids.

    Args:
        content: A message's ``content`` block list from one transcript line.
        wanted: The tool-call ids present on this spoke's tool spans (others are skipped).
        found: Accumulator mapping a tool-call id to its ``{"input"/"output": value}``.
    """
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("id") in wanted:
            found.setdefault(block["id"], {})["input"] = block.get("input")
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in wanted:
            found.setdefault(block["tool_use_id"], {})["output"] = block.get("content")


def _scan_file(path: Path, wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Scan one transcript file line by line, ignoring malformed lines.

    Args:
        path: The transcript ``*.jsonl`` file.
        wanted: The tool-call ids present on this spoke's tool spans.
        found: Accumulator passed through to :func:`_scan_blocks`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("cannot read transcript %s: %s", path, e)
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            _scan_blocks(content, wanted, found)


def scan_transcripts(root: Path, wanted: set[str]) -> dict[str, ToolContent]:
    """Scan every transcript under ``root`` for the wanted tool calls' input/output.

    Tool-call ids are globally unique, so no per-session transcript mapping is needed — only
    the ids on this spoke's tool spans are collected. An id is returned when the transcripts
    carry an ``input`` block for it, an ``output`` block, or both.

    Args:
        root: The Claude Code projects root holding session ``*.jsonl`` transcripts.
        wanted: The tool-call ids present on this spoke's tool spans.

    Returns:
        A mapping of ``tool_use_id`` to its :class:`ToolContent`.
    """
    found: dict[str, dict[str, object]] = {}
    if not wanted:
        return {}
    for path in sorted(root.rglob("*.jsonl")):
        _scan_file(path, wanted, found)
    return {
        tuid: ToolContent(parts.get("input"), parts.get("output")) for tuid, parts in found.items()
    }


def filled_tool_spans(traces: list[TraceObservations], tool_content: dict[str, ToolContent]) -> int:
    """Count the tool spans whose create body would gain transcript content (see summary)."""
    return sum(
        bool(_tool_additions(observation, tool_content))
        for _orig_trace_id, observations in traces
        for observation in observations
    )


def prefix_total(traces: list[TraceObservations]) -> int:
    """Return the full session prefix size from the first LLM call's token usage.

    Claude Code writes the whole session prefix (rules, skills, tools, base system
    prompt, ...) to the prompt cache on the first call. A cold cache writes it all as
    ``cache_creation``; a warm one splits it into ``cache_read`` + ``cache_creation``.
    The prefix total is therefore their SUM on the earliest observation carrying usage
    (chosen by ``startTime``) — ``cache_creation`` alone undercounts a warm session to
    near zero. That total is the figure the loaded-context items reconcile against.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        The first call's ``cache_read + cache_creation`` token total, or 0 when no usage
        is present.
    """
    best_start: str | None = None
    best_value = 0
    for _orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            read = usage.get("cache_read_input_tokens")
            written = usage.get("cache_creation_input_tokens")
            if read is None and written is None:
                continue
            start = observation.get("startTime") or ""
            if best_start is None or start < best_start:
                best_start = start
                best_value = int(read or 0) + int(written or 0)
    return best_value


def _lc_id(spoke_run_id: str, key: str) -> str:
    """Return the deterministic id of one loaded-context node for a spoke."""
    digest = hashlib.sha1(f"{spoke_run_id}:{key}".encode()).hexdigest()[:24]
    return _LC_PREFIX + digest


def _human_tokens(tokens: int) -> str:
    """Render a token count compactly for a node label (e.g. ``3.2k``)."""
    return f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)


def _lc_node(
    *, node_id: str, parent_id: str, trace_id: str, name: str, base_ts: str, metadata: dict
) -> IngestEvent:
    """Shape one loaded-context span-create event."""
    return {
        "id": node_id,
        "type": "span-create",
        "timestamp": base_ts,
        "body": {
            "id": node_id,
            "traceId": trace_id,
            "parentObservationId": parent_id,
            "name": name,
            "startTime": base_ts,
            "metadata": metadata,
        },
    }


def build_loaded_context_events(
    spoke_run_id: str,
    item_rows: list[dict[str, object]],
    *,
    category_order: tuple[str, ...],
    base_ts: str,
    prefix_total: int | None = None,
    price: float | None = None,
) -> list[IngestEvent]:
    """Build the itemized loaded-context subtree under the spoke root.

    Emits a ``loaded-context`` parent under the synthetic root, one category node per
    group (in ``category_order``) carrying its rolled-up total, and one item node per name
    (token size + cost + source, plus a ``cached`` flag when the row carries one).

    The primary, request-body path itemizes the WHOLE first-call prefix — every tool / MCP
    tool / system block / reminder by name and exact size — so it needs no reconciliation;
    ``prefix_total`` is then left None. The disk fallback (no request body) can only measure
    the on-disk categories, so it passes ``prefix_total`` and ``price`` to append a single
    ``remainder`` node = ``prefix_total - Σ measured`` (clamped ≥ 0) absorbing the base
    system prompt, all tool schemas, and MCP together.

    All ids derive from the spoke run id so a rerun overwrites the same nodes.

    Args:
        spoke_run_id: The spoke run identifier.
        item_rows: Per-name measured rows (from :func:`measure_request_items` or
            :func:`measure_items`), each with ``category``, ``name``, ``tokens``,
            ``cost_usd``, ``source``, ``estimated`` (and optionally ``cached``).
        category_order: The category keys to render, in display order; empties are dropped.
        base_ts: ISO timestamp stamped on every synthetic node.
        prefix_total: The first-call ``cache_read + cache_creation`` total; pass it (with
            ``price``) only on the disk fallback to append the reconciled remainder node.
        price: Cache-creation price in USD per token, for the remainder node's cost.

    Returns:
        The loaded-context ingestion events: parent, categories, items, and — on the disk
        fallback only — the reconciled remainder.
    """
    trace_id = trace_id_for(spoke_run_id)
    root_id = root_id_for(spoke_run_id)
    lc_id = _lc_id(spoke_run_id, "loaded-context")

    measured_tokens = sum(int(cast(int, row["tokens"])) for row in item_rows)
    measured_cost = sum(float(cast(float, row["cost_usd"])) for row in item_rows)
    events = [
        _lc_node(
            node_id=lc_id,
            parent_id=root_id,
            trace_id=trace_id,
            name="loaded-context",
            base_ts=base_ts,
            metadata={"tokens": measured_tokens, "cost_usd": measured_cost},
        )
    ]
    for category, rows in _group_rows_by_category(item_rows, category_order):
        cat_id = _lc_id(spoke_run_id, category)
        events.append(
            _lc_node(
                node_id=cat_id,
                parent_id=lc_id,
                trace_id=trace_id,
                name=category,
                base_ts=base_ts,
                metadata={
                    "tokens": sum(int(cast(int, r["tokens"])) for r in rows),
                    "cost_usd": sum(float(cast(float, r["cost_usd"])) for r in rows),
                },
            )
        )
        events.extend(
            _lc_item_node(spoke_run_id, category, cat_id, trace_id, base_ts, row) for row in rows
        )
    if prefix_total is not None and price is not None:
        events.append(
            _remainder_node(
                spoke_run_id, lc_id, trace_id, base_ts, prefix_total, measured_tokens, price
            )
        )
    return events


def _group_rows_by_category(
    item_rows: list[dict[str, object]], category_order: tuple[str, ...]
) -> list[tuple[str, list[dict[str, object]]]]:
    """Group item rows by category in ``category_order``, dropping empty groups."""
    groups: list[tuple[str, list[dict[str, object]]]] = []
    for category in category_order:
        rows = [row for row in item_rows if row["category"] == category]
        if rows:
            groups.append((category, rows))
    return groups


def _lc_item_node(
    spoke_run_id: str,
    category: str,
    cat_id: str,
    trace_id: str,
    base_ts: str,
    row: dict[str, object],
) -> IngestEvent:
    """Shape one per-name item node with its token size, cost, source, and cache flag."""
    tokens = int(cast(int, row["tokens"]))
    metadata: dict[str, object] = {
        "tokens": tokens,
        "cost_usd": row["cost_usd"],
        "source": row["source"],
    }
    if row.get("estimated"):
        metadata["estimated"] = True
    if "cached" in row:
        metadata["cached"] = bool(row["cached"])
    return _lc_node(
        node_id=_lc_id(spoke_run_id, f"{category}/{row['name']}"),
        parent_id=cat_id,
        trace_id=trace_id,
        name=f"{row['name']}: {_human_tokens(tokens)}",
        base_ts=base_ts,
        metadata=metadata,
    )


def _remainder_node(
    spoke_run_id: str,
    lc_id: str,
    trace_id: str,
    base_ts: str,
    prefix: int,
    measured: int,
    price: float,
) -> IngestEvent:
    """Shape the single fallback remainder node used when no floor was calibrated."""
    tokens = max(0, prefix - measured)
    return _lc_node(
        node_id=_lc_id(spoke_run_id, "remainder"),
        parent_id=lc_id,
        trace_id=trace_id,
        name=f"remainder: {_human_tokens(tokens)}",
        base_ts=base_ts,
        metadata={
            "tokens": tokens,
            "cost_usd": tokens * price,
            "source": _REMAINDER_SOURCE,
            "note": _REMAINDER_NOTE,
        },
    )


def loaded_context_rows(
    root: Path, *, counter: TokenCounter, price: float
) -> list[dict[str, object]]:
    """Measure the disk-sourceable loaded-context entries, one row per name.

    Only the on-disk categories (rules / memory / skills / sub-agents / environment) are
    itemized; the built-in tool and MCP schemas are NOT on disk and are not obtainable
    per-tool, so they are reconciled in aggregate by :func:`build_loaded_context_events`.

    Args:
        root: Worktree root for the disk-measurable items.
        counter: Token counter; raises ``CountTokensError`` when unreachable.
        price: Cache-creation price in USD per token.

    Returns:
        The per-name rows for rules / memory / skills / sub-agents / environment.
    """
    return measure_items(assemble_items(root), counter=counter, price=price)


def find_request_files(bodies_dir: Path) -> list[Path]:
    """Return the ``*.request.json`` dumps in ``bodies_dir``, sorted by name (emission order)."""
    return sorted(bodies_dir.glob("*.request.json")) if bodies_dir.is_dir() else []


def request_context_rows(
    bodies_dir: Path, *, counter: TokenCounter, price: float
) -> list[dict[str, object]] | None:
    """Itemize the loaded context from the first real raw request body in ``bodies_dir``.

    Picks the first ``.request.json`` whose ``tools`` array is non-empty (skipping any
    degenerate aux call), parses it, and measures every tool / MCP tool / system block /
    reminder by name and exact size. This is the primary, fully-itemized path.

    Args:
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.
        counter: Token counter; raises ``CountTokensError`` when unreachable.
        price: Cache-creation price in USD per token.

    Returns:
        The per-name request-body rows, or None when no real request body is found (the
        caller then falls back to disk measurement).
    """
    path = first_real_request(find_request_files(bodies_dir))
    if path is None:
        return None
    parsed = parse_request_body(path)
    return measure_request_items(parsed.items, counter=counter, price=price)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the spoke-tree assembler."""
    env = os.environ
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id (session id) to assemble.")
    parser.add_argument(
        "--projects",
        type=Path,
        default=_DEFAULT_PROJECTS,
        help="Root holding Claude Code session transcripts (default: ~/.claude/projects).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Worktree root for the disk-measurable loaded-context items (default: cwd).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id for count_tokens.")
    parser.add_argument(
        "--request-bodies",
        type=Path,
        default=Path(env[_BODY_DIR_ENV]) if env.get(_BODY_DIR_ENV) else None,
        help=(
            "Dir of OTEL_LOG_RAW_API_BODIES=file:<dir> request dumps to itemize the loaded "
            f"context from (default: ${_BODY_DIR_ENV}). Falls back to disk when absent."
        ),
    )
    parser.add_argument(
        "--endpoint",
        default=env.get("ANTHROPIC_BASE_URL", DEFAULT_ENDPOINT),
        help="Anthropic API base URL for count_tokens.",
    )
    parser.add_argument(
        "--api-key", default=env.get("ANTHROPIC_API_KEY"), help="Anthropic API key."
    )
    parser.add_argument(
        "--price", type=float, default=_DEFAULT_PRICE, help="Cache-creation USD per token."
    )
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
    tool_content = scan_transcripts(args.projects, _tool_span_ids(traces))
    batch = build_batch(traces, args.spoke_run_id, tool_content)

    counter = make_counter(endpoint=args.endpoint, api_key=args.api_key, model=args.model)
    base_ts = _earliest_start(traces)
    request_rows = (
        request_context_rows(args.request_bodies, counter=counter, price=args.price)
        if args.request_bodies is not None
        else None
    )
    if request_rows is not None:
        rows, source = request_rows, "request body"
        context_events = build_loaded_context_events(
            args.spoke_run_id, rows, category_order=_REQUEST_CATEGORY_ORDER, base_ts=base_ts
        )
    else:
        rows, source = (
            loaded_context_rows(args.root.resolve(), counter=counter, price=args.price),
            "disk",
        )
        context_events = build_loaded_context_events(
            args.spoke_run_id,
            rows,
            category_order=_DISK_CATEGORY_ORDER,
            base_ts=base_ts,
            prefix_total=prefix_total(traces),
            price=args.price,
        )
    post_in_chunks(batch + context_events, post)

    trace_id = trace_id_for(args.spoke_run_id)
    filled = filled_tool_spans(traces, tool_content)
    print(
        f"{len(batch) - 2} observations assembled under trace {trace_id} "
        f"(roots collapsed to 1), {filled} tool spans filled from transcript, "
        f"{len(rows)} loaded-context items itemized (source: {source})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
