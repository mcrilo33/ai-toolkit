"""Unit tests for the fold / guard-group / level passes (:mod:`telemetry.spoke_tree.folding`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.folding import (
    _apply_levels,
    _fold_attrs,
    _guard_group_metadata,
    _guard_noop,
    _hook_event_exclude,
    _level_for,
    _stamp_hook_endtimes,
)


def _copy(obs_id: str, name: str, **body) -> dict:
    return {"id": obs_id, "type": "span-create", "body": {"id": obs_id, "name": name, **body}}


class TestFoldAttrs:
    def test_execution_span_contributes_ms_and_success(self) -> None:
        obs = {
            "name": "claude_code.tool.execution",
            "startTime": "2026-01-02T00:00:00Z",
            "endTime": "2026-01-02T00:00:00.500Z",
            "metadata": {"attributes": {"success": True}},
        }
        attrs = _fold_attrs(obs)
        assert attrs["execution_ms"] == 500
        assert attrs["success"] is True

    def test_decision_span_contributes_decision(self) -> None:
        obs = {"name": "tool_decision:deny", "metadata": {}}
        assert _fold_attrs(obs)["decision"] == "deny"

    def test_non_fold_span_contributes_nothing(self) -> None:
        assert _fold_attrs({"name": "tool:Bash", "metadata": {}}) == {}


class TestGuardNoop:
    def test_allow_success_under_1s_is_noop(self) -> None:
        body = {
            "startTime": "2026-01-02T00:00:00Z",
            "endTime": "2026-01-02T00:00:00.100Z",
            "metadata": {"attributes": {"decision": "allow", "status": "success"}},
        }
        assert _guard_noop(body)

    def test_deny_is_not_noop(self) -> None:
        body = {
            "startTime": "2026-01-02T00:00:00Z",
            "endTime": "2026-01-02T00:00:00.100Z",
            "metadata": {"attributes": {"decision": "deny", "status": "success"}},
        }
        assert not _guard_noop(body)


class TestGuardGroupMetadata:
    def test_rollup_counts_every_member_and_sorts(self) -> None:
        members = [
            _copy(
                "g1",
                "PreToolUse.sh",
                startTime="2026-01-02T00:00:00Z",
                endTime="2026-01-02T00:00:00.010Z",
                metadata={"attributes": {"decision": "allow"}},
            ),
            _copy(
                "g2",
                "PostToolUse.sh",
                startTime="2026-01-02T00:00:00Z",
                endTime="2026-01-02T00:00:00.020Z",
                metadata={"attributes": {"decision": "deny"}},
            ),
        ]
        meta = _guard_group_metadata(members)
        assert meta["count"] == 2
        assert meta["total_ms"] == 30
        assert meta["decisions"] == ["allow", "deny"]
        assert list(meta["by_hook"]) == ["PostToolUse.sh", "PreToolUse.sh"]


class TestLevels:
    def test_failed_tool_is_error(self) -> None:
        assert _level_for({"name": "tool:Bash", "metadata": {"success": False}}) == "ERROR"

    def test_blocked_tool_is_warning(self) -> None:
        assert _level_for({"name": "blocked-tool:Bash"}) == "WARNING"

    def test_clean_tool_has_no_level(self) -> None:
        assert _level_for({"name": "tool:Bash", "metadata": {"success": True}}) is None

    def test_apply_levels_stamps_in_place(self) -> None:
        copies = [_copy("t1", "tool:Bash", metadata={"error": "boom"})]
        _apply_levels(copies)
        assert copies[0]["body"]["level"] == "ERROR"


class TestHookEndTimes:
    def test_stamp_derives_endtime_and_flags_lagging(self) -> None:
        copies = [
            _copy(
                "h1",
                "hook_execution_complete:PreToolUse",
                startTime="2026-01-02T00:00:00Z",
                metadata={"attributes": {"total_duration_ms": 250}},
            )
        ]
        _stamp_hook_endtimes(copies)
        body = copies[0]["body"]
        assert body["endTime"] == "2026-01-02T00:00:00.250000Z"
        assert body["metadata"]["time_source"] == "lagging"

    def test_hook_events_are_excluded_from_duration(self) -> None:
        copies = [
            _copy("h1", "hook_execution_complete:PreToolUse"),
            _copy("t1", "tool:Bash"),
        ]
        assert _hook_event_exclude(copies) == {"h1"}
