"""Unit tests for the per-request context deltas (:mod:`telemetry.spoke_tree.context_deltas`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.context_deltas import (
    _apply_context_rollups,
    _blob_hash,
    _label_mcp_def_loads,
    _match_skill_output,
    _sum_context,
)


class TestBlobHash:
    def test_stable_for_equal_json(self) -> None:
        assert _blob_hash({"a": 1, "b": 2}) == _blob_hash({"b": 2, "a": 1})

    def test_differs_for_different_content(self) -> None:
        assert _blob_hash("x") != _blob_hash("y")


class TestMatchSkillOutput:
    def test_matches_tool_result_block_by_hash(self) -> None:
        skill_output = "SKILL BODY"
        hashes = {_blob_hash(skill_output): "afk"}
        message = json.dumps({"content": [{"type": "tool_result", "content": skill_output}]})
        assert _match_skill_output(message, hashes) == "afk"

    def test_none_when_no_match(self) -> None:
        assert _match_skill_output(json.dumps({"content": "hi"}), {}) is None

    def test_none_on_non_json_text(self) -> None:
        assert _match_skill_output("not json", {"h": "x"}) is None


class TestLabelMcpDefLoads:
    """#234: an added mcp-category def row (a ToolSearch schema load) is tagged with its server."""

    def test_mcp_def_row_labeled_with_server(self) -> None:
        added = [
            {"category": "mcp", "name": "mcp__chrome__navigate", "tokens": 40},
            {"category": "tools", "name": "Bash", "tokens": 10},
            {"category": "messages", "name": "m0", "tokens": 5},
        ]

        _label_mcp_def_loads(added)

        assert added[0]["mcp_def_load"] == "chrome"
        assert "mcp_def_load" not in added[1]
        assert "mcp_def_load" not in added[2]


class TestSumContext:
    def test_sums_descendant_summaries(self) -> None:
        children: dict[str | None, list[str]] = {"step": ["g1", "g2"], "g1": [], "g2": []}
        summaries = {
            "g1": {"net_tokens": 10, "added": 12, "removed": 2},
            "g2": {"net_tokens": 5, "added": 5, "removed": 0},
        }
        assert _sum_context("step", children, summaries) == {
            "net_tokens": 15,
            "added": 17,
            "removed": 2,
        }

    def test_none_without_any_delta(self) -> None:
        children: dict[str | None, list[str]] = {"step": ["g1"], "g1": []}
        assert _sum_context("step", children, {}) is None


class TestApplyContextRollups:
    def test_stamps_context_onto_step_node(self) -> None:
        events = [
            {
                "type": "span-create",
                "body": {
                    "id": "tree-step-abc",
                    "parentObservationId": None,
                    "metadata": {"rollup": {"written": 1}},
                },
            },
            {
                "type": "generation-create",
                "body": {"id": "g1", "parentObservationId": "tree-step-abc"},
            },
        ]
        _apply_context_rollups(events, {"g1": {"net_tokens": 7, "added": 7, "removed": 0}})
        assert events[0]["body"]["metadata"]["rollup"]["context"]["net_tokens"] == 7
