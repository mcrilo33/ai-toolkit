"""Re-parent each source observation and copy it verbatim into the assembled trace.

The heart of the span-copy: :func:`_resolve_parent` picks a copy's parent across the original
trace boundaries (intra-trace parent, matching tool / llm_request / ``tool:Skill``, enclosing
turn, or the synthetic root), and :func:`_copy_event` shapes the ``*-create`` event, grafting
transcript-sourced ``input``/``output`` onto graftable spans (:func:`_tool_additions`) and sizing
tool results (#101, :func:`_tool_result_size`). Depends on the foundation modules and
:mod:`~telemetry.spoke_tree.indices`.
"""

from __future__ import annotations

import json
from typing import Any

from telemetry.langfuse_rollup import Observation
from telemetry.spoke_tree.ids import _copy_id
from telemetry.spoke_tree.indices import (
    InteractionIndex,
    SkillCandidate,
    _enclosing_turn,
    _match_skill_tool,
)
from telemetry.spoke_tree.observations import (
    _INGEST_TIMESTAMP,
    IngestEvent,
    ToolContent,
    TraceObservations,
    _is_graftable_span,
    _is_request_audit_event,
    _is_skill_activated,
    _is_tool_span,
    _joins_under_tool,
    _request_id,
    _tool_use_id,
)

# Observation fields copied verbatim into the assembled trace when present.
_COPIED_FIELDS = ("input", "output", "usageDetails", "costDetails", "metadata", "model", "level")
# Tool content (e.g. a large file Read) can be huge; cap the serialized text past this.
_MAX_CONTENT_CHARS = 20_000
_TRUNCATION_MARKER = "...[truncated]"


def _resolve_parent(
    observation: Observation,
    *,
    orig_trace_id: str,
    root_id: str,
    tool_index: dict[str, str],
    request_index: dict[str, str],
    interaction_index: InteractionIndex,
    skill_index: dict[str, list[SkillCandidate]],
) -> str:
    """Resolve the assembled-trace parent id for one source observation.

    Args:
        observation: The source observation.
        orig_trace_id: The id of the trace the observation came from.
        root_id: The synthetic root span id (the single collapsed root).
        tool_index: Tool-call-id to tool-copy-id map from :func:`_build_tool_index`.
        request_index: Request-id to llm_request-copy-id map from :func:`_build_request_index`.
        interaction_index: Enclosing-turn lookup from :func:`_build_interaction_index`.
        skill_index: Prompt-id to ``tool:Skill`` candidates from :func:`_build_skill_index`.

    Returns:
        The copy id of the intra-trace parent, the matching tool / llm_request / ``tool:Skill``,
        the enclosing turn (for an unmatched-tool satellite), or the synthetic root.
    """
    parent = observation.get("parentObservationId")
    if parent:
        return _copy_id(orig_trace_id, parent)
    if _joins_under_tool(observation):
        tuid = _tool_use_id(observation)
        if tuid and tuid in tool_index:
            return tool_index[tuid]
        # #110 AC1 / #157: a satellite naming a tool that produced no span (denied/cancelled) is
        # normally already resolved above — #157 synthesizes a blocked-tool node for every such
        # orphaned tuid and augments tool_index, so the branch above catches it. This is a
        # defensive fallback (re-home to the enclosing turn) kept for depth in case a satellite's
        # tuid ever escapes synthesis; a hook naming no tool (SessionStart/Stop) has no tuid and
        # still falls through to the root.
        if tuid:
            turn = _enclosing_turn(observation, interaction_index)
            if turn is not None:
                return turn
    if _is_skill_activated(observation):
        # #110 AC2: nest under the tool:Skill that activated it, else its enclosing turn.
        skill_tool = _match_skill_tool(observation, skill_index)
        if skill_tool is not None:
            return skill_tool
        turn = _enclosing_turn(observation, interaction_index)
        if turn is not None:
            return turn
    if _is_request_audit_event(observation):
        rid = _request_id(observation)
        if rid and rid in request_index:
            return request_index[rid]
    return root_id


def _tool_span_ids(traces: list[TraceObservations]) -> set[str]:
    """Collect the tool-call ids of every graftable ``tool:`` / ``sub-agent:`` span."""
    ids: set[str] = set()
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_graftable_span(observation):
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

    A visible ``tool:`` span or a ``sub-agent:<type>`` container with a matching transcript
    entry contributes, and only for a field the source span does not already carry — so
    collector-provided content (Bash's ``input``) is never overwritten and non-graftable spans
    are untouched. Oversized values are truncated by :func:`_capped`.

    Args:
        observation: The source observation being copied.
        tool_content: Tool-call-id to :class:`ToolContent` from :func:`scan_transcripts`.

    Returns:
        A mapping with ``input`` and/or ``output`` to merge into the body, or ``{}``.
    """
    if not _is_graftable_span(observation):
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


def _tool_result_size(observation: Observation, tool_content: dict[str, ToolContent]) -> int | None:
    """Return the byte size of a tool span's reconstructed ``tool_result``, or None (#101).

    Measures the RAW transcript output (before :func:`_capped` truncates the display copy), so a
    large tool result reports its true size. None for a non-tool span or one with no reconstructed
    output, so the caller emits no score for it.
    """
    # UPGRADE: sizing stays tool:-only, so a sub-agent's grafted output is not sized for the #101
    # bloat chart — widen to _is_graftable_span if sub-agent report bloat needs charting (it would
    # add a tool_result_size score per sub-agent, a cardinality change worth its own test).
    if not _is_tool_span(observation):
        return None
    content = tool_content.get(_tool_use_id(observation) or "")
    if content is None or content.output is None:
        return None
    # UPGRADE: structured output is sized by its json.dumps envelope (keys/braces/quotes), a
    # slight over-count vs the rendered text — switch to summing the content blocks' text if the
    # bloat chart ever needs to compare structured and plain-string results apples-to-apples.
    text = (
        content.output
        if isinstance(content.output, str)
        else json.dumps(content.output, ensure_ascii=False)
    )
    return len(text.encode("utf-8"))


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
    For a graftable (``tool:`` / ``sub-agent:``) span, transcript-sourced ``input``/``output`` is
    grafted into the create body (see :func:`_tool_additions`) so the fresh observation carries
    content the native span lacked, set in the same create event that fixes its name and type.

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
    size = _tool_result_size(observation, tool_content)
    if size is not None:
        body.setdefault("metadata", {})["tool_result_size"] = size
    return {"id": new_id, "type": event_type, "timestamp": start, "body": body}
