"""Shared step-node primitives for the dashboard's meta-by-kind + spoke_tree views.

Extracted from the former ``dashboard/tree.py`` when Issue #80 deleted the legacy
timestamp-bucketed forest builder (``SpanStore.spoke_steps``). These are the primitives
the *remaining* consumers still need: ``queries._meta_nodes`` (the meta-by-kind tab's
span->node build with once-per-turn cost attribution), ``queries.spoke_tree`` (the
``parent_id`` forest + its rollup), and the additive subtree rollup the causal forest
attaches (``queries.spoke_causal_forest``). ``_parse_ts`` is the shared ISO-timestamp
parse used across the query layer (e.g. gate-window matching).

They are behaviour-preserving verbatim moves; the bucketing / interval / synthetic-node
builders that lived alongside them were the legacy path and are gone with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_ts(ts: str | None) -> float | None:
    """ISO-8601 UTC string to epoch seconds (None if missing/malformed).

    Parsed numerically rather than compared lexically because push spans carry
    second precision (``…00Z``) and pull spans millisecond (``…00.000Z``), which
    sort in the wrong order as strings.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _step_node(row: dict[str, Any]) -> dict[str, Any]:
    node = {
        "span_id": row["span_id"],
        "parent_id": row["parent_id"],
        "kind": row["kind"],
        "name": row["name"],
        "summary": row["summary"],
        "phase": row["phase"],
        "status": row["status"],
        "ts_start": row["ts_start"],
        "ts_end": row["ts_end"],
        "duration_ms": row["duration_ms"] or 0,
        "human_type": row["human_type"],
        "human_wait_ms": row["human_wait_ms"],
        "human_count": 1 if row["human_type"] else 0,
        # Filled by the once-per-turn attribution pass; the span schema carries
        # no model, so these come from the turns relation, never spans.cost_usd.
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "actor": _actor_for(row),
        "children": [],
    }
    # The v3 emission link (Issue #54): surface a script span's `emits` (the
    # step/lifecycle marker it produced) so the trace can draw the script→marker
    # chain. Added ONLY when set — a node with no emission keeps no key, so the
    # frozen v1/v2 golden forest stays byte-identical.
    if row.get("emits"):
        node["emits"] = row["emits"]
    return node


def _actor_for(row: dict[str, Any]) -> str:
    """The v3 Actor column for a real span (Issue #50/#52).

    ``main`` for the primary agent; an ``agent`` span carries its sub-agent name
    (``Explore``, ``code-review``, …); ``workflow`` brackets a fan-out; a ``hook`` is
    ``sidecar`` when it shells out to a ``claude -p`` session, else ``hooks``; a
    ``script`` is ``script``. Everything else is ``main``.
    """
    kind = row["kind"]
    if kind == "agent":
        return row["name"] or "subagent"
    if kind in ("workflow", "workflow_phase"):
        return "workflow"
    if kind == "script":
        return "script"
    if kind == "hook":
        return "sidecar" if row.get("sidecar_session") else "hooks"
    return "main"


def _acc() -> dict[str, Any]:
    return {"cost": 0.0, "in": 0, "out": 0, "models": set()}


def _add_turn(acc: dict[str, Any], turn: dict[str, Any]) -> None:
    acc["cost"] += turn["cost_usd"] or 0.0
    acc["in"] += turn["tokens_in"] or 0
    acc["out"] += turn["tokens_out"] or 0
    if turn["model"]:
        acc["models"].add(turn["model"])


def _fill_owned(node: dict[str, Any], acc: dict[str, Any]) -> None:
    node["own_cost_usd"] = acc["cost"]
    node["own_tokens_in"] = acc["in"]
    node["own_tokens_out"] = acc["out"]
    node["models"] = sorted(acc["models"])


def _attribute_turns(nodes: list[dict[str, Any]], turns: list[dict[str, Any]]) -> None:
    """Fill each ``agent`` node's owned subagent cost/tokens/models for meta-by-kind.

    A **subagent** turn attaches to the tightest enclosing ``agent`` span; every
    other turn (main, or a subagent turn with no enclosing agent) is off-node and
    contributes to no span's owned cost. This fills the flat-node ``own_cost`` the
    meta-by-kind view reads; the drill-down ignores it and routes the raw turn rows
    to turn nodes instead (so the tree never double-counts the agent's cost).
    """
    bounds = {n["span_id"]: (_parse_ts(n["ts_start"]), _parse_ts(n["ts_end"])) for n in nodes}
    owned: dict[str, dict[str, Any]] = {n["span_id"]: _acc() for n in nodes}
    for turn in turns:
        if turn["source"] != "subagent":
            continue
        owner_id = _subagent_owner(turn, nodes, bounds)
        if owner_id is not None:
            _add_turn(owned[owner_id], turn)
    for node in nodes:
        _fill_owned(node, owned[node["span_id"]])


def _subagent_owner(
    turn: dict[str, Any],
    nodes: list[dict[str, Any]],
    bounds: dict[str, tuple[float | None, float | None]],
) -> str | None:
    """The id of the tightest ``agent`` span containing a subagent turn.

    A parallel-agent caveat: with overlapping agent windows the smallest one wins
    by ``span_id`` tie-break — still counted once, just possibly attributed to a
    sibling agent.
    """
    ts = _parse_ts(turn["ts"])
    if ts is None:
        return None
    best_key: tuple[float, float, str] | None = None
    best_id: str | None = None
    for node in nodes:
        if node["kind"] != "agent":
            continue
        start, end = bounds[node["span_id"]]
        if start is None or end is None or not (start <= ts <= end):
            continue
        key = (end - start, start, node["span_id"])
        if best_key is None or key < best_key:
            best_key, best_id = key, node["span_id"]
    return best_id


def _roll_up_steps(node: dict[str, Any]) -> dict[str, Any]:
    """Attach an additive subtree ``rollup`` to ``node`` (post-order).

    A collapsed ``hooks`` node owns no metrics itself — its hook children carry
    them — so summing self + children never double-counts. The returned dict
    carries ``models`` as a set for merging; the node stores it sorted.

    Status is **last-event-wins** (Issue #57): a container's ``rollup.status`` is the
    status of the chronologically last leaf in its subtree (by ``ts_start`` — in
    practice the step's closing marker, emitted last), NOT the worst severity. A leaf
    keeps its own status, so a recovered deny/failure stays at its leaf and never
    reddens an ancestor that completed. The returned ``terminal_ts`` threads that last
    leaf up the post-order walk.
    """
    models: set[str] = set(node.get("models") or [])
    human = node["human_count"]
    cost = node.get("own_cost_usd", 0.0)
    tokens_in = node.get("own_tokens_in", 0)
    tokens_out = node.get("own_tokens_out", 0)
    terminal_ts: float | None = None
    terminal_status: str | None = None
    for child in node["children"]:
        child_rollup = _roll_up_steps(child)
        human += child_rollup["human_count"]
        cost += child_rollup["cost_usd"]
        tokens_in += child_rollup["tokens_in"]
        tokens_out += child_rollup["tokens_out"]
        models |= child_rollup["models"]
        child_ts = child_rollup["terminal_ts"]
        if terminal_status is None or (child_ts or 0.0) >= (terminal_ts or 0.0):
            terminal_ts, terminal_status = child_ts, child_rollup["terminal_status"]
    if not node["children"]:
        # A leaf is itself the terminal event; it keeps its own status.
        terminal_ts, terminal_status = _parse_ts(node["ts_start"]), node["status"]
    node["rollup"] = {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": sorted(models),
        "status": terminal_status,
    }
    return {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": models,
        "terminal_ts": terminal_ts,
        "terminal_status": terminal_status,
    }


def _roll_up(node: dict[str, Any]) -> dict[str, int | float]:
    """Attach a ``subtree`` rollup to ``node`` and return it (post-order)."""
    subtree = {
        "duration_ms": node["duration_ms"] or 0,
        "cost_usd": node["cost_usd"] or 0.0,
        "tokens_in": node["tokens_in"] or 0,
        "tokens_out": node["tokens_out"] or 0,
        "human_count": node["human_count"],
    }
    for child in node["children"]:
        child_subtree = _roll_up(child)
        for key, value in child_subtree.items():
            subtree[key] += value
    node["subtree"] = subtree
    return subtree
