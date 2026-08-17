"""Unit tests for the numeric-score emission (:mod:`telemetry.spoke_tree.scores`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.ids import root_id_for
from telemetry.spoke_tree.observations import Lifecycle
from telemetry.spoke_tree.scores import (
    _step_phase,
    _step_phase_of,
    build_agent_cost_scores,
    build_agent_verdict_scores,
    build_lifecycle_stage_scores,
    build_mcp_call_scores,
    build_mcp_carry_cost_scores,
    build_mcp_def_load_scores,
    build_normalization_scores,
    build_score_events,
    build_script_success_scores,
    build_skill_cost_scores,
    build_skill_success_scores,
    build_step_cost_scores,
    build_step_duration_scores,
    build_step_total_cost_scores,
    build_window_rollup_scores,
)

SPOKE = "feature/22-demo+1700000000"
_BASE_TS = "2026-01-02T00:00:00Z"


def _span(name: str, start: str, end: str, **attrs: str) -> dict:
    obs: dict[str, object] = {"name": name, "startTime": start, "endTime": end}
    if attrs:
        obs["metadata"] = {"attributes": attrs}
    return obs


def _stage_traces() -> list:
    """Source traces carrying every stage-window span the #280 stage scores read."""
    # The real-duration review signal is the code-review Agent container (otelcol-renamed to
    # sub-agent:code-review); the broker's agent:review intervention spans are zero-duration.
    review = _span(
        "sub-agent:code-review", "2026-01-02T00:10:00Z", "2026-01-02T00:12:00Z"
    )  # 120_000 ms
    push = _span("spoke-push", "2026-01-02T00:20:00Z", "2026-01-02T00:22:00Z")  # 120_000 ms
    land = _span("worktree-land", "2026-01-02T00:30:00Z", "2026-01-02T00:33:00Z")  # 180_000 ms
    gate = _span("script:gate", "2026-01-02T00:59:00Z", "2026-01-02T01:00:00Z")  # onset = end
    resume = {
        "name": "llm_request",
        "type": "GENERATION",
        "startTime": "2026-01-02T02:00:00Z",
        "endTime": "2026-01-02T02:00:01Z",
    }
    return [("tr", [review, push, land, gate, resume])]


# dispatched 00:00:00Z; first commit 00:05:00Z -> spawn+seed = 300_000 ms.
_DISPATCHED = 1767312000  # 2026-01-02T00:00:00Z
_FIRST_COMMIT = [{"authored_at": "2026-01-02T00:05:00Z"}]
# gate onset 01:00:00Z; answer attempt 01:05:00Z -> gate answer = 300_000 ms.
_ANSWER_ATTEMPT = 1767315900  # 2026-01-02T01:05:00Z


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

    def _park_value(self, events: list) -> int | None:
        for event in events:
            if event["body"]["name"] == "gate_park_ms":
                return event["body"]["value"]
        return None

    def test_gate_park_ms_uses_resume_without_answer_epoch(self) -> None:
        # gate onset 01:00:00Z -> resume 02:00:00Z = 3_600_000 ms (the narrow first-activity window).
        events = build_score_events(SPOKE, _stage_traces(), [], base_ts=_BASE_TS)
        assert self._park_value(events) == 3_600_000

    def test_gate_park_ms_widens_to_answer_epoch(self) -> None:
        # #345: with the drain's answer epoch (01:05:00Z), the park is onset -> answer = 300_000 ms,
        # agreeing with stage_gate_answer_ms rather than the 3_600_000 ms first-activity window.
        events = build_score_events(
            SPOKE, _stage_traces(), [], base_ts=_BASE_TS, answer_epoch=_ANSWER_ATTEMPT
        )
        assert self._park_value(events) == 300_000

    def test_gate_park_ms_falls_back_when_answer_epoch_stale(self) -> None:
        # An epoch before the gate onset is a stale/unrelated attempt -> fall back to the resume.
        events = build_score_events(
            SPOKE, _stage_traces(), [], base_ts=_BASE_TS, answer_epoch=_DISPATCHED
        )
        assert self._park_value(events) == 3_600_000


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


class TestBuildSkillCostScores:
    """#322: sum a skill span's generation-descendant cost into a skill_cost_usd:<name> score.

    A ``skill:<name>`` span is a relabeled ``tool:Skill`` span whose own ``costDetails`` is $0 — the
    real spend lives in its generation descendants — so Langfuse's own-cost-summing metrics API
    returns $0 for a ``skill:`` filter. Each skill node therefore emits ``skill_cost_usd:<name>``, the
    summed ``costDetails`` of every generation in its subtree, observation-scoped to the skill node.
    A skill with no generation descendants is SKIPPED (never scored 0) — absence of spend is not a
    cost, mirroring the ready-but-latent ``skill_success`` idiom.
    """

    def _skill(self, obs_id: str, name: str, parent: str | None = None) -> dict:
        return {
            "type": "span-create",
            "body": {"id": obs_id, "name": name, "parentObservationId": parent},
        }

    def _gen(self, obs_id: str, parent: str, cost: dict[str, float]) -> dict:
        return {
            "type": "generation-create",
            "body": {"id": obs_id, "parentObservationId": parent, "costDetails": cost},
        }

    def test_two_skill_nodes_sum_generation_descendants(self) -> None:
        # skill:code-review has a generation nested one level down under a sub-agent container
        # (subtree depth > 1) plus a direct generation; skill:brainstorming has one generation.
        batch = [
            self._skill("sc", "skill:code-review"),
            {"type": "span-create", "body": {"id": "sac", "parentObservationId": "sc"}},
            self._gen("g_rev", "sac", {"total": 2.0}),
            self._gen("g_rev2", "sc", {"input": 0.3, "output": 0.7}),
            self._skill("sb", "skill:brainstorming"),
            self._gen("g_bs", "sb", {"total": 5.0}),
        ]

        events = build_skill_cost_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"] for e in events}
        assert set(by_name) == {"skill_cost_usd:code-review", "skill_cost_usd:brainstorming"}
        assert all(e["type"] == "score-create" for e in events)
        assert by_name["skill_cost_usd:code-review"]["value"] == pytest.approx(3.0)
        assert by_name["skill_cost_usd:code-review"]["observationId"] == "sc"
        assert by_name["skill_cost_usd:brainstorming"]["value"] == pytest.approx(5.0)
        assert by_name["skill_cost_usd:brainstorming"]["observationId"] == "sb"

    def test_zero_descendant_skill_is_skipped(self) -> None:
        batch = [
            self._skill("sc", "skill:code-review"),
            self._gen("g_rev", "sc", {"total": 2.0}),
            self._skill("sl", "skill:land"),  # no generation descendants
        ]

        events = build_skill_cost_scores(SPOKE, batch, base_ts="t")

        assert {e["body"]["name"] for e in events} == {"skill_cost_usd:code-review"}

    def test_explicit_total_wins_over_component_sum(self) -> None:
        batch = [
            self._skill("sc", "skill:code-review"),
            self._gen("g", "sc", {"input": 0.3, "output": 0.7, "total": 1.0}),
        ]

        events = build_skill_cost_scores(SPOKE, batch, base_ts="t")

        assert events[0]["body"]["value"] == pytest.approx(1.0)

    def test_nested_skills_each_report_their_subtree(self) -> None:
        # skill:brainstorming nested under skill:code-review: the inner generation rolls into BOTH
        # (the subtree-rollup boundary), while code-review's own direct generation is only its own.
        batch = [
            self._skill("sc", "skill:code-review"),
            self._gen("g_outer", "sc", {"total": 1.0}),
            self._skill("sb", "skill:brainstorming", parent="sc"),
            self._gen("g_inner", "sb", {"total": 4.0}),
        ]

        events = build_skill_cost_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert by_name["skill_cost_usd:code-review"] == pytest.approx(5.0)
        assert by_name["skill_cost_usd:brainstorming"] == pytest.approx(4.0)

    def test_generation_under_no_skill_is_ignored(self) -> None:
        batch = [self._gen("g", "i1", {"total": 9.0})]

        assert build_skill_cost_scores(SPOKE, batch, base_ts="t") == []

    def test_same_skill_run_twice_keeps_both_scores(self) -> None:
        # Two distinct spans share the skill name; each gets its own observation-scoped score with a
        # distinct id (keyed off the span id), so both survive ingest rather than upserting onto one.
        batch = [
            self._skill("sc1", "skill:code-review"),
            self._gen("g1", "sc1", {"total": 2.0}),
            self._skill("sc2", "skill:code-review"),
            self._gen("g2", "sc2", {"total": 5.0}),
        ]

        events = build_skill_cost_scores(SPOKE, batch, base_ts="t")

        assert all(e["body"]["name"] == "skill_cost_usd:code-review" for e in events)
        assert {e["body"]["observationId"] for e in events} == {"sc1", "sc2"}
        assert len({e["body"]["id"] for e in events}) == 2


class TestBuildAgentCostScores:
    """#323: sum a sub-agent container's generation-descendant cost into an agent_cost_usd:<type>.

    A ``sub-agent:<type>`` span is the otelcol-renamed ``tool:Agent`` container whose own
    ``costDetails`` is $0 — the real spend lives in its ``sub-agent:llm`` generation descendants —
    so Langfuse's own-cost-summing metrics API returns $0 for a ``sub-agent:`` filter. Each
    container therefore emits ``agent_cost_usd:<type>``, the summed ``costDetails`` of every
    generation in its subtree, observation-scoped to the container. A container with no generation
    descendants is SKIPPED (never scored 0), mirroring the #322 skill-cost discipline. This reuses
    the same subtree-rollup helper as :func:`build_skill_cost_scores`.
    """

    def _agent(self, obs_id: str, name: str, parent: str | None = None) -> dict:
        return {
            "type": "span-create",
            "body": {"id": obs_id, "name": name, "parentObservationId": parent},
        }

    def _gen(self, obs_id: str, parent: str, cost: dict[str, float]) -> dict:
        return {
            "type": "generation-create",
            "body": {
                "id": obs_id,
                "name": "sub-agent:llm",
                "parentObservationId": parent,
                "costDetails": cost,
            },
        }

    def test_two_agent_types_sum_generation_descendants(self) -> None:
        # sub-agent:code-review has a generation nested one level down under an inner span (subtree
        # depth > 1) plus a direct generation; sub-agent:general-purpose has one generation.
        batch = [
            self._agent("acr", "sub-agent:code-review"),
            {"type": "span-create", "body": {"id": "inner", "parentObservationId": "acr"}},
            self._gen("g_rev", "inner", {"total": 2.0}),
            self._gen("g_rev2", "acr", {"input": 0.3, "output": 0.7}),
            self._agent("agp", "sub-agent:general-purpose"),
            self._gen("g_gp", "agp", {"total": 5.0}),
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"] for e in events}
        assert set(by_name) == {
            "agent_cost_usd:code-review",
            "agent_cost_usd:general-purpose",
        }
        assert all(e["type"] == "score-create" for e in events)
        assert by_name["agent_cost_usd:code-review"]["value"] == pytest.approx(3.0)
        assert by_name["agent_cost_usd:code-review"]["observationId"] == "acr"
        assert by_name["agent_cost_usd:general-purpose"]["value"] == pytest.approx(5.0)
        assert by_name["agent_cost_usd:general-purpose"]["observationId"] == "agp"

    def test_zero_descendant_agent_is_skipped(self) -> None:
        batch = [
            self._agent("acr", "sub-agent:code-review"),
            self._gen("g_rev", "acr", {"total": 2.0}),
            self._agent("aex", "sub-agent:Explore"),  # no generation descendants
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        assert {e["body"]["name"] for e in events} == {"agent_cost_usd:code-review"}

    def test_sub_agent_llm_generation_is_not_a_boundary(self) -> None:
        # The generation leaves are named sub-agent:llm; they are summed, never scored themselves.
        batch = [
            self._agent("agp", "sub-agent:general-purpose"),
            self._gen("g", "agp", {"total": 4.0}),
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        assert {e["body"]["name"] for e in events} == {"agent_cost_usd:general-purpose"}

    def test_explicit_total_wins_over_component_sum(self) -> None:
        batch = [
            self._agent("acr", "sub-agent:code-review"),
            self._gen("g", "acr", {"input": 0.3, "output": 0.7, "total": 1.0}),
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        assert events[0]["body"]["value"] == pytest.approx(1.0)

    def test_nested_agents_each_report_their_subtree(self) -> None:
        # A general-purpose agent spawns a nested planner: the inner generation rolls into BOTH
        # (the subtree-rollup boundary), while general-purpose's own generation is only its own.
        batch = [
            self._agent("agp", "sub-agent:general-purpose"),
            self._gen("g_outer", "agp", {"total": 1.0}),
            self._agent("apl", "sub-agent:planner", parent="agp"),
            self._gen("g_inner", "apl", {"total": 4.0}),
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert by_name["agent_cost_usd:general-purpose"] == pytest.approx(5.0)
        assert by_name["agent_cost_usd:planner"] == pytest.approx(4.0)

    def test_generation_under_no_agent_is_ignored(self) -> None:
        batch = [self._gen("g", "i1", {"total": 9.0})]

        assert build_agent_cost_scores(SPOKE, batch, base_ts="t") == []

    def test_same_agent_type_run_twice_keeps_both_scores(self) -> None:
        # A fan-out of two same-type agents: each gets its own observation-scoped score with a
        # distinct id (keyed off the container id), so both survive ingest and the dashboard's
        # Sum-by-Name folds them into one volume-aware bar.
        batch = [
            self._agent("a1", "sub-agent:general-purpose"),
            self._gen("g1", "a1", {"total": 2.0}),
            self._agent("a2", "sub-agent:general-purpose"),
            self._gen("g2", "a2", {"total": 5.0}),
        ]

        events = build_agent_cost_scores(SPOKE, batch, base_ts="t")

        assert all(e["body"]["name"] == "agent_cost_usd:general-purpose" for e in events)
        assert {e["body"]["observationId"] for e in events} == {"a1", "a2"}
        assert len({e["body"]["id"] for e in events}) == 2


class TestBuildMcpCallScores:
    """#234: per-server mcp_success:<server> 0/1 + mcp_calls:<server> count from the mcp groups."""

    def _group(self, obs_id: str, server: str, *, calls: int, failures: int) -> dict:
        return {
            "body": {
                "id": obs_id,
                "name": f"mcp:{server}",
                "metadata": {"server": server, "calls": calls, "failures": failures},
            }
        }

    def test_clean_group_scores_success_and_call_count(self) -> None:
        batch = [self._group("g1", "chrome", calls=3, failures=0)]

        events = build_mcp_call_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert by_name == {"mcp_success:chrome": 1.0, "mcp_calls:chrome": 3}
        assert all(e["body"]["observationId"] == "g1" for e in events)

    def test_group_with_failures_scores_zero_success(self) -> None:
        batch = [self._group("g1", "chrome", calls=3, failures=1)]

        by_name = {
            e["body"]["name"]: e["body"]["value"]
            for e in build_mcp_call_scores(SPOKE, batch, base_ts="t")
        }

        assert by_name["mcp_success:chrome"] == 0.0

    def test_non_mcp_group_nodes_ignored(self) -> None:
        batch = [{"body": {"id": "t", "name": "tool:Bash", "metadata": {}}}]

        assert build_mcp_call_scores(SPOKE, batch, base_ts="t") == []


class TestBuildMcpCarryCostScores:
    """#234: per-server mcp_carry_cost_usd:<server> from the loaded-context mcp rows."""

    def test_carry_cost_aggregates_mcp_rows_per_server(self) -> None:
        rows = [
            {"category": "mcp", "name": "mcp__chrome__navigate", "tokens": 100},
            {"category": "mcp", "name": "mcp__chrome__read", "tokens": 100},
            {"category": "mcp", "name": "mcp__notion__query", "tokens": 50},
            {"category": "tools", "name": "Bash", "tokens": 999},
        ]

        events = build_mcp_carry_cost_scores(SPOKE, rows, 4, base_ts="t", price=0.001)

        names = {e["body"]["name"] for e in events}
        assert names == {"mcp_carry_cost_usd:chrome", "mcp_carry_cost_usd:notion"}
        chrome = next(e for e in events if e["body"]["name"] == "mcp_carry_cost_usd:chrome")
        # 200 tokens x (4 reads x price x 0.08 read-ratio + price) one-time write.
        assert chrome["body"]["value"] == pytest.approx(200 * (4 * 0.001 * 0.08 + 0.001))

    def test_no_mcp_rows_no_scores(self) -> None:
        rows = [{"category": "rules", "name": "a", "tokens": 10}]
        assert build_mcp_carry_cost_scores(SPOKE, rows, 4, base_ts="t", price=0.001) == []


class TestBuildMcpDefLoadScores:
    """#234: mcp_def_loads:<server> counts on-demand ToolSearch schema loads from delta labels."""

    def _call(self, obs_id: str, *servers: str) -> dict:
        added = [{"category": "mcp", "name": f"mcp__{s}__x", "mcp_def_load": s} for s in servers]
        return {"body": {"id": obs_id, "metadata": {"context_delta": {"added": added}}}}

    def test_counts_loads_per_server(self) -> None:
        batch = [self._call("g1", "chrome", "chrome"), self._call("g2", "notion")]

        events = build_mcp_def_load_scores(SPOKE, batch, base_ts="t")

        by_name = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert by_name == {"mcp_def_loads:chrome": 2, "mcp_def_loads:notion": 1}

    def test_no_loads_no_scores(self) -> None:
        batch = [{"body": {"id": "g", "metadata": {}}}]
        assert build_mcp_def_load_scores(SPOKE, batch, base_ts="t") == []


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

    @pytest.mark.parametrize("agent_type", ["bug-scoper", "planner"])
    def test_shipped_brief_completed_status_scores_a_verdict(
        self, agent_type: str, tmp_path: Path
    ) -> None:
        # #325: guards the REAL return contract of shipped briefs. bug-scoper and planner end
        # with a single JSON object carrying status:"completed" (an _AGENT_SUCCESS_STATUSES
        # member), so the generic _sub_agent_verdict path mints agent_verdict:<type> = 1.0 for
        # every non-code-review type — not only code-review. A refactor that drops the generic
        # branch fails here.
        batch = [
            {
                "body": {
                    "id": "sa",
                    "name": f"sub-agent:{agent_type}",
                    "output": json.dumps({"status": "completed", "summary": "..."}),
                }
            }
        ]

        events = build_agent_verdict_scores(SPOKE, batch, tmp_path / "absent", base_ts="t")

        assert len(events) == 1
        body = events[0]["body"]
        assert body["name"] == f"agent_verdict:{agent_type}"
        assert body["value"] == 1.0
        assert body["observationId"] == "sa"

    @pytest.mark.parametrize("agent_type", ["Explore", "general-purpose", "claude-code-guide"])
    def test_research_agent_prose_return_scores_nothing(
        self, agent_type: str, tmp_path: Path
    ) -> None:
        # #325: pure research / read-only agents carry NO terminal status by design, so a prose
        # return scores nothing. That absence is expected (AFK Design Principle 6) and must never
        # be read as a failure — pins that the documented out-of-scope agents stay unscored.
        batch = [{"body": {"id": "sa", "name": f"sub-agent:{agent_type}", "output": "prose"}}]

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


class TestBuildLifecycleStageScores:
    def _scores(self, **kw) -> dict:
        lifecycle = Lifecycle(
            dispatched=kw.pop("dispatched", _DISPATCHED),
            answer_attempt=kw.pop("answer_attempt", _ANSWER_ATTEMPT),
            **kw,
        )
        events = build_lifecycle_stage_scores(
            SPOKE, _stage_traces(), _FIRST_COMMIT, lifecycle, base_ts=_BASE_TS
        )
        assert all(e["type"] == "score-create" for e in events)
        return {e["body"]["name"]: e["body"]["value"] for e in events}

    def test_all_five_stages_when_every_source_present(self) -> None:
        assert self._scores() == {
            "stage_spawn_seed_ms": 300_000,
            "stage_gate_answer_ms": 300_000,
            "stage_review_ms": 120_000,
            "stage_push_gate_ms": 120_000,
            "stage_land_ms": 180_000,
        }

    def test_absent_dispatch_epoch_skips_spawn_seed(self) -> None:
        assert "stage_spawn_seed_ms" not in self._scores(dispatched=None)

    def test_absent_answer_attempt_skips_gate_answer(self) -> None:
        assert "stage_gate_answer_ms" not in self._scores(answer_attempt=None)

    def test_first_commit_before_dispatch_skips_spawn_seed(self) -> None:
        # A relaunch re-stamps dispatch to the LAST dispatch; an earlier dead-run commit yields a
        # negative delta that must be dropped, not double-counted.
        assert "stage_spawn_seed_ms" not in self._scores(dispatched=_DISPATCHED + 10_000)

    def test_no_stage_spans_yields_no_scores(self) -> None:
        events = build_lifecycle_stage_scores(
            SPOKE, [("tr", [])], [], Lifecycle(), base_ts=_BASE_TS
        )
        assert events == []

    def test_zero_duration_review_span_skips_the_review_stage(self) -> None:
        # The broker's agent:review drain-intervention spans carry no --start-ms (zero duration), so
        # a spoke with only those (no real code review) must skip stage_review_ms, not emit 0.
        instant = _span(
            "agent:review",
            "2026-01-02T00:10:00Z",
            "2026-01-02T00:10:00Z",
            **{"workflow.phase": "review", "workflow.kind": "agent"},
        )
        events = build_lifecycle_stage_scores(
            SPOKE, [("tr", [instant])], [], Lifecycle(), base_ts=_BASE_TS
        )
        assert all(e["body"]["name"] != "stage_review_ms" for e in events)

    def test_afk_review_span_with_duration_is_counted(self) -> None:
        # Forward-compat: if _afk_emit_span ever gains a --start-ms, its agent:review window counts.
        timed = _span(
            "agent:review",
            "2026-01-02T00:10:00Z",
            "2026-01-02T00:10:30Z",
            **{"workflow.phase": "review", "workflow.kind": "agent"},
        )
        events = build_lifecycle_stage_scores(
            SPOKE, [("tr", [timed])], [], Lifecycle(), base_ts=_BASE_TS
        )
        review = next(e for e in events if e["body"]["name"] == "stage_review_ms")
        assert review["body"]["value"] == 30_000

    def test_score_ids_are_deterministic_across_reruns(self) -> None:
        first = build_lifecycle_stage_scores(
            SPOKE,
            _stage_traces(),
            _FIRST_COMMIT,
            Lifecycle(dispatched=_DISPATCHED),
            base_ts=_BASE_TS,
        )
        second = build_lifecycle_stage_scores(
            SPOKE,
            _stage_traces(),
            _FIRST_COMMIT,
            Lifecycle(dispatched=_DISPATCHED),
            base_ts=_BASE_TS,
        )
        assert [e["id"] for e in first] == [e["id"] for e in second]


def _root_batch(components: dict) -> list:
    return [
        {
            "type": "span-create",
            "body": {
                "id": root_id_for(SPOKE),
                "metadata": {"rollup": {"duration": {"components": components}}},
            },
        }
    ]


def _stage_score_events(*values: int) -> list:
    return [{"body": {"value": v}} for v in values]


class TestBuildWindowRollupScores:
    def test_issues_per_hour_and_autonomy_from_window_snapshot(self) -> None:
        lifecycle = Lifecycle(
            spokes_serviced=4,
            interventions=1,
            window_start=_DISPATCHED,
            landed=_DISPATCHED + 18_000,
        )
        events = build_window_rollup_scores(SPOKE, [], [], lifecycle, base_ts=_BASE_TS)
        scores = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert scores["issues_per_hour"] == pytest.approx(0.8)  # 4 spokes / 5h
        assert scores["autonomy_score"] == pytest.approx(0.75)  # 1 - 1/4

    def test_overhead_work_ratio_divides_stage_sum_by_work_buckets(self) -> None:
        batch = _root_batch({"llm_request": 1000, "tool": 500, "wait": 9999, "self": 8888})
        stage_scores = _stage_score_events(300_000)
        events = build_window_rollup_scores(
            SPOKE, batch, stage_scores, Lifecycle(), base_ts=_BASE_TS
        )
        scores = {e["body"]["name"]: e["body"]["value"] for e in events}
        assert scores["overhead_work_ratio"] == pytest.approx(300_000 / 1500)

    def test_zero_work_buckets_skips_ratio(self) -> None:
        batch = _root_batch({"wait": 5000, "self": 3000})  # no work classes
        events = build_window_rollup_scores(
            SPOKE, batch, _stage_score_events(100), Lifecycle(), base_ts=_BASE_TS
        )
        assert all(e["body"]["name"] != "overhead_work_ratio" for e in events)

    def test_no_spokes_serviced_skips_throughput_and_autonomy(self) -> None:
        events = build_window_rollup_scores(
            SPOKE, [], [], Lifecycle(spokes_serviced=0), base_ts=_BASE_TS
        )
        names = {e["body"]["name"] for e in events}
        assert "issues_per_hour" not in names and "autonomy_score" not in names

    def test_absent_ledger_counts_as_zero_interventions(self) -> None:
        # interventions None (no ledger) -> 0 firings -> fully autonomous, matching _wd_intervention_count.
        lifecycle = Lifecycle(spokes_serviced=3, interventions=None)
        events = build_window_rollup_scores(SPOKE, [], [], lifecycle, base_ts=_BASE_TS)
        autonomy = next(e["body"]["value"] for e in events if e["body"]["name"] == "autonomy_score")
        assert autonomy == pytest.approx(1.0)

    def test_non_positive_window_skips_issues_per_hour(self) -> None:
        lifecycle = Lifecycle(spokes_serviced=2, window_start=_DISPATCHED, landed=_DISPATCHED)
        events = build_window_rollup_scores(SPOKE, [], [], lifecycle, base_ts=_BASE_TS)
        assert all(e["body"]["name"] != "issues_per_hour" for e in events)


class TestNormalizationDumpPresence:
    """#344: the commits/files/lines base counts must reflect whether a commit dump was PARSED.

    A land whose commits dump was dropped (the #344 empty-range bug, or a bare-branch/--local
    checkout the ingest resolve-or-skips) gives the builder no `--commits`, so `commits` here is
    an empty list that carries NO information about churn. Emitting `commits=0 / files_changed=0 /
    lines_changed=0` then is a WRONG value that poisons the #231 normalization dashboards —
    absence of a dump is not evidence of zero churn. So the three base counts are emitted only
    when a dump was actually present; `subtasks` (independent of commits) stays unconditional.
    A genuinely empty spoke with a present-but-empty dump still legitimately reads 0.
    """

    def _names(self, scores: list[dict]) -> set[str]:
        return {s["body"]["name"] for s in scores}

    def _val(self, scores: list[dict], name: str) -> float:
        return next(s["body"]["value"] for s in scores if s["body"]["name"] == name)

    def test_no_base_counts_when_commits_dump_absent(self) -> None:
        scores = build_normalization_scores(
            SPOKE, [], [], 3, base_ts=_BASE_TS, commits_dump_present=False
        )

        names = self._names(scores)
        assert "commits" not in names
        assert "files_changed" not in names
        assert "lines_changed" not in names
        assert self._val(scores, "subtasks") == 3  # independent of commits, still emitted

    def test_base_counts_zero_when_dump_present_but_empty(self) -> None:
        scores = build_normalization_scores(
            SPOKE, [], [], 3, base_ts=_BASE_TS, commits_dump_present=True
        )

        assert self._val(scores, "commits") == 0
        assert self._val(scores, "files_changed") == 0
        assert self._val(scores, "lines_changed") == 0
