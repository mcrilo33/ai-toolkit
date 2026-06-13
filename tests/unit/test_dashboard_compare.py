"""A/B compare-view query tests (Issue #23, subtask 3 — RED).

The A/B view is the decision tool: pick two ``workflow_rev``s and read the
per-step delta on time, cost, and human interaction — each normalized PER
INVOCATION so differing spoke counts between revs don't skew the comparison.
Because small spoke counts are noisy, every row carries its sample sizes
(``n_a``/``n_b``) and a ``low_confidence`` flag. These tests pin that against
the fixture, where rev ``old1234`` ran spokes A+B and rev ``new5678`` ran C+D.
"""

from __future__ import annotations

from _dashboard_helpers import store

REV_A = "old1234"
REV_B = "new5678"


def _group(rows, kind, name, phase):
    for row in rows:
        if (row["kind"], row["name"], row["phase"]) == (kind, name, phase):
            return row
    raise KeyError((kind, name, phase))


def test_workflow_revs_are_unique_and_sorted():
    assert store().workflow_revs() == ["new5678", "old1234"]


def test_green_delta_is_normalized_per_invocation():
    rows = store().ab_compare(REV_A, REV_B)
    green = _group(rows, "step", "solo-cycle", "green")

    # rev A green: 1000, 2000 -> mean 1500 ; rev B green: 600, 800 -> mean 700
    assert green["n_a"] == 2
    assert green["n_b"] == 2
    assert green["mean_duration_a"] == 1500
    assert green["mean_duration_b"] == 700
    assert green["delta_duration_ms"] == -800
    # cost per invocation: A mean 0.20, B mean 0.06 -> delta -0.14
    assert round(green["delta_cost_usd"], 2) == -0.14
    # human per invocation: A 1.0 (both have approval), B 0.5 (only sd) -> -0.5
    assert round(green["delta_human_per_invocation"], 2) == -0.5


def test_improvement_shows_as_negative_delta():
    green = _group(store().ab_compare(REV_A, REV_B), "step", "solo-cycle", "green")

    # green got faster and cheaper from A to B
    assert green["delta_duration_ms"] < 0
    assert green["delta_cost_usd"] < 0


def test_low_confidence_flag_respects_threshold():
    # default threshold flags everything (max n in the fixture is 2)
    green_default = _group(store().ab_compare(REV_A, REV_B), "step", "solo-cycle", "green")
    assert green_default["low_confidence"] is True

    # with threshold 2, green (min n = 2) clears it but review (min n = 1) does not
    rows = store().ab_compare(REV_A, REV_B, low_confidence_n=2)
    green = _group(rows, "step", "solo-cycle", "green")
    review = _group(rows, "step", "solo-cycle", "review")
    assert green["low_confidence"] is False
    assert review["low_confidence"] is True


def test_group_present_in_only_one_rev_is_low_confidence():
    rows = store().ab_compare(REV_A, REV_B)
    # the lifecycle root exists only under rev A (spoke A); rev B has none
    lifecycle = _group(rows, "lifecycle", "worktree-new", "spawn")

    assert lifecycle["n_a"] == 1
    assert lifecycle["n_b"] == 0
    assert lifecycle["mean_duration_b"] == 0
    assert lifecycle["low_confidence"] is True


def test_rows_sorted_by_abs_delta_duration_desc():
    rows = store().ab_compare(REV_A, REV_B)

    abs_deltas = [abs(row["delta_duration_ms"]) for row in rows]
    assert abs_deltas == sorted(abs_deltas, reverse=True)


def test_same_rev_compared_to_itself_has_zero_deltas():
    rows = store().ab_compare(REV_A, REV_A)

    assert rows  # groups exist
    for row in rows:
        assert row["delta_duration_ms"] == 0
        assert row["delta_cost_usd"] == 0
