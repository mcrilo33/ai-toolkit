#!/usr/bin/env python3
"""Fill tool-call CONTENT (input/output) onto a spoke's Langfuse tool spans.

Claude Code's native OTel surfaces the full ``full_command`` for Bash, but for every other
tool (TaskCreate/TaskUpdate, Read, Edit, ...) the span arrives with ``input=None`` -- only
``tool_name``, ``tool_use_id`` and ``duration`` are emitted. The real content lives in the
session TRANSCRIPT (``*.jsonl``): each assistant ``tool_use`` block carries
``{id, name, input}`` and the matching user ``tool_result`` block carries
``{tool_use_id, content}``. This post-run script joins them by ``tool_use_id`` and PATCHes
the matching Langfuse observation's ``input``/``output``::

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_BASIC_AUTH="Basic <base64(pk:sk)>" \\
        python3 scripts/telemetry/langfuse_tool_content.py <spoke_run_id>

The join chain is:

- Step A: fetch the session's traces and observations; map ``tool_use_id -> (observation_id,
  type)`` for the VISIBLE tool span only -- an observation whose ``name`` starts with
  ``tool:`` (e.g. ``tool:TaskCreate``, ``tool:Read``). Many sibling observations share one
  ``tool_use_id`` -- ``claude_code.tool.execution``, ``claude_code.tool.blocked_on_user``,
  and the ``*.sh`` hook spans -- so indexing every id-bearing observation would let a hook or
  execution span shadow the real ``tool:`` span and leave it with ``input=None``. The
  ``tool:`` name filter keeps exactly one tool span per id as the patch target.
- Step B: scan the transcripts for ``tool_use`` (id -> input) and ``tool_result``
  (tool_use_id -> content) blocks, keeping only ids present in the Step-A map. Tool-call ids
  are globally unique, so no per-session transcript mapping is needed.
- Step C: PATCH each matched observation (``generation-update`` for a GENERATION, else
  ``span-update``) with ``input`` and, when present, ``output``. The event id is derived from
  the observation id, so a rerun overwrites instead of appending; oversized output is
  truncated with a marker.

Ingestion-event note: ``tool:`` spans are type ``SPAN`` (not ``GENERATION``), and a
``span-update`` event's body DOES carry ``input``/``output`` (Langfuse's update-span body
accepts both), so the patch lands on the SPAN observation. ``generation-update`` is reserved
for the rare GENERATION case; both update-event bodies support ``input``/``output``.

Run BEFORE re-running ``langfuse_spoke_tree.py``: the tree copies ``input``/``output``
verbatim, so it only picks up the content once these source spans carry it.

Import-safe: no environment is read at import time, so the pure helpers are unit-testable
with no network. The HTTP I/O happens only in :func:`main`. Stdlib only; reuses the
fetch/post helpers and session walk of ``langfuse_rollup`` / ``langfuse_spoke_tree``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

from telemetry.langfuse_rollup import GetFn, Observation, PostFn, make_get, make_post
from telemetry.langfuse_spoke_tree import (
    TraceObservations,
    fetch_session,
    post_in_chunks,
)

logger = logging.getLogger("langfuse_tool_content")

IngestEvent = dict[str, Any]

# Langfuse ingestion requires a timestamp on every event; an update event only patches an
# existing observation, so the value is not meaningful and a fixed stamp keeps reruns stable.
_INGEST_TIMESTAMP = "2026-01-01T00:00:00Z"

# Metadata keys that may carry a tool-call id, in priority order (nested under "attributes").
_TOOL_USE_ID_KEYS = ("tool_use_id", "gen_ai.tool.call.id")

# Deterministic ingestion-event id prefix -- a rerun updates the same observation.
_EVENT_PREFIX = "toolcontent-"

# Tool output (e.g. a large file Read) can be huge; cap the serialized text past this.
_MAX_CONTENT_CHARS = 20_000
_TRUNCATION_MARKER = "...[truncated]"

# Default root holding Claude Code session transcripts.
_DEFAULT_PROJECTS = Path("~/.claude/projects").expanduser()


class ToolSpan(NamedTuple):
    """A Langfuse tool observation keyed by its tool-call id."""

    observation_id: str
    observation_type: str  # "GENERATION" or "SPAN"


class ToolContent(NamedTuple):
    """The transcript-sourced content of one tool call."""

    input: object  # the tool_use input args
    output: object | None  # the tool_result content, or None when no result block exists


def _tool_use_id(observation: Observation) -> str | None:
    """Return an observation's tool-call id from its metadata, or None if absent.

    Langfuse nests OTel span attributes under ``metadata["attributes"]``, so each candidate
    key is read from there first and only then from the top level (a fallback for flatter
    shapes).

    Args:
        observation: A Langfuse observation as returned by the public API.

    Returns:
        The tool-call id as a string, or None when the observation carries none.
    """
    metadata = observation.get("metadata") or {}
    attributes = metadata.get("attributes") or {}
    for key in _TOOL_USE_ID_KEYS:
        value = attributes.get(key) or metadata.get(key)
        if value:
            return str(value)
    return None


def _is_tool_span(observation: Observation) -> bool:
    """Whether an observation is the visible ``tool:<Name>`` span (the patch target).

    Many observations share one ``tool_use_id`` -- ``claude_code.tool.execution``,
    ``claude_code.tool.blocked_on_user``, and ``*.sh`` hook spans -- but only the span whose
    ``name`` starts with ``tool:`` (e.g. ``tool:TaskCreate``) carries the tool call the user
    sees; the rest are excluded so they cannot shadow it.

    Args:
        observation: A Langfuse observation as returned by the public API.

    Returns:
        True when the observation's name marks it as the visible tool span.
    """
    name = observation.get("name") or ""
    return name.startswith("tool:")


def build_span_index(traces: list[TraceObservations]) -> dict[str, ToolSpan]:
    """Map each tool-call id to the visible ``tool:`` span that owns it across a session.

    Only observations whose name starts with ``tool:`` are indexed (see :func:`_is_tool_span`);
    sibling hook/execution/blocked spans that share the same ``tool_use_id`` are skipped, so the
    real tool span stays the patch target. One ``tool_use_id`` maps to exactly one tool span.

    Args:
        traces: Each native trace paired with all of its observations.

    Returns:
        A mapping of ``tool_use_id`` to its :class:`ToolSpan`.
    """
    index: dict[str, ToolSpan] = {}
    for _trace_id, observations in traces:
        for observation in observations:
            if not _is_tool_span(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid and tuid not in index:
                index[tuid] = ToolSpan(observation["id"], observation.get("type") or "SPAN")
    return index


def _scan_blocks(content: list[Any], wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Collect ``tool_use`` inputs and ``tool_result`` contents for the wanted ids.

    Args:
        content: A message's ``content`` block list from one transcript line.
        wanted: The tool-call ids present in the Langfuse session (others are skipped).
        found: Accumulator mapping a tool-call id to its ``{"input"/"output": value}``.
    """
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("id") in wanted:
            found.setdefault(block["id"], {})["input"] = block.get("input")
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in wanted:
            found.setdefault(block["tool_use_id"], {})["output"] = block.get("content")


def scan_transcripts(root: Path, wanted: set[str]) -> dict[str, ToolContent]:
    """Scan every transcript under ``root`` for the wanted tool calls' input/output.

    Args:
        root: The Claude Code projects root holding session ``*.jsonl`` transcripts.
        wanted: The tool-call ids present in the Langfuse session.

    Returns:
        A mapping of ``tool_use_id`` to its :class:`ToolContent`, for ids that carry an
        ``input`` block in the transcripts (a bare ``tool_result`` with no ``tool_use`` is
        dropped, as it cannot identify a tool span on its own).
    """
    found: dict[str, dict[str, object]] = {}
    if not wanted:
        return {}
    for path in sorted(root.rglob("*.jsonl")):
        _scan_file(path, wanted, found)
    return {
        tuid: ToolContent(parts["input"], parts.get("output"))
        for tuid, parts in found.items()
        if "input" in parts
    }


def _scan_file(path: Path, wanted: set[str], found: dict[str, dict[str, object]]) -> None:
    """Scan one transcript file line by line, ignoring malformed lines.

    Args:
        path: The transcript ``*.jsonl`` file.
        wanted: The tool-call ids present in the Langfuse session.
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


def _capped(value: object) -> object:
    """Return ``value`` unchanged, or a truncated string when its serialized form is large.

    Small structured values are passed through so Langfuse renders them richly; only content
    whose serialized text exceeds :data:`_MAX_CONTENT_CHARS` (e.g. a large file Read) is
    flattened to a truncated string with a marker.

    Args:
        value: The tool input or output to size-check.

    Returns:
        The original value, or a truncated string when it is too large.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + _TRUNCATION_MARKER
    return value


def content_event(span: ToolSpan, content: ToolContent) -> IngestEvent:
    """Shape one ingestion event patching input/output onto a tool observation.

    The event type tracks the observation type (``generation-update`` for a GENERATION,
    ``span-update`` otherwise). The event id derives from the observation id so a rerun
    overwrites the same observation instead of appending.

    Args:
        span: The target observation (id and type).
        content: The transcript-sourced input and optional output.

    Returns:
        A Langfuse ingestion batch event.
    """
    event_type = "generation-update" if span.observation_type == "GENERATION" else "span-update"
    body: dict[str, Any] = {"id": span.observation_id, "input": _capped(content.input)}
    if content.output is not None:
        body["output"] = _capped(content.output)
    return {
        "id": _EVENT_PREFIX + span.observation_id,
        "type": event_type,
        "timestamp": _INGEST_TIMESTAMP,
        "body": body,
    }


def build_batch(index: dict[str, ToolSpan], contents: dict[str, ToolContent]) -> list[IngestEvent]:
    """Build the ingestion batch patching content onto every matched tool span.

    Args:
        index: Tool-call-id to :class:`ToolSpan` map from :func:`build_span_index`.
        contents: Tool-call-id to :class:`ToolContent` map from :func:`scan_transcripts`.

    Returns:
        One ingestion event per tool-call id present in both maps, in ``index`` order.
    """
    return [content_event(index[tuid], contents[tuid]) for tuid in index if tuid in contents]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the CLI arguments for the tool-content filler."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("spoke_run_id", help="The spoke run id (session id) to enrich.")
    parser.add_argument(
        "--projects",
        type=Path,
        default=_DEFAULT_PROJECTS,
        help="Root holding Claude Code session transcripts (default: ~/.claude/projects).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Fill tool input/output onto a spoke's Langfuse tool spans from the transcript.

    Args:
        argv: CLI arguments excluding the program name; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success.

    Raises:
        KeyError: When ``LANGFUSE_BASIC_AUTH`` is not set.
    """
    logging.basicConfig(level=logging.INFO, format="[tool-content] %(message)s")
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    auth = os.environ["LANGFUSE_BASIC_AUTH"]  # "Basic <base64(pk:sk)>"
    get: GetFn = make_get(host, auth)
    post: PostFn = make_post(host, auth)

    index = build_span_index(fetch_session(args.spoke_run_id, get))
    contents = scan_transcripts(args.projects, set(index))
    batch = build_batch(index, contents)
    post_in_chunks(batch, post)

    print(
        f"{len(batch)} tool spans enriched (of {len(contents)} matched / {len(index)} in session)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
