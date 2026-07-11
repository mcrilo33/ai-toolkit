"""Unit tests for the loaded-context enrichment (:mod:`telemetry.spoke_tree.loaded_context`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.loaded_context import (
    _breakdown_by_category,
    _human_tokens,
    build_loaded_context_events,
    find_request_files,
    prefix_total,
)


def _row(category: str, name: str, tokens: int, cost: float = 0.0) -> dict:
    return {"category": category, "name": name, "tokens": tokens, "cost_usd": cost}


class TestPrefixTotal:
    def test_sums_cache_read_and_creation_of_earliest_call(self) -> None:
        traces = [
            (
                "tr",
                [
                    {
                        "startTime": "2026-01-02T00:00:00Z",
                        "usageDetails": {
                            "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 300,
                        },
                    },
                    {
                        "startTime": "2026-01-02T00:00:05Z",
                        "usageDetails": {"cache_read_input_tokens": 1200},
                    },
                ],
            )
        ]
        assert prefix_total(traces) == 1200

    def test_no_usage_is_zero(self) -> None:
        assert prefix_total([("tr", [{"startTime": "x"}])]) == 0


class TestHumanTokens:
    def test_under_1000_is_bare(self) -> None:
        assert _human_tokens(750) == "750"

    def test_thousands_are_compact(self) -> None:
        assert _human_tokens(3200) == "3.2k"


class TestBreakdownByCategory:
    def test_groups_and_sums_duplicate_names(self) -> None:
        rows = [_row("rules", "a", 10), _row("rules", "a", 5), _row("skills", "b", 7)]
        assert _breakdown_by_category(rows, ("rules", "skills", "tools")) == {
            "rules": {"a": 15},
            "skills": {"b": 7},
        }


class TestBuildLoadedContextEvents:
    def test_single_node_with_total_and_breakdown(self) -> None:
        rows = [_row("rules", "a", 10, 0.1), _row("skills", "b", 20, 0.2)]
        events = build_loaded_context_events(
            "sp", rows, category_order=("rules", "skills"), base_ts="2026-01-02T00:00:00Z"
        )
        assert len(events) == 1
        meta = events[0]["body"]["metadata"]
        assert meta["tokens"] == 30
        assert meta["breakdown"] == {"rules": {"a": 10}, "skills": {"b": 20}}

    def test_mcp_rows_add_a_per_server_carry_line(self) -> None:
        rows = [
            _row("mcp", "mcp__chrome__navigate", 40),
            _row("mcp", "mcp__chrome__read_page", 60),
            _row("mcp", "mcp__notion__query", 30),
            _row("tools", "Bash", 10),
        ]
        events = build_loaded_context_events(
            "sp", rows, category_order=("tools", "mcp"), base_ts="2026-01-02T00:00:00Z"
        )
        assert events[0]["body"]["metadata"].get("mcp_by_server") == {"chrome": 100, "notion": 30}

    def test_no_mcp_rows_omit_the_per_server_line(self) -> None:
        rows = [_row("rules", "a", 10)]
        events = build_loaded_context_events(
            "sp", rows, category_order=("rules",), base_ts="2026-01-02T00:00:00Z"
        )
        assert "mcp_by_server" not in events[0]["body"]["metadata"]

    def test_disk_fallback_folds_reconciled_remainder(self) -> None:
        rows = [_row("rules", "a", 10, 0.0)]
        events = build_loaded_context_events(
            "sp",
            rows,
            category_order=("rules",),
            base_ts="2026-01-02T00:00:00Z",
            prefix_total=100,
            price=0.001,
        )
        meta = events[0]["body"]["metadata"]
        assert meta["remainder"] == 90
        assert meta["tokens"] == 100


class TestFindRequestFiles:
    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert find_request_files(tmp_path / "nope") == []

    def test_returns_request_json_oldest_first(self, tmp_path: Path) -> None:
        import os

        first = tmp_path / "a.request.json"
        second = tmp_path / "b.request.json"
        first.write_text("{}", encoding="utf-8")
        second.write_text("{}", encoding="utf-8")
        os.utime(first, (1, 1))
        os.utime(second, (2, 2))
        assert find_request_files(tmp_path) == [first, second]
