"""Aggregate-view query tests (Issue #23, subtask 2 — RED).

The aggregate view rolls the step tree up across all spokes in a time window:
per (kind, name, phase) group it reports frequency (invocation count), totals,
and per-invocation mean/median for time, cost, tokens, and human interactions.
These tests pin that rollup against the shared JSONL fixture.
"""

from __future__ import annotations

from _dashboard_helpers import store


def _group(rows, kind, name, phase):
    for row in rows:
        if (row["kind"], row["name"], row["phase"]) == (kind, name, phase):
            return row
    raise KeyError((kind, name, phase))


def test_green_step_rollup_mean_median_and_frequency():
    rows = store().aggregate()
    green = _group(rows, "step", "solo-cycle", "green")

    # durations across 4 spokes: 1000, 2000, 600, 800
    assert green["invocations"] == 4
    assert green["frequency"] == 4
    assert green["total_duration_ms"] == 4400
    assert green["mean_duration_ms"] == 1100
    assert green["median_duration_ms"] == 900


def test_green_step_cost_and_human_normalized_per_invocation():
    green = _group(store().aggregate(), "step", "solo-cycle", "green")

    # costs: 0.10 + 0.30 + 0.05 + 0.07 = 0.52
    assert round(green["total_cost_usd"], 2) == 0.52
    assert round(green["mean_cost_usd"], 3) == 0.13
    # humans on green: sa, sb, sd (sc has none) -> 3 over 4 invocations
    assert green["human_count"] == 3
    assert round(green["human_per_invocation"], 2) == 0.75


def test_red_step_rollup_is_uniform():
    red = _group(store().aggregate(), "step", "solo-cycle", "red")

    assert red["invocations"] == 4
    assert red["mean_duration_ms"] == 500
    assert red["median_duration_ms"] == 500


def test_window_filters_by_ts_start():
    # Spokes A and B run on 2026-06-10; C and D on 2026-06-11.
    rows = store().aggregate(window_start="2026-06-11T00:00:00Z")
    green = _group(rows, "step", "solo-cycle", "green")

    # only sc_green (600) and sd_green (800) fall in the window
    assert green["invocations"] == 2
    assert green["mean_duration_ms"] == 700
    assert green["median_duration_ms"] == 700


def test_window_upper_bound_is_exclusive_of_later_runs():
    rows = store().aggregate(window_end="2026-06-11T00:00:00Z")
    green = _group(rows, "step", "solo-cycle", "green")

    # only the 2026-06-10 runs: sa_green (1000), sb_green (2000)
    assert green["invocations"] == 2
    assert green["total_duration_ms"] == 3000


def test_rows_sorted_by_total_duration_desc():
    rows = store().aggregate()

    totals = [row["total_duration_ms"] for row in rows]
    assert totals == sorted(totals, reverse=True)
    # green is the single largest sink (4400ms total)
    assert (rows[0]["kind"], rows[0]["name"], rows[0]["phase"]) == (
        "step",
        "solo-cycle",
        "green",
    )


def test_empty_window_yields_no_groups():
    assert store().aggregate(window_start="2030-01-01T00:00:00Z") == []
