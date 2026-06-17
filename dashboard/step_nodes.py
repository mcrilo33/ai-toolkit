"""Shared step-node primitives for the dashboard spoke views.

Extracted from the former ``dashboard/tree.py`` when Issue #80 deleted the legacy
timestamp-bucketed forest builder (``SpanStore.spoke_steps``). These survivors are the
primitives the *remaining* consumers still need: ``queries._attributed_nodes`` (the
meta-by-kind tab), ``queries.spoke_tree``, the rollups the causal forest attaches
(``queries.spoke_causal_forest``), and the gate-window timestamp parse. They are
behaviour-preserving verbatim moves — what lived here as bucketing/collapsing/synthetic
builders was the legacy path and is gone with it.

What lives here: ISO-timestamp parsing, span->step node construction with actor
inference, phase-interval reconstruction, the once-per-turn cost attribution, and the
subtree rollups.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Synthetic bucket ids for the phase-interval attribution (Issue #46): the leading
# pre-cycle region and the off-spine catch-all. Real intervals key on their marker's
# ``span_id``, so these sentinels never collide with a real span.
_SETUP_KEY = "__setup__"
_UNRESOLVED_KEY = "__unresolved__"

# A phase that ran real work but emitted no ``step`` marker is synthesized from its
# ``in_progress`` todo transition (Issue #52); the badge marks the label as inferred.
_NO_MARKER_BADGE = "⟨from todo — no marker⟩"

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

def _build_intervals(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct contiguous phase intervals from the step/lifecycle marker spine.

    Markers fire at phase *completion*, so the spine is sorted by ``ts_end`` and
    ``interval[i] = (M[i-1].ts_end, M[i].ts_end]`` is the work that culminated in
    marker ``M[i]``; ``interval[0]`` floors at the earliest marker ``ts_start`` (the
    spawn). Sorting by completion time keeps ``hi`` monotonic, so the intervals tile
    ``[earliest_start, Mn.ts_end]`` with no gap or inversion even when a wide marker
    overlaps a later one (point markers make this moot, but the contract is robust).
    Every interval up to and including the first ``step`` marker keys to the
    ``setup`` bucket (the pre-cycle gap has no phase-start signal, so its work is
    honestly coarse rather than mislabelled); the rest key per-phase by their
    marker's ``span_id``.
    """
    markers = sorted(
        (
            n
            for n in nodes
            if n["kind"] in ("step", "lifecycle")
            and _parse_ts(n["ts_start"]) is not None
            and _parse_ts(n["ts_end"]) is not None
        ),
        key=lambda n: (_parse_ts(n["ts_end"]) or 0.0, n["span_id"] or ""),
    )
    if not markers:
        return []
    first_step = next((i for i, m in enumerate(markers) if m["kind"] == "step"), None)
    floor_iso = min(markers, key=lambda n: _parse_ts(n["ts_start"]) or 0.0)["ts_start"]
    intervals: list[dict[str, Any]] = []
    for i, marker in enumerate(markers):
        lo_iso = floor_iso if i == 0 else markers[i - 1]["ts_end"]
        is_setup = first_step is not None and i <= first_step
        intervals.append(
            {
                "lo": _parse_ts(lo_iso),
                "hi": _parse_ts(marker["ts_end"]),
                "lo_iso": lo_iso,
                "hi_iso": marker["ts_end"],
                "first": i == 0,
                "key": _SETUP_KEY if is_setup else marker["span_id"],
                "label": "setup" if is_setup else (marker["phase"] or marker["name"]),
                # ``setup`` and the lifecycle (teardown) envelope are honestly
                # coarse: a todo never renames them (Issue #52, the phantom-first-
                # step fix). Real per-phase buckets stay nameable by the todo they
                # advance (Issue #47).
                "lock_label": is_setup or marker["kind"] == "lifecycle",
            }
        )
    # Refine the coarse spine with the todo ``in_progress`` transitions (Issue #52):
    # split the leading setup at the first transition, and synthesize a marker-less
    # phase wherever a transition runs work inside a lifecycle (teardown) region.
    todos = sorted(
        (
            n
            for n in nodes
            if n["kind"] == "todo" and n.get("summary") and _parse_ts(n["ts_start"]) is not None
        ),
        key=lambda n: (_parse_ts(n["ts_start"]) or 0.0, n["span_id"] or ""),
    )
    intervals = _split_spawn(intervals, markers, first_step, floor_iso, todos)
    intervals = _synthesize_no_marker(intervals, markers, todos)
    # Capping a merged setup interval at the split can invert a sub-interval whose
    # ``lo`` already sat past the split (overlapping wide markers); drop those — the
    # first=True setup interval still floors the envelope, so no key is lost.
    intervals = [iv for iv in intervals if (iv["lo"] or 0.0) <= (iv["hi"] or 0.0)]
    # Keep ``hi`` monotonic so ``_span_bucket_key`` floors/ceils on the true envelope.
    intervals.sort(key=lambda iv: (iv["hi"] or 0.0, iv["lo_iso"] or ""))
    return intervals

def _split_spawn(
    intervals: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    first_step: int | None,
    floor_iso: str,
    todos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split the leading ``setup`` block at the first ``in_progress`` todo transition.

    Setup + planning + ledger creation stay in ``spawn``; the first real phase
    becomes its own step, split at the first summarised todo that lands inside the
    setup region (Issue #52, decision 2). With no such transition (the v1/v2
    fixtures) the spine is returned unchanged, so the regression golden holds.
    """
    if first_step is None or not todos:
        return intervals
    fs = markers[first_step]
    floor, fs_end = _parse_ts(floor_iso), _parse_ts(fs["ts_end"])
    if floor is None or fs_end is None:
        return intervals
    split = next((t for t in todos if floor < (_parse_ts(t["ts_start"]) or 0.0) <= fs_end), None)
    if split is None:
        return intervals
    split_ts, split_iso = _parse_ts(split["ts_start"]), split["ts_start"]
    capped = [
        {**iv, "hi": split_ts, "hi_iso": split_iso} if iv["key"] == _SETUP_KEY else iv
        for iv in intervals
    ]
    red = {
        "lo": split_ts,
        "hi": fs_end,
        "lo_iso": split_iso,
        "hi_iso": fs["ts_end"],
        "first": False,
        "key": fs["span_id"],
        # The first real phase is named for the todo whose transition split it off
        # (Issue #47 todo-naming, now on the phase bucket — never on setup). Locked
        # so the boundary todo landing back in spawn can't strip it to a bare phase.
        "label": split["summary"],
        "lock_label": True,
    }
    return [*capped, red]

def _synthesize_no_marker(
    intervals: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    todos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carve marker-less phases out of a lifecycle region at todo transitions.

    A lifecycle (teardown) interval is coarse — it has no phase-start signal. Each
    summarised todo strictly inside one began a phase that emitted no ``step``
    marker; the region keeps ``[lo, first todo]`` under its own (teardown) key and
    every transition opens its own ``⟨from todo — no marker⟩``-badged bucket running
    to the next transition (or the region end), so each phase is distinct and its
    work never falls to ``(unresolved)`` (Issue #52). ``< hi`` is strict so a todo on
    the region boundary never synthesizes an empty zero-width bucket.
    """
    lifecycle_keys = {m["span_id"] for m in markers if m["kind"] == "lifecycle"}
    out: list[dict[str, Any]] = []
    for iv in intervals:
        lo, hi = iv["lo"], iv["hi"]
        inside = (
            sorted(
                (
                    t
                    for t in todos
                    if lo is not None
                    and hi is not None
                    and lo < (_parse_ts(t["ts_start"]) or 0.0) < hi
                ),
                key=lambda t: (_parse_ts(t["ts_start"]) or 0.0, t["span_id"] or ""),
            )
            if iv["key"] in lifecycle_keys
            else []
        )
        if not inside:
            out.append(iv)
            continue
        out.append({**iv, "hi": _parse_ts(inside[0]["ts_start"]), "hi_iso": inside[0]["ts_start"]})
        for i, todo in enumerate(inside):
            nxt = inside[i + 1] if i + 1 < len(inside) else None
            out.append(
                {
                    "lo": _parse_ts(todo["ts_start"]),
                    "hi": _parse_ts(nxt["ts_start"]) if nxt else hi,
                    "lo_iso": todo["ts_start"],
                    "hi_iso": nxt["ts_start"] if nxt else iv["hi_iso"],
                    "first": False,
                    "key": todo["span_id"],
                    "label": f"{todo['summary']} {_NO_MARKER_BADGE}",
                    "lock_label": True,
                }
            )
    return out

def _interval_containing(ts: float, intervals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The interval whose window holds ``ts`` (right-closed at each marker boundary).

    The first interval is closed at both ends; the rest are left-open, so a turn on
    a shared marker boundary lands in the earlier interval — counted exactly once.
    """
    for iv in intervals:
        lo, hi = iv["lo"], iv["hi"]
        if lo is None or hi is None:
            continue
        if (lo <= ts if iv["first"] else lo < ts) and ts <= hi:
            return iv
    return None

def _main_turn_bucket(turn: dict[str, Any], intervals: list[dict[str, Any]]) -> str:
    """The bucket id a main turn belongs to (``(unresolved)`` when off the spine)."""
    ts = _parse_ts(turn["ts"])
    if ts is None or not intervals:
        return _UNRESOLVED_KEY
    iv = _interval_containing(ts, intervals)
    return iv["key"] if iv is not None else _UNRESOLVED_KEY

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

def _turns_by_owner(
    turns: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    bounds: dict[str, tuple[float | None, float | None]],
) -> dict[str, list[dict[str, Any]]]:
    """Group turn rows by owner: a main turn → its phase-interval bucket key; a
    subagent turn → the tightest enclosing ``agent`` span id, else its phase-interval
    bucket (or ``(unresolved)`` only when truly off-spine). Each list is time-ordered,
    so turn nodes render in inference order.

    The interval fallback (Issue #52, defect #3) is what keeps a subagent turn whose
    agent span lives one directory deeper than the parser walked — so no ``agent``
    span brackets it — attributed to the phase its timestamp sits in, rather than
    orphaning the whole Workflow fan-out to a bogus ``(unresolved)``.
    """
    owners: dict[str, list[dict[str, Any]]] = {}
    for turn in turns:
        if turn.get("source") == "subagent":
            owner = _subagent_owner(turn, nodes, bounds) or _main_turn_bucket(turn, intervals)
        else:
            owner = _main_turn_bucket(turn, intervals)
        owners.setdefault(owner, []).append(turn)
    for rows in owners.values():
        rows.sort(key=lambda t: _parse_ts(t["ts"]) or 0.0)
    return owners

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
