"""Unit tests for the View B cycle-axis lens (:mod:`telemetry.spoke_tree.cycle`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.cycle import (
    _POST_STEP_KEY,
    _PRE_STEP_KEY,
    _cycle_step_for,
    _cycle_step_ids,
)
from telemetry.spoke_tree.steps import StepWindow

_W1 = StepWindow("1", "RED", "2026-01-02T00:00:02Z", "2026-01-02T00:00:04Z", "completed")
_W2 = StepWindow("2", "GREEN", "2026-01-02T00:00:06Z", "2026-01-02T00:00:08Z", "completed")
_WINDOWS = [_W1, _W2]


class TestCycleStepFor:
    def test_before_first_window_is_pre(self) -> None:
        assert _cycle_step_for("2026-01-02T00:00:00Z", _WINDOWS) == _PRE_STEP_KEY

    def test_after_last_completed_is_post(self) -> None:
        assert _cycle_step_for("2026-01-02T00:00:09Z", _WINDOWS) == _POST_STEP_KEY

    def test_inside_a_window_is_its_task(self) -> None:
        assert _cycle_step_for("2026-01-02T00:00:03Z", _WINDOWS) == "1"

    def test_inter_step_gap_attaches_to_preceding(self) -> None:
        assert _cycle_step_for("2026-01-02T00:00:05Z", _WINDOWS) == "1"


class TestCycleStepIds:
    def test_maps_every_axis_key_to_a_distinct_id(self) -> None:
        ids = _cycle_step_ids("sp", _WINDOWS)
        assert set(ids) == {_PRE_STEP_KEY, _POST_STEP_KEY, "1", "2"}
        assert len(set(ids.values())) == 4

    def test_ids_are_deterministic(self) -> None:
        assert _cycle_step_ids("sp", _WINDOWS) == _cycle_step_ids("sp", _WINDOWS)
