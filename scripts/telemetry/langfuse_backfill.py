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

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from telemetry.causal_tree import causal_forest_from_parsed
from telemetry.langfuse_rollup import (
    GetFn,
    build_tree,
    make_get,
    make_post,
    subtree_totals,
)
from telemetry.langfuse_spoke_tree import all_traces, post_in_chunks
from telemetry.session_parser import parse_projects_dir, parse_session_file, thinking_by_turn

logger = logging.getLogger("langfuse_backfill")

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

# langfuse_spoke_tree's assembled-trace id/name prefixes — its output, like the backfill's
# own, is a synthetic view, NOT a native live-push trace.
_SPOKE_TREE_TRACE_PREFIX = "spoketree-"
_SPOKE_TREE_NAME_PREFIX = "spoke-tree:"

# Opt-in flag (env) for emitting the extended-thinking body (volume / privacy).
_THINKING_ENV = "AI_TOOLKIT_BACKFILL_THINKING"
# Default root holding Claude Code session transcripts.
_DEFAULT_PROJECTS = Path("~/.claude/projects").expanduser()


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
    events: list[IngestEvent] = _trace_and_root(spoke_run_id, base_ts)
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


def _trace_and_root(spoke_run_id: str, base_ts: str) -> list[IngestEvent]:
    """The ``trace-create`` + synthetic-root span both emit paths share."""
    trace_id = backfill_trace_id(spoke_run_id)
    root_id = backfill_root_id(spoke_run_id)
    return [
        {
            "id": trace_id,
            "type": "trace-create",
            "timestamp": base_ts,
            "body": {
                "id": trace_id,
                "name": _TRACE_NAME_PREFIX + spoke_run_id,
                "sessionId": spoke_run_id,
                "timestamp": base_ts,
            },
        },
        {
            "id": root_id,
            "type": "span-create",
            "timestamp": base_ts,
            "body": {
                "id": root_id,
                "traceId": trace_id,
                "name": _ROOT_NAME_PREFIX + spoke_run_id,
                "startTime": base_ts,
            },
        },
    ]


def is_native_trace(trace: dict[str, Any]) -> bool:
    """Whether a fetched session trace is a native live-push trace (not a synthetic view).

    The live OTel push lands native per-turn / marker / hook traces with arbitrary ids and
    names. The backfill's own trace (``spokefill-`` / ``spoke-backfill:``) and
    ``langfuse_spoke_tree``'s assembled tree (``spoketree-`` / ``spoke-tree:``) are synthetic
    views, recognised by their id/name prefixes; everything else is native.

    Args:
        trace: A trace dict as returned by the Langfuse traces endpoint.

    Returns:
        True when the trace is a native live-push trace.
    """
    trace_id = trace.get("id") or ""
    name = trace.get("name") or ""
    synthetic = (
        trace_id.startswith(_TRACE_PREFIX)
        or name.startswith(_TRACE_NAME_PREFIX)
        or trace_id.startswith(_SPOKE_TREE_TRACE_PREFIX)
        or name.startswith(_SPOKE_TREE_NAME_PREFIX)
    )
    return not synthetic


def session_is_covered(spoke_run_id: str, get: GetFn) -> bool:
    """Whether the live push already covered this session (a native trace exists).

    Args:
        spoke_run_id: The session id (``langfuse.session.id``) to check.
        get: Path-to-JSON fetcher (see :data:`telemetry.langfuse_rollup.GetFn`).

    Returns:
        True when at least one native (non-synthetic) trace exists for the session, so a
        full backfill tree would duplicate what the live push already wrote.
    """
    return any(is_native_trace(trace) for trace in all_traces(spoke_run_id, get))


def _reasoning_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect every ``reasoning`` node across the forest, depth-first."""
    found: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("kind") == "reasoning":
            found.append(node)
        found.extend(_reasoning_nodes(node["children"]))
    return found


def reasoning_only_events(
    forest: list[dict[str, Any]], spoke_run_id: str, thinking: dict[str, str]
) -> list[IngestEvent]:
    """Emit ONLY the reasoning nodes (the gap the live push lacks), under the spoke root.

    For a session the live push already covered, the assembled view exists already; the one
    thing the transcript adds is the extended-thinking body (redacted in every raw API body).
    Each reasoning node is emitted under the backfill root as a standalone thinking subtree —
    new observations the live push never had, so no double-write — with deterministic ids so
    a rerun overwrites. Returns an empty batch when no reasoning node is present.

    Args:
        forest: The spoke's causal forest, built WITH the thinking map (so reasoning exists).
        spoke_run_id: The spoke run id; the trace's ``sessionId``.
        thinking: ``turn uuid -> extended-thinking body`` supplying each node's body.

    Returns:
        A ``trace-create`` + root + one span per reasoning node, or ``[]`` when none.
    """
    reasoning = _reasoning_nodes(forest)
    if not reasoning:
        return []
    trace_id = backfill_trace_id(spoke_run_id)
    root_id = backfill_root_id(spoke_run_id)
    base_ts = _earliest_ts(forest)
    events = _trace_and_root(spoke_run_id, base_ts)
    for node in reasoning:
        events.append(
            _node_event(
                node,
                spoke_run_id=spoke_run_id,
                trace_id=trace_id,
                parent_id=root_id,
                thinking=thinking,
            )
        )
    return events


def backfill_events(
    forest: list[dict[str, Any]],
    spoke_run_id: str,
    thinking: dict[str, str],
    *,
    covered: bool,
) -> list[IngestEvent]:
    """The dedup decision: what to ingest given live-push coverage and the thinking opt-in.

    - **uncovered** (OTel off / collector down / historical) -> the full forest, the only
      complete trace for that session;
    - **covered + thinking** -> reasoning-only, the body the live push could not capture;
    - **covered + no thinking** -> nothing (the live push already covers everything else).

    Args:
        forest: The spoke's causal forest.
        spoke_run_id: The spoke run id.
        thinking: ``turn uuid -> extended-thinking body`` (empty unless the opt-in is set).
        covered: Whether the live push already covered this session.

    Returns:
        The ingestion events to post (possibly empty).
    """
    if not covered:
        return forest_to_events(forest, spoke_run_id, thinking)
    if thinking:
        return reasoning_only_events(forest, spoke_run_id, thinking)
    return []


def _gather_thinking(session: Path | None, projects: Path) -> dict[str, str]:
    """The opt-in ``turn uuid -> thinking body`` map for one session or a whole projects root.

    A single ``--session`` transcript is read directly; otherwise every top-level session
    under ``projects`` is scanned and merged (uuids are globally unique, so a flat merge is
    safe). Called only when the opt-in flag is set, so the body is never read otherwise.
    """
    if session is not None:
        return thinking_by_turn(session)
    merged: dict[str, str] = {}
    for path in sorted(projects.glob("*/*.jsonl")):
        if "subagents" in path.parts:
            continue
        merged.update(thinking_by_turn(path))
    return merged


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the transcript→Langfuse backfill."""
    env = os.environ
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id (session id) to backfill.")
    parser.add_argument(
        "--projects",
        type=Path,
        default=_DEFAULT_PROJECTS,
        help="Root holding Claude Code session transcripts (default: ~/.claude/projects).",
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=None,
        help="A single session transcript to backfill (default: scan --projects).",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        default=env.get(_THINKING_ENV) == "1",
        help=f"Emit the extended-thinking body (opt-in; or ${_THINKING_ENV}=1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Backfill one spoke's transcript into Langfuse, deduped against the live push.

    Parses the transcript, builds the causal forest (reusing ``causal_forest_from_parsed``),
    queries Langfuse for live-push coverage, and posts the dedup decision's events. Thinking
    bodies are read only under the opt-in flag.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[backfill] %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    parsed = parse_session_file(args.session) if args.session else parse_projects_dir(args.projects)
    thinking = _gather_thinking(args.session, args.projects) if args.thinking else {}
    # UPGRADE: join ccusage session costs here so backfilled turns carry real cost; tokens
    # are exact, cost is omitted for now (the ccusage pull is out of this subtask's scope).
    forest: list[Any] = causal_forest_from_parsed(parsed, [], {}, thinking)

    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    get, post = make_get(host, auth), make_post(host, auth)

    covered = session_is_covered(args.spoke_run_id, get)
    events = backfill_events(forest, args.spoke_run_id, thinking, covered=covered)
    if events:
        post_in_chunks(events, post)

    mode = (
        "covered→reasoning-only" if covered and thinking else "covered→noop" if covered else "full"
    )
    print(
        f"{max(len(events) - 2, 0)} observations backfilled under trace "
        f"{backfill_trace_id(args.spoke_run_id)} (mode: {mode}, "
        f"{len(thinking)} thinking bodies)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
