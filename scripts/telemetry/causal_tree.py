"""Causal spoke-tree builder — ids, not timestamps (Issue #65, Phase 1, S3).

Assembles one spoke's drillable trace from the causal ids the parser surfaces (S2),
replacing the timestamp-window correlation in ``dashboard/tree.py``:

- **turn** — a cost-attributed turn row; a main turn buckets into the push-marker
  **phase spine** (kept), a sub-agent turn nests under its agent (``agent_id`` ==
  the agent span's ``agent_link``);
- **tool / skill / todo / agent** — a pull span, parented under the turn that issued
  it via ``tool_parents[span_id] -> turn uuid`` (else its span ``parent_id``);
- **tool-scoped hook** — a push span whose ``parent_id`` is the tool's id, nested
  under that tool;
- **script** — a push span, at the spoke root when it has no in-tree parent.

The spine still partitions the run into ``step``/``lifecycle`` intervals, but the
*internals* of each interval are causal: idle→prompt→turn→tool→hook→sub-agent,
recursive to any depth. Cost lives only on the turn/agent leaves it was spent in, so
``Σ owned == Σ turns``. Loaded context (the per-turn ``context`` node) is layered on
in S4; idle/resume dividers in S5.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from telemetry.causal import CausalNode, InputContext, causal_node

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
) -> list[CausalNode]:
    """Build the causal forest for one spoke.

    Args:
        turns: Cost-attributed turn rows, each carrying ``uuid``/``parent_uuid``/
            ``source``/``agent_id``/``is_sidechain`` plus ``cost_usd``/``tokens_*``.
        spans: The spoke's unified spans (pull tool/skill/agent + push hook/script/
            step/lifecycle) as dicts.
        tool_parents: ``span_id -> issuing turn uuid`` (the parser's causal edge map).

    Returns:
        The top-level causal nodes (the phase-interval spine + any root-level script),
        ordered by start time. Every node satisfies the :mod:`telemetry.causal` contract.
    """
    nodes: dict[str, CausalNode] = {}
    main_turns: list[CausalNode] = []
    main_turn_rows: list[tuple[CausalNode, dict[str, Any]]] = []
    sub_turns: list[tuple[CausalNode, str | None]] = []
    for row in turns:
        node = _turn_node(row)
        nodes[node["node_id"]] = node
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
        (hooks if kind == "hook" else scripts if kind == "script" else actions).append(span)

    for node, row in main_turn_rows:
        node["children"].insert(
            0, _context_child(node, row, context_by_session.get(row.get("session_id")))
        )

    roots: list[CausalNode] = list(_build_spine(markers, main_turns))
    for node, agent_id in sub_turns:
        parent = nodes.get(agent_by_link.get(agent_id or "") or "")
        if parent is not None:
            node["actor"] = parent["name"]
            # The agent leaf carried the subagent transcript's pooled cost; now that its
            # per-sub-turn rows nest under it (each owning its slice), drop the pool so
            # Σ owned == Σ turns. An agent with no sub-turns parsed keeps it as a leaf.
            parent["own_cost_usd"] = 0.0
            parent["own_tokens_in"] = 0
            parent["own_tokens_out"] = 0
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
        _attach(nodes.get(span.get("parent_id") or ""), node, roots)

    _sort_tree(roots)
    return roots


def _attach(parent: CausalNode | None, node: CausalNode, roots: list[CausalNode]) -> None:
    """Place ``node`` under ``parent`` (recording the structural parent id), else at root."""
    if parent is None:
        node["parent_id"] = None
        roots.append(node)
    else:
        node["parent_id"] = parent["node_id"]
        parent["children"].append(node)


def _build_spine(markers: list[dict[str, Any]], main_turns: list[CausalNode]) -> list[CausalNode]:
    """The L1 phase-interval spine, with every main turn bucketed into a covering interval.

    Intervals run ``(prev_marker.ts_end, marker.ts_end]`` with the first opening at
    ``-inf`` and the last closing at ``+inf``, so the bucketing is total — no main turn
    falls to ``(unresolved)``. With no markers the whole run is one synthetic interval.
    """
    if not markers:
        if not main_turns:
            return []
        run = causal_node(
            node_id="__run__",
            kind="interval",
            name="run",
            ts_start=min((t["ts_start"] for t in main_turns if t["ts_start"]), default=None),
            ts_end=max((t["ts_end"] for t in main_turns if t["ts_end"]), default=None),
        )
        for turn in main_turns:
            _attach(run, turn, [])
        return [run]

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

    for turn in main_turns:
        _attach(_interval_for(_ts(turn["ts_start"]), bounds), turn, [])
    return [interval for _, _, interval, _ in bounds]


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
    schemas = {"count": ctx["schema_count"], "tokens": ctx["schema_tokens"]}
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
    """The integer token estimate from a ``~N tokens`` context-load summary (else 0)."""
    if not isinstance(summary, str):
        return 0
    digits = re.sub(r"[^\d]", "", summary)
    return int(digits) if digits else 0


def _turn_node(row: dict[str, Any]) -> CausalNode:
    is_sub = row.get("source") == "subagent"
    return causal_node(
        node_id=row.get("uuid") or f"turn:{row.get('ts')}",
        kind="turn",
        name="turn",
        actor="subagent" if is_sub else "main",
        ts_start=row.get("ts"),
        ts_end=row.get("ts"),
        own_cost_usd=float(row.get("cost_usd") or 0.0),
        own_tokens_in=int(row.get("tokens_in") or 0),
        own_tokens_out=int(row.get("tokens_out") or 0),
    )


def _span_node(span: dict[str, Any]) -> CausalNode:
    kind = span["kind"]
    links: dict[str, Any] = {}
    for key in ("emits", "sidecar_session", "agent_link"):
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
        own_cost_usd=float(span.get("cost_usd") or 0.0) if kind == "agent" else 0.0,
        human_count=1 if span.get("human_type") else 0,
        **links,
    )


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
