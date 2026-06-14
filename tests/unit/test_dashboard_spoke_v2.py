"""v2 Spoke-view query tests (Issue #35).

The v2 spoke view fixes the v1 flat dump: real spans carry ``parent_id: null``,
so the tree is reconstructed by *time-bracketing* (smallest-enclosing window).
``spoke_steps`` returns the Level-1 spine (lifecycle/step) with sub-steps and raw
spans nested by their windows, hooks collapsed into one line, and metrics rolled
up. These tests pin the structural side (S2); cost/model attribution (S3) and
meta-by-kind (S4) are tested separately.
"""

from __future__ import annotations

from _dashboard_helpers import store_v2

RUN = "feature/v2+1000"


def _find(nodes, span_id):
    """Depth-first lookup by span_id within a forest (raises if absent)."""
    for node in nodes:
        if node["span_id"] == span_id:
            return node
        try:
            return _find(node["children"], span_id)
        except KeyError:
            continue
    raise KeyError(span_id)


def _kinds(nodes):
    return sorted(n["kind"] for n in nodes)


def test_level1_is_the_spine_in_time_order():
    forest = store_v2().spoke_steps(RUN)

    # Roots are the lifecycle/step spine, ordered by ts_start — no flat dump,
    # no hook or pull span leaking to the top level.
    assert [n["span_id"] for n in forest] == [
        "v2_life_new",
        "v2_red",
        "v2_green",
        "v2_life_done",
    ]


def test_substeps_nest_under_their_step_by_time():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")

    # red brackets a skill, a todo, and two hooks (collapsed) — by window only.
    assert _kinds(red["children"]) == ["hooks", "skill", "todo"]


def test_hooks_collapse_into_one_expandable_node():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    hooks = next(c for c in red["children"] if c["kind"] == "hooks")

    assert hooks["collapsed_count"] == 2
    assert hooks["duration_ms"] == 180  # 100 + 80, summed
    # The raw hook spans remain reachable for expansion.
    assert {c["span_id"] for c in hooks["children"]} == {"v2_red_hook1", "v2_red_hook2"}


def test_collapsed_hooks_surface_worst_status():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    hooks = next(c for c in red["children"] if c["kind"] == "hooks")

    # One hook warned — the collapsed line must not hide it behind "success".
    assert hooks["status"] == "warn"


def test_third_level_nests_agent_under_skill():
    forest = store_v2().spoke_steps(RUN)
    skill = _find(forest, "v2_skill")

    # The agent ran inside the skill's window → depth-3 drill-down.
    assert [c["span_id"] for c in skill["children"]] == ["v2_agent"]


def test_identical_window_siblings_do_not_nest():
    forest = store_v2().spoke_steps(RUN)
    green = _find(forest, "v2_green")

    # Three TaskCreate spans share one window; none parents another, no hooks.
    todos = [c for c in green["children"] if c["kind"] == "todo"]
    assert {c["span_id"] for c in todos} == {"v2_task1", "v2_task2", "v2_task3"}
    assert all(not c["children"] for c in todos)


def test_step_shows_own_wallclock_duration_not_a_rolled_sum():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")

    # Duration is the step's own wall-clock, never a sum of overlapping children.
    assert red["duration_ms"] == 50000


def test_human_count_rolls_up_without_double_counting():
    forest = store_v2().spoke_steps(RUN)
    green = _find(forest, "v2_green")

    # One human interaction (the approval) under green, counted once.
    assert green["rollup"]["human_count"] == 1
    assert _find(forest, "v2_red")["rollup"]["human_count"] == 0


def test_unknown_spoke_returns_empty_forest():
    assert store_v2().spoke_steps("does/not+exist") == []
