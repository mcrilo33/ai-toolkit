"""Causal spoke-tree builder — ids, not timestamps (Issue #65, Phase 1, S3).

Assembles one spoke's drillable trace from the causal ids the parser surfaces (S2),
replacing the timestamp-window correlation the dashboard previously used (the former
``dashboard/tree.py``, removed in #80):

- **human** — a prompt span, bucketed into its covering phase interval; the main turn
  it triggered (``turn.parent_uuid`` resolves to the prompt record via
  ``derive_span_id``) NESTS under it, and continuation turns of that agent loop are
  time-ordered SIBLINGS under the same prompt (the turn->turn loop stays flat);
- **turn** — a turn row; a main turn nests under its triggering
  human prompt (or buckets into its covering **phase spine** interval otherwise), a
  sub-agent turn nests under its agent (``agent_id`` == the agent span's ``agent_link``);
- **tool / skill / todo / agent** — a pull span, parented under the turn that issued
  it via ``tool_parents[span_id] -> turn uuid`` (else its span ``parent_id``);
- **tool-scoped hook** — a push span whose ``parent_id`` is the tool's id, nested
  under that tool;
- **script / parentless hook** — a push span; nested under its in-tree parent when it
  has one, else bucketed into its covering phase interval (never dumped to root).

The spine still partitions the run into ``step``/``lifecycle`` intervals, but the
*internals* of each interval are causal: idle→prompt→turn→tool→hook→sub-agent,
recursive to any depth. Cost is no longer attributed here — the otelcol remaps tokens
to ``gen_ai.usage.*`` and Langfuse computes cost from its model-pricing config (Issue
#91). Loaded context (the per-turn ``context`` node) is layered on in S4; idle/resume
dividers in S5.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from telemetry.causal import CausalNode, InputContext, SchemaSummary, causal_node
from telemetry.spans import derive_span_id

if TYPE_CHECKING:
    from telemetry.session_parser import ParsedSession, ReasoningRef, UsageEvent

# Push markers that form the L1 phase spine; folded into ``interval`` bucket nodes.
_MARKER_KINDS = frozenset({"step", "lifecycle"})
# Actors that are their own owner — a child does not inherit these from its parent.
_OWN_ACTORS = frozenset({"main", "hooks", "script", "workflow"})
# The loaded-context subtype each ``rule``-kind span's ``phase`` names.
_CTX_RULE = "rule"
_CTX_CLAUDE_MD = "CLAUDE.md"
_CTX_MEMORY = "memory"
_CTX_TOOL_SCHEMA = "tool-schema"


def build_causal_forest(
    turns: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    tool_parents: dict[str, str],
    thinking: dict[str, str] | None = None,
) -> list[CausalNode]:
    """Build the causal forest for one spoke.

    Args:
        turns: Turn rows, each carrying ``uuid``/``parent_uuid``/``source``/
            ``agent_id``/``is_sidechain`` plus ``tokens_*`` (cost is Langfuse's job).
        spans: The spoke's unified spans (pull tool/skill/agent + push hook/script/
            step/lifecycle) as dicts.
        tool_parents: ``span_id -> issuing turn uuid`` (the parser's causal edge map).
        thinking: Optional ``turn node id -> extended-thinking body`` map (Issue #92),
            keyed by the turn's ``uuid`` (which is its node id). When given (only the
            backfill's opt-in supplies it), each turn whose node id is a key gains a
            ``reasoning`` child — gist as summary, owning no cost. Omitted by the dashboard
            path, so the default forest carries no reasoning node.

    Returns:
        The top-level causal nodes (the phase-interval spine + any root-level script),
        ordered by start time. Every node satisfies the :mod:`telemetry.causal` contract.
    """
    thinking = thinking or {}
    nodes: dict[str, CausalNode] = {}
    main_turns: list[CausalNode] = []
    main_turn_rows: list[tuple[CausalNode, dict[str, Any]]] = []
    sub_turns: list[tuple[CausalNode, str | None]] = []
    for row in turns:
        node = _turn_node(row)
        nodes[node["node_id"]] = node
        if node["node_id"] in thinking:
            node["children"].append(_reasoning_child(node, row.get("reasoning")))
        if row.get("source") == "subagent":
            sub_turns.append((node, row.get("agent_id")))
        else:
            main_turns.append(node)
            main_turn_rows.append((node, row))

    agent_by_link: dict[str, str] = {}
    context_by_session: dict[str | None, dict[str, Any]] = {}
    markers: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []
    humans: list[dict[str, Any]] = []
    for span in spans:
        kind = span.get("kind")
        if kind in _MARKER_KINDS:
            markers.append(span)
            continue
        if kind == "rule":
            _collect_context(context_by_session, span)  # folded into per-turn context (S4)
            continue
        node = _span_node(span)
        nodes[node["node_id"]] = node
        if kind == "agent" and span.get("agent_link"):
            agent_by_link[span["agent_link"]] = node["node_id"]
        bucket = (
            humans
            if kind == "human"
            else hooks
            if kind == "hook"
            else scripts
            if kind == "script"
            else actions
        )
        bucket.append(span)

    for node, row in main_turn_rows:
        node["children"].insert(
            0, _context_child(node, row, context_by_session.get(row.get("session_id")))
        )

    # The phase spine plus a placement fn that buckets any parentless turn/human/hook/
    # script into its covering interval (the #76 root-dump fix) instead of the root.
    bucketable = [*main_turns, *(nodes[s["span_id"]] for s in (*humans, *hooks, *scripts))]
    intervals, place = _build_spine(markers, bucketable)
    roots: list[CausalNode] = list(intervals)

    for span in humans:
        node = nodes[span["span_id"]]
        _attach(place(node["ts_start"]), node, roots)

    _attach_main_turns(main_turn_rows, humans, nodes, place, roots)

    for node, agent_id in sub_turns:
        parent = nodes.get(agent_by_link.get(agent_id or "") or "")
        if parent is not None:
            node["actor"] = parent["name"]
        _attach(parent, node, roots)
    for span in actions:
        node = nodes[span["span_id"]]
        turn_id = tool_parents.get(span["span_id"]) or span.get("parent_id") or ""
        parent = nodes.get(turn_id)
        if parent is not None and node["kind"] != "agent" and parent["actor"] not in _OWN_ACTORS:
            node["actor"] = parent["actor"]
        _attach(parent, node, roots)
    for span in (*hooks, *scripts):
        node = nodes[span["span_id"]]
        # A tool-scoped hook (or emitting script) nests under its parent; a parentless
        # session-level hook/script buckets into its covering interval, not the root (#76).
        parent = (
            nodes.get(span.get("parent_id") or "")
            or _hook_tool_node(span, nodes)
            or place(node["ts_start"])
        )
        _attach(parent, node, roots)

    _sort_tree(roots)
    return roots


def _attach_main_turns(
    main_turn_rows: list[tuple[CausalNode, dict[str, Any]]],
    humans: list[dict[str, Any]],
    nodes: dict[str, CausalNode],
    place: Callable[[str | None], CausalNode | None],
    roots: list[CausalNode],
) -> None:
    """Place each main turn by genuine causal trigger, sibling on continuation (#76).

    Processed per session in start order so a continuation can inherit the prior turn's
    parent. A turn whose ``parent_uuid`` is a human prompt record NESTS under that prompt
    (the parser derives the prompt's span id from that uuid); a continuation turn — whose
    parent is the prior turn's tool_result, not the prompt — is a time-ordered SIBLING
    under the same prompt; otherwise the turn buckets into its covering phase interval.
    The turn→turn loop never recurses, so depth stays bounded.
    """
    human_nodes = {span["span_id"]: nodes[span["span_id"]] for span in humans}
    last_parent: dict[str | None, CausalNode | None] = {}
    last_turn: dict[str | None, CausalNode] = {}
    ordered = sorted(main_turn_rows, key=lambda nr: (_ts(nr[0]["ts_start"]), nr[0]["node_id"]))
    for node, row in ordered:
        session = row.get("session_id")
        parent_uuid = row.get("parent_uuid")
        human = human_nodes.get(derive_span_id(session or "", parent_uuid)) if parent_uuid else None
        if human is not None:
            parent: CausalNode | None = human
            _open_turn_at(node, human["ts_start"])  # inference latency = turn.ts − prompt.ts
        elif (prev := last_parent.get(session)) is not None and prev["kind"] == "human":
            parent = prev  # continuation of the prompt's agent loop ⇒ sibling under it
            # The trigger is the prior turn's tool_result; proxy it with that turn's end,
            # then the leaf partition carves the intervening tool out of this window (#79).
            _open_turn_at(node, prior["ts_end"] if (prior := last_turn.get(session)) else None)
        else:
            parent = place(node["ts_start"])
        _attach(parent, node, roots)
        last_parent[session] = parent
        last_turn[session] = node


def _attach(parent: CausalNode | None, node: CausalNode, roots: list[CausalNode]) -> None:
    """Place ``node`` under ``parent`` (recording the structural parent id), else at root."""
    if parent is None:
        node["parent_id"] = None
        roots.append(node)
    else:
        node["parent_id"] = parent["node_id"]
        parent["children"].append(node)


def _build_spine(
    markers: list[dict[str, Any]], bucketable: list[CausalNode]
) -> tuple[list[CausalNode], Callable[[str | None], CausalNode | None]]:
    """The L1 phase-interval spine plus a ``place(ts) -> interval`` bucketing function.

    Intervals run ``(prev_marker.ts_end, marker.ts_end]`` with the first opening at
    ``-inf`` and the last closing at ``+inf``, so ``place`` is total — every turn/human/
    hook/script resolves to a covering interval, none fall to ``(unresolved)``. With no
    markers the whole run is one synthetic interval spanning ``bucketable``.
    """
    if not markers:
        if not bucketable:
            return [], lambda _: None
        run = causal_node(
            node_id="__run__",
            kind="interval",
            name="run",
            ts_start=min((n["ts_start"] for n in bucketable if n["ts_start"]), default=None),
            ts_end=max((n["ts_end"] for n in bucketable if n["ts_end"]), default=None),
        )
        return [run], lambda _: run

    ordered = sorted(markers, key=lambda m: _ts(m.get("ts_end")))
    bounds: list[tuple[float, float, CausalNode, bool]] = []
    for index, marker in enumerate(ordered):
        lo = float("-inf") if index == 0 else _ts(ordered[index - 1].get("ts_end"))
        hi = _ts(marker.get("ts_end"))
        interval = causal_node(
            node_id=marker["span_id"],
            kind="interval",
            name=marker.get("name") or marker.get("kind") or "step",
            phase=marker.get("phase"),
            status=marker.get("status") or "success",
            ts_start=marker.get("ts_start"),
            ts_end=marker.get("ts_end"),
            duration_ms=int(marker.get("duration_ms") or 0),
        )
        bounds.append((lo, hi, interval, index == len(ordered) - 1))

    return [interval for _, _, interval, _ in bounds], lambda ts: _interval_for(_ts(ts), bounds)


def _interval_for(ts: float, bounds: list[tuple[float, float, CausalNode, bool]]) -> CausalNode:
    """The interval whose right-closed window holds ``ts`` (the last interval extends to +inf)."""
    for lo, hi, interval, is_last in bounds:
        if lo < ts <= hi or (is_last and ts > hi):
            return interval
    return bounds[0][2]


def _collect_context(by_session: dict[str | None, dict[str, Any]], span: dict[str, Any]) -> None:
    """Fold one loaded-context ``rule`` span into its session's accumulator by subtype."""
    ctx = by_session.setdefault(
        span.get("session_id"),
        {"rules": [], "claude_md": None, "memory": [], "schema_count": 0, "schema_tokens": 0},
    )
    item: dict[str, Any] = {
        "name": span.get("name") or "",
        "tokens": _estimate_tokens(span.get("summary")),
    }
    phase = span.get("phase")
    if phase == _CTX_RULE:
        ctx["rules"].append(item)
    elif phase == _CTX_CLAUDE_MD:
        ctx["claude_md"] = item
    elif phase == _CTX_MEMORY:
        ctx["memory"].append(item)
    elif phase == _CTX_TOOL_SCHEMA:
        ctx["schema_count"] += 1
        ctx["schema_tokens"] += item["tokens"]


def _context_child(turn: CausalNode, row: dict[str, Any], ctx: dict[str, Any] | None) -> CausalNode:
    """The single ``context`` node a main turn carries — its named input state + real tokens.

    The named items keep their byte-size estimates; the cached prefix total is the turn's
    real ``cache_read + cache_creation`` (never less than the named items), and ``history``
    is whatever of that prefix the named items do not account for (the modeled split,
    anchored to the real total per the spec).
    """
    ctx = ctx or {
        "rules": [],
        "claude_md": None,
        "memory": [],
        "schema_count": 0,
        "schema_tokens": 0,
    }
    rules, claude_md, memory = ctx["rules"], ctx["claude_md"], ctx["memory"]
    schemas: SchemaSummary = {"count": ctx["schema_count"], "tokens": ctx["schema_tokens"]}
    named = (
        sum(r["tokens"] for r in rules)
        + (claude_md["tokens"] if claude_md else 0)
        + sum(m["tokens"] for m in memory)
        + schemas["tokens"]
    )
    total = max(int(row.get("cache_read") or 0) + int(row.get("cache_creation") or 0), named)
    input_context: InputContext = {
        "rules": rules,
        "claude_md": claude_md,
        "memory": memory,
        "schemas": schemas,
        "history_tokens": total - named,
        "total_tokens": total,
    }
    return causal_node(
        node_id=f"ctx:{turn['node_id']}",
        kind="context",
        name="context",
        parent_id=turn["node_id"],
        actor=turn["actor"],
        ts_start=turn["ts_start"],
        ts_end=turn["ts_start"],
        input_context=input_context,
    )


def _estimate_tokens(summary: object) -> int:
    """The integer token estimate from a ``~N tokens`` context-load summary (else 0).

    The parser emits ``~{N:,} tokens`` (comma-grouped), so the first ``[\\d,]+`` run is
    the count; stripping commas yields the int. Anchoring on the leading numeric run
    keeps an odd format from silently scaling the number — an unparseable label is 0,
    not a wrong magnitude.
    """
    if not isinstance(summary, str):
        return 0
    match = re.search(r"[\d,]+", summary)
    return int(match.group().replace(",", "")) if match else 0


def _turn_node(row: dict[str, Any]) -> CausalNode:
    is_sub = row.get("source") == "subagent"
    node = causal_node(
        node_id=row.get("uuid") or f"turn:{row.get('ts')}",
        kind="turn",
        name="turn",
        actor="subagent" if is_sub else "main",
        ts_start=row.get("ts"),
        ts_end=row.get("ts"),
        own_cost_usd=0.0,  # cost is computed downstream by Langfuse (Issue #91)
        own_tokens_in=int(row.get("tokens_in") or 0),
        own_tokens_out=int(row.get("tokens_out") or 0),
    )
    # Cache breakdown for the composition lens; display-only, never folded into cost.
    node["cache_read"] = int(row.get("cache_read") or 0)
    node["cache_creation"] = int(row.get("cache_creation") or 0)
    # Cache-write TTL split (Issue #97): the backfill maps each tier to its Langfuse
    # usage type so cost reflects the 2x (1h) / 1.25x (5m) price difference.
    node["cache_creation_5m"] = int(row.get("cache_creation_5m") or 0)
    node["cache_creation_1h"] = int(row.get("cache_creation_1h") or 0)
    # Response service tier (Issue #101): surfaced only when present, so a turn without it
    # (older / push-only records) carries no key rather than a null.
    service_tier = row.get("service_tier")
    if service_tier:
        node["service_tier"] = str(service_tier)
    return node


def _reasoning_child(turn: CausalNode, gist: str | None) -> CausalNode:
    """The ``reasoning`` node a turn carries when its thinking body was extracted (#92).

    Owns no tokens — usage stays on the turn. Its ``summary`` is the turn's privacy-safe
    narration gist; the thinking BODY is not held here (the backfill joins it by the turn
    uuid at Langfuse-translation time).
    """
    return causal_node(
        node_id=f"reasoning:{turn['node_id']}",
        kind="reasoning",
        name="reasoning",
        parent_id=turn["node_id"],
        summary=gist,
        actor=turn["actor"],
        ts_start=turn["ts_start"],
        ts_end=turn["ts_start"],
    )


def _span_node(span: dict[str, Any]) -> CausalNode:
    kind = span["kind"]
    links: dict[str, Any] = {}
    for key in ("emits", "sidecar_session", "agent_link", "hook_event"):
        if span.get(key):
            links[key] = span[key]
    if span.get("human_type"):
        links["human_type"] = span["human_type"]
        links["human_wait_ms"] = span.get("human_wait_ms")
    return causal_node(
        node_id=span["span_id"],
        kind=kind,
        name=span.get("name") or kind,
        summary=span.get("summary"),
        actor=_actor_for(span),
        phase=span.get("phase"),
        status=span.get("status") or "success",
        ts_start=span.get("ts_start"),
        ts_end=span.get("ts_end"),
        duration_ms=int(span.get("duration_ms") or 0),
        own_cost_usd=0.0,  # cost is computed downstream by Langfuse (Issue #91)
        human_count=1 if span.get("human_type") else 0,
        **links,
    )


def _hook_tool_node(span: dict[str, Any], nodes: dict[str, CausalNode]) -> CausalNode | None:
    """The tool node a Pre/PostToolUse hook nests under, resolved by the real id (Issue #82).

    The push emitter sets a hook's ``parent_id`` to the RAW ``tool_use_id`` from the
    payload, but the parser keys the tool node by ``derive_span_id(session_id, tool_use_id)``.
    Bridge that here: re-derive the tool node's id from the hook's own session + raw parent.
    Hook-scoped — a script's parent never goes through this. ``None`` when the hook is not a
    tool event (its parent is the spoke root / a script id, which won't re-derive to a node).
    """
    if span.get("kind") != "hook":
        return None
    parent_id = span.get("parent_id")
    if not parent_id:
        return None
    return nodes.get(derive_span_id(span.get("session_id") or "", parent_id))


def _actor_for(span: dict[str, Any]) -> str:
    kind = span.get("kind")
    if kind == "hook":
        return "sidecar" if span.get("sidecar_session") else "hooks"
    if kind == "script":
        return "script"
    if kind in ("workflow", "workflow_phase"):
        return "workflow"
    if kind == "agent":
        return span.get("name") or "agent"
    return "main"


def _sort_tree(nodes: list[CausalNode]) -> None:
    nodes.sort(key=lambda n: (_ts(n["ts_start"]), n["node_id"]))
    for node in nodes:
        _sort_tree(node["children"])


def _ts(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return float("-inf")


def _ms(value: str | None) -> int | None:
    """The integer-millisecond epoch for an ISO timestamp, or ``None`` if unparseable."""
    ts = _ts(value)
    return None if ts == float("-inf") else round(ts * 1000)


def _open_turn_at(turn: CausalNode, trigger_ts: str | None) -> None:
    """Open a turn's window at its triggering record so its ``duration`` is the latency.

    A turn lands as a point at ``turn.ts`` (the inference's end); its real wall-clock is
    ``turn.ts − triggering-record.ts`` (Issue #79). Set ``ts_start`` to the trigger and
    recompute ``duration_ms``; a missing/forward-skewed trigger leaves the turn zero-width
    rather than inventing a negative span.
    """
    start, end = _ms(trigger_ts), _ms(turn["ts_end"])
    if start is None or end is None or start > end:
        return
    turn["ts_start"] = trigger_ts
    turn["duration_ms"] = end - start


# Kinds that occupy wall-clock as a work leaf. Everything else — interval/context/gap/
# session/human — is a container or marker that cedes its span to the work inside it,
# so the time it "spans" is attributed to those leaves (or to ``idle`` when none run).
_WORK_KINDS = frozenset({"turn", "tool", "skill", "todo", "agent", "hook", "script"})
_IDLE_KIND = "idle"


def leaf_time_slices(forest: list[CausalNode]) -> dict[str, int]:
    """Partition the spoke wall-clock by deepest-active leaf; return milliseconds per kind.

    Models time as the innermost running span at each instant: a hook nested in a tool
    beats the tool, a sub-turn beats its agent, and a parent's wait while a sub-agent
    runs is attributed to the sub-agent's slice — the parent is not "spending" that time
    twice. Any instant no work span covers is ``idle``. The slices tile ``[spawn,
    teardown]`` (the earliest start to the latest end across the whole forest) with no
    overlap, so ``Σ values == total wall-clock`` — the time analog of ``Σ owned == Σ
    turns``. Computing in integer milliseconds keeps that equality exact.

    Args:
        forest: The spoke's causal forest (the start-ordered L1 spine + dividers).

    Returns:
        ``kind -> milliseconds``, including an ``idle`` entry for uncovered gaps. Empty
        when the forest carries no resolvable timestamps.
    """
    work: list[tuple[int, int, int, str]] = []
    bounds: list[int] = []
    _collect_slices(forest, depth=0, work=work, bounds=bounds)
    if not bounds:
        return {}

    spawn, teardown = min(bounds), max(bounds)
    cuts = sorted({c for s, e, _d, _k in work for c in (s, e) if spawn <= c <= teardown})
    cuts = [spawn, *cuts, teardown]
    totals: dict[str, int] = {}
    for lo, hi in pairwise(cuts):
        if hi <= lo:
            continue
        kind = _deepest_kind(work, lo, hi)
        totals[kind] = totals.get(kind, 0) + (hi - lo)
    return totals


def _collect_slices(
    nodes: list[CausalNode],
    *,
    depth: int,
    work: list[tuple[int, int, int, str]],
    bounds: list[int],
) -> None:
    """Gather (start, end, depth, kind) for every work leaf and bounds for every node."""
    for node in nodes:
        start, end = _ms(node["ts_start"]), _ms(node["ts_end"])
        bounds.extend(b for b in (start, end) if b is not None)
        if node["kind"] in _WORK_KINDS and start is not None and end is not None and end > start:
            work.append((start, end, depth, node["kind"]))
        _collect_slices(node["children"], depth=depth + 1, work=work, bounds=bounds)


def _deepest_kind(work: list[tuple[int, int, int, str]], lo: int, hi: int) -> str:
    """The kind of the deepest work span covering ``[lo, hi]``, else ``idle``."""
    best_depth, best_kind = -1, _IDLE_KIND
    for start, end, depth, kind in work:
        if start <= lo and end >= hi and depth > best_depth:
            best_depth, best_kind = depth, kind
    return best_kind


# Idle longer than this between two main turns renders as a ``gap`` divider, not a phase.
_IDLE_GAP_SECONDS = 300.0


def causal_dividers(turns: list[dict[str, Any]]) -> list[CausalNode]:
    """Root-level idle/resume dividers for the main timeline (Issue #52 / #65 S5).

    A change of ``session_id`` between adjacent main turns is a **resume** (cold cache),
    rendered as a ``session`` divider carrying the cold re-read note; a long idle gap is
    a ``gap`` divider. Dividers own nothing and slot into the forest by start time, so
    they never disturb the token rollup.
    """
    main = sorted(
        (t for t in turns if t.get("source") != "subagent" and t.get("ts")),
        key=lambda t: _ts(t["ts"]),
    )
    dividers: list[CausalNode] = []
    for prev, cur in pairwise(main):
        anchor = cur.get("uuid") or cur["ts"]
        if cur.get("session_id") != prev.get("session_id"):
            cold = int(cur.get("cache_creation") or 0)
            divider = causal_node(
                node_id=f"session:{anchor}",
                kind="session",
                name="resume",
                summary=f"cold cache re-read +{cold:,} tokens" if cold else "resume",
                ts_start=cur["ts"],
                ts_end=cur["ts"],
            )
            # The renderer reads the cold magnitude from resume_cache_creation; it stays
            # off own_tokens so it never folds into the once-per-turn rollup (#59).
            divider["resume_cache_creation"] = cold
            dividers.append(divider)
        elif _ts(cur["ts"]) - _ts(prev["ts"]) > _IDLE_GAP_SECONDS:
            dividers.append(
                causal_node(
                    node_id=f"gap:{anchor}",
                    kind="gap",
                    name="idle",
                    ts_start=prev["ts"],
                    ts_end=cur["ts"],
                    duration_ms=int((_ts(cur["ts"]) - _ts(prev["ts"])) * 1000),
                )
            )
    return dividers


def per_turn_rows(
    usage_events: list[UsageEvent],
    *,
    reasoning_refs: list[ReasoningRef] | None = None,
) -> list[dict[str, object]]:
    """One row per usage event, with model, token counts, and a reasoning gist.

    Each turn appears exactly once. Cost is no longer attributed here — the otelcol
    remaps tokens to ``gen_ai.usage.*`` and Langfuse computes cost from its model-pricing
    config (Issue #91). Each row carries the turn's ``cache_read`` / ``cache_creation``
    breakdown (the renderer frames cheap reuse against cold writes). When ``reasoning_refs``
    are given, each row carries the turn's reasoning ``summary`` gist (matched on
    session/source/agent/ts) so the tree can render a ``reasoning`` node.

    Args:
        usage_events: Per-turn usage from the parsed sessions (main + subagent).
        reasoning_refs: Per-turn reasoning summaries to join onto their turn.

    Returns:
        One dict per turn: ``session_id, ts, model, source, agent_id, uuid,
        parent_uuid, is_sidechain, tokens_in, tokens_out, tokens_total, cache_read,
        cache_creation, cache_creation_5m, cache_creation_1h, reasoning``.
    """
    gists = _reasoning_by_turn(reasoning_refs or [])
    rows: list[dict[str, object]] = []
    for event in usage_events:
        rows.append(
            {
                "session_id": event.session_id,
                "ts": event.ts,
                "model": event.model,
                "source": event.source,
                "agent_id": event.agent_id,
                # Causal ids (Issue #65) — the keys the v3 builder keys turns on.
                "uuid": event.uuid,
                "parent_uuid": event.parent_uuid,
                "is_sidechain": event.is_sidechain,
                "tokens_in": event.input_tokens,
                "tokens_out": event.output_tokens,
                "tokens_total": _event_total(event),
                "cache_read": event.cache_read,
                "cache_creation": event.cache_creation,
                # Cache-write TTL split (Issue #97): 1h writes bill 2x input, 5m 1.25x.
                "cache_creation_5m": event.cache_creation_5m,
                "cache_creation_1h": event.cache_creation_1h,
                # Response service tier (Issue #101): the other half of the cache item.
                "service_tier": event.service_tier,
                "reasoning": gists.get(
                    _turn_key(event.session_id, event.source, event.agent_id, event.ts)
                ),
            }
        )
    return rows


def _turn_key(
    session_id: str | None, source: str, agent_id: str | None, ts: str | None
) -> tuple[str | None, str, str | None, str | None]:
    """The identity a usage event and its reasoning ref share (same assistant record)."""
    return (session_id, source, agent_id, ts)


def _reasoning_by_turn(
    refs: list[ReasoningRef],
) -> dict[tuple[str | None, str, str | None, str | None], str]:
    """Map each turn key to its reasoning gist, keeping only refs that carry one.

    Two inferences can share a millisecond ``ts``; on such a collision the last ref's
    gist wins and both turns display it. The gist is display-only, so the cosmetic
    mislabel is acceptable.
    """
    return {
        _turn_key(ref.session_id, ref.source, ref.agent_id, ref.ts): ref.summary
        for ref in refs
        if ref.summary
    }


def _event_total(event: UsageEvent) -> int:
    return event.input_tokens + event.output_tokens + event.cache_read + event.cache_creation


def causal_forest_from_parsed(
    parsed: ParsedSession,
    push_spans: list[dict[str, Any]],
    thinking: dict[str, str] | None = None,
) -> list[CausalNode]:
    """Assemble a spoke's full causal forest from a parsed session + its push spans.

    The per-spoke entry point the dashboard wires in: the parser's pull spans + per-turn
    rows (carrying the causal ids) and the parser's ``tool_parents`` edge map feed the
    builder; the push spans supply the phase-spine markers, hooks and scripts; idle/resume
    dividers are appended. Returns the start-ordered forest.

    Cost is not attributed here — the otelcol remaps tokens to ``gen_ai.usage.*`` and
    Langfuse computes cost from its model-pricing config (Issue #91).

    ``thinking`` (Issue #92) is the optional ``turn uuid -> extended-thinking body`` map
    the backfill passes under its opt-in; it threads through to attach ``reasoning`` nodes.
    """
    turns = per_turn_rows(parsed.usage_events, reasoning_refs=parsed.reasoning_refs)
    pull = [span.to_dict() for span in parsed.spans]
    forest = build_causal_forest(turns, [*pull, *push_spans], parsed.tool_parents, thinking)
    forest.extend(causal_dividers(turns))
    _sort_tree(forest)
    return forest
