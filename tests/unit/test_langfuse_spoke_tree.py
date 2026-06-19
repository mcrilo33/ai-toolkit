"""Unit tests for the assembled single nested spoke-tree emitter (Issue #83).

The emitter (:mod:`telemetry.langfuse_spoke_tree`) SOURCES FROM LANGFUSE: it fetches the
spoke's existing rich per-turn / marker / hook observations and copies them, verbatim,
into ONE nested trace, re-parenting across the original trace boundaries. These tests run
with NO network: :func:`build_batch` is pure (fed a hand-built list of source traces +
observations), and the fetch path is exercised with a stubbed ``get``. They assert one
``trace-create`` + one synthetic root + one copy per source observation, verbatim field
copying, intra-trace parent remapping, root collapsing, hook re-parenting, and id
determinism.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_spoke_tree import (
    _copy_id,
    build_batch,
    fetch_session,
    root_id_for,
    trace_id_for,
)

SPOKE = "feature/22-demo+1700000000"


def _obs(
    obs_id: str, name: str, *, type_: str = "SPAN", parent: str | None = None, **extra
) -> dict:
    """Build a source observation carrying only the keys the emitter reads."""
    observation = {
        "id": obs_id,
        "name": name,
        "type": type_,
        "parentObservationId": parent,
        "startTime": extra.pop("startTime", None),
        "endTime": extra.pop("endTime", None),
    }
    observation.update(extra)
    return observation


def _traces() -> list[tuple[str, list[dict]]]:
    """Four source traces: an interaction tree, a marker, a matching hook, a stray hook."""
    interaction = _obs("i1", "claude_code.interaction", parent=None, metadata={"kind": "turn"})
    tool = _obs("t1", "Bash", parent="i1", metadata={"kind": "tool", "tool_use_id": "tu-1"})
    generation = _obs(
        "g1",
        "llm_request",
        type_="GENERATION",
        parent="i1",
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-02T00:00:01Z",
        input="hello",
        output="hi there",
        model="claude-opus-4-8",
        usageDetails={
            "input": 120,
            "output": 45,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
        },
        metadata={"kind": "turn", "rollup": {"input": 120}},
    )
    marker = _obs("m1", "step:green", parent=None, metadata={"kind": "step"})
    hook_match = _obs(
        "h1",
        "PreToolUse.sh",
        parent=None,
        metadata={"hook_event": "PreToolUse", "tool_use_id": "tu-1"},
    )
    hook_stray = _obs("h2", "Stop.sh", parent=None, metadata={"hook_event": "Stop"})
    return [
        ("trace-int", [interaction, tool, generation]),
        ("trace-marker", [marker]),
        ("trace-hook", [hook_match]),
        ("trace-stray", [hook_stray]),
    ]


def _by_orig(batch: list[dict], orig_trace_id: str, orig_obs_id: str) -> dict:
    """Return the copy event for one source observation by its deterministic copy id."""
    copy_id = _copy_id(orig_trace_id, orig_obs_id)
    return next(event for event in batch if event["id"] == copy_id)


class TestBuildBatch:
    def test_emits_trace_create_then_one_synthetic_root(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        assert batch[0]["type"] == "trace-create"
        assert batch[0]["body"]["sessionId"] == SPOKE
        assert batch[0]["body"]["name"] == f"spoke-tree:{SPOKE}"
        root = batch[1]
        assert root["id"] == root_id_for(SPOKE)
        assert root["body"]["name"] == f"spoke:{SPOKE}"
        assert "parentObservationId" not in root["body"]

    def test_one_copy_per_source_observation(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        copies = batch[2:]
        # 6 source observations across the 4 traces.
        assert len(copies) == 6
        assert all(event["body"]["traceId"] == trace_id_for(SPOKE) for event in copies)
        assert {event["type"] for event in copies} == {"span-create", "generation-create"}

    def test_generation_fields_copied_verbatim(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        copy = _by_orig(batch, "trace-int", "g1")
        assert copy["type"] == "generation-create"
        body = copy["body"]
        assert body["input"] == "hello"
        assert body["output"] == "hi there"
        assert body["model"] == "claude-opus-4-8"
        assert body["usageDetails"] == {
            "input": 120,
            "output": 45,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
        }
        assert body["metadata"] == {"kind": "turn", "rollup": {"input": 120}}
        assert body["startTime"] == "2026-01-02T00:00:00Z"
        assert body["endTime"] == "2026-01-02T00:00:01Z"

    def test_absent_fields_are_not_invented(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        marker = _by_orig(batch, "trace-marker", "m1")
        assert "usageDetails" not in marker["body"]
        assert "input" not in marker["body"]
        assert "model" not in marker["body"]

    def test_intra_trace_parent_is_remapped(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        tool = _by_orig(batch, "trace-int", "t1")
        assert tool["body"]["parentObservationId"] == _copy_id("trace-int", "i1")

    def test_interaction_and_marker_roots_collapse_to_spoke_root(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        root_id = root_id_for(SPOKE)
        interaction = _by_orig(batch, "trace-int", "i1")
        marker = _by_orig(batch, "trace-marker", "m1")
        assert interaction["body"]["parentObservationId"] == root_id
        assert marker["body"]["parentObservationId"] == root_id

    def test_hook_matching_a_tool_is_parented_under_that_tool(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        hook = _by_orig(batch, "trace-hook", "h1")
        assert hook["body"]["parentObservationId"] == _copy_id("trace-int", "t1")

    def test_hook_without_a_match_collapses_to_spoke_root(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        stray = _by_orig(batch, "trace-stray", "h2")
        assert stray["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_ids_are_deterministic_across_runs(self) -> None:
        first = {event["id"] for event in build_batch(_traces(), SPOKE)}

        second = {event["id"] for event in build_batch(_traces(), SPOKE)}

        assert first == second

    def test_empty_session_emits_only_trace_and_root(self) -> None:
        batch = build_batch([], SPOKE)

        assert [event["type"] for event in batch] == ["trace-create", "span-create"]


class TestFetchSession:
    def test_paginates_traces_and_observations(self) -> None:
        pages: dict[str, dict] = {
            "/traces?sessionId=sess&limit=100&page=1": {
                "data": [{"id": "tr-a"}],
                "meta": {"totalPages": 2},
            },
            "/traces?sessionId=sess&limit=100&page=2": {
                "data": [{"id": "tr-b"}],
                "meta": {"totalPages": 2},
            },
            "/observations?traceId=tr-a&limit=100&page=1": {
                "data": [{"id": "o1"}],
                "meta": {"totalPages": 1},
            },
            "/observations?traceId=tr-b&limit=100&page=1": {
                "data": [{"id": "o2"}],
                "meta": {"totalPages": 1},
            },
        }

        result = fetch_session("sess", lambda path: pages[path])

        assert result == [("tr-a", [{"id": "o1"}]), ("tr-b", [{"id": "o2"}])]
