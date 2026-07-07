"""Unit tests for the trace-metadata enrichment (:mod:`telemetry.spoke_tree.metadata`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.metadata import (
    _merge_trace_tags,
    _read_pointer,
    apply_mode_lane_tags,
    read_mode_lane,
)


def _batch() -> list[dict]:
    return [{"type": "trace-create", "body": {"id": "T"}}]


class TestReadPointer:
    def test_valid_value_is_returned(self, tmp_path: Path) -> None:
        pointer = tmp_path / "mode"
        pointer.write_text("afk\n", encoding="utf-8")
        assert _read_pointer(pointer, ("afk", "attended"), "attended") == "afk"

    def test_missing_file_falls_back(self, tmp_path: Path) -> None:
        assert _read_pointer(tmp_path / "nope", ("afk",), "attended") == "attended"

    def test_invalid_value_falls_back(self, tmp_path: Path) -> None:
        pointer = tmp_path / "lane"
        pointer.write_text("bogus", encoding="utf-8")
        assert _read_pointer(pointer, ("spoke",), "spoke") == "spoke"


class TestReadModeLane:
    def test_defaults_for_empty_root(self, tmp_path: Path) -> None:
        assert read_mode_lane(tmp_path) == ("attended", "spoke")

    def test_reads_both_pointers(self, tmp_path: Path) -> None:
        (tmp_path / ".ai-toolkit").mkdir()
        (tmp_path / ".ai-toolkit" / "mode").write_text("afk", encoding="utf-8")
        (tmp_path / ".ai-toolkit" / "lane").write_text("quick", encoding="utf-8")
        assert read_mode_lane(tmp_path) == ("afk", "quick")


class TestMergeTraceTags:
    def test_dedupes_and_preserves_order(self) -> None:
        batch = _batch()
        _merge_trace_tags(batch, ["a", "b"])
        _merge_trace_tags(batch, ["b", "c"])
        assert batch[0]["body"]["tags"] == ["a", "b", "c"]


class TestApplyModeLaneTags:
    def test_stamps_tags_and_metadata(self) -> None:
        batch = _batch()
        apply_mode_lane_tags(batch, "afk", "spoke")
        body = batch[0]["body"]
        assert body["tags"] == ["mode:afk", "lane:spoke"]
        assert body["metadata"] == {"mode": "afk", "lane": "spoke"}
