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
- It was a trace-root satellite of a tool call -> the copy of the tool whose
  ``tool_use_id`` matches the satellite's; or the synthetic root when there is no id or no
  match. A satellite is a gate hook (name ends ``.sh`` or
  ``metadata.attributes.workflow.kind == hook``) or a #93 tool-scoped audit event
  (``tool_result``, minted on the per-spoke audit trace with its ``tool_use_id`` in flat
  metadata). (Langfuse nests OTel span attributes under ``metadata["attributes"]``; the audit
  events carry their id at the metadata top level.)

Three native 1:1 sub-spans do NOT nest — they FOLD into their tool's metadata and their nodes
are dropped (#100, :func:`_fold_tool_subspans`): ``claude_code.tool.execution`` ->
``execution_ms``/``success``/``error``, ``claude_code.tool.blocked_on_user`` ->
``blocked_on_user_ms``/``decision``/``decision_source``, and the ``tool_decision:<d>`` audit
event -> ``decision``/``decision_source``. An unmatched ``tool_decision`` (no tool) keeps its
node and collapses to the root.

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
import re
import sys
import urllib.parse
from datetime import datetime
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
    rollup_metadata,
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
    ContextDelta,
    ContextItem,
    decompose_request_body,
    diff_snapshots,
    first_real_request,
    measure_request_items,
    parse_request_body,
    snapshot_items_from_path,
)
from telemetry.session_parser import project_dir_for_worktree

logger = logging.getLogger("langfuse_spoke_tree")

IngestEvent = dict[str, Any]
# One source trace paired with all of its observations: ``(orig_trace_id, observations)``.
TraceObservations = tuple[str, list[Observation]]

# Deterministic id prefixes — a rerun resolves to the same trace/observation ids.
_TRACE_PREFIX = "spoketree-"
_ROOT_PREFIX = "spokeroot-"
_COPY_PREFIX = "tree-"
# Deterministic id prefix for the synthetic cycle-step nodes (#100, derived from the ledger).
_STEP_PREFIX = "tree-step-"
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
# Name prefixes of the audit observations that are scoped to a single tool call and carry its
# ``tool_use_id`` (``tool_decision:<decision>``, ``tool_result`` from #93;
# ``hook_execution_complete:<PreToolUse|PostToolUse>`` from hook-event-nest, whose id the
# bridge resolves by event.sequence). Like gate hooks, they join the tool sharing that id
# rather than the synthetic root. ``tool_result`` / ``hook_execution_complete`` nest as nodes;
# ``tool_decision`` instead FOLDS into the tool's metadata (#100, see _is_fold_subspan) when
# matched, and only an UNMATCHED ``tool_decision`` (or a ``hook_execution_complete`` with no
# tool, e.g. ``:SessionStart``) collapses to the root, unchanged.
_TOOL_AUDIT_EVENT_PREFIXES = ("tool_decision", "tool_result", "hook_execution_complete")

# The three native 1:1 sub-spans of a tool call that FOLD into their ``tool:`` node's metadata
# (#100 part 2) instead of nesting as child nodes: the execution span (-> ``execution_ms`` /
# ``success`` / ``error``), the human-block span (-> ``blocked_on_user_ms`` / ``decision`` /
# ``decision_source``), and the #93 ``tool_decision:<d>`` audit event (-> ``decision`` /
# ``decision_source``). Gate hooks, ``tool_result``, and ``hook_execution_complete`` are NOT
# folded — they stay nested under their tool.
_FOLD_EXECUTION_NAME = "claude_code.tool.execution"
_FOLD_BLOCKED_NAME = "claude_code.tool.blocked_on_user"
_FOLD_DECISION_PREFIX = "tool_decision"

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
# Token slack for the advisory net-vs-cache_creation reconciliation cross-check (#98). The
# count_tokens tokenizer and the billed cache_creation never agree exactly (thinking blocks,
# tool-use framing), so a turn "reconciles" when the two are within this many tokens.
# UPGRADE: scale the tolerance with the turn's net magnitude if large turns drift past a flat
# slack — when absolute-token slack proves too tight on big context loads.
_RECONCILE_TOLERANCE = 2_000
# Category order for the request-body itemization (the primary, fully-itemized path).
_REQUEST_CATEGORY_ORDER = ("tools", "mcp", "system", "context")
# Component order for the per-llm_request cache decomposition (#99), in request order so the
# stable prefix (tools/system/rules/skills) groups ahead of the volatile messages.
_DECOMP_CATEGORY_ORDER = (
    "tools",
    "mcp",
    "system",
    "rules",
    "skills",
    "environment",
    "context",
    "messages",
)
# Deterministic id prefix for the per-llm_request cache-decomposition nodes.
_DECOMP_PREFIX = "tree-dc-"
# Category order for the disk fallback used when no request body is available.
_DISK_CATEGORY_ORDER = ("rules", "memory", "skills", "sub-agents", "environment")
# Env var naming the per-spoke dir of OTEL_LOG_RAW_API_BODIES=file:<dir> dumps.
_BODY_DIR_ENV = "AI_TOOLKIT_OTEL_BODY_DIR"
# Conventional per-spoke body dir under a worktree root (worktree-new.sh writes here).
_BODY_DIR_CONVENTION = Path(".ai-toolkit/raw-bodies")


class ToolContent(NamedTuple):
    """The transcript-sourced content of one tool call (either field may be absent)."""

    input: object | None  # the tool_use input args
    output: object | None  # the tool_result content


class StepWindow(NamedTuple):
    """One solo-cycle step derived from the todo ledger (#100).

    The ``subject`` is the ``TaskCreate`` title (``S1 RED: …``); the window spans the task's
    ``in_progress`` ``TaskUpdate`` start to its ``completed`` ``TaskUpdate`` end. Every timeline
    node whose ``startTime`` falls in ``[start, end]`` is re-homed under the step node.
    """

    task_id: str
    subject: str
    start: str
    end: str
    status: str


# Matches the numeric task id in a TaskCreate result ("Task #1 created successfully: …"); the
# matching TaskUpdate carries the same id (bare digits) in its ``taskId`` input.
_TASK_ID_RE = re.compile(r"#(\d+)")
# The native per-turn container span; dissolved into the step timeline when grouping is active.
_INTERACTION_NAME = "claude_code.interaction"


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


def _is_tool_audit_event(observation: Observation) -> bool:
    """Whether an observation is an audit event scoped to a single tool call.

    These (``tool_decision:<decision>``, ``tool_result``, and a Pre/PostToolUse
    ``hook_execution_complete``) are minted on the per-spoke audit trace and carry their
    ``tool_use_id`` in flat metadata; they are recognised by name prefix
    (:data:`_TOOL_AUDIT_EVENT_PREFIXES`). None of these prefixes collides with a visible
    ``tool:<Name>`` span or a bare tool name like ``Bash``.
    """
    name = observation.get("name") or ""
    return name.startswith(_TOOL_AUDIT_EVENT_PREFIXES)


def _joins_under_tool(observation: Observation) -> bool:
    """Whether an observation nests under the tool sharing its ``tool_use_id``.

    True for a gate hook or a tool-scoped audit event — the satellites of a tool call.
    Both are skipped as index owners (so the genuine tool span stays the re-parent target)
    and both join by ``tool_use_id`` in :func:`_resolve_parent`.
    """
    return _is_hook(observation) or _is_tool_audit_event(observation)


def _is_fold_subspan(observation: Observation) -> bool:
    """Whether an observation is one of the three 1:1 sub-spans that fold into their tool.

    The execution / blocked-on-user spans and the ``tool_decision:<d>`` audit event fold into
    the ``tool:`` node's metadata (:func:`_fold_attrs`); they are never re-parent targets, so
    they are also skipped as tool-index owners in :func:`_build_tool_index`.
    """
    name = observation.get("name") or ""
    return name in (_FOLD_EXECUTION_NAME, _FOLD_BLOCKED_NAME) or name.startswith(
        _FOLD_DECISION_PREFIX
    )


def _duration_ms(observation: Observation) -> int | None:
    """Return a span's wall-clock duration in ms from its ISO start/end, or None."""
    start, end = observation.get("startTime"), observation.get("endTime")
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return int(delta.total_seconds() * 1000)


def _attr(observation: Observation, *keys: str) -> object | None:
    """Read the first present key from the span attributes, then flat metadata."""
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    for key in keys:
        value = attributes.get(key)
        if value is None:
            value = metadata.get(key)
        if value is not None:
            return value
    return None


def _fold_attrs(observation: Observation) -> dict[str, Any]:
    """Return the metadata a fold sub-span contributes to its tool node (see :func:`_is_fold_subspan`)."""
    name = observation.get("name") or ""
    out: dict[str, Any] = {}
    if name == _FOLD_EXECUTION_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["execution_ms"] = ms
        success = _attr(observation, "success")
        if success is not None:
            out["success"] = bool(success)
        error = _attr(observation, "error")
        if error:
            out["error"] = error
    elif name == _FOLD_BLOCKED_NAME:
        ms = _duration_ms(observation)
        if ms is not None:
            out["blocked_on_user_ms"] = ms
        decision = _attr(observation, "decision")
        if decision:
            out["decision"] = decision
        source = _attr(observation, "decision_source", "source")
        if source:
            out["decision_source"] = source
    elif name.startswith(_FOLD_DECISION_PREFIX):
        decision = name.split(":", 1)[1] if ":" in name else _attr(observation, "decision")
        if decision:
            out["decision"] = decision
        source = _attr(observation, "decision_source", "source")
        if source:
            out["decision_source"] = source
    return out


def _fold_owner(
    observation: Observation, orig_trace_id: str, tool_index: dict[str, str]
) -> str | None:
    """Return the copy id of the tool a fold sub-span belongs to, or None.

    The audit ``tool_decision`` joins by ``tool_use_id``; the native execution / blocked spans
    are children of their tool, so they also fall back to the copy of their ``parentObservationId``.
    """
    tuid = _tool_use_id(observation)
    if tuid and tuid in tool_index:
        return tool_index[tuid]
    parent = observation.get("parentObservationId")
    return _copy_id(orig_trace_id, parent) if parent else None


def _fold_tool_subspans(
    copies: list[IngestEvent], traces: list[TraceObservations], tool_index: dict[str, str]
) -> list[IngestEvent]:
    """Fold the three 1:1 tool sub-spans into their tool's metadata, dropping their nodes (#100).

    Each execution / blocked-on-user / ``tool_decision`` sub-span's fields are merged onto the
    owning ``tool:`` node's metadata and the sub-span copy is removed. A sub-span whose tool is
    absent (an unmatched audit event) is left as-is — it keeps its node and collapses to the root.

    Args:
        copies: The source observation copies; owner tool bodies are mutated in place.
        traces: The source traces (to walk every sub-span and resolve its owner).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.

    Returns:
        The copies with the folded sub-spans removed.
    """
    by_id = {event["body"]["id"]: event for event in copies}
    folded: set[str] = set()
    for orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_fold_subspan(observation):
                continue
            owner = _fold_owner(observation, orig_trace_id, tool_index)
            if owner is None or owner not in by_id:
                continue  # no tool to fold into — leave the sub-span as a node
            attrs = _fold_attrs(observation)
            if attrs:
                by_id[owner]["body"].setdefault("metadata", {}).update(attrs)
            folded.add(_copy_id(orig_trace_id, observation["id"]))
    return [event for event in copies if event["body"]["id"] not in folded]


def _build_tool_index(traces: list[TraceObservations]) -> dict[str, str]:
    """Map each tool-call id to the copy id of the tool observation that owns it.

    A tool's satellites (gate hooks, tool-scoped audit events, and the three folding sub-spans)
    are skipped so none indexes its own ``tool_use_id``; the surviving owner is the tool
    observation, which is the re-parent target for the satellites and the fold target for the
    sub-spans.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        A mapping of ``tool_use_id`` to the assembled-trace copy id of its tool.
    """
    index: dict[str, str] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            if _joins_under_tool(observation) or _is_fold_subspan(observation):
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
    if _joins_under_tool(observation):
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
        body.setdefault("metadata", {})["rollup"] = rollup_metadata(totals)


def _earliest_start(traces: list[TraceObservations]) -> str:
    """Return the earliest ISO ``startTime`` across all observations, or the fixed base."""
    starts = [
        observation["startTime"]
        for _, observations in traces
        for observation in observations
        if observation.get("startTime")
    ]
    return min(starts) if starts else _INGEST_TIMESTAMP


def _is_interaction(observation: Observation) -> bool:
    """Whether an observation is a native per-turn ``claude_code.interaction`` container."""
    return (observation.get("name") or "") == _INTERACTION_NAME


def _step_id(spoke_run_id: str, task_id: str) -> str:
    """Return the deterministic id of one cycle-step node for a spoke."""
    digest = hashlib.sha1(f"{spoke_run_id}:step:{task_id}".encode()).hexdigest()[:24]
    return _STEP_PREFIX + digest


def _task_id_from_create(output: object | None) -> str | None:
    """Extract the created task id from a ``TaskCreate`` result, or None.

    The transcript ``tool_result`` content is usually the ``"Task #N created…"`` string but can
    arrive as a list of content blocks (``[{"type": "text", "text": …}]``); both are searched by
    serializing non-string output, since the only ``#N`` in the result is the task id.
    """
    if output is None:
        return None
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    match = _TASK_ID_RE.search(text)
    return match.group(1) if match else None


def _ledger_subjects(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, str]:
    """Map each created task id to its ``TaskCreate`` subject (the step title)."""
    subjects: dict[str, str] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskCreate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            subject = content.input.get("subject")
            task_id = _task_id_from_create(content.output)
            if subject and task_id:
                subjects[task_id] = str(subject)
    return subjects


def _ledger_bounds(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, dict[str, str]]:
    """Map each task id to its window bounds from ``TaskUpdate`` status transitions.

    ``start`` is the earliest ``in_progress`` update's ``startTime``; ``end`` is the latest
    ``completed`` update's ``endTime`` (resumes can re-mark a task, so the extremes win).
    """
    bounds: dict[str, dict[str, str]] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != "tool:TaskUpdate":
                continue
            content = tool_content.get(_tool_use_id(observation) or "")
            if content is None or not isinstance(content.input, dict):
                continue
            task_id = str(content.input.get("taskId") or "")
            if not task_id:
                continue
            entry = bounds.setdefault(task_id, {})
            status = content.input.get("status")
            start = observation.get("startTime")
            if status == "in_progress" and start:
                entry["start"] = start if "start" not in entry else min(entry["start"], start)
            if status == "completed":
                end = observation.get("endTime") or observation.get("startTime") or ""
                if end:
                    entry["end"] = end if "end" not in entry else max(entry["end"], end)
                entry["status"] = "completed"
    return bounds


def _latest_time(traces: list[TraceObservations]) -> str:
    """Return the latest ISO ``endTime``/``startTime`` across all observations, or the base."""
    times = [
        observation.get("endTime") or observation.get("startTime") or ""
        for _orig_trace_id, observations in traces
        for observation in observations
        if observation.get("endTime") or observation.get("startTime")
    ]
    return max(times) if times else _INGEST_TIMESTAMP


def build_step_windows(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> list[StepWindow]:
    """Derive the solo-cycle step windows from the todo ledger (#100).

    Each ``TaskCreate`` subject is a step; its ``in_progress`` → ``completed`` ``TaskUpdate``
    timestamps bound the window. A task created but never started (no ``in_progress``) has no
    window and is skipped. An in-flight task (no ``completed``) clamps its end to the spoke's
    last observation. Non-ledger spokes (no ``TaskCreate``) yield ``[]`` — no step grouping.

    Args:
        traces: The source traces paired with their observations.
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).

    Returns:
        The step windows in chronological start order.
    """
    subjects = _ledger_subjects(traces, tool_content)
    if not subjects:
        return []
    bounds = _ledger_bounds(traces, tool_content)
    fallback_end = _latest_time(traces)
    windows: list[StepWindow] = []
    for task_id, subject in subjects.items():
        bound = bounds.get(task_id)
        if not bound or "start" not in bound:
            continue
        windows.append(
            StepWindow(
                task_id=task_id,
                subject=subject,
                start=bound["start"],
                end=bound.get("end") or fallback_end,
                status=bound.get("status", "in_progress"),
            )
        )
    windows.sort(key=lambda window: window.start)
    return windows


def _containing_window(start: str, windows: list[StepWindow]) -> StepWindow | None:
    """Return the innermost step window containing ``start`` (latest start wins), or None.

    ``windows`` is ordered by start, so iterating and overwriting yields the latest-starting
    window that contains the timestamp — the innermost on an overlap.
    """
    chosen: StepWindow | None = None
    for window in windows:
        if window.start <= start <= window.end:
            chosen = window
    return chosen


def _step_event(window: StepWindow, step_id: str, root_id: str, trace_id: str) -> IngestEvent:
    """Shape one cycle-step span-create event under the synthetic root."""
    return {
        "id": step_id,
        "type": "span-create",
        "timestamp": window.start,
        "body": {
            "id": step_id,
            "traceId": trace_id,
            "parentObservationId": root_id,
            "name": f"step:{window.subject}",
            "startTime": window.start,
            "endTime": window.end,
            "metadata": {
                "subject": window.subject,
                "status": window.status,
                "started": window.start,
                "completed": window.end,
            },
        },
    }


def _apply_step_grouping(
    copies: list[IngestEvent],
    traces: list[TraceObservations],
    tool_content: dict[str, ToolContent],
    *,
    root_id: str,
    spoke_run_id: str,
    trace_id: str,
) -> list[IngestEvent]:
    """Group the flat root-level satellites under their cycle step (#100), in place.

    Only the synthetic-root's OWN satellite children move: the ``step:*`` / ``lifecycle:*``
    markers, the ``mcp`` / ``spoke-push`` / ``script:ready`` script spans, and the gate hooks
    that did not match a tool. When such a root child's ``startTime`` falls in a step window it is
    re-parented under that step node (innermost wins on overlap).

    The ``claude_code.interaction`` subtrees are LEFT UNTOUCHED at the root — their per-turn
    structure and W3C-TRACEPARENT nesting (a resume can legitimately nest under an earlier
    command's ``tool.execution``) reflect causal reality and are never flattened. Because tools
    and turns live under those interactions (not at the root), they are naturally excluded.

    Args:
        copies: The re-parented source observation copies; root-level satellites are mutated
            in place.
        traces: The source traces (for ledger windows + interaction detection).
        tool_content: Tool-call-id to :class:`ToolContent` (the ledger ops' input/output).
        root_id: The synthetic root span id.
        spoke_run_id: The spoke run identifier (for deterministic step ids).
        trace_id: The assembled trace id every step node references.

    Returns:
        The new step span events (empty when the spoke has no ledger windows).
    """
    windows = build_step_windows(traces, tool_content)
    if not windows:
        return []
    interaction_ids = {
        _copy_id(orig_trace_id, observation["id"])
        for orig_trace_id, observations in traces
        for observation in observations
        if _is_interaction(observation)
    }
    step_ids = {window.task_id: _step_id(spoke_run_id, window.task_id) for window in windows}
    step_events = [_step_event(w, step_ids[w.task_id], root_id, trace_id) for w in windows]
    for event in copies:
        body = event["body"]
        if body.get("parentObservationId") != root_id or body["id"] in interaction_ids:
            continue  # only the root's own non-interaction satellites move
        start = body.get("startTime")
        window = _containing_window(start, windows) if start else None
        if window is not None:
            body["parentObservationId"] = step_ids[window.task_id]
    return step_events


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

    When the spoke ran a solo cycle, the todo ledger yields per-phase step nodes
    (:func:`build_step_windows`) under the root, and the root's own flat satellites (markers,
    lifecycle, script, and unmatched hook spans) re-home under the step whose window contains
    them (:func:`_apply_step_grouping`); the ``claude_code.interaction`` subtrees are left
    untouched. A non-ledger spoke emits no step nodes.

    Args:
        traces: Each source trace paired with all of its observations, as fetched from
            Langfuse with full fields.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`;
            defaults to empty (no tool content filled).

    Returns:
        The ingestion events: a ``trace-create``, the synthetic root, the cycle-step nodes,
        then the copies.
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
    copies = _fold_tool_subspans(copies, traces, tool_index)
    step_events = _apply_step_grouping(
        copies, traces, tool_content, root_id=root_id, spoke_run_id=spoke_run_id, trace_id=trace_id
    )
    events = [trace_event, root_event, *step_events, *copies]
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


def transcript_scan_root(projects_root: Path, worktree: Path) -> Path:
    """Scope the transcript scan to the spoke's own Claude Code project dir when present.

    The default scan rglobbed EVERY session under ``projects_root`` on each land. Matching is
    by globally-unique ``tool_use_id``, so it never cross-attached another spoke's content
    (unlike #92's reasoning backfill) — but scoping to the worktree's project dir
    (:func:`telemetry.session_parser.project_dir_for_worktree`) avoids the all-projects rglob.
    Falls back to the full root when that dir is absent (a standalone run from a non-worktree
    cwd), preserving the prior behavior.

    Args:
        projects_root: The Claude Code projects root (``--projects``).
        worktree: The spoke's worktree dir (``--root``).

    Returns:
        The worktree's project dir when it exists, else ``projects_root``.
    """
    project_dir = project_dir_for_worktree(worktree, projects_root)
    return project_dir if project_dir.is_dir() else projects_root


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


def per_turn_cache_creation(traces: list[TraceObservations]) -> list[int]:
    """Return each LLM call's ``cache_creation`` token count, ordered by ``startTime``.

    Used for the advisory per-turn reconciliation: aligned by chronological position with the
    diffed request bodies, each value is the billed cache write the turn's net is checked
    against. Only observations carrying ``cache_creation_input_tokens`` usage are counted, so
    non-LLM spans do not shift the alignment.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        The ``cache_creation`` token counts of the LLM calls, oldest first.
    """
    calls: list[tuple[str, int]] = []
    for _orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            written = usage.get("cache_creation_input_tokens")
            if written is None:
                continue
            calls.append((observation.get("startTime") or "", int(written)))
    return [written for _start, written in sorted(calls)]


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
    """Return the ``*.request.json`` dumps in ``bodies_dir``, oldest first.

    Sorted by modification time, not name: the dumps are ``<uuid>.request.json`` and random
    UUIDs are not chronological, so a name sort would not yield emission order.
    """
    if not bodies_dir.is_dir():
        return []
    return sorted(bodies_dir.glob("*.request.json"), key=lambda path: path.stat().st_mtime)


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


# The diff buckets rendered as child nodes under each evolving turn, in display order.
_EVO_BUCKETS = ("added", "removed", "changed")


def _signed(tokens: int) -> str:
    """Render a signed token count for a turn-node label (e.g. ``+370`` / ``-15000``)."""
    return f"+{tokens}" if tokens >= 0 else str(tokens)


def _evo_summary(row: dict[str, object]) -> dict[str, object]:
    """Compact a measured diff row to the fields shown in a turn's added/removed/changed list."""
    summary: dict[str, object] = {
        "category": row["category"],
        "name": row["name"],
        "tokens": row["tokens"],
    }
    if "delta_tokens" in row:
        summary["delta_tokens"] = row["delta_tokens"]
    return summary


def _evo_item_node(
    spoke_run_id: str,
    turn_index: int,
    bucket: str,
    parent_id: str,
    trace_id: str,
    base_ts: str,
    row: dict[str, object],
) -> IngestEvent:
    """Shape one added/removed/changed component node under an evolving turn.

    The id is namespaced by turn and bucket so the same component name recurring across turns
    (a tool re-loaded, a message kind re-added) does not collide.
    """
    tokens = int(cast(int, row["tokens"]))
    metadata: dict[str, object] = {
        "change": bucket,
        "category": row["category"],
        "tokens": tokens,
        "cost_usd": row["cost_usd"],
    }
    if "delta_tokens" in row:
        metadata["delta_tokens"] = row["delta_tokens"]
    return _lc_node(
        node_id=_lc_id(spoke_run_id, f"evo/{turn_index}/{bucket}/{row['category']}/{row['name']}"),
        parent_id=parent_id,
        trace_id=trace_id,
        name=f"{row['name']}: {_human_tokens(tokens)}",
        base_ts=base_ts,
        metadata=metadata,
    )


def _evo_turn_events(
    spoke_run_id: str,
    turn_index: int,
    delta: ContextDelta,
    *,
    evo_id: str,
    trace_id: str,
    base_ts: str,
    cache_creation: int | None,
) -> list[IngestEvent]:
    """Build one evolving-turn node and its per-component child nodes."""
    turn_id = _lc_id(spoke_run_id, f"evo/turn-{turn_index}")
    suffix = f" [{delta.label}]" if delta.label else ""
    metadata: dict[str, object] = {
        "turn": turn_index,
        "net_tokens": delta.net_tokens,
        "label": delta.label,
        "added": [_evo_summary(row) for row in delta.added],
        "removed": [_evo_summary(row) for row in delta.removed],
        "changed": [_evo_summary(row) for row in delta.changed],
    }
    if cache_creation is not None:
        metadata["cache_creation_observed"] = cache_creation
        metadata["reconciles"] = abs(delta.net_tokens - cache_creation) <= _RECONCILE_TOLERANCE
    turn_node = _lc_node(
        node_id=turn_id,
        parent_id=evo_id,
        trace_id=trace_id,
        name=f"turn {turn_index}: net {_signed(delta.net_tokens)}{suffix}",
        base_ts=base_ts,
        metadata=metadata,
    )
    buckets = {"added": delta.added, "removed": delta.removed, "changed": delta.changed}
    children = [
        _evo_item_node(spoke_run_id, turn_index, bucket, turn_id, trace_id, base_ts, row)
        for bucket in _EVO_BUCKETS
        for row in buckets[bucket]
    ]
    return [turn_node, *children]


def build_context_evolution_events(
    spoke_run_id: str,
    deltas: list[tuple[int, ContextDelta]],
    *,
    base_ts: str,
    cache_creation_by_turn: dict[int, int] | None = None,
) -> list[IngestEvent]:
    """Build the per-turn context-evolution subtree under the spoke root.

    Emits a ``context-evolution`` parent under the synthetic root (a sibling of the #87
    ``loaded-context`` baseline), then one node per evolving turn carrying its ``added`` /
    ``removed`` / ``changed`` component lists, ``net_tokens``, and compaction ``label`` in
    metadata, with one child node per added / removed / changed component (so a ToolSearch
    turn decomposes into the loaded schemas by name and a compaction shows the dropped
    messages). Turns with no change are not passed in (the caller filters them).

    When ``cache_creation_by_turn`` maps a turn to that turn's observed ``cache_creation``,
    the turn node records it as ``cache_creation_observed`` plus a ``reconciles`` flag — the
    advisory (≈) cross-check of the net against the billed delta. All ids derive from the
    spoke run id so a rerun overwrites the same nodes.

    Args:
        spoke_run_id: The spoke run identifier.
        deltas: ``(turn_index, ContextDelta)`` pairs for the evolving turns, in turn order.
        base_ts: ISO timestamp stamped on every synthetic node.
        cache_creation_by_turn: Optional per-turn observed ``cache_creation`` for the
            reconciliation cross-check; absent turns simply omit the cross-check metadata.

    Returns:
        The context-evolution ingestion events: the parent, then each turn with its children.
    """
    trace_id = trace_id_for(spoke_run_id)
    evo_id = _lc_id(spoke_run_id, "context-evolution")
    cache_creation_by_turn = cache_creation_by_turn or {}
    events = [
        _lc_node(
            node_id=evo_id,
            parent_id=root_id_for(spoke_run_id),
            trace_id=trace_id,
            name="context-evolution",
            base_ts=base_ts,
            metadata={"turns": len(deltas)},
        )
    ]
    for turn_index, delta in deltas:
        events.extend(
            _evo_turn_events(
                spoke_run_id,
                turn_index,
                delta,
                evo_id=evo_id,
                trace_id=trace_id,
                base_ts=base_ts,
                cache_creation=cache_creation_by_turn.get(turn_index),
            )
        )
    return events


def context_evolution_deltas(
    bodies_dir: Path, *, counter: TokenCounter, price: float
) -> list[tuple[int, ContextDelta]]:
    """Diff every consecutive raw request body in ``bodies_dir`` into per-turn deltas.

    The bodies are itemized in chronological order (``find_request_files`` sorts by mtime)
    and each consecutive pair is diffed; turn 0 is the baseline, so a delta's turn index is
    the newer body's position. Turns whose context did not change are dropped, so only evolving
    turns are returned.

    The turn index is the RAW file position, NOT a position in the parsed-only list: an
    unparseable body becomes a ``None`` hole that drops the two transitions touching it but
    leaves every other turn index intact, so the indices keep aligning with
    :func:`_reconciliation_map` (which keys off the full file list).

    Note the turn-0 baseline here is the first dumped body, which may be a degenerate aux call
    (empty ``tools``) that the #87 ``loaded-context`` path skips via ``first_real_request``; the
    first evolving turn then surfaces the real prefix load as a large ADD. The #87 baseline
    itself is unaffected.

    Args:
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.
        counter: Token counter; raises ``CountTokensError`` to trigger the char/4 fallback.
        price: Cache-creation price in USD per token.

    Returns:
        ``(turn_index, ContextDelta)`` pairs for the evolving turns, in turn order.
    """
    snapshots: list[list[ContextItem] | None] = []
    for path in find_request_files(bodies_dir):
        try:
            snapshots.append(snapshot_items_from_path(path))
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot itemize request body %s", path)
            snapshots.append(None)  # a hole: preserve raw positions, never bridge across it
    deltas: list[tuple[int, ContextDelta]] = []
    for index in range(1, len(snapshots)):
        prev, curr = snapshots[index - 1], snapshots[index]
        if prev is None or curr is None:
            continue
        delta = diff_snapshots(prev, curr, counter=counter, price=price)
        if delta.added or delta.removed or delta.changed:
            deltas.append((index, delta))
    return deltas


def _decomp_id(spoke_run_id: str, key: str) -> str:
    """Return the deterministic id of one cache-decomposition node for a spoke."""
    digest = hashlib.sha1(f"{spoke_run_id}:{key}".encode()).hexdigest()[:24]
    return _DECOMP_PREFIX + digest


def _llm_requests_in_order(traces: list[TraceObservations]) -> list[tuple[str, Observation]]:
    """Return ``(orig_trace_id, observation)`` for each LLM call, oldest first by ``startTime``.

    An LLM call is any observation carrying ``cache_read_input_tokens`` or
    ``cache_creation_input_tokens`` usage — the same set the request-body dumps correspond to,
    so the two align positionally (the basis of the count gate in
    :func:`build_llm_decomposition_events`).
    """
    calls: list[tuple[str, str, Observation]] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            usage = observation.get("usageDetails") or {}
            if (
                usage.get("cache_read_input_tokens") is None
                and usage.get("cache_creation_input_tokens") is None
            ):
                continue
            calls.append((observation.get("startTime") or "", orig_trace_id, observation))
    calls.sort(key=lambda call: call[0])
    return [(orig_trace_id, observation) for _start, orig_trace_id, observation in calls]


def _split_rows_by_cache(
    rows: list[dict[str, object]], *, cache_read: int, cache_creation: int
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Partition itemized rows into the cache_read / cache_creation budgets by cumulative fit.

    Walking the items in request order, the first ``cache_read`` tokens fall in the reused
    prefix, the next ``cache_creation`` tokens are the portion written this turn, and anything
    beyond is fresh input (not shown). Each item is assigned WHOLE by its cumulative start
    offset, so the split reconciles to the observed counters with a per-bucket remainder rather
    than the ``cached`` flag, which mislabels both cold calls (whole prefix written, not read)
    and the freshly-written delta of warm calls.
    """
    read: list[dict[str, object]] = []
    creation: list[dict[str, object]] = []
    offset = 0
    for row in rows:
        start = offset
        offset += int(cast(int, row["tokens"]))
        if start < cache_read:
            read.append(row)
        elif start < cache_read + cache_creation:
            creation.append(row)
    return read, creation


def _decomp_item_node(
    spoke_run_id: str,
    ns: str,
    bucket: str,
    category: str,
    cat_id: str,
    trace_id: str,
    base_ts: str,
    row: dict[str, object],
) -> IngestEvent:
    """Shape one per-item leaf under a decomposition component, carrying tokens/cost/cache flag."""
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
        node_id=_decomp_id(spoke_run_id, f"{ns}/{bucket}/{category}/{row['name']}"),
        parent_id=cat_id,
        trace_id=trace_id,
        name=f"{row['name']}: {_human_tokens(tokens)}",
        base_ts=base_ts,
        metadata=metadata,
    )


def _decomp_bucket_events(
    spoke_run_id: str,
    ns: str,
    *,
    parent_id: str,
    trace_id: str,
    base_ts: str,
    bucket: str,
    rows: list[dict[str, object]],
    observed: int,
    price: float,
) -> list[IngestEvent]:
    """Build one cache bucket (``cache_read`` / ``cache_creation``) and its component subtree.

    The bucket node parents under the llm_request copy; under it sit one component node per
    category (in :data:`_DECOMP_CATEGORY_ORDER`) with one item leaf each, plus a ``remainder``
    node = ``observed - Σ measured`` so the children reconcile (≈) to the billed counter.
    """
    bucket_id = _decomp_id(spoke_run_id, f"{ns}/{bucket}")
    measured = sum(int(cast(int, row["tokens"])) for row in rows)
    events = [
        _lc_node(
            node_id=bucket_id,
            parent_id=parent_id,
            trace_id=trace_id,
            name=f"{bucket} {observed}",
            base_ts=base_ts,
            metadata={"tokens": observed, "measured_tokens": measured},
        )
    ]
    for category, crows in _group_rows_by_category(rows, _DECOMP_CATEGORY_ORDER):
        cat_id = _decomp_id(spoke_run_id, f"{ns}/{bucket}/{category}")
        events.append(
            _lc_node(
                node_id=cat_id,
                parent_id=bucket_id,
                trace_id=trace_id,
                name=category,
                base_ts=base_ts,
                metadata={
                    "tokens": sum(int(cast(int, r["tokens"])) for r in crows),
                    "cost_usd": sum(float(cast(float, r["cost_usd"])) for r in crows),
                },
            )
        )
        events.extend(
            _decomp_item_node(spoke_run_id, ns, bucket, category, cat_id, trace_id, base_ts, row)
            for row in crows
        )
    remainder = observed - measured
    events.append(
        _lc_node(
            node_id=_decomp_id(spoke_run_id, f"{ns}/{bucket}/remainder"),
            parent_id=bucket_id,
            trace_id=trace_id,
            name=f"remainder: {_human_tokens(abs(remainder))}",
            base_ts=base_ts,
            metadata={
                "tokens": remainder,
                "cost_usd": max(0, remainder) * price,
                "source": _REMAINDER_SOURCE,
            },
        )
    )
    return events


def build_llm_decomposition_events(
    traces: list[TraceObservations],
    bodies_dir: Path,
    spoke_run_id: str,
    *,
    counter: TokenCounter,
    price: float,
    base_ts: str,
) -> list[IngestEvent]:
    """Build the per-``llm_request`` cache_read/cache_creation decomposition subtrees (#99).

    For each LLM call (aligned positionally with its raw request body) the body is itemized by
    :func:`telemetry.request_body.decompose_request_body` — rules per file, skills per skill,
    every message — and the items are split into the observed ``cache_read`` / ``cache_creation``
    token budgets by cumulative fit (see :func:`_split_rows_by_cache`). Each split is attached as
    a two-bucket subtree (component -> item, with a reconciling remainder) directly under the
    llm_request's copy node, so the operator sees the decomposition on the call itself.

    The alignment is positional (LLM calls by ``startTime`` ↔ bodies by mtime) and is only built
    when the counts match — otherwise an aux/degenerate call has skewed the alignment and the
    decomposition is skipped entirely, mirroring :func:`_reconciliation_map`.

    All ids derive from the spoke run id and the source ``(trace_id, observation_id)`` pair, so a
    rerun overwrites the same nodes.

    UPGRADE: collapse the stable cache_read prefix (near-identical every turn) into a shared
    reference if the per-call repetition makes the ingestion batch too large on long spokes.

    Args:
        traces: The source traces paired with their observations.
        bodies_dir: The per-spoke ``OTEL_LOG_RAW_API_BODIES=file:<dir>`` dump directory.
        spoke_run_id: The spoke run identifier.
        counter: Token counter; raises ``CountTokensError`` to trigger the char/4 fallback.
        price: Cache-creation price in USD per token.
        base_ts: ISO timestamp stamped on every synthetic node.

    Returns:
        The decomposition ingestion events, or ``[]`` when no call/body pair aligns safely.
    """
    calls = _llm_requests_in_order(traces)
    bodies = find_request_files(bodies_dir)
    if not calls or len(calls) != len(bodies):
        return []
    trace_id = trace_id_for(spoke_run_id)
    events: list[IngestEvent] = []
    for (orig_trace_id, observation), body_path in zip(calls, bodies):
        try:
            items = decompose_request_body(body_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot decompose request body %s", body_path)
            continue
        rows = measure_request_items(items, counter=counter, price=price)
        usage = observation.get("usageDetails") or {}
        read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        read_rows, creation_rows = _split_rows_by_cache(
            rows, cache_read=read_tokens, cache_creation=creation_tokens
        )
        copy_id = _copy_id(orig_trace_id, observation["id"])
        ns = f"llm/{orig_trace_id}/{observation['id']}"
        events.extend(
            _decomp_bucket_events(
                spoke_run_id,
                ns,
                parent_id=copy_id,
                trace_id=trace_id,
                base_ts=base_ts,
                bucket="cache_read",
                rows=read_rows,
                observed=read_tokens,
                price=price,
            )
        )
        events.extend(
            _decomp_bucket_events(
                spoke_run_id,
                ns,
                parent_id=copy_id,
                trace_id=trace_id,
                base_ts=base_ts,
                bucket="cache_creation",
                rows=creation_rows,
                observed=creation_tokens,
                price=price,
            )
        )
    return events


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
            f"context from (default: ${_BODY_DIR_ENV}, else <root>/{_BODY_DIR_CONVENTION}). "
            "Falls back to disk measurement when no real request body is found."
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
    scan_root = transcript_scan_root(args.projects, args.root.resolve())
    tool_content = scan_transcripts(scan_root, _tool_span_ids(traces))
    batch = build_batch(traces, args.spoke_run_id, tool_content)

    counter = make_counter(endpoint=args.endpoint, api_key=args.api_key, model=args.model)
    base_ts = _earliest_start(traces)
    bodies_dir = args.request_bodies or (args.root.resolve() / _BODY_DIR_CONVENTION)
    request_rows = request_context_rows(bodies_dir, counter=counter, price=args.price)
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
    deltas = context_evolution_deltas(bodies_dir, counter=counter, price=args.price)
    evolution_events = build_context_evolution_events(
        args.spoke_run_id,
        deltas,
        base_ts=base_ts,
        cache_creation_by_turn=_reconciliation_map(traces, bodies_dir),
    )
    decomposition_events = build_llm_decomposition_events(
        traces, bodies_dir, args.spoke_run_id, counter=counter, price=args.price, base_ts=base_ts
    )
    post_in_chunks(batch + context_events + evolution_events + decomposition_events, post)

    trace_id = trace_id_for(args.spoke_run_id)
    filled = filled_tool_spans(traces, tool_content)
    decomposed = sum(
        1 for event in decomposition_events if event["body"]["name"].startswith("cache_read ")
    )
    print(
        f"{len(batch) - 2} observations assembled under trace {trace_id} "
        f"(roots collapsed to 1), {filled} tool spans filled from transcript, "
        f"{len(rows)} loaded-context items itemized (source: {source}), "
        f"{len(deltas)} evolving turns diffed, "
        f"{decomposed} llm_requests cache-decomposed"
    )
    return 0


def _reconciliation_map(traces: list[TraceObservations], bodies_dir: Path) -> dict[int, int] | None:
    """Map each turn index to its observed ``cache_creation`` for the advisory cross-check.

    Aligns the LLM calls (by ``startTime``) with the request bodies (by mtime) positionally.
    The mapping is returned ONLY when the two counts match — otherwise an aux/degenerate call
    has skewed the alignment and a wrong cross-check would mislead, so it is omitted entirely.
    This is why the cross-check is advisory (``≈``): the count gate catches a missing call but
    not a reordering, since the two orderings come from independent clocks.

    UPGRADE: join LLM calls to bodies by a shared key (``prompt.id`` / request id) instead of
    positional order — when near-simultaneous requests can flush spans and dump files in
    different orders and the positional alignment silently mismatches.
    """
    writes = per_turn_cache_creation(traces)
    bodies = find_request_files(bodies_dir)
    if not writes or len(writes) != len(bodies):
        return None
    return dict(enumerate(writes))


if __name__ == "__main__":
    raise SystemExit(main())
