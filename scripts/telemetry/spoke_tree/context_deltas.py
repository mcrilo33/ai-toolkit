"""Per-request context deltas: what each llm_request added / removed vs its predecessor (#160).

For every LLM call after the first, its raw body is diffed against the previous
(:func:`apply_context_deltas`) into added / removed / changed rows + ``net_tokens``, stamped as
``metadata.context_delta`` on the call's copy; an added message matching a ``tool:Skill`` output is
tagged with the skill name. :func:`_apply_context_rollups` aggregates each step's llm_request
deltas onto its ``metadata.rollup.context``. Depends on the foundation, ``indices``, ``steps``, and
``request_body``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import cast

from telemetry.langfuse_rollup import build_tree
from telemetry.measure_context_cost import TokenCounter
from telemetry.request_body import (
    ContextDelta,
    ContextItem,
    diff_snapshots,
    snapshot_items_from_path,
)
from telemetry.spoke_tree.ids import _CYCLE_STEP_PREFIX, _copy_id
from telemetry.spoke_tree.indices import _SKILL_TOOL_NAME, _activated_skill_name
from telemetry.spoke_tree.loaded_context import find_request_files
from telemetry.spoke_tree.observations import (
    IngestEvent,
    ToolContent,
    TraceObservations,
    _llm_requests_in_order,
    _tool_use_id,
)
from telemetry.spoke_tree.steps import _STEP_PREFIX

logger = logging.getLogger("langfuse_spoke_tree")


def _blob_hash(value: object) -> str:
    """Return a stable content hash of a str or JSON-able value (skill-output match identity)."""
    text = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skill_output_hashes(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, str]:
    """Map each ``tool:Skill`` transcript output's content hash to its skill name (#160).

    The exact identity a skill-load injects into the next request is the tool_result the
    ``tool:Skill`` returned, so its content hash keys the attribution; the name comes from the
    tool's transcript input (:func:`_activated_skill_name`).
    """
    hashes: dict[str, str] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("name") or "") != _SKILL_TOOL_NAME:
                continue
            tuid = _tool_use_id(observation)
            content = tool_content.get(tuid or "")
            name = _activated_skill_name(tuid, tool_content)
            if content is None or content.output is None or not name:
                continue
            hashes[_blob_hash(content.output)] = name
    return hashes


def _match_skill_output(text: str | None, skill_hashes: dict[str, str]) -> str | None:
    """Return the skill whose output an added message injected, matched by content hash, else None.

    The message text is the canonical ``{role, content}`` JSON; a skill-load rides a
    ``tool_result`` block whose ``content`` is the skill's output — that block's hash (or, for a
    plain-string message content, the content itself) is matched against :func:`_skill_output_hashes`.
    """
    if not text:
        return None
    try:
        message = json.loads(text)
    except (TypeError, ValueError):
        return None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return skill_hashes.get(_blob_hash(content))
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            skill = skill_hashes.get(_blob_hash(block.get("content")))
            if skill:
                return skill
    return None


def _label_skill_loads(
    added: list[dict[str, object]],
    curr_items: list[ContextItem],
    skill_hashes: dict[str, str],
) -> None:
    """Label each added-message row whose injected content matches a skill output, in place (#160)."""
    if not skill_hashes:
        return
    text_by_name = {item.name: item.text for item in curr_items if item.category == "messages"}
    for row in added:
        if row.get("category") != "messages":
            continue
        skill = _match_skill_output(text_by_name.get(str(row.get("name"))), skill_hashes)
        if skill:
            row["skill"] = skill


def _context_delta_summary(delta: ContextDelta) -> dict[str, int]:
    """Reduce a context delta to the token totals rolled up onto a step (net / added / removed)."""
    added = sum(int(cast(int, row["tokens"])) for row in delta.added)
    removed = sum(int(cast(int, row["tokens"])) for row in delta.removed)
    return {"net_tokens": delta.net_tokens, "added": added, "removed": removed}


def apply_context_deltas(
    batch: list[IngestEvent],
    traces: list[TraceObservations],
    bodies_dir: Path,
    *,
    counter: TokenCounter,
    price: float,
    tool_content: dict[str, ToolContent],
) -> dict[tuple[str, str], dict[str, int]]:
    """Stamp ``metadata.context_delta`` on each llm_request copy from consecutive bodies (#160).

    For every LLM call after the first (aligned positionally with its raw request body, same count
    gate as :func:`apply_llm_decomposition`), the body is diffed against its predecessor
    (:func:`telemetry.request_body.diff_snapshots`) into added / removed / size-changed rows,
    ``net_tokens`` (which reconciles ± remainder against the call's observed ``cache_creation``),
    and a compaction ``label``. An added message whose injected content matches a ``tool:Skill``
    output is tagged with the skill name (:func:`_label_skill_loads`). The delta is stamped on the
    call's View A copy only (single-emit) as metadata — it never touches billed usage.

    Args:
        batch: The assembled View A events; the llm_request copies are mutated in place.
        traces: The source traces paired with their observations.
        bodies_dir: The per-spoke raw-body dump directory.
        counter: Token counter (memoize it — the stable prefix repeats every snapshot).
        price: Cache-creation price in USD per token.
        tool_content: Tool-call-id to :class:`ToolContent`, the source of skill outputs.

    Returns:
        A map of each stamped call's ``(orig_trace_id, observation_id)`` to its
        :func:`_context_delta_summary`, so the step ``rollup.context`` can be aggregated per view.
    """
    calls = _llm_requests_in_order(traces)
    bodies = find_request_files(bodies_dir)
    if not calls or len(calls) != len(bodies):
        return {}
    by_id = {event["body"]["id"]: event for event in batch}
    skill_hashes = _skill_output_hashes(traces, tool_content)
    summaries: dict[tuple[str, str], dict[str, int]] = {}
    prev_items: list[ContextItem] | None = None
    for (orig_trace_id, observation), body_path in zip(calls, bodies, strict=False):
        try:
            curr_items = snapshot_items_from_path(body_path)
        except (OSError, json.JSONDecodeError):
            logger.warning("cannot snapshot request body %s", body_path)
            prev_items = None
            continue
        predecessor, prev_items = prev_items, curr_items
        if predecessor is None:
            continue  # the first call has no prior snapshot to diff against
        event = by_id.get(_copy_id(orig_trace_id, observation["id"]))
        if event is None:
            continue  # the call's copy is not in the batch (defensive; should not happen)
        delta = diff_snapshots(predecessor, curr_items, counter=counter, price=price)
        _label_skill_loads(delta.added, curr_items, skill_hashes)
        event["body"].setdefault("metadata", {})["context_delta"] = {
            "added": delta.added,
            "removed": delta.removed,
            "changed": delta.changed,
            "net_tokens": delta.net_tokens,
            "label": delta.label,
        }
        summaries[(orig_trace_id, observation["id"])] = _context_delta_summary(delta)
    return summaries


def _apply_context_rollups(
    events: list[IngestEvent], summary_by_id: dict[str, dict[str, int]]
) -> None:
    """Aggregate ``metadata.rollup.context`` onto each step node from its llm_request deltas (#160).

    Sums the per-call context summaries of every llm_request copy in a step's subtree into
    ``{net_tokens, added, removed}`` under the step's existing ``metadata.rollup``, so per-cycle
    context cost reads without a full-trace GET. View-agnostic: the caller keys ``summary_by_id``
    by that view's copy ids (View A ``tree-…`` / View B ``cyc-…``). A step with no llm_request
    delta gets no ``context`` key.

    Args:
        events: The assembled events for one view; step-node bodies are mutated in place.
        summary_by_id: Copy id (in this view's namespace) to its context-delta summary.
    """
    bodies = [event["body"] for event in events if event["type"] != "trace-create"]
    _by_id, children = build_tree(bodies)
    for body in bodies:
        node_id = body["id"]
        if not (node_id.startswith(_STEP_PREFIX) or node_id.startswith(_CYCLE_STEP_PREFIX)):
            continue
        context = _sum_context(node_id, children, summary_by_id)
        if context is not None:
            body.setdefault("metadata", {}).setdefault("rollup", {})["context"] = context


def _sum_context(
    node_id: str, children: dict[str | None, list[str]], summary_by_id: dict[str, dict[str, int]]
) -> dict[str, int] | None:
    """Sum the context summaries of a step's descendant llm_requests, or None when it has none."""
    total = {"net_tokens": 0, "added": 0, "removed": 0}
    found = False
    stack = list(children.get(node_id, []))
    while stack:
        current = stack.pop()
        summary = summary_by_id.get(current)
        if summary is not None:
            found = True
            for key in total:
                total[key] += summary[key]
        stack.extend(children.get(current, []))
    return total if found else None
