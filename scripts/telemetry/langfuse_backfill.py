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

- a ``turn``/``agent`` leaf -> a ``generation-create`` carrying the turn's four-component
  ``usageDetails`` (Langfuse computes ``costDetails`` from its model-pricing config);
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
    rollup_metadata,
    subtree_totals,
)
from telemetry.langfuse_spoke_tree import all_traces, post_in_chunks
from telemetry.session_parser import (
    parse_project_dir,
    parse_projects_dir,
    parse_session_file,
    project_dir_for_worktree,
    thinking_by_turn,
)

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
    """Token usage of an inference node (zeros when a field is absent).

    Cache writes are split by ephemeral TTL (Issue #97): the 5m tier maps to Langfuse's
    ``cache_creation_input_tokens`` (priced 1.25x input) and the 1h tier to its
    ``input_cache_creation_1h`` usage type (2x). The 1h key is emitted only when nonzero,
    so a turn with no 1h writes keeps the pre-#97 four-component shape. When the split is
    absent (older nodes) the whole flat total falls into the 5m tier.
    """
    flat_creation = int(node.get("cache_creation") or 0)
    creation_5m = int(node["cache_creation_5m"]) if "cache_creation_5m" in node else flat_creation
    creation_1h = int(node.get("cache_creation_1h") or 0)
    usage = {
        "input": int(node.get("own_tokens_in") or 0),
        "output": int(node.get("own_tokens_out") or 0),
        "cache_read_input_tokens": int(node.get("cache_read") or 0),
        "cache_creation_input_tokens": creation_5m,
    }
    if creation_1h:
        usage["input_cache_creation_1h"] = creation_1h
    return usage


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
        body.setdefault("metadata", {})["rollup"] = rollup_metadata(totals)


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


def _reasoning_node(turn_uuid: str, base_ts: str) -> dict[str, Any]:
    """A synthetic ``reasoning`` node for one thinking body, keyed by its turn uuid.

    Sourced from the ``thinking`` map directly, not from the forest: the dedup that keeps
    one usage event per inference (Issue #78) keeps a DIFFERENT transcript record than the
    one carrying the thinking block, so the surviving turn's ``uuid`` rarely matches the
    thinking key and the forest's attached ``reasoning`` children are usually absent. The
    node id stays ``reasoning:<turn uuid>`` so ``_add_reasoning_body`` joins the body and the
    id matches the full-backfill path's, keeping a rerun idempotent.
    """
    return {
        "node_id": f"{_REASONING_PREFIX}{turn_uuid}",
        "kind": "reasoning",
        "name": "reasoning",
        "ts_start": base_ts,
        "ts_end": base_ts,
        "children": [],
    }


def reasoning_only_events(
    forest: list[dict[str, Any]], spoke_run_id: str, thinking: dict[str, str]
) -> list[IngestEvent]:
    """Emit ONLY the reasoning bodies (the gap the live push lacks), under the spoke root.

    For a session the live push already covered, the assembled view exists already; the one
    thing the transcript adds is the extended-thinking body (redacted in every raw API body).
    One reasoning observation is emitted per ``thinking`` entry — sourced from the map, NOT
    from the forest's ``reasoning`` children, which the usage dedup (Issue #78) usually
    strips by keeping a turn whose ``uuid`` differs from the thinking record's. Each is a new
    observation the live push never had (no double-write), parented under the backfill root,
    with deterministic ids so a rerun overwrites. Returns ``[]`` when no thinking is present.

    Args:
        forest: The spoke's causal forest — used only for its earliest timestamp.
        spoke_run_id: The spoke run id; the trace's ``sessionId``.
        thinking: ``turn uuid -> extended-thinking body``; one observation per entry.

    Returns:
        A ``trace-create`` + root + one span per thinking body, or ``[]`` when none.
    """
    if not thinking:
        return []
    trace_id = backfill_trace_id(spoke_run_id)
    root_id = backfill_root_id(spoke_run_id)
    base_ts = _earliest_ts(forest)
    events = _trace_and_root(spoke_run_id, base_ts)
    for turn_uuid in thinking:
        events.append(
            _node_event(
                _reasoning_node(turn_uuid, base_ts),
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


def _gather_thinking(
    session: Path | None, projects: Path, project_dir: Path | None = None
) -> dict[str, str]:
    """The opt-in ``turn uuid -> thinking body`` map for one session, one project, or all.

    A single ``--session`` transcript is read directly; a ``project_dir`` (the spoke's own,
    via ``--worktree``) scans only that dir's sessions (``*.jsonl``); otherwise every
    top-level session under ``projects`` (``*/*.jsonl``) is scanned. Results merge (uuids are
    globally unique, so a flat merge is safe). Scoping to ``project_dir`` is what keeps an
    unrelated session's reasoning out of this spoke's backfill (Issues #92/#98). Called only
    when the opt-in flag is set, so the body is never read otherwise.
    """
    if session is not None:
        return thinking_by_turn(session)
    paths = project_dir.glob("*.jsonl") if project_dir is not None else projects.glob("*/*.jsonl")
    merged: dict[str, str] = {}
    for path in sorted(paths):
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
        "--worktree",
        type=Path,
        default=None,
        help=(
            "Scope the backfill to the spoke's own worktree: only the sessions under that "
            "worktree's Claude Code project dir are read (resumes included), never the hub "
            "or sibling worktrees (Issues #92/#98). Overrides --projects' broad scan."
        ),
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
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # --worktree binds the backfill to the spoke's own project dir with NO fallback to the
    # broad scan: a missing dir yields an empty backfill, never another spoke's transcripts.
    # That strictness is the fix — do NOT add a whole-root fallback here the way
    # langfuse_spoke_tree.transcript_scan_root does (its unique-id match makes a broad scan
    # contamination-safe; this reasoning/content path is not). See Issues #92/#98.
    project_dir = project_dir_for_worktree(args.worktree, args.projects) if args.worktree else None
    if args.session:
        parsed = parse_session_file(args.session)
    elif project_dir is not None:
        parsed = parse_project_dir(project_dir)
    else:
        parsed = parse_projects_dir(args.projects)
    thinking = _gather_thinking(args.session, args.projects, project_dir) if args.thinking else {}
    # Tokens are exact; Langfuse computes cost from the gen_ai.usage remap (Issue #91).
    forest: list[Any] = causal_forest_from_parsed(parsed, [], thinking)

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
    logger.info(
        "%d observations backfilled under trace %s (mode: %s, %d thinking bodies)",
        max(len(events) - 2, 0),
        backfill_trace_id(args.spoke_run_id),
        mode,
        len(thinking),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
