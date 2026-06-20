#!/usr/bin/env python3
"""Translate a spoke's causal forest into Langfuse ingestion events (Issue #92).

The session transcript uniquely owns three things the live OTel push cannot reconstruct:
extended-thinking bodies (redacted in every raw API body), true causal edges
(``uuid``/``parentUuid``), and coverage of sessions that ran un-instrumented / while the
collector was down / historically. This module is the keystone that makes Langfuse the
single complete source: it reuses the parser (``session_parser``) and the causal builder
(``causal_tree``) to assemble one forest from the local transcript, then SOURCES FROM THE
TRANSCRIPT to emit that forest as one Langfuse trace — the second sink for the same forest
the dashboard renders, distinct from ``langfuse_spoke_tree`` which sources from Langfuse.

The translator here is pure and network-free (:func:`forest_to_events`); the coverage
query and HTTP ingestion live in the CLI wiring (added alongside). Each causal node becomes
one ingestion event under a backfill-owned trace:

- a cost-bearing ``turn``/``agent`` leaf -> a ``generation-create`` carrying the turn's
  four-component ``usageDetails`` and its ccusage ``costDetails``;
- every other node (interval / tool / context / reasoning / hook / ...) -> a
  ``span-create``; a ``reasoning`` node additionally carries the extended-thinking body as
  its ``output`` (joined by turn uuid from the opt-in ``thinking`` map);
- every container (a node with children) gets ``metadata.rollup`` summed over its subtree,
  reusing ``langfuse_rollup``'s sum logic.

All ids derive from ``(spoke_run_id, node_id)`` — and a causal ``node_id`` is the transcript
``uuid`` — so a rerun overwrites the same trace/observations instead of appending. The
backfill trace lives in its OWN id namespace (``spokefill-``/``fillroot-``/``fill-``) so it
never collides with the live push's native traces or ``langfuse_spoke_tree``'s assembled
tree; the CLI's coverage query is what avoids re-emitting what the live push already covered.

Import-safe: no environment is read at import time. Stdlib only; reuses ``langfuse_rollup``'s
tree/sum helpers.
"""

from __future__ import annotations

import hashlib
from typing import Any

from telemetry.langfuse_rollup import build_tree, subtree_totals

IngestEvent = dict[str, Any]

# Deterministic id namespaces — distinct from langfuse_spoke_tree's, so a backfill trace
# never collides with the live push's native traces or the assembled tree.
_TRACE_PREFIX = "spokefill-"
_ROOT_PREFIX = "fillroot-"
_NODE_PREFIX = "fill-"
_TRACE_NAME_PREFIX = "spoke-backfill:"
_ROOT_NAME_PREFIX = "spoke:"

# Langfuse requires a timestamp on every event; fixed so reruns stay stable when a node
# carries no usable ``ts_start``.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Causal kinds that spent tokens on an inference — rendered as generations.
_GENERATION_KINDS = frozenset({"turn", "agent"})
# The reasoning node's body can be large; cap the serialized output past this.
_MAX_OUTPUT_CHARS = 20_000
_TRUNCATION_MARKER = "...[truncated]"
# Node-id prefix marking a reasoning node, ``reasoning:<turn uuid>``.
_REASONING_PREFIX = "reasoning:"


def _sha16(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:16]


def backfill_trace_id(spoke_run_id: str) -> str:
    """Return the deterministic backfill trace id for a spoke."""
    return _TRACE_PREFIX + _sha16(spoke_run_id)


def backfill_root_id(spoke_run_id: str) -> str:
    """Return the deterministic synthetic-root id for a spoke's backfill trace."""
    return _ROOT_PREFIX + _sha16(spoke_run_id)


def backfill_node_id(spoke_run_id: str, node_id: str) -> str:
    """Return the deterministic observation id for one causal node's copy.

    Derived from ``(spoke_run_id, node_id)`` — and a causal ``node_id`` is the transcript
    ``uuid`` — so a rerun resolves to the same id and overwrites rather than appends.
    """
    return _NODE_PREFIX + hashlib.sha1(f"{spoke_run_id}:{node_id}".encode()).hexdigest()[:24]


def _is_generation(node: dict[str, Any]) -> bool:
    """Whether a node is an inference leaf that should render as a Langfuse generation."""
    if node.get("kind") not in _GENERATION_KINDS:
        return False
    return bool(
        node.get("own_tokens_in")
        or node.get("own_tokens_out")
        or node.get("own_cost_usd")
        or node.get("cache_read")
        or node.get("cache_creation")
    )


def _usage_details(node: dict[str, Any]) -> dict[str, int]:
    """The four-component token usage of an inference node (zeros when a field is absent)."""
    return {
        "input": int(node.get("own_tokens_in") or 0),
        "output": int(node.get("own_tokens_out") or 0),
        "cache_read_input_tokens": int(node.get("cache_read") or 0),
        "cache_creation_input_tokens": int(node.get("cache_creation") or 0),
    }


def _metadata(node: dict[str, Any]) -> dict[str, Any]:
    """The node's display metadata: kind plus any present phase/summary/status/actor/context."""
    metadata: dict[str, Any] = {"kind": node.get("kind")}
    for key in ("actor", "phase", "summary", "status"):
        if node.get(key) is not None:
            metadata[key] = node[key]
    if "input_context" in node:
        metadata["input_context"] = node["input_context"]
    return metadata


def _capped(text: str) -> str:
    """Truncate an oversized reasoning body to keep each ingestion event small."""
    return text if len(text) <= _MAX_OUTPUT_CHARS else text[:_MAX_OUTPUT_CHARS] + _TRUNCATION_MARKER


def _node_event(
    node: dict[str, Any],
    *,
    spoke_run_id: str,
    trace_id: str,
    parent_id: str,
    thinking: dict[str, str],
) -> IngestEvent:
    """Shape one ingestion event for a causal node (generation for inferences, else span)."""
    node_id = backfill_node_id(spoke_run_id, node["node_id"])
    start = node.get("ts_start") or _INGEST_TIMESTAMP
    body: dict[str, Any] = {
        "id": node_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        "name": node.get("name") or node.get("kind"),
        "startTime": start,
        "endTime": node.get("ts_end") or start,
        "metadata": _metadata(node),
    }
    if _is_generation(node):
        body["usageDetails"] = _usage_details(node)
        if node.get("own_cost_usd"):
            body["costDetails"] = {"total": float(node["own_cost_usd"])}
        event_type = "generation-create"
    else:
        event_type = "span-create"
    _add_reasoning_body(body, node, thinking)
    return {"id": node_id, "type": event_type, "timestamp": start, "body": body}


def _add_reasoning_body(
    body: dict[str, Any], node: dict[str, Any], thinking: dict[str, str]
) -> None:
    """Set a reasoning node's ``output`` to its extended-thinking body, joined by turn uuid."""
    node_id = node.get("node_id") or ""
    if node.get("kind") != "reasoning" or not node_id.startswith(_REASONING_PREFIX):
        return
    text = thinking.get(node_id[len(_REASONING_PREFIX) :])
    if text:
        body["output"] = _capped(text)


def _walk(
    nodes: list[dict[str, Any]],
    *,
    spoke_run_id: str,
    trace_id: str,
    parent_id: str,
    thinking: dict[str, str],
    events: list[IngestEvent],
) -> None:
    """Append one event per node, depth-first, re-parenting each child under its parent copy."""
    for node in nodes:
        event = _node_event(
            node,
            spoke_run_id=spoke_run_id,
            trace_id=trace_id,
            parent_id=parent_id,
            thinking=thinking,
        )
        events.append(event)
        _walk(
            node["children"],
            spoke_run_id=spoke_run_id,
            trace_id=trace_id,
            parent_id=event["body"]["id"],
            thinking=thinking,
            events=events,
        )


def _earliest_ts(nodes: list[dict[str, Any]]) -> str:
    """The earliest ``ts_start`` across the forest, or the fixed base when none is present."""
    starts = _collect_starts(nodes)
    return min(starts) if starts else _INGEST_TIMESTAMP


def _collect_starts(nodes: list[dict[str, Any]]) -> list[str]:
    starts: list[str] = []
    for node in nodes:
        if node.get("ts_start"):
            starts.append(node["ts_start"])
        starts.extend(_collect_starts(node["children"]))
    return starts


def _apply_rollups(events: list[IngestEvent]) -> None:
    """Set ``metadata.rollup`` on every container node (one with children), summed bottom-up.

    Reuses :mod:`telemetry.langfuse_rollup`'s tree + subtree-sum logic over the event bodies;
    leaves (no children) carry no rollup. Mutates the event bodies in place.
    """
    nodes = [event["body"] for event in events if event["type"] != "trace-create"]
    by_id, children = build_tree(nodes)
    for body in nodes:
        if not children.get(body["id"]):
            continue
        totals = subtree_totals(body["id"], by_id, children)
        body.setdefault("metadata", {})["rollup"] = {
            "reused": totals["cache_read_input_tokens"],
            "written": totals["cache_creation_input_tokens"],
            "input": totals["input"],
            "output": totals["output"],
        }


def forest_to_events(
    forest: list[dict[str, Any]], spoke_run_id: str, thinking: dict[str, str] | None = None
) -> list[IngestEvent]:
    """Translate a spoke's causal forest into Langfuse ingestion events.

    Emits a ``trace-create``, a synthetic root span, and one copy per causal node
    re-parented across the tree (see module docstring). All ids derive from
    ``(spoke_run_id, node_id)``, so the batch is idempotent. Reasoning nodes carry the
    extended-thinking body as ``output``, joined by turn uuid from ``thinking``.

    Args:
        forest: The spoke's causal forest (from ``causal_tree.causal_forest_from_parsed``).
        spoke_run_id: The spoke run id; becomes the trace's ``sessionId``.
        thinking: Optional ``turn uuid -> extended-thinking body`` map (the backfill's
            opt-in). Reasoning nodes only exist in ``forest`` when this was given to the
            builder; here it supplies their body.

    Returns:
        The ingestion events: a ``trace-create``, the synthetic root, then the node copies.
    """
    thinking = thinking or {}
    trace_id = backfill_trace_id(spoke_run_id)
    root_id = backfill_root_id(spoke_run_id)
    base_ts = _earliest_ts(forest)
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
    events: list[IngestEvent] = [trace_event, root_event]
    _walk(
        forest,
        spoke_run_id=spoke_run_id,
        trace_id=trace_id,
        parent_id=root_id,
        thinking=thinking,
        events=events,
    )
    _apply_rollups(events)
    return events
