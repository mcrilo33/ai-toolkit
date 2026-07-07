"""Unit tests for the per-call cache decomposition (:mod:`telemetry.spoke_tree.llm_decomp`)."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.llm_decomp import (
    _decomp_metadata,
    _memoized_counter,
    _split_rows_by_cache,
)


def _row(category: str, name: str, tokens: int) -> dict:
    return {"category": category, "name": name, "tokens": tokens}


class TestSplitRowsByCache:
    def test_partitions_by_cumulative_offset(self) -> None:
        rows = [_row("tools", "a", 100), _row("system", "b", 50), _row("messages", "c", 40)]
        read, creation = _split_rows_by_cache(rows, cache_read=100, cache_creation=50)
        assert [r["name"] for r in read] == ["a"]
        assert [r["name"] for r in creation] == ["b"]

    def test_beyond_budget_is_dropped(self) -> None:
        rows = [_row("tools", "a", 100), _row("messages", "b", 100)]
        read, creation = _split_rows_by_cache(rows, cache_read=100, cache_creation=0)
        assert [r["name"] for r in read] == ["a"]
        assert creation == []


class TestDecompMetadata:
    def test_reconciles_observed_measured_remainder(self) -> None:
        rows = [_row("tools", "a", 30), _row("system", "b", 20)]
        meta = _decomp_metadata(rows, observed=100)
        assert meta["measured"] == 50
        assert meta["remainder"] == 50
        assert meta["components"] == {"tools": {"a": 30}, "system": {"b": 20}}


class TestMemoizedCounter:
    def test_counts_each_distinct_text_once(self) -> None:
        calls: list[str] = []

        def counter(text: str) -> int:
            calls.append(text)
            return len(text)

        memoized = _memoized_counter(counter)
        assert memoized("abc") == 3
        assert memoized("abc") == 3
        assert memoized("de") == 2
        assert calls == ["abc", "de"]

    def test_failure_is_not_cached(self) -> None:
        state = {"fail": True}

        def counter(text: str) -> int:
            if state["fail"]:
                raise RuntimeError("boom")
            return len(text)

        memoized = _memoized_counter(counter)
        with contextlib.suppress(RuntimeError):
            memoized("x")
        state["fail"] = False
        assert memoized("x") == 1
