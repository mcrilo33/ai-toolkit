"""Generalized ``xN`` collapse to wide identical leaf siblings (Issue #56 — RED).

``dashboard/tree.py`` historically collapsed only ``hook`` siblings into a
``hooks xN`` group (``_collapse_hooks`` → ``_hooks_node``). This generalizes the
collapse to wide *identical* childless leaf siblings of kind ``todo`` / ``agent``
(the spec's ``todo x3`` and ``agent x3 parallel``).

``turn`` is deliberately excluded although #56 lists it: under the strict key a turn
would only ever group with a same-timestamp turn, but those are distinct
cost-bearing inferences the tree keeps separate (the per-turn cost/composition panel
+ the ``test_same_ts_*_turns_are_both_kept`` guards), and a ``turn`` group would
masquerade as a real turn to any kind-based counter.

Equivalence is strict — keyed on ``(kind, name, summary, ts_start, model)`` — so
only genuinely duplicate or parallel leaves *at the same timestamp* group. Collapse
never omits: the members ride along as the group's ``children``, reachable by
drilling. The group takes its status from the same shared rollup helper the tree
uses (not a hardcoded worst-child), so it stays correct after #57 swaps the status
semantics.
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

from _dashboard_helpers import DASHBOARD_DIR, store_v2

V2_RUN_ID = "feature/v2+1000"


def _load_tree() -> ModuleType:
    """Import ``dashboard/tree.py`` as a standalone module (by file path)."""
    path = DASHBOARD_DIR / "tree.py"
    spec = importlib.util.spec_from_file_location("tree", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load tree module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk(forest: list[dict]):
    stack = list(forest)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node["children"])


def _leaf(kind: str, name: str, ts_start: str, **over) -> dict:
    """A childless real-span-shaped node carrying every key the collapse reads."""
    base = {
        "span_id": over.get("span_id", name),
        "parent_id": None,
        "kind": kind,
        "name": name,
        "summary": None,
        "phase": None,
        "status": "success",
        "ts_start": ts_start,
        "ts_end": ts_start,
        "duration_ms": 1000,
        "model": None,
        "human_count": 0,
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "children": [],
    }
    base.update(over)
    return base


# --- direct unit tests on the generalized helper -------------------------------


def test_identical_todo_leaf_siblings_collapse_into_one_group() -> None:
    tree = _load_tree()
    siblings = [
        _leaf("todo", "TaskCreate", "2026-06-12T12:01:05Z", span_id=f"t{i}") for i in range(3)
    ]

    result = tree._collapse_leaves(siblings)

    assert len(result) == 1
    group = result[0]
    assert group["kind"] == "todo"
    assert group["collapsed"] is True
    assert group["collapsed_count"] == 3
    assert {m["span_id"] for m in group["children"]} == {"t0", "t1", "t2"}


def test_identical_agent_leaf_siblings_collapse_parallel_fan_out() -> None:
    tree = _load_tree()
    siblings = [_leaf("agent", "Plan", "2026-06-12T23:02:05Z", span_id=f"a{i}") for i in range(2)]

    result = tree._collapse_leaves(siblings)

    assert len(result) == 1
    assert result[0]["kind"] == "agent"
    assert result[0]["collapsed_count"] == 2


def test_turns_are_never_collapsed_even_at_the_same_timestamp() -> None:
    # ``turn`` is excluded from the collapse: same-timestamp turns are distinct
    # cost-bearing inferences the tree keeps separate (the per-turn cost/composition
    # panel + the same-ts-turn guards), so even identical-key turns stay expanded.
    tree = _load_tree()
    turns = [
        _leaf("turn", "turn", "2026-06-12T12:00:05Z", model="claude-opus-4-8", span_id=f"tn{i}")
        for i in range(3)
    ]

    result = tree._collapse_leaves(turns)

    assert len(result) == 3
    assert all("collapsed_count" not in n for n in result)


def test_lone_leaf_is_left_untouched() -> None:
    tree = _load_tree()
    result = tree._collapse_leaves([_leaf("todo", "TodoWrite", "2026-06-12T12:00:30Z")])

    assert len(result) == 1
    assert "collapsed_count" not in result[0]


def test_a_leaf_with_children_does_not_collapse() -> None:
    # Only *leaf* siblings collapse; an agent that owns sub-work stays expanded even
    # when an identical bare agent sits beside it (one leaf is not a wide group).
    tree = _load_tree()
    parent = _leaf("agent", "Plan", "2026-06-12T23:02:05Z", span_id="busy")
    parent["children"] = [_leaf("tool", "Read", "2026-06-12T23:02:06Z")]
    bare = _leaf("agent", "Plan", "2026-06-12T23:02:05Z", span_id="bare")

    result = tree._collapse_leaves([parent, bare])

    assert {n["span_id"] for n in result} == {"busy", "bare"}
    assert all("collapsed_count" not in n for n in result)


def test_collapsed_group_status_comes_from_shared_rollup_helper() -> None:
    # The group's status must equal the tree's single status helper over its members
    # — never a hardcoded worst-child — so #57's terminal/last-event-wins swap of
    # that helper carries the collapsed group along with it.
    tree = _load_tree()
    members = [
        _leaf("todo", "TaskCreate", "2026-06-12T12:01:05Z", span_id="ok", status="success"),
        _leaf("todo", "TaskCreate", "2026-06-12T12:01:05Z", span_id="bad", status="deny"),
    ]

    group = tree._collapse_leaves(members)[0]

    assert group["status"] == tree._worst_status(members)


def test_groups_differing_only_by_summary_get_distinct_span_ids() -> None:
    # Two groups that share kind/name/ts/model but differ by summary are distinct
    # (summary is part of the collapse key), so their derived span_ids must not
    # collide — the span_id is derived from the *full* key, not a subset.
    tree = _load_tree()
    siblings = [
        _leaf("agent", "Plan", "2026-06-12T23:02:05Z", summary="alpha", span_id="a0"),
        _leaf("agent", "Plan", "2026-06-12T23:02:05Z", summary="alpha", span_id="a1"),
        _leaf("agent", "Plan", "2026-06-12T23:02:05Z", summary="beta", span_id="b0"),
        _leaf("agent", "Plan", "2026-06-12T23:02:05Z", summary="beta", span_id="b1"),
    ]

    result = tree._collapse_leaves(siblings)

    groups = [n for n in result if n.get("collapsed_count")]
    assert len(groups) == 2
    assert len({g["span_id"] for g in groups}) == 2


def test_collapse_recurses_into_nested_sibling_lists() -> None:
    tree = _load_tree()
    inner = [_leaf("todo", "TaskCreate", "2026-06-12T12:01:05Z", span_id=f"t{i}") for i in range(3)]
    turn = _leaf("turn", "turn", "2026-06-12T12:01:05Z", model="claude-opus-4-8", span_id="turn")
    turn["children"] = inner

    result = tree._collapse_leaves([turn])

    assert len(result) == 1 and result[0]["span_id"] == "turn"
    assert len(result[0]["children"]) == 1
    assert result[0]["children"][0]["collapsed_count"] == 3


# --- end-to-end through the built forest ---------------------------------------


def test_v2_identical_taskcreate_spans_collapse_in_built_forest() -> None:
    # The motivating bug (surfaced reviewing #53): the v2 fixture's three identical
    # ``todo · TaskCreate`` spans at 12:01:05 must render as one ``todo x3`` group,
    # not three separate rows.
    forest = store_v2().spoke_steps(V2_RUN_ID)

    todo_groups = [n for n in _walk(forest) if n["kind"] == "todo" and n.get("collapsed_count")]

    assert len(todo_groups) == 1
    group = todo_groups[0]
    assert group["collapsed_count"] == 3
    assert {m["span_id"] for m in group["children"]} == {"v2_task1", "v2_task2", "v2_task3"}


def test_v2_collapse_omits_no_member_and_leaves_lone_todo_alone() -> None:
    forest = store_v2().spoke_steps(V2_RUN_ID)

    seen = {n.get("span_id") for n in _walk(forest)}

    # Collapse never omits — every collapsed TaskCreate member is still reachable.
    assert {"v2_task1", "v2_task2", "v2_task3"} <= seen
    # The lone ``TodoWrite`` (distinct name + timestamp) never collapses.
    lone = next(n for n in _walk(forest) if n.get("span_id") == "v2_todo")
    assert "collapsed_count" not in lone
