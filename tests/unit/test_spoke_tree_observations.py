"""Unit tests for the source-observation read layer (:mod:`telemetry.spoke_tree.observations`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.observations import (
    _attr,
    _duration_ms,
    _is_blocked_tool,
    _is_fold_subspan,
    _is_gate_observation,
    _is_graftable_span,
    _is_guards_group,
    _is_hook,
    _is_hook_event,
    _is_interaction,
    _is_mcp_group,
    _is_mcp_tool_span,
    _is_skill_span,
    _is_tool_span,
    _joins_under_tool,
    _mcp_server,
    _obs_envelope,
    _parse_utc,
    _prompt_id,
    _request_id,
    _tool_use_id,
)


def _obs(name: str, **metadata) -> dict:
    return {"name": name, "metadata": metadata}


class TestAccessors:
    def test_tool_use_id_reads_from_nested_attributes(self) -> None:
        obs = _obs("Bash", attributes={"tool_use_id": "tu-1"})
        assert _tool_use_id(obs) == "tu-1"

    def test_tool_use_id_falls_back_to_flat_metadata(self) -> None:
        assert _tool_use_id({"name": "x", "metadata": {"gen_ai.tool.call.id": "tu-9"}}) == "tu-9"

    def test_tool_use_id_absent_is_none(self) -> None:
        assert _tool_use_id(_obs("Bash")) is None

    def test_prompt_id_reads_nested_then_flat(self) -> None:
        assert _prompt_id(_obs("i", attributes={"prompt.id": "p1"})) == "p1"
        assert _prompt_id({"name": "e", "metadata": {"prompt.id": "p2"}}) == "p2"

    def test_request_id_prefers_request_id_key(self) -> None:
        assert _request_id({"name": "e", "metadata": {"request_id": "r1"}}) == "r1"

    def test_attr_probes_keys_in_order(self) -> None:
        obs = _obs("x", attributes={"b": 2}, a=None)
        assert _attr(obs, "a", "b") == 2

    def test_duration_ms_from_iso_bounds(self) -> None:
        obs = {"startTime": "2026-01-02T00:00:00Z", "endTime": "2026-01-02T00:00:01Z"}
        assert _duration_ms(obs) == 1000

    def test_duration_ms_missing_bound_is_none(self) -> None:
        assert _duration_ms({"startTime": "2026-01-02T00:00:00Z"}) is None


class TestPredicates:
    def test_is_hook_by_sh_name(self) -> None:
        assert _is_hook(_obs("PreToolUse.sh"))

    def test_is_hook_by_workflow_kind(self) -> None:
        assert _is_hook(_obs("guard", attributes={"workflow.kind": "hook"}))

    def test_joins_under_tool_covers_hooks_and_audit_events(self) -> None:
        assert _joins_under_tool(_obs("Stop.sh"))
        assert _joins_under_tool(_obs("tool_result"))
        assert not _joins_under_tool(_obs("Bash"))

    def test_is_fold_subspan_matches_the_three_subspans(self) -> None:
        assert _is_fold_subspan(_obs("claude_code.tool.execution"))
        assert _is_fold_subspan(_obs("claude_code.tool.blocked_on_user"))
        assert _is_fold_subspan(_obs("tool_decision:deny"))
        assert not _is_fold_subspan(_obs("tool_result"))

    def test_is_interaction_matches_the_container_name(self) -> None:
        assert _is_interaction(_obs("claude_code.interaction"))
        assert not _is_interaction(_obs("Bash"))


class TestNodePredicates:
    def test_is_tool_span_matches_tool_prefix(self) -> None:
        assert _is_tool_span(_obs("tool:TaskCreate"))
        assert not _is_tool_span(_obs("claude_code.tool.execution"))

    def test_is_graftable_span_covers_tool_and_sub_agent(self) -> None:
        assert _is_graftable_span(_obs("tool:Bash"))
        assert _is_graftable_span(_obs("sub-agent:code-review"))
        assert not _is_graftable_span(_obs("claude_code.interaction"))

    def test_is_guards_group_matches_both_group_names(self) -> None:
        assert _is_guards_group(_obs("guards"))
        assert _is_guards_group(_obs("guards:session"))
        assert not _is_guards_group(_obs("Bash"))

    def test_is_skill_span_matches_skill_prefix(self) -> None:
        assert _is_skill_span(_obs("skill:code-review"))
        assert _is_skill_span(_obs("skill:claude-hud:setup"))
        assert not _is_skill_span(_obs("tool:Skill"))
        assert not _is_skill_span(_obs("tool:Bash"))

    def test_is_mcp_tool_span_matches_mcp_tool_prefix(self) -> None:
        assert _is_mcp_tool_span(_obs("tool:mcp__claude-in-chrome__navigate"))
        assert not _is_mcp_tool_span(_obs("tool:Bash"))
        assert not _is_mcp_tool_span(_obs("mcp:claude-in-chrome"))

    def test_is_mcp_group_matches_group_prefix(self) -> None:
        assert _is_mcp_group(_obs("mcp:claude-in-chrome"))
        assert not _is_mcp_group(_obs("tool:mcp__claude-in-chrome__navigate"))
        assert not _is_mcp_group(_obs("mcp_server_connection"))

    def test_mcp_server_extracts_server_between_separators(self) -> None:
        assert _mcp_server("tool:mcp__claude-in-chrome__navigate") == "claude-in-chrome"
        assert _mcp_server("tool:mcp__my_server__do__thing") == "my_server"
        assert _mcp_server("tool:Bash") is None

    def test_is_blocked_tool_matches_prefix(self) -> None:
        assert _is_blocked_tool(_obs("blocked-tool:Bash"))
        assert not _is_blocked_tool(_obs("tool:Bash"))

    def test_is_hook_event_matches_prefix(self) -> None:
        assert _is_hook_event(_obs("hook_execution_complete:PreToolUse"))
        assert not _is_hook_event(_obs("tool_result"))

    def test_is_gate_observation_by_name_and_attributes(self) -> None:
        assert _is_gate_observation(_obs("script:gate"))
        assert _is_gate_observation(
            _obs("x", attributes={"workflow.kind": "script", "workflow.phase": "gate"})
        )
        assert not _is_gate_observation(_obs("script:push"))


class TestTimeHelpers:
    def test_parse_utc_assumes_naive_is_utc(self) -> None:
        parsed = _parse_utc("2026-01-02T00:00:00")
        assert parsed is not None and parsed.tzinfo is not None

    def test_parse_utc_none_on_garbage(self) -> None:
        assert _parse_utc("not-a-timestamp") is None

    def test_obs_envelope_spans_min_start_to_max_end(self) -> None:
        obs = [
            {"startTime": "2026-01-02T00:00:05Z", "endTime": "2026-01-02T00:00:06Z"},
            {"startTime": "2026-01-02T00:00:01Z", "endTime": "2026-01-02T00:00:09Z"},
        ]
        assert _obs_envelope(obs) == ("2026-01-02T00:00:01Z", "2026-01-02T00:00:09Z")
