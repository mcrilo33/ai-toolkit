"""Unit tests for the numeric-score emission (:mod:`telemetry.spoke_tree.scores`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.scores import (
    _step_phase,
    _step_phase_of,
    build_score_events,
    build_script_success_scores,
    build_step_cost_scores,
    build_step_duration_scores,
    build_step_total_cost_scores,
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


class TestBuildScriptSuccessScores:
    """#233: mirror each control-script span's status into a script_success:<name> 0/1 score."""

    def _script(self, obs_id: str, name: str, status: str, phase: str | None = None) -> dict:
        attributes: dict[str, str] = {"workflow.kind": "script", "status": status}
        if phase is not None:
            attributes["workflow.phase"] = phase
        return {"body": {"id": obs_id, "name": name, "metadata": {"attributes": attributes}}}

    def test_success_status_scores_one_per_script_node(self) -> None:
        batch = [self._script("s1", "spoke-push", "success")]

        events = build_script_success_scores(SPOKE, batch, base_ts="t")

        assert len(events) == 1
        body = events[0]["body"]
        assert events[0]["type"] == "score-create"
        assert body["name"] == "script_success:spoke-push"
        assert body["value"] == 1.0
        assert body["observationId"] == "s1"

    def test_failure_status_scores_zero(self) -> None:
        batch = [self._script("s1", "spoke-push", "failure")]

        events = build_script_success_scores(SPOKE, batch, base_ts="t")

        assert events[0]["body"]["value"] == 0.0

    def test_phased_script_name_strips_the_kind_prefix(self) -> None:
        batch = [self._script("g", "script:gate", "success", phase="gate")]

        events = build_script_success_scores(SPOKE, batch, base_ts="t")

        assert events[0]["body"]["name"] == "script_success:gate"

    def test_non_script_nodes_are_ignored(self) -> None:
        batch = [{"body": {"id": "t1", "name": "tool:Bash", "metadata": {}}}]

        assert build_script_success_scores(SPOKE, batch, base_ts="t") == []

    def test_repeat_invocations_keep_distinct_observation_scores(self) -> None:
        batch = [
            self._script("s1", "spoke-push", "success"),
            self._script("s2", "spoke-push", "failure"),
        ]

        events = build_script_success_scores(SPOKE, batch, base_ts="t")

        assert len(events) == 2
        assert {e["body"]["observationId"] for e in events} == {"s1", "s2"}


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
        assert by_name["step_cache_write_usd:RED"] == 1.0
        assert by_name["step_tokens_written:RED"] == 1000

    def test_step_without_rollup_is_skipped(self) -> None:
        cycle_batch = [{"body": {"id": "cycstep-abc", "name": "preStep", "metadata": {}}}]
        assert build_step_cost_scores(SPOKE, cycle_batch, base_ts="t", price=0.001) == []


class TestBuildStepTotalCostScores:
    """#230: sum each generation's costDetails into its nearest cycle-step ancestor."""

    def _green_step(self) -> dict:
        return {
            "type": "span-create",
            "body": {
                "id": "cycstep-green",
                "name": "step:GREEN x",
                "metadata": {"subject": "GREEN x"},
            },
        }

    def test_generation_cost_attributed_to_nearest_step_ancestor(self) -> None:
        # A sub-agent generation nested under sub-agent:code-review under the GREEN step, plus a
        # main-loop generation directly under the step. Both roll into GREEN.
        cycle_batch = [
            self._green_step(),
            {
                "type": "span-create",
                "body": {"id": "sac", "parentObservationId": "cycstep-green"},
            },
            {
                "type": "generation-create",
                "body": {"id": "sag", "parentObservationId": "sac", "costDetails": {"total": 2.0}},
            },
            {
                "type": "generation-create",
                "body": {
                    "id": "mg",
                    "parentObservationId": "cycstep-green",
                    "costDetails": {"input": 0.3, "output": 0.7},
                },
            },
        ]

        events = build_step_total_cost_scores(SPOKE, cycle_batch, base_ts="t")

        score = next(e for e in events if e["body"]["name"] == "step_total_cost_usd:GREEN")
        assert score["body"]["value"] == pytest.approx(3.0)
        assert score["body"]["observationId"] == "cycstep-green"

    def test_explicit_total_wins_over_component_sum(self) -> None:
        # costDetails carrying BOTH components and a reserved total must not double-count.
        cycle_batch = [
            self._green_step(),
            {
                "type": "generation-create",
                "body": {
                    "id": "mg",
                    "parentObservationId": "cycstep-green",
                    "costDetails": {"input": 0.3, "output": 0.7, "total": 1.0},
                },
            },
        ]

        events = build_step_total_cost_scores(SPOKE, cycle_batch, base_ts="t")

        score = next(e for e in events if e["body"]["name"] == "step_total_cost_usd:GREEN")
        assert score["body"]["value"] == pytest.approx(1.0)

    def test_no_generations_no_scores(self) -> None:
        assert build_step_total_cost_scores(SPOKE, [self._green_step()], base_ts="t") == []


class TestBuildStepDurationScores:
    """#230: per-phase step latency as a numeric score from the cycle-step window length."""

    def test_emits_window_length_in_ms_per_phase(self) -> None:
        cycle_batch = [
            {
                "type": "span-create",
                "body": {
                    "id": "cycstep-green",
                    "name": "step:GREEN x",
                    "startTime": "2026-01-02T00:00:10Z",
                    "endTime": "2026-01-02T00:00:25Z",
                    "metadata": {"subject": "GREEN x"},
                },
            }
        ]

        events = build_step_duration_scores(SPOKE, cycle_batch, base_ts="t")

        score = next(e for e in events if e["body"]["name"] == "step_duration_ms:GREEN")
        assert score["body"]["value"] == 15000
        assert score["body"]["observationId"] == "cycstep-green"

    def test_non_step_nodes_are_ignored(self) -> None:
        cycle_batch = [
            {
                "type": "generation-create",
                "body": {
                    "id": "gen",
                    "startTime": "2026-01-02T00:00:10Z",
                    "endTime": "2026-01-02T00:00:25Z",
                },
            }
        ]
        assert build_step_duration_scores(SPOKE, cycle_batch, base_ts="t") == []
