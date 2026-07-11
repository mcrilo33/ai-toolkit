"""Unit tests for the numeric-score emission (:mod:`telemetry.spoke_tree.scores`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.scores import (
    _step_phase,
    _step_phase_of,
    build_agent_verdict_scores,
    build_score_events,
    build_script_success_scores,
    build_skill_success_scores,
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

    def test_gate_park_span_is_excluded(self) -> None:
        # script:gate is a PLAN-gate human-WAIT node with an always-success status, not a real
        # control script — scoring it would add a bogus 100%-successful "gate" series.
        batch = [self._script("g", "script:gate", "success", phase="gate")]

        assert build_script_success_scores(SPOKE, batch, base_ts="t") == []

    def test_phased_non_gate_script_name_strips_the_kind_prefix(self) -> None:
        batch = [self._script("s", "script:teardown", "success", phase="teardown")]

        events = build_script_success_scores(SPOKE, batch, base_ts="t")

        assert events[0]["body"]["name"] == "script_success:teardown"

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


class TestBuildSkillSuccessScores:
    """#234: mirror a skill span's SCRIPTED exit-status into a skill_success:<name> 0/1 score.

    Ready-but-latent (like #233 script_success): a score is emitted ONLY when a skill carries a
    scripted status attribute — a skill with no scripted status is not scored 0 (absence is not a
    failure), so no skill self-reports success and the widget stays empty until the SKILL.md/hook
    contract stamps a status.
    """

    def _skill(self, obs_id: str, name: str, status: str | None) -> dict:
        attributes: dict[str, str] = {}
        if status is not None:
            attributes["skill.status"] = status
        return {"body": {"id": obs_id, "name": name, "metadata": {"attributes": attributes}}}

    def test_scripted_success_scores_one(self) -> None:
        batch = [self._skill("s1", "skill:code-review", "success")]

        events = build_skill_success_scores(SPOKE, batch, base_ts="t")

        assert len(events) == 1
        body = events[0]["body"]
        assert events[0]["type"] == "score-create"
        assert body["name"] == "skill_success:code-review"
        assert body["value"] == 1.0
        assert body["observationId"] == "s1"

    def test_scripted_failure_scores_zero(self) -> None:
        batch = [self._skill("s1", "skill:code-review", "failure")]

        assert build_skill_success_scores(SPOKE, batch, base_ts="t")[0]["body"]["value"] == 0.0

    def test_skill_without_scripted_status_emits_nothing(self) -> None:
        batch = [self._skill("s1", "skill:code-review", None)]

        assert build_skill_success_scores(SPOKE, batch, base_ts="t") == []

    def test_non_skill_nodes_are_ignored(self) -> None:
        batch = [{"body": {"id": "t1", "name": "tool:Skill", "metadata": {}}}]

        assert build_skill_success_scores(SPOKE, batch, base_ts="t") == []


class TestBuildAgentVerdictScores:
    """#233: agent_verdict:<type> — code-review from the .review artifact, sub-agents from
    their returned status, reaper-killed sub-agents score a died class."""

    def _write_review(self, review_dir: Path, stem: str, verdict: str) -> None:
        review_dir.mkdir(exist_ok=True)
        (review_dir / f"{stem}.json").write_text(
            json.dumps({"verdict": verdict, "reviewer": "code-review"}) + "\n"
        )

    def test_code_review_approve_artifact_scores_one(self, tmp_path: Path) -> None:
        review = tmp_path / ".review"
        self._write_review(review, "abc123", "APPROVE")

        events = build_agent_verdict_scores(SPOKE, [], review, base_ts="t")

        assert len(events) == 1
        body = events[0]["body"]
        assert events[0]["type"] == "score-create"
        assert body["name"] == "agent_verdict:code-review"
        assert body["value"] == 1.0
        assert "observationId" not in body  # trace-level (the artifact is not a span)

    def test_code_review_request_changes_scores_zero(self, tmp_path: Path) -> None:
        review = tmp_path / ".review"
        self._write_review(review, "abc123", "REQUEST_CHANGES")

        events = build_agent_verdict_scores(SPOKE, [], review, base_ts="t")

        assert events[0]["body"]["value"] == 0.0

    def test_multiple_review_artifacts_each_score_distinctly(self, tmp_path: Path) -> None:
        review = tmp_path / ".review"
        self._write_review(review, "hashone", "APPROVE")
        self._write_review(review, "hashtwo", "REQUEST_CHANGES")

        events = build_agent_verdict_scores(SPOKE, [], review, base_ts="t")

        assert len(events) == 2
        assert all(e["body"]["name"] == "agent_verdict:code-review" for e in events)
        assert len({e["body"]["id"] for e in events}) == 2  # distinct ids so both survive ingest
        assert sorted(e["body"]["value"] for e in events) == [0.0, 1.0]

    def test_missing_review_dir_no_code_review_score(self, tmp_path: Path) -> None:
        assert build_agent_verdict_scores(SPOKE, [], tmp_path / "absent", base_ts="t") == []

    def test_non_object_review_artifact_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # A valid-but-non-object artifact (a JSON array / scalar / null) must be skipped, not raise
        # AttributeError and abort the whole land-time build.
        review = tmp_path / ".review"
        review.mkdir()
        (review / "arr.json").write_text("[1, 2, 3]\n")
        (review / "null.json").write_text("null\n")
        (review / "ok.json").write_text(json.dumps({"verdict": "APPROVE"}) + "\n")

        events = build_agent_verdict_scores(SPOKE, [], review, base_ts="t")

        assert [e["body"]["value"] for e in events] == [1.0]  # only the object artifact scores

    def test_sub_agent_error_level_scores_a_separate_died_flag(self, tmp_path: Path) -> None:
        batch = [{"body": {"id": "sa", "name": "sub-agent:bug-scoper", "level": "ERROR"}}]

        events = build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t")

        assert len(events) == 1
        body = events[0]["body"]
        assert body["name"] == "agent_verdict:bug-scoper:died"  # separate name, off the 0/1 rate
        assert body["value"] == 1.0
        assert body["observationId"] == "sa"

    def test_sub_agent_status_output_scores_verdict(self, tmp_path: Path) -> None:
        batch = [
            {"body": {"id": "p1", "name": "sub-agent:planner", "output": {"status": "completed"}}},
            {"body": {"id": "p2", "name": "sub-agent:planner", "output": {"status": "failed"}}},
        ]

        events = build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t")

        by_obs = {e["body"]["observationId"]: e["body"]["value"] for e in events}
        assert by_obs == {"p1": 1.0, "p2": 0.0}

    def test_sub_agent_status_from_json_string_output(self, tmp_path: Path) -> None:
        # A structured return grafted as a raw JSON STRING (the common tool_result shape) is decoded.
        batch = [
            {"body": {"id": "p1", "name": "sub-agent:planner", "output": '{"status": "success"}'}}
        ]

        events = build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t")

        assert events[0]["body"]["value"] == 1.0

    def test_killed_code_review_scores_died_but_not_a_colliding_verdict(
        self, tmp_path: Path
    ) -> None:
        # A killed code-review still scores a died COUNT (distinct :died name, no collision), but
        # never an agent_verdict:code-review 0/1 score — that verdict is owned by the .review artifacts.
        batch = [{"body": {"id": "cr", "name": "sub-agent:code-review", "level": "ERROR"}}]

        events = build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t")

        assert len(events) == 1
        assert events[0]["body"]["name"] == "agent_verdict:code-review:died"
        assert events[0]["body"]["value"] == 1.0

    def test_code_review_container_verdict_is_not_scored_from_output(self, tmp_path: Path) -> None:
        # A (non-killed) code-review container's status output must NOT mint a verdict — the .review
        # artifact is authoritative, so scoring the container too would double-count.
        batch = [
            {"body": {"id": "cr", "name": "sub-agent:code-review", "output": {"status": "ok"}}}
        ]

        assert build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t") == []

    def test_sub_agent_llm_calls_are_ignored(self, tmp_path: Path) -> None:
        batch = [{"body": {"id": "g", "name": "sub-agent:llm", "level": "ERROR"}}]

        assert build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t") == []

    def test_sub_agent_with_no_signal_scores_nothing(self, tmp_path: Path) -> None:
        batch = [{"body": {"id": "sa", "name": "sub-agent:Explore", "output": "some prose"}}]

        assert build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t") == []


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
