"""Per-container token + wall-clock duration rollups (#128).

Every container node (the synthetic root, each interaction / ``tool:Agent`` / sub-agent) gets a
``metadata.rollup`` = the subtree token sum (reusing :mod:`telemetry.langfuse_rollup`) plus a
``duration`` split of its subtree wall-clock by class (:func:`_duration_class`) via exclusive-time
attribution (:func:`_duration_rollup`). :func:`_strip_container_usage` drops own usage from any
container with a generation descendant so cost never double-counts (#161). Depends on the
foundation modules and :func:`~telemetry.spoke_tree.folding._guards_total_ms`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telemetry.langfuse_rollup import (
    Observation,
    build_tree,
    rollup_metadata,
    subtree_totals,
)
from telemetry.spoke_tree.folding import _guards_total_ms
from telemetry.spoke_tree.observations import (
    _FOLD_BLOCKED_NAME,
    _INTERACTION_NAME,
    _POST_STEP_NAME,
    _PRE_STEP_NAME,
    _WAIT_PREFIX,
    IngestEvent,
    _attr,
    _is_gate_observation,
    _is_guards_group,
    _is_hook,
    _is_tool_span,
    _parse_utc,
)

_DURATION_CLASSES: tuple[str, ...] = (
    "llm_request",
    "tool",
    "hook",
    "script",
    "step",
    "wait",
    "turn",
    "self",
    "other",
)

_Interval = tuple[datetime, datetime]


def _duration_class(event: IngestEvent) -> str:
    """Return the duration-attribution class of one assembled node (#128).

    The buckets mirror the issue's split — LLM calls, tool calls, hooks, scripts, cycle
    steps, human/gate wait, turns — plus ``other`` for anything unclassified. Human wait
    covers the gate script (``spoke-ready --gate``) and an UNMATCHED blocked-on-user span
    (its tool was denied/cancelled, so the #100 fold never absorbed it); the tool-side
    share of ``wait`` (folded ``blocked_on_user_ms``) is carved out in
    :func:`_duration_rollup`. ``script:`` outranks the ``.sh`` hook suffix so a
    script-labelled span never drifts into the hook bucket.
    """
    body = event["body"]
    if event["type"] == "generation-create":
        return "llm_request"
    name = body.get("name") or ""
    if _is_gate_observation(body) or name == _FOLD_BLOCKED_NAME or name.startswith(_WAIT_PREFIX):
        return "wait"
    if name.startswith("step:") or name in (_PRE_STEP_NAME, _POST_STEP_NAME):
        return "step"
    if name == _INTERACTION_NAME:
        return "turn"
    if name.startswith("script:") or _attr(body, "workflow.kind") == "script":
        return "script"
    if _is_guards_group(body) or _is_hook(body):
        return "hook"
    if _is_tool_span(body):
        return "tool"
    return "other"


def _interval_ms(interval: _Interval | None) -> int:
    """An interval's length in ms (0 for None)."""
    if interval is None:
        return 0
    return int((interval[1] - interval[0]).total_seconds() * 1000)


def _union_ms(intervals: list[_Interval | None], clip: _Interval) -> int:
    """Total ms covered by the union of ``intervals``, clipped to ``clip``.

    Overlapping child spans (parallel tool calls, concurrent sub-agents) are counted
    once, so a parent's uncovered gap is never over-subtracted by concurrency.
    """
    lo, hi = clip
    clipped = sorted(
        (max(start, lo), min(end, hi))
        for start, end in filter(None, intervals)
        if max(start, lo) < min(end, hi)
    )
    total = 0
    cursor: datetime | None = None
    for start, end in clipped:
        if cursor is None or start > cursor:
            total += _interval_ms((start, end))
            cursor = end
        elif end > cursor:
            total += _interval_ms((cursor, end))
            cursor = end
    return total


def _effective_intervals(
    bodies: list[Observation],
    children: dict[str | None, list[str]],
    exclude: frozenset[str] | set[str],
) -> dict[str, _Interval | None]:
    """Each node's attribution interval: its own parsed start->end, else its subtree span.

    The subtree-span fallback lets an untimed container (the synthetic root, an interaction
    the collector closed without a valid endTime) still cover its children, so the gap
    between them surfaces as that container's own time instead of vanishing. Timestamps are
    PARSED (never string-compared — mixed ``Z``/``+hh:mm`` forms misorder lexicographically)
    and excluded nodes contribute nothing.
    """
    times = {
        body["id"]: (_parse_utc(body.get("startTime")), _parse_utc(body.get("endTime")))
        for body in bodies
    }
    memo: dict[str, _Interval | None] = {}

    def visit(node_id: str) -> _Interval | None:
        if node_id in memo:
            return memo[node_id]
        kid_intervals = [visit(kid) for kid in children.get(node_id, []) if kid not in exclude]
        start, end = times[node_id]
        if start is not None and end is not None and end >= start:
            interval: _Interval | None = (start, end)
        else:
            starts = [i[0] for i in kid_intervals if i] + ([start] if start else [])
            ends = [i[1] for i in kid_intervals if i] + ([end] if end else [])
            interval = (min(starts), max(ends)) if starts and ends else None
            if interval and interval[1] < interval[0]:
                interval = None
        memo[node_id] = interval
        return interval

    for body in bodies:
        visit(body["id"])
    return memo


def _blocked_ms(body: Observation) -> int:
    """The folded ``blocked_on_user_ms`` on a tool node, 0 when absent or non-numeric."""
    raw = (body.get("metadata") or {}).get("blocked_on_user_ms")
    return max(0, int(raw)) if isinstance(raw, (int, float)) else 0


def _duration_rollup(
    root_id: str,
    *,
    by_id: dict[str, Observation],
    children: dict[str | None, list[str]],
    class_of: dict[str, str],
    intervals: dict[str, _Interval | None],
    exclude: frozenset[str] | set[str],
) -> dict[str, Any]:
    """The ``rollup.duration`` object for one container: subtree wall-clock split by class (#128).

    ``total_ms`` is the observed subtree wall-clock. Each subtree node books its exclusive
    time — its interval length minus the union of its children's intervals (clipped to its
    own) — into its class bucket; the container being rolled up books its own uncovered gap
    under ``self``, and a tool's folded ``blocked_on_user_ms`` (#100) is carved out of its
    exclusive time into ``wait``. On serial (non-overlapping) spans the components sum
    exactly to ``total_ms``; CONCURRENT siblings each book their full span time, so class
    buckets are span-time and may sum past the wall-clock (like CPU-time vs wall-time) while
    gap buckets (``self``/``turn``/``step``) stay true — union-based subtraction never
    erases them. Nodes in ``exclude`` (View B turn-markers, whose spans overlap their
    re-homed former children) contribute nothing and are not subtracted.
    """
    components = dict.fromkeys(_DURATION_CLASSES, 0)

    def visit(node_id: str) -> None:
        kids = [kid for kid in children.get(node_id, []) if kid not in exclude]
        own = intervals.get(node_id)
        # A guards-group child covers only its summed RAW guard time (``total_ms``), never its
        # min…max envelope (#157): the envelope brackets the tool's own execution, so unioning it
        # would erase that execution from the tool's exclusive time. Plain children union by
        # interval as before.
        # UPGRADE: guard_cover is summed as a scalar, so when a guard's real interval overlaps a
        # PLAIN sibling (a mid-turn Notification/Stop hook over its turn, a gate over an
        # llm_request under a sub-agent) that overlap is counted in both terms and the container's
        # own gap bucket is under-reported by the overlap — bounded (guards are short), never
        # inflating, and sum(components)==total still holds. Switch to unioning the group's real
        # member intervals into the parent if per-bucket gap exactness ever matters.
        guard_cover = sum(
            _guards_total_ms(by_id[kid]) for kid in kids if _is_guards_group(by_id.get(kid))
        )
        plain = [kid for kid in kids if not _is_guards_group(by_id.get(kid))]
        union = _union_ms([intervals.get(kid) for kid in plain], clip=own) if own else 0
        covered = min(_interval_ms(own), union + guard_cover)
        exclusive = max(0, _interval_ms(own) - covered)
        bucket = "self" if node_id == root_id else class_of.get(node_id, "other")
        if bucket == "tool":
            wait = min(exclusive, _blocked_ms(by_id[node_id]))
            components["wait"] += wait
            components["tool"] += exclusive - wait
        elif _is_guards_group(by_id.get(node_id)):
            # The group books its RAW guard time minus the slice its surviving children already
            # book, so root's ``hook`` bucket is real guard cost and dropping no-op guards leaves
            # the components unchanged.
            kept = sum(_interval_ms(intervals.get(kid)) for kid in kids)
            components["hook"] += max(0, _guards_total_ms(by_id[node_id]) - kept)
        else:
            components[bucket] += exclusive
        for kid in kids:
            visit(kid)

    visit(root_id)
    return {"total_ms": _interval_ms(intervals.get(root_id)), "components": components}


def _container_rollup(
    node_id: str,
    *,
    by_id: dict[str, Observation],
    children: dict[str | None, list[str]],
    class_of: dict[str, str],
    intervals: dict[str, _Interval | None],
    exclude: frozenset[str] | set[str],
) -> dict[str, Any]:
    """One container's full ``metadata.rollup``: the shared token sum plus the duration split.

    The single assembly point for both writers — the container stamping in
    :func:`_apply_container_rollups` and the View B turn-marker stamping in
    :func:`_apply_cycle_axis` — so the two rollup shapes cannot drift.
    """
    rollup: dict[str, Any] = dict(rollup_metadata(subtree_totals(node_id, by_id, children)))
    rollup["duration"] = _duration_rollup(
        node_id,
        by_id=by_id,
        children=children,
        class_of=class_of,
        intervals=intervals,
        exclude=exclude,
    )
    return rollup


def _apply_container_rollups(
    events: list[IngestEvent], *, duration_exclude: frozenset[str] | set[str] = frozenset()
) -> None:
    """Set ``metadata.rollup`` on every container node of the assembled tree, in place.

    A container is any node with children once the tree is re-parented (the synthetic
    root, each ``interaction`` / ``tool:Agent`` / sub-agent). Its rollup is the subtree
    sum of the four usage components over itself and all descendants, computed from the
    create-body shapes (``id`` / ``parentObservationId`` / ``usageDetails``) — the same
    sum logic as :mod:`telemetry.langfuse_rollup`, but written into the create body
    rather than patched — plus the ``duration`` wall-clock split (#128,
    :func:`_duration_rollup`). Leaves (tools, single generations) are left untouched.

    Args:
        events: The assembled ingestion events; only ``*-create`` span/generation bodies
            participate (the ``trace-create`` is skipped). Mutated in place.
        duration_exclude: Node ids that must not contribute duration (View B's flattened
            turn-markers — their spans overlap their re-homed former children).
    """
    nodes = [event for event in events if event["type"] != "trace-create"]
    bodies = [event["body"] for event in nodes]
    by_id, children = build_tree(bodies)
    class_of = {event["body"]["id"]: _duration_class(event) for event in nodes}
    intervals = _effective_intervals(bodies, children, duration_exclude)
    for body in bodies:
        if not children.get(body["id"]):
            continue  # only containers (those with children) carry a rollup
        body.setdefault("metadata", {})["rollup"] = _container_rollup(
            body["id"],
            by_id=by_id,
            children=children,
            class_of=class_of,
            intervals=intervals,
            exclude=duration_exclude,
        )


def _strip_container_usage(copies: list[IngestEvent]) -> list[IngestEvent]:
    """Drop own usage from any span copy that has a generation descendant, in place (#161).

    A container span's own ``usageDetails`` would double-count against its generation children
    in both the subtree rollup and Langfuse's trace cost, so a span (never a generation) with a
    ``generation-create`` anywhere in its subtree must carry no usage of its own. Native
    sub-agent / interaction containers already ship empty usage; this is the future-proof guard
    should the collector ever stamp usage on a container.

    Args:
        copies: The assembled observation copies. Mutated in place and returned.

    Returns:
        The same list, with container usage/cost stripped.
    """
    bodies = [event["body"] for event in copies]
    _by_id, children = build_tree(bodies)
    generation_ids = {
        event["body"]["id"] for event in copies if event["type"] == "generation-create"
    }

    def _has_generation_descendant(node_id: str) -> bool:
        stack = list(children.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in generation_ids:
                return True
            stack.extend(children.get(current, []))
        return False

    for event in copies:
        body = event["body"]
        if event["type"] == "generation-create":
            continue
        if not (body.get("usageDetails") or body.get("costDetails")):
            continue
        if _has_generation_descendant(body["id"]):
            body.pop("usageDetails", None)
            body.pop("costDetails", None)
    return copies
