"""Unit tests for the source-observation read layer (:mod:`telemetry.spoke_tree.observations`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.observations import (
    _attr,
    _duration_ms,
    _is_fold_subspan,
    _is_hook,
    _is_interaction,
    _joins_under_tool,
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
