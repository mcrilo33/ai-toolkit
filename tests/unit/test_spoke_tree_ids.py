"""Unit tests for the deterministic id namespace (:mod:`telemetry.spoke_tree.ids`, #166)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.ids import (
    _blocked_tool_id,
    _copy_id,
    _cycle_copy_id,
    _guards_id,
    _mcp_group_id,
    cycle_copy_id_for,
    cycle_root_id_for,
    cycle_trace_id_for,
    root_id_for,
    trace_id_for,
)

SPOKE = "feature/22-demo+1700000000"


class TestIdDeterminism:
    def test_trace_id_is_stable_across_calls(self) -> None:
        assert trace_id_for(SPOKE) == trace_id_for(SPOKE)

    def test_trace_id_carries_view_a_prefix(self) -> None:
        assert trace_id_for(SPOKE).startswith("spoketree-")

    def test_root_and_trace_ids_differ(self) -> None:
        assert root_id_for(SPOKE) != trace_id_for(SPOKE)

    def test_copy_id_keys_on_the_source_pair(self) -> None:
        assert _copy_id("trace-a", "obs-1") != _copy_id("trace-a", "obs-2")
        assert _copy_id("trace-a", "obs-1") == _copy_id("trace-a", "obs-1")


class TestViewNamespaceSeparation:
    def test_cycle_trace_id_never_collides_with_view_a(self) -> None:
        assert cycle_trace_id_for(SPOKE) != trace_id_for(SPOKE)

    def test_cycle_root_id_never_collides_with_view_a(self) -> None:
        assert cycle_root_id_for(SPOKE) != root_id_for(SPOKE)

    def test_cycle_copy_id_preserves_the_view_a_digest(self) -> None:
        view_a = _copy_id("trace-a", "obs-1")
        assert cycle_copy_id_for("trace-a", "obs-1") == _cycle_copy_id(view_a)
        assert cycle_copy_id_for("trace-a", "obs-1").endswith(view_a[len("tree-") :])


class TestSyntheticNodeIds:
    def test_guards_id_keys_on_parent(self) -> None:
        assert _guards_id("p1") != _guards_id("p2")
        assert _guards_id("p1").startswith("tree-guards-")

    def test_blocked_tool_id_keys_on_tool_use_id(self) -> None:
        assert _blocked_tool_id("tu-1") != _blocked_tool_id("tu-2")
        assert _blocked_tool_id("tu-1").startswith("tree-blocked-")

    def test_mcp_group_id_keys_on_parent_and_server(self) -> None:
        assert _mcp_group_id("p1", "srv-a") != _mcp_group_id("p1", "srv-b")
        assert _mcp_group_id("p1", "srv-a") != _mcp_group_id("p2", "srv-a")
        assert _mcp_group_id("p1", "srv-a") == _mcp_group_id("p1", "srv-a")
        assert _mcp_group_id("p1", "srv-a").startswith("tree-mcp-")
