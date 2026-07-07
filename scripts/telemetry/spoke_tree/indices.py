"""Ownership indices, enclosing-turn resolution, skill matching, and blocked-tool synthesis.

The lookups the re-parent pass consults: which copy owns a ``tool_use_id`` / ``request_id``
(:func:`_build_tool_index` / :func:`_build_request_index`), which interaction encloses an
unmatched-tool satellite (:func:`_build_interaction_index` / :func:`_enclosing_turn`, #110 AC1),
which ``tool:Skill`` a ``skill_activated`` event belongs to (:func:`_build_skill_index` /
:func:`_match_skill_tool`, #110 AC2), and one synthesized ``blocked-tool:`` node per orphaned
tool-call id (:func:`_synthesize_blocked_tools`, #157). Depends only on the foundation modules.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from telemetry.langfuse_rollup import Observation
from telemetry.spoke_tree.ids import _blocked_tool_id, _copy_id
from telemetry.spoke_tree.observations import (
    _BLOCKED_TOOL_NAME_PREFIX,
    _INGEST_TIMESTAMP,
    IngestEvent,
    ToolContent,
    TraceObservations,
    _attr,
    _is_audit_instant,
    _is_fold_subspan,
    _is_interaction,
    _joins_under_tool,
    _obs_envelope,
    _parse_ts,
    _prompt_id,
    _request_id,
    _skill_name,
    _tool_use_id,
)

# Attribute keys naming the blocked tool, in priority order (bare tool name, then the
# ``<HookEvent>:<Tool>`` hook name whose suffix is the tool).
_TOOL_NAME_KEYS = ("tool_name", "gen_ai.tool.name")
_HOOK_NAME_KEY = "hook_name"
_BLOCKED_TOOL_UNKNOWN = "unknown"
# The visible Skill tool span (#110 AC2) and its input key naming the activated skill.
_SKILL_TOOL_NAME = "tool:Skill"
_SKILL_INPUT_KEY = "skill"


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


def _build_request_index(traces: list[TraceObservations]) -> dict[str, str]:
    """Map each LLM ``request_id`` to the copy id of its ``llm_request`` generation (#104).

    Only ``GENERATION`` observations (the native ``llm_request`` spans) own the index; the
    ``api_error`` / ``api_refusal`` audit events that share the id are skipped so they remain the
    re-parent satellites, never the target.

    Args:
        traces: The source traces paired with their observations.

    Returns:
        A mapping of ``request_id`` to the assembled-trace copy id of its ``llm_request``.
    """
    index: dict[str, str] = {}
    for orig_trace_id, observations in traces:
        for observation in observations:
            if (observation.get("type") or "") != "GENERATION":
                continue
            rid = _request_id(observation)
            if rid:
                index[rid] = _copy_id(orig_trace_id, observation["id"])
    return index


class InteractionIndex(NamedTuple):
    """Enclosing-turn lookup for re-homing an unmatched-tool satellite (#110 AC1).

    ``by_prompt`` maps each turn's ``prompt.id`` to its interaction copy id (the primary,
    causal join). ``windows`` lists ``(start, end, copy_id)`` for every interaction that has
    both bounds, sorted ascending, for the ``[start,end]`` containment fallback used when a
    satellite carries no ``prompt.id``.
    """

    by_prompt: dict[str, str]
    windows: list[tuple[str, str, str]]


def _build_interaction_index(traces: list[TraceObservations]) -> InteractionIndex:
    """Index every ``claude_code.interaction`` by ``prompt.id`` and by its time window.

    The first interaction seen for a ``prompt.id`` wins (a resume shares the original turn's
    id; either copy is the same turn). Only interactions carrying both bounds contribute a
    window, kept sorted so :func:`_enclosing_turn` picks the innermost on an overlap.

    Args:
        traces: Each source trace paired with all of its observations.

    Returns:
        The prompt-id map and sorted window list (see :class:`InteractionIndex`).
    """
    by_prompt: dict[str, str] = {}
    windows: list[tuple[str, str, str]] = []
    for orig_trace_id, observations in traces:
        for observation in observations:
            if not _is_interaction(observation):
                continue
            copy = _copy_id(orig_trace_id, observation["id"])
            pid = _prompt_id(observation)
            if pid:
                by_prompt.setdefault(pid, copy)
            start, end = observation.get("startTime"), observation.get("endTime")
            if start and end:
                windows.append((start, end, copy))
    windows.sort()
    return InteractionIndex(by_prompt, windows)


def _enclosing_turn(observation: Observation, index: InteractionIndex) -> str | None:
    """Return the copy id of the interaction enclosing ``observation``, or None (#110 AC1).

    Resolves by ``prompt.id`` first — the reliable causal join. Falls back to ``[start,end]``
    containment (innermost turn wins) only for an event whose ``startTime`` is its true event
    time; a lagging-timestamp audit instant (:func:`_is_audit_instant`, on the batched logs
    signal) is never window-placed, so it resolves by ``prompt.id`` alone and otherwise stays
    at the root.
    """
    pid = _prompt_id(observation)
    if pid and pid in index.by_prompt:
        return index.by_prompt[pid]
    if _is_audit_instant(observation):
        return None
    start = observation.get("startTime")
    if not start:
        return None
    chosen: tuple[str, str, str] | None = None
    for window in index.windows:
        win_start, win_end, _copy = window
        if not win_start <= start <= win_end:
            continue
        # Innermost wins: the latest-starting containing turn, and on an equal start the one
        # that ends earliest (the narrower, more-nested window).
        if (
            chosen is None
            or win_start > chosen[0]
            or (win_start == chosen[0] and win_end < chosen[1])
        ):
            chosen = window
    return chosen[2] if chosen else None


def _blocked_tool_name(satellites: list[Observation]) -> str:
    """Return the blocked tool's name: a ``tool_name`` attr, else a ``hook_name`` suffix, else unknown."""
    for satellite in satellites:
        name = _attr(satellite, *_TOOL_NAME_KEYS)
        if name:
            return str(name)
    for satellite in satellites:
        hook_name = _attr(satellite, _HOOK_NAME_KEY)
        if hook_name and ":" in str(hook_name):
            return str(hook_name).split(":", 1)[1]
    return _BLOCKED_TOOL_UNKNOWN


def _synthesize_blocked_tools(
    traces: list[TraceObservations],
    *,
    tool_index: dict[str, str],
    interaction_index: InteractionIndex,
    trace_id: str,
    root_id: str,
) -> tuple[list[IngestEvent], dict[str, str]]:
    """Synthesize a ``blocked-tool:<Name>`` node per orphaned tool-call id (#157).

    An orphaned id is one carried by a satellite (:func:`_joins_under_tool`) but owned by no
    ``tool:`` span (:func:`_build_tool_index`). Each becomes one WARNING ``blocked-tool:`` node
    parented to its enclosing turn (:func:`_enclosing_turn`, else the root), spanning its
    satellites' time envelope and carrying no usageDetails/model. The returned index maps each
    orphaned id to its node so the copy pass and fold re-home the satellites onto it.

    Args:
        traces: The source traces paired with their observations.
        tool_index: Real-tool ownership map (an id present here is NOT orphaned).
        interaction_index: Enclosing-turn lookup for parenting the synthesized node.
        trace_id: The assembled trace id every synthesized node references.
        root_id: The synthetic root id (parent when no enclosing turn resolves).

    Returns:
        ``(events, index)``: the synthesized ``span-create`` events and the orphaned-id → node-id map.
    """
    by_tuid: dict[str, list[Observation]] = {}
    for _orig_trace_id, observations in traces:
        for observation in observations:
            if not _joins_under_tool(observation):
                continue
            tuid = _tool_use_id(observation)
            if tuid and tuid not in tool_index:
                by_tuid.setdefault(tuid, []).append(observation)
    events: list[IngestEvent] = []
    index: dict[str, str] = {}
    for tuid, satellites in by_tuid.items():
        node_id = _blocked_tool_id(tuid)
        parent = next(
            (turn for s in satellites if (turn := _enclosing_turn(s, interaction_index))), root_id
        )
        start, end = _obs_envelope(satellites)
        body: dict[str, Any] = {
            "id": node_id,
            "traceId": trace_id,
            "parentObservationId": parent,
            "name": _BLOCKED_TOOL_NAME_PREFIX + _blocked_tool_name(satellites),
            "startTime": start or _INGEST_TIMESTAMP,
            "endTime": end,
            "metadata": {"synthesized": True, "tool_use_id": tuid},
        }
        # level WARNING is stamped centrally by _apply_levels (#157), like every other node.
        events.append(
            {
                "id": node_id,
                "type": "span-create",
                "timestamp": start or _INGEST_TIMESTAMP,
                "body": body,
            }
        )
        index[tuid] = node_id
    return events, index


class SkillCandidate(NamedTuple):
    """One ``tool:Skill`` span a ``skill_activated`` event may belong to (#110 AC2).

    ``skill_name`` is the activated skill read from the tool's transcript input (None when the
    content is unavailable); ``start`` is the span's true start, used as the nearest-timestamp
    tiebreak when a turn ran the same skill more than once.
    """

    start: str | None
    copy_id: str
    skill_name: str | None


def _activated_skill_name(tuid: str | None, tool_content: dict[str, ToolContent]) -> str | None:
    """Return the skill named by a ``tool:Skill`` span's transcript input, or None."""
    content = tool_content.get(tuid or "")
    if content is None or not isinstance(content.input, dict):
        return None
    value = content.input.get(_SKILL_INPUT_KEY)
    return str(value) if value else None


def _enclosing_prompt_id(observation: Observation, by_id: dict[str, Observation]) -> str | None:
    """Return the ``prompt.id`` of the nearest ancestor interaction within a trace, or None.

    Walks ``parentObservationId`` up the trace-local node map until an ancestor carries a
    ``prompt.id`` (normally the enclosing ``claude_code.interaction``); a tool span rarely
    carries its own, so this recovers the turn id a ``tool:Skill`` belongs to.
    """
    seen: set[str] = set()
    parent = observation.get("parentObservationId")
    while parent and parent in by_id and parent not in seen:
        seen.add(parent)
        node = by_id[parent]
        pid = _prompt_id(node)
        if pid:
            return pid
        parent = node.get("parentObservationId")
    return None


def _build_skill_index(
    traces: list[TraceObservations], tool_content: dict[str, ToolContent]
) -> dict[str, list[SkillCandidate]]:
    """Index every ``tool:Skill`` span by its turn's ``prompt.id`` (#110 AC2).

    A ``tool:Skill``'s ``prompt.id`` is its own when present, else the enclosing interaction's
    (:func:`_enclosing_prompt_id`); spans whose turn cannot be determined are skipped (the event
    then has no key to match and falls back to the enclosing turn / root).

    Args:
        traces: Each source trace paired with all of its observations.
        tool_content: Tool-call-id to :class:`ToolContent`, the source of each skill's name.

    Returns:
        A mapping of ``prompt.id`` to the candidate ``tool:Skill`` spans of that turn.
    """
    index: dict[str, list[SkillCandidate]] = {}
    for orig_trace_id, observations in traces:
        by_id = {observation["id"]: observation for observation in observations}
        for observation in observations:
            if (observation.get("name") or "") != _SKILL_TOOL_NAME:
                continue
            pid = _prompt_id(observation) or _enclosing_prompt_id(observation, by_id)
            if not pid:
                continue
            tuid = _tool_use_id(observation)
            candidate = SkillCandidate(
                observation.get("startTime"),
                _copy_id(orig_trace_id, observation["id"]),
                _activated_skill_name(tuid, tool_content),
            )
            index.setdefault(pid, []).append(candidate)
    return index


def _match_skill_tool(
    observation: Observation, skill_index: dict[str, list[SkillCandidate]]
) -> str | None:
    """Return the ``tool:Skill`` copy id a ``skill_activated`` event nests under, else None (#110).

    Matches within the event's turn (``prompt.id``): when the turn ran exactly one skill that is
    it; otherwise the candidates whose ``skill.name`` matches are preferred, and ties (the same
    skill activated twice in one turn) are broken by the nearest span start to the event's
    lagging time. None when the event has no ``prompt.id`` or its turn ran no skill.
    """
    pid = _prompt_id(observation)
    if not pid or pid not in skill_index:
        return None
    candidates = skill_index[pid]
    name = _skill_name(observation)
    pool = [candidate for candidate in candidates if candidate.skill_name == name] if name else []
    pool = pool or candidates
    if len(pool) == 1:
        return pool[0].copy_id
    return _nearest_skill(observation.get("startTime"), pool)


def _nearest_skill(event_start: str | None, pool: list[SkillCandidate]) -> str:
    """Return the copy id of the candidate whose start is nearest the event's time.

    Candidates with an unparseable or absent start sort last; on a full tie the first in fetch
    order wins, so the choice is deterministic across reruns.
    """
    event_ts = _parse_ts(event_start or "")

    def distance(candidate: SkillCandidate) -> tuple[int, float]:
        cand_ts = _parse_ts(candidate.start or "")
        if event_ts is None or cand_ts is None:
            return (1, 0.0)
        try:
            return (0, abs((cand_ts - event_ts).total_seconds()))
        except TypeError:  # one side tz-aware, the other naive — sort last, never crash
            return (1, 0.0)

    return min(pool, key=distance).copy_id
