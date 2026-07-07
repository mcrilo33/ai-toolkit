"""Unit tests for the View A step lens (:mod:`telemetry.spoke_tree.steps`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.observations import ToolContent
from telemetry.spoke_tree.steps import (
    StepWindow,
    _collapse_startup_instants,
    _containing_window,
    _step_id,
    _step_node_name,
    build_step_windows,
)


def _tool(obs_id: str, name: str, tuid: str, *, start: str = "", end: str = "") -> dict:
    return {
        "id": obs_id,
        "name": name,
        "startTime": start or None,
        "endTime": end or None,
        "metadata": {"attributes": {"tool_use_id": tuid}},
    }


class TestBuildStepWindows:
    def test_create_plus_update_yields_a_window(self) -> None:
        traces = [
            (
                "tr",
                [
                    _tool("c1", "tool:TaskCreate", "tc"),
                    _tool("u1", "tool:TaskUpdate", "up1", start="2026-01-02T00:00:01Z"),
                    _tool(
                        "u2",
                        "tool:TaskUpdate",
                        "up2",
                        start="2026-01-02T00:00:05Z",
                        end="2026-01-02T00:00:05Z",
                    ),
                ],
            )
        ]
        content = {
            "tc": ToolContent(input={"subject": "S1 RED: x"}, output="Task #1 created"),
            "up1": ToolContent(input={"taskId": "1", "status": "in_progress"}, output=None),
            "up2": ToolContent(input={"taskId": "1", "status": "completed"}, output=None),
        }
        windows = build_step_windows(traces, content)
        assert len(windows) == 1
        assert windows[0].subject == "S1 RED: x"
        assert windows[0].start == "2026-01-02T00:00:01Z"
        assert windows[0].status == "completed"

    def test_no_ledger_yields_no_windows(self) -> None:
        assert build_step_windows([("tr", [])], {}) == []

    def test_created_but_never_started_is_skipped(self) -> None:
        traces = [("tr", [_tool("c1", "tool:TaskCreate", "tc")])]
        content = {"tc": ToolContent(input={"subject": "s"}, output="Task #1 created")}
        assert build_step_windows(traces, content) == []


class TestStepHelpers:
    def test_step_id_is_deterministic_per_parent(self) -> None:
        assert _step_id("sp", "1", "pA") == _step_id("sp", "1", "pA")
        assert _step_id("sp", "1", "pA") != _step_id("sp", "1", "pB")

    def test_step_node_name_prefixes_subject(self) -> None:
        window = StepWindow("1", "RED: x", "s", "e", "completed")
        assert _step_node_name(window) == "step:RED: x"

    def test_containing_window_picks_innermost(self) -> None:
        outer = StepWindow("1", "a", "2026-01-02T00:00:00Z", "2026-01-02T00:00:10Z", "completed")
        inner = StepWindow("2", "b", "2026-01-02T00:00:03Z", "2026-01-02T00:00:06Z", "completed")
        assert _containing_window("2026-01-02T00:00:04Z", [outer, inner]) is inner


class TestCollapseStartupInstants:
    def test_startup_instants_demote_to_root_metadata(self) -> None:
        root = {"id": "R", "type": "span-create", "body": {"id": "R", "metadata": {}}}
        copies = [
            {"id": "s1", "type": "span-create", "body": {"id": "s1", "name": "plugin_loaded"}},
            {"id": "t1", "type": "span-create", "body": {"id": "t1", "name": "tool:Bash"}},
        ]
        kept = _collapse_startup_instants(copies, root)
        assert [e["body"]["id"] for e in kept] == ["t1"]
        assert root["body"]["metadata"]["session_init"] == [{"name": "plugin_loaded"}]
