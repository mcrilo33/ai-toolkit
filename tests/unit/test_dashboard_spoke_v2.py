"""v2 Spoke-view query tests (Issue #35).

The v2 spoke view fixes the v1 flat dump: real spans carry ``parent_id: null``,
so the tree is reconstructed by *time-bracketing* (smallest-enclosing window).
``spoke_steps`` returns the Level-1 spine (lifecycle/step) with sub-steps and raw
spans nested by their windows, hooks collapsed into one line, and metrics rolled
up. These tests pin the structural side (S2); cost/model attribution (S3) and
meta-by-kind (S4) are tested separately.
"""

from __future__ import annotations

import pytest
from _dashboard_helpers import FIXTURE_V2_SPANS, load_queries, store_v2

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
    # no hook or pull span leaking to the top level. (A synthetic untracked-turns
    # node may also be a root; the real-span spine is what's pinned here.)
    spine = [n["span_id"] for n in forest if n["span_id"] is not None]
    assert spine == ["v2_life_new", "v2_red", "v2_green", "v2_life_done"]


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


# --- S3: once-per-turn cost + model/agent attribution --------------------------


def _roots(forest, kind):
    return [n for n in forest if n["kind"] == kind]


def test_subagent_turns_attribute_to_the_agent_node():
    forest = store_v2().spoke_steps(RUN)
    agent = _find(forest, "v2_agent")

    # The two subagent turns (haiku) land on the agent node, not the main spine.
    assert agent["own_cost_usd"] == pytest.approx(0.35)
    assert agent["own_tokens_in"] == 350
    assert agent["own_tokens_out"] == 180
    assert agent["models"] == ["claude-haiku-4-5"]
    assert agent["agent"] == "subagent"


def test_main_turn_attributes_to_deepest_non_agent_span():
    forest = store_v2().spoke_steps(RUN)
    skill = _find(forest, "v2_skill")

    # The 12:00:10 main turn falls inside the skill window → owned by the skill,
    # never by the agent nested within it.
    assert skill["own_cost_usd"] == pytest.approx(0.05)
    assert skill["models"] == ["claude-opus-4-8"]
    assert skill["agent"] == "main"


def test_identical_window_turn_counted_once_not_per_sibling():
    forest = store_v2().spoke_steps(RUN)
    green = _find(forest, "v2_green")
    tasks = [c for c in green["children"] if c["kind"] == "todo"]

    # One turn brackets three identical-window TaskCreate spans; its cost lands on
    # exactly one of them — never replicated across all three.
    own = [t["own_cost_usd"] for t in tasks]
    assert sum(own) == pytest.approx(0.03)
    assert sum(1 for c in own if c > 0) == 1


def test_step_rollup_sums_owned_turns_without_double_count():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    green = _find(forest, "v2_green")

    # red: own 0.02 + skill 0.05 + agent 0.35 + todo 0.01 + hooks 0 = 0.43
    assert red["rollup"]["cost_usd"] == pytest.approx(0.43)
    # green: tasks 0.03 (once) + ask 0.02 = 0.05  (not 0.09)
    assert green["rollup"]["cost_usd"] == pytest.approx(0.05)


def test_rollup_models_bubble_up_distinct_and_sorted():
    forest = store_v2().spoke_steps(RUN)
    red = _find(forest, "v2_red")
    green = _find(forest, "v2_green")

    assert red["rollup"]["models"] == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert green["rollup"]["models"] == ["claude-opus-4-8", "claude-sonnet-4-6"]


def test_orphan_turns_surface_in_an_untracked_node():
    forest = store_v2().spoke_steps(RUN)
    untracked = _roots(forest, "untracked")

    assert len(untracked) == 1
    assert untracked[0]["own_cost_usd"] == pytest.approx(0.005)


def test_run_total_reconciles_to_the_turn_costs():
    forest = store_v2().spoke_steps(RUN)

    # Every turn counted exactly once → the forest's rolled-up cost equals the
    # sum of all turn costs (0.495), the trustworthy run total.
    total = sum(root["rollup"]["cost_usd"] for root in forest)
    assert total == pytest.approx(0.495)


def test_raw_path_without_turns_has_zero_owned_cost():
    queries = load_queries()
    store = queries.SpanStore.from_jsonl(FIXTURE_V2_SPANS)  # no turns table data
    red = _find(store.spoke_steps(RUN), "v2_red")

    assert red["rollup"]["cost_usd"] == 0.0
    assert red["own_cost_usd"] == 0.0
