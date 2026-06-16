"""Spoke-trace forest construction for the dashboard (Issue #50, Part 3).

Extracted verbatim from ``queries.py`` so the SQL + aggregate/meta/A-B layer
(``queries.py``) and the tree layer (this module) stop colliding in one file — the
v3 spoke-trace tracks (Parser / Tree / App / Emission) then develop against this
seam independently. This is a **behaviour-preserving move**: ``queries.py`` imports
these builders back and the forest a reader sees is byte-for-byte identical
(guarded by the golden snapshot in ``tests/unit/test_dashboard_tree_extraction.py``).

What lives here: phase-interval reconstruction, bucket / marker / turn / synthetic
node construction, time- and parent-nesting, hook collapsing, the once-per-turn
cost attribution, and the subtree rollups. What stays in ``queries.py``: the DuckDB
ingestion + SQL, the meta-by-kind / aggregate / A-B rollups, and the formatters.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

# The frozen synthetic-node contract (Issue #50) lives in the shared telemetry
# package under ``scripts/``; the dashboard is otherwise self-contained, so put
# that dir on the path here rather than relying on the caller's sys.path. The
# unit harness loads this module by file path and ``streamlit run`` injects only
# the dashboard dir, so neither would resolve ``telemetry`` without this. Unlike
# the live-DB-only imports in ``queries.py``/``app.py``, ``synthetic_node`` is
# core to every forest build, so the dependency is hard and the import eager.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from telemetry.spans import derive_span_id, synthetic_node

# Status severity for collapsing many spans into one line: a collapsed hooks row
# must surface the worst outcome, never hide a deny/failure/warn behind success.
_STATUS_SEVERITY: dict[str, int] = {
    "deny": 4,
    "failure": 3,
    "warn": 2,
    "skipped": 1,
    "success": 0,
}

# Synthetic bucket ids for the phase-interval attribution (Issue #46): the leading
# pre-cycle region and the off-spine catch-all. Real intervals key on their marker's
# ``span_id``, so these sentinels never collide with a real span.
_SETUP_KEY = "__setup__"
_UNRESOLVED_KEY = "__unresolved__"

# A phase that ran real work but emitted no ``step`` marker is synthesized from its
# ``in_progress`` todo transition (Issue #52); the badge marks the label as inferred.
_NO_MARKER_BADGE = "⟨from todo — no marker⟩"

# Idle longer than this between consecutive activity renders as a ``gap`` divider
# rather than dead phase time (Issue #52). Ten minutes: long enough to skip normal
# inter-turn latency, short enough to surface a real break (a resume, a stall).
_IDLE_GAP_SECONDS = 600


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


def _sort_key(node: dict[str, Any]) -> tuple[float, str]:
    return (_parse_ts(node["ts_start"]) or 0.0, node["span_id"] or "")


def _nest_by_time(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest each node under its parent: explicit ``parent_id`` first, else by time.

    A node carrying a ``parent_id`` that resolves to a peer in this set is attached
    there directly (Issue #47 S3 — sub-agent spans bind to their agent regardless of
    overlapping windows) and is excluded from the time-bracketing candidate pool.
    Every other node (all main/push spans, which carry no ``parent_id``) nests under
    the smallest span that *started before it* and whose window contains it — so
    tool calls issued in the same turn (shared ``ts_start``) stay siblings, while a
    hook that fires during a tool nests under it.
    """
    by_id = {n["span_id"]: n for n in nodes}
    # A self-referential link is never explicit (it would parent a node to itself
    # and vanish from the tree) — such a node degrades to time-bracketing instead.
    explicit = [
        n
        for n in nodes
        if n.get("parent_id") and n["parent_id"] in by_id and n["parent_id"] != n["span_id"]
    ]
    for node in explicit:
        by_id[node["parent_id"]]["children"].append(node)

    floating = [n for n in nodes if n not in explicit]
    bounds = {n["span_id"]: (_parse_ts(n["ts_start"]), _parse_ts(n["ts_end"])) for n in floating}
    roots: list[dict[str, Any]] = []
    for node in floating:
        parent_id = _smallest_container(node, floating, bounds)
        if parent_id is None:
            roots.append(node)
        else:
            by_id[parent_id]["children"].append(node)
    return roots


def _smallest_container(
    child: dict[str, Any],
    nodes: list[dict[str, Any]],
    bounds: dict[str, tuple[float | None, float | None]],
) -> str | None:
    """The id of the tightest span that *started strictly before* and encloses ``child``.

    Requiring a strictly-earlier start (``ps < cs``) keeps tool calls issued in the
    same turn — which share their inference's ``ts_start`` — as siblings, while a
    hook that fires after a tool began nests under it. An at-start child
    (``ps == cs``) is therefore intentionally a sibling, never nested. Ties break
    on ``span_id``.
    """
    cs, ce = bounds[child["span_id"]]
    if cs is None or ce is None:
        return None
    best_key: tuple[float, float, str] | None = None
    best_id: str | None = None
    for cand in nodes:
        if cand is child:
            continue
        ps, pe = bounds[cand["span_id"]]
        if ps is None or pe is None:
            continue
        if ps < cs and pe >= ce:
            key = (pe - ps, ps, cand["span_id"])
            if best_key is None or key < best_key:
                best_key, best_id = key, cand["span_id"]
    return best_id


def _collapse_hooks(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse hook spans among ``siblings`` into one node (recursively)."""
    others = [n for n in siblings if n["kind"] != "hook"]
    hooks = [n for n in siblings if n["kind"] == "hook"]
    for node in others:
        node["children"] = _collapse_hooks(node["children"])
    for hook in hooks:
        hook["children"] = _collapse_hooks(hook["children"])
    result = list(others)
    if hooks:
        result.append(_hooks_node(hooks))
    return sorted(result, key=_sort_key)


# Kinds whose wide identical leaf siblings collapse into one ``<kind> xN`` group
# (Issue #56), generalizing the hooks collapse. ``hook`` keeps its own unconditional
# ``_collapse_hooks`` (it groups every hook regardless of label); these kinds group
# only genuinely duplicate/parallel leaves — see ``_leaf_collapse_key``.
#
# ``turn`` is deliberately excluded though the issue lists it: under the strict key
# below a turn would only ever group with a *same-timestamp* turn, but those are
# distinct cost-bearing inferences the tree keeps separate on purpose (the per-turn
# cost/composition panel, and the ``test_same_ts_*_turns_are_both_kept`` guards). A
# collapsed group also carries its members' kind, so a ``turn`` group would
# masquerade as a real turn to any kind-based counter. todo/agent leaves own no cost
# (it lives on turns), so they collapse cleanly.
_COLLAPSIBLE_LEAF_KINDS = frozenset({"todo", "agent"})


def _leaf_collapse_key(node: dict[str, Any]) -> tuple[str, str, str | None, str | None, str | None]:
    """The identity two leaf siblings must share to collapse into one group.

    Strict by design (Issue #56): identical kind/name/summary at the *same*
    ``ts_start`` (and model). Only true duplicates (a burst of identical ledger
    writes) or a same-instant parallel fan-out collapse — never spread-out siblings.
    """
    return (node["kind"], node["name"], node.get("summary"), node["ts_start"], node.get("model"))


def _collapse_leaves(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse runs of identical childless leaf siblings into ``xN`` groups (recursively).

    Generalizes ``_collapse_hooks`` to the spec's wider ``xN`` groups (``todo x3``,
    ``agent x3 parallel``, ``turns x7``): childless siblings of a collapsible kind
    sharing a :func:`_leaf_collapse_key` group into one node carrying
    ``collapsed_count`` with the members as ``children``. Only a *run* of ≥2 collapses
    — a lone leaf, or a node that owns sub-work, passes through untouched. Recurses
    post-order so nested sibling lists (e.g. bare todos rehomed under a turn) collapse
    too. Members are never omitted; the drill reveals each.
    """
    for node in siblings:
        node["children"] = _collapse_leaves(node["children"])
    groups: dict[tuple[str, str, str | None, str | None, str | None], list[dict[str, Any]]] = {}
    for node in siblings:
        if node["kind"] in _COLLAPSIBLE_LEAF_KINDS and not node["children"]:
            groups.setdefault(_leaf_collapse_key(node), []).append(node)
    collapsed_ids = {id(n) for members in groups.values() if len(members) > 1 for n in members}
    result = [n for n in siblings if id(n) not in collapsed_ids]
    result.extend(_leaf_group_node(members) for members in groups.values() if len(members) > 1)
    return sorted(result, key=_sort_key)


def _leaf_group_node(members: list[dict[str, Any]]) -> dict[str, Any]:
    """One collapsed ``<kind> xN`` group for wide identical todo/turn/agent leaves (#56).

    The group renders as ``<kind> xN`` (the app layer, #53), so it carries its
    members' real span *kind* (``todo`` / ``agent`` / ``turn``). A ``span_id=None``
    node may only carry a *synthetic* kind (the SyntheticNode contract — ``todo`` and
    ``agent`` are span kinds), so the group takes a namespaced, clearly-synthetic
    ``collapse:`` span_id instead. Like every container it owns ``$0`` (its member
    leaves carry the cost) and its status comes from :func:`_worst_status` — the
    single shared rollup helper the tree uses, never a hardcoded worst-child, so the
    group follows that helper if its semantics change (#57).
    """
    key = _leaf_collapse_key(members[0])
    kind, name = key[0], key[1]
    starts = [m["ts_start"] for m in members if m["ts_start"]]
    ends = [m["ts_end"] for m in members if m["ts_end"]]
    # synthetic_node guards its kind against span kinds, so build the canonical node
    # shape under a placeholder synthetic kind, then stamp the group's display kind.
    node: dict[str, Any] = dict(
        synthetic_node(
            kind="hooks",
            name=name,
            status=_worst_status(members),
            ts_start=min(starts, key=lambda s: _parse_ts(s) or 0.0) if starts else None,
            ts_end=max(ends, key=lambda s: _parse_ts(s) or 0.0) if ends else None,
            duration_ms=sum(m["duration_ms"] or 0 for m in members),
            children=sorted(members, key=_sort_key),
        )
    )
    node["kind"] = kind
    # Derive the id from the *full* collapse key (incl. summary) so two groups that
    # differ only by summary never collide on span_id; the ``collapse:`` prefix keeps
    # it visibly synthetic.
    node["span_id"] = f"collapse:{kind}:{derive_span_id(*(str(part) for part in key))}"
    node["collapsed"] = True
    node["collapsed_count"] = len(members)
    return node


def _hooks_node(hooks: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [h["ts_start"] for h in hooks if h["ts_start"]]
    ends = [h["ts_end"] for h in hooks if h["ts_end"]]
    node: dict[str, Any] = dict(
        synthetic_node(
            kind="hooks",
            name="hooks",
            status=_worst_status(hooks),
            ts_start=min(starts, key=lambda s: _parse_ts(s) or 0.0) if starts else None,
            ts_end=max(ends, key=lambda s: _parse_ts(s) or 0.0) if ends else None,
            duration_ms=sum(h["duration_ms"] for h in hooks),
            actor="hooks",
            # A collapsed node owns no turns itself — its hook children carry any.
            children=list(hooks),
        )
    )
    node["collapsed"] = True
    node["collapsed_count"] = len(hooks)
    return node


def _worst_status(nodes: list[dict[str, Any]]) -> str:
    return max(
        (n["status"] for n in nodes),
        key=lambda s: _STATUS_SEVERITY.get(s, 0),
        default="success",
    )


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


def build_dividers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize the root-level ``session`` and ``gap`` divider rows for a spoke.

    A spoke that spans more than one ``session_id`` gets a ``session`` divider at each
    resume (carrying the cold-cache note — a resume re-reads the prompt cache), and a
    stretch of idle longer than :data:`_IDLE_GAP_SECONDS` between consecutive activity
    renders as a ``gap`` divider (Issue #52: idle is a divider, not dead phase time).
    Built from the raw span ``rows`` (which carry ``session_id``); the dividers are
    merged into the bucket forest as roots and sort by ``ts_start`` like any row.
    """
    timed: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        start = _parse_ts(row["ts_start"])
        if start is not None:
            timed.append((row, start))
    timed.sort(key=lambda pair: pair[1])
    dividers: list[dict[str, Any]] = []

    first_seen: dict[str, str] = {}
    for row, _ in timed:
        sid = row.get("session_id")
        if sid and sid not in first_seen:
            first_seen[sid] = row["ts_start"]
    for iso in list(first_seen.values())[1:]:
        dividers.append(_session_divider(iso))

    end_ts: float | None = None
    end_iso: str | None = None
    for row, start in timed:
        if end_ts is not None and start - end_ts > _IDLE_GAP_SECONDS:
            dividers.append(_gap_divider(end_iso, row["ts_start"], (start - end_ts) * 1000.0))
        row_end = _parse_ts(row["ts_end"]) or start
        if end_ts is None or row_end > end_ts:
            end_ts, end_iso = row_end, (row["ts_end"] or row["ts_start"])
    return dividers


def _session_divider(iso: str) -> dict[str, Any]:
    # UPGRADE: surface the real re-read magnitude once the turns schema carries
    # cache_creation tokens; today the note is static (no per-resume signal).
    return dict(
        synthetic_node(
            kind="session",
            name="session resume",
            summary="cold cache (cache_creation) — prompt re-read on resume",
            ts_start=iso,
            ts_end=iso,
        )
    )


def _gap_divider(lo_iso: str | None, hi_iso: str, duration_ms: float) -> dict[str, Any]:
    return dict(
        synthetic_node(
            kind="gap",
            name="idle",
            ts_start=lo_iso,
            ts_end=hi_iso,
            duration_ms=int(duration_ms),
        )
    )


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


def _interval_forest(
    nodes: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    turns_by_owner: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the Level-1 interval-bucket roots with the turn-centric tree beneath.

    Each distinct bucket key becomes one root; its spans nest under the bucket
    whose interval contains their ``ts_start`` (clamped to the envelope). Inside a
    bucket (Issue #47 S3): step/lifecycle markers become thin header leaves, the
    bucket's main turns become turn nodes owning their cost, and the inference's
    spans nest under their turn by ``ts_start`` match. ``(unresolved)`` appears
    only when an off-spine turn or span exists.
    """
    windows = _bucket_windows(intervals)
    # A sub-agent span follows its agent into the agent's bucket — not its own
    # ts_start's interval — so a long-running agent that straddles a phase marker
    # keeps its sub-spans nested under it rather than scattering them across
    # buckets (and under unrelated main turns).
    agent_bucket = {
        n["span_id"]: _span_bucket_key(n, intervals) for n in nodes if n["kind"] == "agent"
    }
    spans_by_key: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        key = agent_bucket.get(node["parent_id"]) or _span_bucket_key(node, intervals)
        spans_by_key.setdefault(key, []).append(node)

    roots: list[dict[str, Any]] = []
    for key, window in windows.items():
        bucket_spans = spans_by_key.get(key, [])
        children = _bucket_children(bucket_spans, turns_by_owner.get(key, []), turns_by_owner)
        todo_label = None if window.get("lock_label") else _bucket_todo_label(bucket_spans)
        roots.append(_bucket_node(window, children, todo_label))

    orphan_spans = spans_by_key.get(_UNRESOLVED_KEY, [])
    orphan_turns = turns_by_owner.get(_UNRESOLVED_KEY, [])
    if orphan_turns or orphan_spans:
        children = _bucket_children(orphan_spans, orphan_turns, turns_by_owner)
        roots.append(_unresolved_node(children))
    roots.sort(key=_bucket_sort_key)
    return roots


def _bucket_children(
    bucket_spans: list[dict[str, Any]],
    bucket_turns: list[dict[str, Any]],
    turns_by_owner: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The turn-centric children of one bucket: marker headers + turn nodes.

    Markers (``step``/``lifecycle``) become thin non-container header leaves that
    keep their own wall-clock. The remaining spans nest by window (hooks under
    their tool, sub-agent spans under their agent via ``parent_id``); each
    resulting top-level span is then re-homed under the turn node whose ``ts``
    equals its ``ts_start`` (the inference that issued it), with no-match spans
    left directly under the bucket. Agent nodes get their sub-agent turn nodes.
    """
    markers = [_marker_leaf(n) for n in bucket_spans if n["kind"] in ("step", "lifecycle")]
    others = [n for n in bucket_spans if n["kind"] not in ("step", "lifecycle")]
    forest = _collapse_hooks(_nest_by_time(others))
    for node in _flatten(forest):
        if node["kind"] == "agent":
            _install_sub_turns(node, turns_by_owner.get(node["span_id"], []))

    turn_nodes = [_turn_node(turn) for turn in bucket_turns]
    orphans = _rehome_under_turns(forest, turn_nodes)
    children = _apply_scope_bands(sorted(markers + turn_nodes + orphans, key=_sort_key))
    children = _collapse_context(children)
    children = _collapse_leaves(children)
    return sorted(children, key=_sort_key)


def _collapse_context(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ``rule`` loads among ``siblings`` into per-subtype ``context`` nodes.

    Loaded context (``rule`` / ``memory`` / ``tool-schema``, keyed on the span
    ``phase``) collapses into one ``context`` group per subtype instead of N bare
    rows (the app renders the ``xN`` from ``collapsed_count``) — once under ``spawn``
    for the startup batch, and again in any later phase where context re-loads
    mid-run (the ``ctx-bust`` inline event). Recurses so a rule nested under a turn is
    grouped too; groups own ``$0`` (rules carry no cost).

    UPGRADE: a mid-run re-load is identified only positionally (a context group in a
    non-spawn bucket); tag it so a reader can tell a re-synced-rules bust from a load.
    """
    others = [n for n in siblings if n["kind"] != "rule"]
    rules = [n for n in siblings if n["kind"] == "rule"]
    for node in others:
        node["children"] = _collapse_context(node["children"])
    by_subtype: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        by_subtype.setdefault(rule["phase"] or "rule", []).append(rule)
    groups = [_context_node(subtype, members) for subtype, members in by_subtype.items()]
    return sorted(others + groups, key=_sort_key)


def _context_node(subtype: str, members: list[dict[str, Any]]) -> dict[str, Any]:
    """A collapsed context group (rendered ``xN``) for one loaded-context subtype."""
    starts = [m["ts_start"] for m in members if m["ts_start"]]
    ends = [m["ts_end"] for m in members if m["ts_end"]]
    node: dict[str, Any] = dict(
        synthetic_node(
            kind="context",
            name=subtype,
            phase=subtype,
            ts_start=min(starts, key=lambda s: _parse_ts(s) or 0.0) if starts else None,
            ts_end=max(ends, key=lambda s: _parse_ts(s) or 0.0) if ends else None,
            duration_ms=sum(m["duration_ms"] or 0 for m in members),
            children=sorted(members, key=_sort_key),
        )
    )
    node["collapsed"] = True
    node["collapsed_count"] = len(members)
    return node


def _apply_scope_bands(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render each ``skill`` span as a soft ``scope-band`` (Issue #52, decision 5).

    A skill loads instructions then guides later turns — a causal scope with no hard
    window — so it renders as a ``[scope]``-tagged band that holds both the work
    time-bracketed under the skill span and the sibling **turn** nodes it influenced,
    from its load until the next skill-load (its scope end). The band carries only the
    skill's own ``$0`` load cost — the turn/agent leaves keep theirs — so the subtree
    rollup is unchanged. ``nodes`` must be time-sorted; applied post-order so a skill
    nested under a turn or agent is banded too.

    UPGRADE: the scope end is the next skill-load (or the bucket/step end, since
    buckets are per-phase); the spec also allows step-end mid-bucket and tagging the
    inferred boundary — not modeled here. Only sibling ``turn`` nodes are pulled in;
    bare sibling spans (a tool with no owning turn) stay at bucket level.
    """
    for node in nodes:
        node["children"] = _apply_scope_bands(node["children"])
    if not any(n["kind"] == "skill" for n in nodes):
        return nodes
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(nodes):
        node = nodes[i]
        if node["kind"] != "skill":
            out.append(node)
            i += 1
            continue
        band = _scope_band_node(node)
        influenced: list[dict[str, Any]] = []
        j = i + 1
        while j < len(nodes) and nodes[j]["kind"] != "skill":
            (influenced if nodes[j]["kind"] == "turn" else out).append(nodes[j])
            j += 1
        band["children"] = sorted(band["children"] + influenced, key=_sort_key)
        out.append(band)
        i = j
    return sorted(out, key=_sort_key)


def _scope_band_node(skill: dict[str, Any]) -> dict[str, Any]:
    """A soft ``[scope]`` band standing in for a skill span, holding its influence."""
    band = dict(
        synthetic_node(
            kind="scope-band",
            name=f"[scope] {skill['name']}",
            summary=skill.get("summary"),
            phase=skill.get("phase"),
            status=skill["status"],
            ts_start=skill["ts_start"],
            ts_end=skill["ts_end"],
            duration_ms=skill.get("duration_ms"),
            human_count=skill.get("human_count", 0),
            children=skill["children"],
        )
    )
    # Synthetic nodes carry no span_id, but keep the skill's so the drill-down can
    # still link the band back to its source skill span.
    band["source_span_id"] = skill["span_id"]
    return band


def _install_sub_turns(agent: dict[str, Any], sub_turns: list[dict[str, Any]]) -> None:
    """Insert the sub-agent's turn nodes between an agent span and its sub-spans.

    The agent's existing children are the sub-agent spans (bound by ``parent_id``);
    they re-home under the sub-turn whose ``ts`` matches their ``ts_start``. The
    sub-turn nodes own the sub-agent cost, so the agent's own owned cost is moved
    onto them (the agent keeps it for the meta-by-kind view, not the tree) to avoid
    double-counting in the rollup.
    """
    if not sub_turns:
        return
    sub_nodes = [_turn_node(turn) for turn in sub_turns]
    orphans = _rehome_under_turns(agent["children"], sub_nodes)
    agent["children"] = sorted(sub_nodes + orphans, key=_sort_key)
    agent["own_cost_usd"] = 0.0
    agent["own_tokens_in"] = 0
    agent["own_tokens_out"] = 0
    agent["models"] = []


def _rehome_under_turns(
    spans: list[dict[str, Any]], turn_nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Re-home each span under the turn node that issued it (``ts_start`` == turn ts).

    Turns are looked up by ``ts_start`` through a list-valued index, never a dict
    keyed by ``ts`` — two inferences can share a millisecond timestamp, and a
    dict would silently overwrite (dropping a turn and its cost). A span whose ts
    matches no turn is returned as an orphan (it stays directly under the bucket).
    """
    by_ts: dict[str, list[dict[str, Any]]] = {}
    for node in turn_nodes:
        by_ts.setdefault(node["ts_start"], []).append(node)
    orphans: list[dict[str, Any]] = []
    for span in spans:
        matches = by_ts.get(span["ts_start"])
        (matches[0]["children"] if matches else orphans).append(span)
    return orphans


def _turn_node(turn: dict[str, Any]) -> dict[str, Any]:
    """A synthetic L2 node for one assistant inference, owning its once-per-turn cost.

    Never a span: ``span_id`` is None, it has no wall-clock, and it never enters the
    spans table, the ``turns`` table, or meta-by-kind — it exists only in the
    drill-down tree, like the ``interval`` / ``hooks`` synthetic nodes.
    """
    node: dict[str, Any] = dict(
        synthetic_node(
            kind="turn",
            name="turn",
            ts_start=turn["ts"],
            ts_end=turn["ts"],
            own_cost_usd=turn.get("cost_usd") or 0.0,
            own_tokens_in=turn.get("tokens_in") or 0,
            own_tokens_out=turn.get("tokens_out") or 0,
            models=[turn["model"]] if turn.get("model") else [],
            actor=turn.get("source", "main"),
        )
    )
    node["model"] = turn.get("model")
    return node


def _marker_leaf(node: dict[str, Any]) -> dict[str, Any]:
    """A step/lifecycle marker as a thin header leaf — its own wall-clock, no children."""
    return {**node, "children": []}


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


def _bucket_windows(intervals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One display window per bucket key, merging the leading ``setup`` intervals."""
    windows: dict[str, dict[str, Any]] = {}
    for iv in intervals:
        window = windows.get(iv["key"])
        if window is None:
            windows[iv["key"]] = {
                "label": iv["label"],
                "lo_iso": iv["lo_iso"],
                "hi_iso": iv["hi_iso"],
                # ``setup``/no-marker buckets keep their own label; a stray todo
                # never renames them (Issue #52).
                "lock_label": iv.get("lock_label", False),
            }
        else:
            window["hi_iso"] = iv["hi_iso"]  # extend setup over its merged intervals
    return windows


def _span_bucket_key(span: dict[str, Any], intervals: list[dict[str, Any]]) -> str:
    """The bucket a span displays under: its interval, clamped into the envelope.

    A ``step``/``lifecycle`` marker fires at phase *completion*, so it is placed by
    its ``ts_end`` — it heads the interval it concludes (keyed by its own span_id),
    even when a wide marker began in an earlier phase or its first-phase split point
    (Issue #52) falls after its start. Every other span is placed by ``ts_start``.
    """
    if not intervals:
        return _UNRESOLVED_KEY
    stamp = span["ts_end"] if span["kind"] in ("step", "lifecycle") else span["ts_start"]
    ts = _parse_ts(stamp)
    if ts is None or ts < intervals[0]["lo"]:
        return intervals[0]["key"]
    if ts > intervals[-1]["hi"]:
        return intervals[-1]["key"]
    iv = _interval_containing(ts, intervals)
    return iv["key"] if iv is not None else intervals[-1]["key"]


def _flatten(nodes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def _bucket_node(
    window: dict[str, Any],
    children: list[dict[str, Any]],
    todo_label: str | None = None,
) -> dict[str, Any]:
    """A synthetic phase-interval root; owns no cost (its turn nodes do), no duration.

    ``todo_label`` (Issue #47) names the bucket for the in-progress todo it
    advances, falling back to the phase/``setup`` label when none resolved.
    """
    return _synthetic_root(
        kind="interval",
        name=todo_label or window["label"],
        ts_start=window["lo_iso"],
        ts_end=window["hi_iso"],
        children=children,
    )


def _bucket_todo_label(bucket_spans: list[dict[str, Any]]) -> str | None:
    """The todo item a bucket advances: the latest summarised todo span in it.

    A todo span with no derived ``summary`` (no in-progress item resolved) is
    ignored — the bucket then keeps its phase label.
    """
    todos = sorted(
        (n for n in bucket_spans if n["kind"] == "todo" and n.get("summary")),
        key=_sort_key,
    )
    return todos[-1]["summary"] if todos else None


def _unresolved_node(children: list[dict[str, Any]]) -> dict[str, Any]:
    """A synthetic root for turns/spans off the lifecycle envelope, so totals reconcile.

    Off-spine turns never frame a window (a malformed ts must not format as garbage),
    so the node carries no ``ts_start``/``ts_end``.
    """
    return _synthetic_root(
        kind="unresolved", name="(unresolved)", ts_start=None, ts_end=None, children=children
    )


def _synthetic_root(
    *,
    kind: str,
    name: str,
    ts_start: str | None,
    ts_end: str | None,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """A synthetic bucket root: owns no cost (its turn-node children do), no duration."""
    return dict(
        synthetic_node(
            kind=kind,
            name=name,
            status=_worst_status(list(_flatten(children))),
            ts_start=ts_start,
            ts_end=ts_end,
            duration_ms=None,  # intervals are attribution-only, never a phase width
            children=children,
        )
    )


def _bucket_sort_key(node: dict[str, Any]) -> tuple[float, str]:
    """Order buckets by interval start; ``(unresolved)`` always sorts last."""
    if node["kind"] == "unresolved":
        return (float("inf"), "")
    return (_parse_ts(node["ts_start"]) or 0.0, node["name"])


def _roll_up_steps(node: dict[str, Any]) -> dict[str, Any]:
    """Attach an additive subtree ``rollup`` to ``node`` (post-order).

    A collapsed ``hooks`` node owns no metrics itself — its hook children carry
    them — so summing self + children never double-counts. The returned dict
    carries ``models`` as a set for merging; the node stores it sorted.

    Status rolls up worst-child (Issue #52, the single uniform propagation point): a
    container's ``rollup.status`` is the worst status among itself and its children,
    so a denied approval or failed hook surfaces all the way up its ancestors.
    """
    models: set[str] = set(node.get("models") or [])
    human = node["human_count"]
    cost = node.get("own_cost_usd", 0.0)
    tokens_in = node.get("own_tokens_in", 0)
    tokens_out = node.get("own_tokens_out", 0)
    statuses = [node["status"]]
    for child in node["children"]:
        child_rollup = _roll_up_steps(child)
        human += child_rollup["human_count"]
        cost += child_rollup["cost_usd"]
        tokens_in += child_rollup["tokens_in"]
        tokens_out += child_rollup["tokens_out"]
        models |= child_rollup["models"]
        statuses.append(child_rollup["status"])
    status = max(statuses, key=lambda s: _STATUS_SEVERITY.get(s, 0))
    node["rollup"] = {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": sorted(models),
        "status": status,
    }
    return {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": models,
        "status": status,
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
