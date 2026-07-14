"""Unit tests for the trace-metadata enrichment (:mod:`telemetry.spoke_tree.metadata`)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.spoke_tree.metadata import (
    _merge_trace_tags,
    _read_pointer,
    apply_lifecycle_metadata,
    apply_mode_lane_tags,
    build_lifecycle_timeline,
    read_mode_lane,
)
from telemetry.spoke_tree.observations import Lifecycle


def _batch() -> list[dict]:
    return [{"type": "trace-create", "body": {"id": "T"}}]


def _ready_span(end: str = "2026-01-02T05:00:00Z") -> dict:
    # The completion span's OTLP name is the "<kind>:<phase>" label — script:ready — not the
    # --name value (telemetry.sh), exactly like the sibling gate span is "script:gate".
    return {
        "id": "sr1",
        "name": "script:ready",
        "startTime": end,
        "endTime": end,
        "metadata": {"attributes": {"workflow.kind": "script", "workflow.phase": "ready"}},
    }


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


class TestBuildLifecycleTimeline:
    def test_assembles_five_legs_in_chronological_order(self) -> None:
        lifecycle = Lifecycle(
            filed="2026-01-01T00:00:00Z",
            dispatched=1767312000,  # 2026-01-02T00:00:00Z
            landed=1767330000,  # 2026-01-02T05:00:00Z
        )
        commits = [{"authored_at": "2026-01-02T00:10:00Z"}]
        traces = [("tr", [_ready_span()])]

        timeline = build_lifecycle_timeline(lifecycle, commits, traces)

        assert list(timeline) == ["filed", "dispatched", "first_commit", "ready", "landed"]
        assert timeline["dispatched"] == "2026-01-02T00:00:00Z"
        assert timeline["first_commit"] == "2026-01-02T00:10:00Z"
        assert timeline["ready"] == "2026-01-02T05:00:00Z"

    def test_absent_sources_are_omitted_not_guessed(self) -> None:
        # Only the land instant is available: no filed / dispatch / commits / ready span.
        timeline = build_lifecycle_timeline(Lifecycle(landed=1767330000), [], [("tr", [])])
        assert timeline == {"landed": "2026-01-02T05:00:00Z"}

    def test_ready_leg_matches_production_script_ready_name(self) -> None:
        # Regression: the completion span's OTLP name is script:ready (not the --name "spoke-ready"),
        # so keying off the raw --name dropped the leg on 100% of spokes.
        timeline = build_lifecycle_timeline(Lifecycle(), [], [("tr", [_ready_span()])])
        assert timeline == {"ready": "2026-01-02T05:00:00Z"}

    def test_ready_leg_falls_back_to_workflow_attributes(self) -> None:
        # Robustness (mirrors _is_gate_observation): a label-format change is tolerated because the
        # workflow.kind/phase attributes also identify the completion span.
        span = {
            "id": "sr1",
            "name": "some-other-label",
            "endTime": "2026-01-02T05:00:00Z",
            "metadata": {"attributes": {"workflow.kind": "script", "workflow.phase": "ready"}},
        }
        timeline = build_lifecycle_timeline(Lifecycle(), [], [("tr", [span])])
        assert timeline == {"ready": "2026-01-02T05:00:00Z"}

    def test_empty_lifecycle_yields_empty_timeline(self) -> None:
        assert build_lifecycle_timeline(Lifecycle(), [], [("tr", [])]) == {}

    def test_first_commit_offset_is_normalized_to_utc_z(self) -> None:
        # A git author time carrying a +02:00 offset renders consistently with the epoch-derived Z
        # legs (cosmetic uniformity of the stored map).
        commits = [{"authored_at": "2026-01-02T02:10:00+02:00"}]
        timeline = build_lifecycle_timeline(Lifecycle(), commits, [("tr", [])])
        assert timeline == {"first_commit": "2026-01-02T00:10:00Z"}


class TestApplyLifecycleMetadata:
    def test_stamps_timeline_on_both_views(self) -> None:
        batch, cycle_batch = _batch(), _batch()
        timeline = {"landed": "2026-01-02T05:00:00Z"}

        apply_lifecycle_metadata(batch, cycle_batch, timeline)

        assert batch[0]["body"]["metadata"]["lifecycle"] == timeline
        assert cycle_batch[0]["body"]["metadata"]["lifecycle"] == timeline

    def test_empty_timeline_leaves_traces_untouched(self) -> None:
        batch, cycle_batch = _batch(), _batch()
        apply_lifecycle_metadata(batch, cycle_batch, {})
        assert "metadata" not in batch[0]["body"]
        assert "metadata" not in cycle_batch[0]["body"]

    def test_rerun_is_idempotent(self) -> None:
        batch, cycle_batch = _batch(), _batch()
        timeline = {"filed": "2026-01-01T00:00:00Z", "landed": "2026-01-02T05:00:00Z"}
        apply_lifecycle_metadata(batch, cycle_batch, timeline)
        apply_lifecycle_metadata(batch, cycle_batch, timeline)
        assert batch[0]["body"]["metadata"]["lifecycle"] == timeline
