"""Unit tests for the numeric-score emission (:mod:`telemetry.spoke_tree.scores`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.scores import (
    _step_phase,
    _step_phase_of,
    build_score_events,
    build_step_cost_scores,
)

SPOKE = "feature/22-demo+1700000000"


class TestBuildScoreEvents:
    def test_permission_wait_and_tool_result_size_become_scores(self) -> None:
        batch = [
            {
                "body": {
                    "id": "t1",
                    "metadata": {"blocked_on_user_ms": 400, "tool_result_size": 1200},
                }
            }
        ]
        events = build_score_events(SPOKE, [("tr", [])], batch, base_ts="2026-01-02T00:00:00Z")
        names = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert names == {"permission_wait_ms": 400, "tool_result_size": 1200}
        assert all(e["type"] == "score-create" for e in events)

    def test_no_signals_no_scores(self) -> None:
        batch = [{"body": {"id": "t1", "metadata": {}}}]
        assert build_score_events(SPOKE, [("tr", [])], batch, base_ts="x") == []


class TestStepPhase:
    def test_parses_leftmost_known_phase(self) -> None:
        assert _step_phase("A-RED: red first") == "RED"
        assert _step_phase("REVIEW + PUSH") == "REVIEW"

    def test_unknown_subject_is_other(self) -> None:
        assert _step_phase("misc cleanup") == "other"

    def test_step_phase_of_maps_boundary_partitions(self) -> None:
        assert _step_phase_of({"name": "preStep"}) == "pre"
        assert _step_phase_of({"name": "postStep"}) == "post"
        assert _step_phase_of({"name": "step:x", "metadata": {"subject": "GREEN it"}}) == "GREEN"


class TestBuildStepCostScores:
    def test_emits_cost_and_tokens_per_phase(self) -> None:
        cycle_batch = [
            {
                "body": {
                    "id": "cycstep-abc",
                    "name": "step:RED x",
                    "metadata": {"subject": "RED x", "rollup": {"written": 1000}},
                }
            }
        ]
        events = build_step_cost_scores(SPOKE, cycle_batch, base_ts="t", price=0.001)
        by_name = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert by_name["step_cost_usd:RED"] == 1.0
        assert by_name["step_tokens_written:RED"] == 1000

    def test_step_without_rollup_is_skipped(self) -> None:
        cycle_batch = [{"body": {"id": "cycstep-abc", "name": "preStep", "metadata": {}}}]
        assert build_step_cost_scores(SPOKE, cycle_batch, base_ts="t", price=0.001) == []
