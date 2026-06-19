#!/usr/bin/env python3
"""Re-emit one spoke's strict causal forest as a single nested Langfuse trace.

The dashboard already builds the strict nested causal forest for a spoke
(``SpanStore.spoke_causal_forest``): turns own their cost, tools/sub-agents/hooks
nest underneath, and a marker spine threads the lifecycle. Natively each turn lands
as its own flat Langfuse trace, so a spoke reads as dozens of disconnected traces.
This post-run script rebuilds that one forest and ships it to Langfuse as ONE trace
via the ingestion API, so the whole spoke renders as a single tree.

Run AFTER the native per-turn traces are ingested::

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \\
        python3 scripts/telemetry/langfuse_spoke_tree.py <spoke_run_id>

All ids are derived from the spoke run id and each node's tree path, so a rerun
overwrites the same trace/observations instead of appending duplicates. This trace
DUPLICATES the native per-turn traces by design — it is the assembled, nested view.

Import-safe: no environment and no dashboard import at module load. The pure builders
(:func:`build_batch`) are unit-testable with no network and no DuckDB; the heavy
``SpanStore`` import and Langfuse I/O happen only in :func:`main`. Stdlib only; reuses
the same env vars and ingestion endpoint as ``langfuse_rollup``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("langfuse_spoke_tree")

CausalNode = dict[str, Any]
IngestEvent = dict[str, Any]

# Deterministic id prefixes — a rerun resolves to the same trace/observation ids.
_TRACE_PREFIX = "spoketree-"
_NODE_PREFIX = "node-"
_TRACE_NAME_PREFIX = "spoke-tree:"

# Base instant for synthesizing time windows when a node carries no absolute times;
# fixed so the emission stays deterministic and idempotent across reruns.
_SYNTH_BASE = "2026-01-01T00:00:00Z"

# Readable label prefixes by node kind (e.g. ``tool:Bash``, ``sub-agent:Explore``).
_LABEL_PREFIX: dict[str, str] = {
    "tool": "tool",
    "agent": "sub-agent",
    "step": "step",
    "hook": "hook",
    "script": "marker",
}


def trace_id_for(spoke_run_id: str) -> str:
    """Return the deterministic trace id for a spoke's assembled tree.

    Args:
        spoke_run_id: The spoke run identifier.

    Returns:
        A stable ``spoketree-<sha1[:16]>`` id, identical across reruns.
    """
    return _TRACE_PREFIX + hashlib.sha1(spoke_run_id.encode()).hexdigest()[:16]


def _node_id(trace_id: str, node_path: str) -> str:
    """Return the deterministic observation id for a node at ``node_path`` in the tree."""
    digest = hashlib.sha1(f"{trace_id}:{node_path}".encode()).hexdigest()[:16]
    return _NODE_PREFIX + digest


def _is_token_bearing(node: CausalNode) -> bool:
    """Whether a node carries any tokens/cost — i.e. should map to a generation."""
    return bool(
        node.get("own_tokens_in")
        or node.get("own_tokens_out")
        or node.get("own_cost_usd")
        or node.get("cache_read")
        or node.get("cache_creation")
    )


def _label(node: CausalNode) -> str:
    """Build a readable observation name from the node's kind and summary."""
    kind = node["kind"]
    detail = node.get("summary") or node.get("name") or ""
    prefix = _LABEL_PREFIX.get(kind)
    if prefix and detail:
        return f"{prefix}:{detail}"
    return node.get("name") or kind


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``); None if unparseable."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_iso(moment: datetime) -> str:
    """Render a datetime as a millisecond-precision ISO-8601 string with a ``Z`` suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Clock:
    """A monotonic cursor that fills in time windows for nodes lacking absolute times.

    Real ``ts_start``/``ts_end`` pass through unchanged; otherwise sequential windows are
    synthesized from ``duration_ms`` (a minimum 1ms step) so ordering still renders.
    """

    def __init__(self, base_iso: str) -> None:
        self.cursor: datetime = _parse_iso(base_iso) or datetime(2026, 1, 1, tzinfo=UTC)

    def window(self, node: CausalNode) -> tuple[str, str]:
        """Return the ``(startTime, endTime)`` ISO strings for one node, advancing the cursor."""
        start_dt = _parse_iso(node.get("ts_start") or "")
        start_iso = node["ts_start"] if start_dt else _to_iso(self.cursor)
        start = start_dt or self.cursor
        duration = timedelta(milliseconds=int(node.get("duration_ms") or 0))
        end_dt = _parse_iso(node.get("ts_end") or "")
        if end_dt:
            end_iso, end = node["ts_end"], end_dt
        else:
            end = start + (duration or timedelta(milliseconds=1))
            end_iso = _to_iso(end)
        self.cursor = max(self.cursor + timedelta(milliseconds=1), end)
        return start_iso, end_iso


def _observation_event(
    node: CausalNode, *, obs_id: str, trace_id: str, parent_id: str | None, window: tuple[str, str]
) -> IngestEvent:
    """Shape one ingestion event for a node: a generation if token-bearing, else a span."""
    start, end = window
    token_bearing = _is_token_bearing(node)
    body: dict[str, Any] = {
        "id": obs_id,
        "traceId": trace_id,
        "name": _label(node),
        "startTime": start,
        "endTime": end,
        "metadata": {
            "kind": node["kind"],
            "status": node.get("status"),
            "rollup": node.get("rollup"),
        },
    }
    if parent_id is not None:
        body["parentObservationId"] = parent_id
    if token_bearing:
        body["usageDetails"] = {
            "input": int(node.get("own_tokens_in") or 0),
            "output": int(node.get("own_tokens_out") or 0),
            "cache_read_input_tokens": int(node.get("cache_read") or 0),
            "cache_creation_input_tokens": int(node.get("cache_creation") or 0),
        }
    event_type = "generation-create" if token_bearing else "span-create"
    return {"id": obs_id, "type": event_type, "timestamp": start, "body": body}


def _emit_node(
    node: CausalNode,
    *,
    trace_id: str,
    parent_id: str | None,
    path: str,
    clock: _Clock,
    out: list[IngestEvent],
) -> None:
    """Depth-first: append this node's event, then recurse into its children."""
    obs_id = _node_id(trace_id, path)
    out.append(
        _observation_event(
            node, obs_id=obs_id, trace_id=trace_id, parent_id=parent_id, window=clock.window(node)
        )
    )
    for index, child in enumerate(node["children"]):
        _emit_node(
            child,
            trace_id=trace_id,
            parent_id=obs_id,
            path=f"{path}/{index}",
            clock=clock,
            out=out,
        )


def build_batch(forest: list[CausalNode], spoke_run_id: str) -> list[IngestEvent]:
    """Build the full ingestion batch for a spoke: one trace plus one event per node.

    Walks the forest depth-first so observations are emitted parent-before-child, each
    linked to its parent via ``parentObservationId``. All ids derive from the spoke run
    id and tree path, so the batch is idempotent.

    Args:
        forest: The strict causal forest from ``SpanStore.spoke_causal_forest``.
        spoke_run_id: The spoke run identifier (becomes the trace's ``sessionId``).

    Returns:
        The ingestion events: a leading ``trace-create`` followed by the DFS node events.
    """
    trace_id = trace_id_for(spoke_run_id)
    root_ts = (forest[0].get("ts_start") if forest else None) or _SYNTH_BASE
    trace_event: IngestEvent = {
        "id": trace_id,
        "type": "trace-create",
        "timestamp": root_ts,
        "body": {
            "id": trace_id,
            "name": _TRACE_NAME_PREFIX + spoke_run_id,
            "sessionId": spoke_run_id,
            "timestamp": root_ts,
        },
    }
    clock = _Clock(_SYNTH_BASE)
    nodes: list[IngestEvent] = []
    for index, root in enumerate(forest):
        _emit_node(root, trace_id=trace_id, parent_id=None, path=str(index), clock=clock, out=nodes)
    return [trace_event, *nodes]


def _load_span_store(events_path: Path) -> Any:
    """Import ``dashboard/queries.py`` by file path and build a SpanStore from the WAL.

    The dashboard is deliberately off the Python package path (its own
    ``requirements.txt``), so it is loaded by file path here exactly as the test
    harness does — keeping this script import-safe and DuckDB-free until run.

    Args:
        events_path: The telemetry ``events.jsonl`` push-span WAL.

    Returns:
        A ``SpanStore`` over the WAL.

    Raises:
        ImportError: When ``dashboard/queries.py`` cannot be loaded.
    """
    queries_path = Path(__file__).resolve().parents[2] / "dashboard" / "queries.py"
    spec = importlib.util.spec_from_file_location("dashboard_queries", queries_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load queries module from {queries_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SpanStore.from_jsonl(events_path)


def _post(batch: list[IngestEvent], *, host: str, auth: str) -> None:
    """POST an ingestion batch to the Langfuse ingestion endpoint."""
    data = json.dumps({"batch": batch}).encode()
    request = urllib.request.Request(
        f"{host}/api/public/ingestion",
        data=data,
        headers={"Authorization": auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        resp.read()


def _default_events_path() -> Path:
    """The default telemetry WAL path, honoring ``AI_TOOLKIT_TELEMETRY_DIR``."""
    base = os.environ.get("AI_TOOLKIT_TELEMETRY_DIR") or str(
        Path.home() / ".ai-toolkit" / "telemetry"
    )
    return Path(base) / "events.jsonl"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the spoke-tree emitter."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id to assemble and emit.")
    parser.add_argument(
        "--events", type=Path, default=None, help="Telemetry events.jsonl (default: telemetry dir)."
    )
    parser.add_argument(
        "--projects",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="Claude projects root holding the spoke's session transcripts.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Assemble a spoke's causal forest and emit it as one nested Langfuse trace.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 1 when the spoke has no parseable forest.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[spoke-tree] %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    events_path = args.events or _default_events_path()
    store = _load_span_store(events_path)
    forest = store.spoke_causal_forest(args.spoke_run_id, args.projects)
    if not forest:
        logger.error("no causal forest for spoke %s (no parseable transcript)", args.spoke_run_id)
        return 1

    batch = build_batch(forest, args.spoke_run_id)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    _post(batch, host=host, auth=auth)

    trace_id = batch[0]["id"]
    print(f"{len(batch) - 1} nodes emitted under trace {trace_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
