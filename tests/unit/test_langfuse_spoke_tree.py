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

import json
import shutil
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_spoke_tree import (
    _DISK_CATEGORY_ORDER,
    _REQUEST_CATEGORY_ORDER,
    ToolContent,
    _copy_id,
    _is_own_output,
    _memoized_counter,
    apply_context_deltas,
    apply_llm_decomposition,
    apply_mode_lane_tags,
    apply_request_body_metadata,
    build_batch,
    build_cycle_batch,
    build_enforcement_fire_scores,
    build_loaded_context_events,
    build_rule_carry_cost_scores,
    build_rule_invocation_scores,
    build_score_events,
    build_step_cost_scores,
    build_step_duration_scores,
    build_step_total_cost_scores,
    build_tooldef_carry_cost_scores,
    cycle_copy_id_for,
    cycle_root_id_for,
    cycle_trace_id_for,
    fetch_session,
    main_loop_request_count,
    prefix_total,
    purge_own_views,
    read_mode_lane,
    request_context_rows,
    root_id_for,
    scan_transcripts,
    trace_id_for,
    transcript_scan_root,
)
from telemetry.request_body import (
    ContextItem,
    decompose_request_body,
    measure_request_items,
)
from telemetry.session_parser import project_dir_for_worktree
from telemetry.spoke_tree.assembly import (
    _MAX_CONTENT_CHARS,
    _TRUNCATION_MARKER,
    _tool_span_ids,
)
from telemetry.spoke_tree.commits import _gate_park_ms, _parse_commits
from telemetry.spoke_tree.context_deltas import _label_rule_injections, load_scoped_rules
from telemetry.spoke_tree.ids import _CYCLE_STEP_PREFIX
from telemetry.spoke_tree.llm_decomp import _decomp_metadata
from telemetry.spoke_tree.loaded_context import find_request_files
from telemetry.spoke_tree.scores import _step_phase
from telemetry.spoke_tree.steps import _STEP_PREFIX, build_cycle_windows, build_step_windows

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
    # Real Langfuse nests OTel span attributes under metadata["attributes"]; a tool carries
    # tool_use_id there (== gen_ai.tool.call.id).
    tool = _obs(
        "t1",
        "Bash",
        parent="i1",
        metadata={
            "attributes": {
                "tool_name": "Bash",
                "tool_use_id": "tu-1",
                "gen_ai.tool.call.id": "tu-1",
            }
        },
    )
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
    # A hook's signal is workflow.kind == "hook" and its tool_use_id, both nested under
    # metadata["attributes"] just like a real Langfuse observation.
    hook_match = _obs(
        "h1",
        "PreToolUse.sh",
        parent=None,
        metadata={
            "attributes": {
                "workflow.kind": "hook",
                "hook_event": "PreToolUse",
                "tool_use_id": "tu-1",
            }
        },
    )
    hook_stray = _obs(
        "h2",
        "Stop.sh",
        parent=None,
        metadata={"attributes": {"workflow.kind": "hook", "hook_event": "Stop"}},
    )
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


def _dur(total_ms: int, components: dict[str, int] | None = None) -> dict:
    """The expected ``rollup.duration`` object: every class key present, zeros filled in."""
    classes = (
        "llm_request",
        "sub-agent",
        "tool",
        "skill",
        "mcp",
        "hook",
        "script",
        "step",
        "wait",
        "turn",
        "self",
        "other",
    )
    filled = {key: 0 for key in classes}
    filled.update(components or {})
    return {"total_ms": total_ms, "components": filled}


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

        # 6 source observations across the 4 traces, but the step:green cycle-marker is suppressed
        # (#235), leaving 5 copies; the two `.sh` hooks additionally spawn a `guards` +
        # `guards:session` group each (#157), so filter those out to count the sources.
        copies = [
            event
            for event in batch[2:]
            if event["body"].get("name") not in ("guards", "guards:session")
        ]
        assert len(copies) == 5
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

        interaction = _by_orig(batch, "trace-int", "i1")
        assert "usageDetails" not in interaction["body"]
        assert "input" not in interaction["body"]
        assert "model" not in interaction["body"]

    def test_cycle_marker_span_is_suppressed(self) -> None:
        # #235: the step:<phase> marker is consumed to build the cycle spine, never copied verbatim.
        batch = build_batch(_traces(), SPOKE)

        copy_id = _copy_id("trace-marker", "m1")
        assert not any(event["id"] == copy_id for event in batch)
        assert "step:green" not in {event["body"].get("name") for event in batch}

    def test_intra_trace_parent_is_remapped(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        tool = _by_orig(batch, "trace-int", "t1")
        assert tool["body"]["parentObservationId"] == _copy_id("trace-int", "i1")

    def test_interaction_root_collapses_to_spoke_root(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        root_id = root_id_for(SPOKE)
        interaction = _by_orig(batch, "trace-int", "i1")
        assert interaction["body"]["parentObservationId"] == root_id

    def test_hook_matching_a_tool_is_parented_under_that_tool(self) -> None:
        # #157: the hook nests under its tool's `guards` group, which nests under the tool.
        batch = build_batch(_traces(), SPOKE)

        group = _guards_group(batch)
        hook = _by_orig(batch, "trace-hook", "h1")
        assert hook["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("trace-int", "t1")

    def test_hook_without_a_match_collapses_to_spoke_root(self) -> None:
        # #157: a root-level (session) hook nests under the root `guards:session` group.
        batch = build_batch(_traces(), SPOKE)

        session = _session_guards(batch)
        stray = _by_orig(batch, "trace-stray", "h2")
        assert stray["body"]["parentObservationId"] == session["body"]["id"]
        assert session["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_hook_matches_tool_via_gen_ai_tool_call_id(self) -> None:
        # The tool exposes its id only as gen_ai.tool.call.id; the hook references it as
        # tool_use_id — both nested under metadata["attributes"]. They must still join.
        tool = _obs(
            "t9",
            "Bash",
            parent=None,
            metadata={"attributes": {"gen_ai.tool.call.id": "tu-9"}},
        )
        hook = _obs(
            "h9",
            "PreToolUse.sh",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-9"}},
        )
        traces = [("trace-tool", [tool]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        group = _guards_group(batch)
        copy = _by_orig(batch, "trace-hook", "h9")
        assert copy["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("trace-tool", "t9")

    def test_hook_detected_by_workflow_kind_without_sh_name(self) -> None:
        # A hook whose name does not end in ".sh" is still detected via workflow.kind; with no
        # tool_use_id it is a session guard, nested under the root `guards:session` group (#157).
        hook = _obs(
            "h8",
            "hook-emit",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "hook_event": "Stop"}},
        )

        batch = build_batch([("trace-hook", [hook])], SPOKE)

        session = _session_guards(batch)
        copy = _by_orig(batch, "trace-hook", "h8")
        assert copy["body"]["parentObservationId"] == session["body"]["id"]
        assert session["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_tool_result_audit_event_nests_under_its_tool(self) -> None:
        # tool_result audit observations join by the same rule (forward-compat with the
        # task contract; matched by the tool_* audit-name prefix).
        tool = _obs("t6", "Read", parent=None, metadata={"attributes": {"tool_use_id": "tu-6"}})
        result = _obs(
            "r6",
            "tool_result",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-6"},
        )
        traces = [("trace-tool", [tool]), ("trace-audit", [result])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "r6")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t6")

    def test_audit_event_without_a_match_synthesizes_blocked_tool_at_root(self) -> None:
        # #157: an unmatched tool_use_id synthesizes a blocked-tool node; with no enclosing turn it
        # sits at the root, and the tool_decision folds its decision onto it.
        decision = _obs(
            "d0",
            "tool_decision:reject",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-absent", "decision": "reject"},
        )

        batch = build_batch([("trace-audit", [decision])], SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == root_id_for(SPOKE)
        assert blocked["body"]["metadata"]["decision"] == "reject"

    def test_hook_nests_under_tool_not_a_sibling_audit_event(self) -> None:
        # A tool_decision audit event shares the tool_use_id but must NOT become the
        # re-parent target: an audit event is skipped as an index owner, so the hook still
        # nests under the genuine tool span even when the audit event is fetched last.
        tool = _obs("t5", "Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-5"}})
        hook = _obs(
            "h5",
            "PreToolUse.sh",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-5"}},
        )
        decision = _obs(
            "d5",
            "tool_decision:allow",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-5", "decision": "allow"},
        )
        traces = [("trace-tool", [tool]), ("trace-hook", [hook]), ("trace-audit", [decision])]

        batch = build_batch(traces, SPOKE)

        # #157: the gate hook still resolves to its tool — now via its tool's `guards` group.
        group = _guards_group(batch)
        hook_copy = _by_orig(batch, "trace-hook", "h5")
        assert hook_copy["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("trace-tool", "t5")

    def test_hook_execution_complete_event_nests_under_its_tool(self) -> None:
        # A hook_execution_complete:PreToolUse audit observation now carries the tool_use_id
        # the bridge stamped (flat metadata, no parentObservationId). It must nest under the
        # tool sharing that id, exactly like a gate hook / tool_decision (issue hook-event-nest).
        tool = _obs("t8", "Edit", parent=None, metadata={"attributes": {"tool_use_id": "tu-8"}})
        hook = _obs(
            "h8a",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PreToolUse",
                "hook_name": "PreToolUse:Edit",
                "tool_use_id": "tu-8",
            },
        )
        traces = [("trace-tool", [tool]), ("trace-audit", [hook])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "h8a")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t8")

    def test_hook_execution_complete_session_event_collapses_to_root(self) -> None:
        # A SessionStart hook event carries no tool_use_id (no tool triggered it) and must
        # stay at the synthetic root, never nested under a tool.
        hook = _obs(
            "h9a",
            "hook_execution_complete:SessionStart",
            type_="EVENT",
            parent=None,
            metadata={"hook_event": "SessionStart", "hook_name": "SessionStart"},
        )

        batch = build_batch([("trace-audit", [hook])], SPOKE)

        copy = _by_orig(batch, "trace-audit", "h9a")
        assert copy["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_unmatched_hook_execution_complete_event_nests_under_blocked_tool_at_root(self) -> None:
        # #157: a Pre/PostToolUse hook whose tool_use_id matches no tool span synthesizes a
        # blocked-tool node (at the root, no enclosing turn) and nests under it.
        hook = _obs(
            "h10",
            "hook_execution_complete:PostToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PostToolUse",
                "hook_name": "PostToolUse:Edit",
                "tool_use_id": "tu-absent",
            },
        )

        batch = build_batch([("trace-audit", [hook])], SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == root_id_for(SPOKE)
        assert (
            _by_orig(batch, "trace-audit", "h10")["body"]["parentObservationId"]
            == blocked["body"]["id"]
        )

    def test_hook_execution_complete_event_is_not_a_tool_index_owner(self) -> None:
        # A hook_execution_complete event shares the tool_use_id but must NOT become the
        # re-parent target: it is skipped as an index owner, so a gate hook sharing the id
        # still nests under the genuine tool span even when fetched after the audit event.
        tool = _obs("t11", "Edit", parent=None, metadata={"attributes": {"tool_use_id": "tu-11"}})
        gate_hook = _obs(
            "h11",
            "PreToolUse.sh",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-11"}},
        )
        audit_hook = _obs(
            "h11a",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PreToolUse",
                "hook_name": "PreToolUse:Edit",
                "tool_use_id": "tu-11",
            },
        )
        traces = [
            ("trace-tool", [tool]),
            ("trace-hook", [gate_hook]),
            ("trace-audit", [audit_hook]),
        ]

        batch = build_batch(traces, SPOKE)

        # #157: the gate hook resolves to its tool via the tool's `guards` group.
        group = _guards_group(batch)
        gate_copy = _by_orig(batch, "trace-hook", "h11")
        assert gate_copy["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("trace-tool", "t11")

    def test_ids_are_deterministic_across_runs(self) -> None:
        first = {event["id"] for event in build_batch(_traces(), SPOKE)}

        second = {event["id"] for event in build_batch(_traces(), SPOKE)}

        assert first == second

    def test_empty_session_emits_only_trace_and_root(self) -> None:
        batch = build_batch([], SPOKE)

        assert [event["type"] for event in batch] == ["trace-create", "span-create"]


class TestEnclosingTurnFallback:
    """#110 AC1 as reshaped by #157: a satellite whose tool was denied/cancelled (no tool span)
    now gets a synthesized ``blocked-tool`` node, and the #110 enclosing-turn resolution runs on
    THAT node — by prompt.id first, then by [start,end] window containment for a real-timing gate
    hook — instead of on each satellite. A lagging-timestamp audit instant is never window-placed
    (anti-lag), and a blocked tool with no enclosing turn at all stays at the root. The satellites
    then adopt the blocked-tool node (gate hooks via their guards group; audit events directly).
    """

    def test_unmatched_gate_hook_nests_under_turn_by_prompt_id(self) -> None:
        # A PreToolUse gate hook whose tool was denied carries a tool_use_id matching no tool
        # span, but shares its turn's prompt.id — it nests under that interaction, not the root.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        hook = _obs(
            "h1",
            "PreToolUse.sh",
            parent=None,
            metadata={
                "attributes": {
                    "workflow.kind": "hook",
                    "tool_use_id": "tu-denied",
                    "prompt.id": "p1",
                }
            },
        )
        traces = [("trace-int", [interaction]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        # The blocked-tool node resolves to the turn by prompt.id; the gate hook adopts it via its group.
        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        assert _guards_group(batch)["body"]["parentObservationId"] == blocked["body"]["id"]

    def test_unmatched_hook_execution_complete_nests_under_turn_by_prompt_id(self) -> None:
        # A hook_execution_complete audit instant (lagging time) with an unmatched tool_use_id
        # joins its turn by prompt.id — never window-placed by its lagging startTime.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p7"}},
        )
        hook = _obs(
            "h7",
            "hook_execution_complete:PostToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PostToolUse",
                "hook_name": "PostToolUse:Edit",
                "tool_use_id": "tu-denied",
                "prompt.id": "p7",
            },
        )
        traces = [("trace-int", [interaction]), ("trace-audit", [hook])]

        batch = build_batch(traces, SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        assert (
            _by_orig(batch, "trace-audit", "h7")["body"]["parentObservationId"]
            == blocked["body"]["id"]
        )

    def test_interaction_prompt_id_in_flat_metadata_rehomes_event(self) -> None:
        # #111: the message bridge stamps prompt.id onto the interaction as a FLAT metadata key
        # (metadata["prompt.id"], the same shape the audit layer uses), NOT nested under
        # metadata["attributes"]. This locks that bridge output shape to the reader: a turn whose
        # prompt.id lives only in flat metadata still anchors a floating hook_execution_complete.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"prompt.id": "p-flat", "attributes": {"interaction.sequence": "3"}},
        )
        hook = _obs(
            "h8",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PreToolUse",
                "hook_name": "PreToolUse:Bash",
                "tool_use_id": "tu-denied",
                "prompt.id": "p-flat",
            },
        )
        traces = [("trace-int", [interaction]), ("trace-audit", [hook])]

        batch = build_batch(traces, SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        assert (
            _by_orig(batch, "trace-audit", "h8")["body"]["parentObservationId"]
            == blocked["body"]["id"]
        )

    def test_unmatched_gate_hook_nests_by_time_window_without_prompt_id(self) -> None:
        # A real-timing gate hook with no prompt.id falls back to [start,end] containment: its
        # true startTime sits inside exactly one interaction window.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        hook = _obs(
            "h2",
            "PreToolUse.sh",
            parent=None,
            startTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-denied"}},
        )
        traces = [("trace-int", [interaction]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        # The blocked-tool node window-places to the containing turn; the gate hook adopts it.
        assert _one_blocked(batch)["body"]["parentObservationId"] == _copy_id("trace-int", "i1")

    def test_innermost_turn_wins_on_overlapping_windows(self) -> None:
        # When a resume interaction nests inside an outer one, a window-matched hook homes to
        # the innermost (narrowest) containing turn.
        outer = _obs(
            "i_out",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:30Z",
        )
        inner = _obs(
            "i_in",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:05Z",
            endTime="2026-01-02T00:00:15Z",
        )
        hook = _obs(
            "h3",
            "PreToolUse.sh",
            parent=None,
            startTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-denied"}},
        )
        traces = [("trace-int", [outer, inner]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        assert _one_blocked(batch)["body"]["parentObservationId"] == _copy_id("trace-int", "i_in")

    def test_innermost_turn_wins_on_equal_start_windows(self) -> None:
        # Two turns sharing a start: the narrower (earlier end) is the innermost and wins.
        outer = _obs(
            "i_out",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:30Z",
        )
        inner = _obs(
            "i_in",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        hook = _obs(
            "h6",
            "PreToolUse.sh",
            parent=None,
            startTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-denied"}},
        )
        traces = [("trace-int", [outer, inner]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        assert _one_blocked(batch)["body"]["parentObservationId"] == _copy_id("trace-int", "i_in")

    def test_unmatched_tool_decision_nests_under_turn_by_prompt_id(self) -> None:
        # The canonical denied-tool case: a tool_decision:reject for a tool that produced no
        # span carries the turn's prompt.id and re-homes under that interaction, not the root.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p9"}},
        )
        decision = _obs(
            "d9",
            "tool_decision:reject",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-denied", "decision": "reject", "prompt.id": "p9"},
        )
        traces = [("trace-int", [interaction]), ("trace-audit", [decision])]

        batch = build_batch(traces, SPOKE)

        # The tool_decision folds its decision onto the blocked-tool node, which homes to the turn.
        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        assert blocked["body"]["metadata"]["decision"] == "reject"

    def test_audit_instant_hook_without_prompt_id_is_not_window_placed(self) -> None:
        # The anti-lag guard: a hook_execution_complete carries a LAGGING startTime, so without
        # a prompt.id it is NOT placed by window containment — it stays at the root.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        hook = _obs(
            "h4",
            "hook_execution_complete:PostToolUse",
            type_="EVENT",
            parent=None,
            startTime="2026-01-02T00:00:05Z",
            metadata={
                "hook_event": "PostToolUse",
                "hook_name": "PostToolUse:Edit",
                "tool_use_id": "tu-denied",
            },
        )
        traces = [("trace-int", [interaction]), ("trace-audit", [hook])]

        batch = build_batch(traces, SPOKE)

        # No prompt.id and a lagging audit timestamp -> the blocked-tool node is not window-placed,
        # so it (and the hook under it) stay at the root.
        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == root_id_for(SPOKE)
        assert (
            _by_orig(batch, "trace-audit", "h4")["body"]["parentObservationId"]
            == blocked["body"]["id"]
        )

    def test_matched_tool_still_wins_over_enclosing_turn(self) -> None:
        # The fallback never overrides a real match: a hook whose tool_use_id DOES resolve to a
        # tool span nests under the tool, even when an enclosing turn shares its prompt.id.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        tool = _obs(
            "t1",
            "Bash",
            parent="i1",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        hook = _obs(
            "h5",
            "PreToolUse.sh",
            parent=None,
            metadata={
                "attributes": {"workflow.kind": "hook", "tool_use_id": "tu-1", "prompt.id": "p1"}
            },
        )
        traces = [("trace-int", [interaction, tool]), ("trace-hook", [hook])]

        batch = build_batch(traces, SPOKE)

        # #157: the matched tool still wins over the enclosing turn — reached via its `guards` group.
        group = _guards_group(batch)
        copy = _by_orig(batch, "trace-hook", "h5")
        assert copy["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("trace-int", "t1")


class TestSessionScopedHookNoDangle:
    """#153: with the bridge's hook->decision join scoped to session.id, every
    hook_execution_complete a spoke emits now carries a SAME-session tool_use_id that
    resolves to one of the spoke's own tool spans. So the audit hook nests under its tool
    rather than dangling on claude_code.interaction for lack of a matching tool -- the
    contamination symptom the fix removes upstream. This locks that assembled-tree invariant.
    """

    def test_same_session_hook_nests_under_its_tool_not_the_interaction(self) -> None:
        # Repro shape (spoke #149): a PreToolUse hook_execution_complete instant referencing
        # its Edit call. Pre-fix the bridge bound a FOREIGN session's tool_use_id (matching no
        # own tool), so this re-homed to the enclosing turn (dangled). Post-fix it carries the
        # own tu-own and must nest under the Edit tool span.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        tool = _obs(
            "t1",
            "Edit",
            parent="i1",
            metadata={"attributes": {"tool_name": "Edit", "tool_use_id": "tu-own"}},
        )
        hook = _obs(
            "h1",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PreToolUse",
                "hook_name": "PreToolUse:Edit",
                "tool_use_id": "tu-own",
                "prompt.id": "p1",
            },
        )
        traces = [("trace-int", [interaction, tool]), ("trace-audit", [hook])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "h1")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "t1")
        assert copy["body"]["parentObservationId"] != _copy_id("trace-int", "i1")


class TestToolSubspanFolding:
    """#100 part 2: the three 1:1 native sub-spans fold into their tool's metadata instead of
    nesting as child nodes — claude_code.tool.execution -> execution_ms/success/error,
    claude_code.tool.blocked_on_user -> blocked_on_user_ms (+decision/source), tool_decision:*
    -> decision/decision_source. Gate hooks, tool_result, and hook_execution_complete stay
    nested under their tool.
    """

    def _dropped(self, batch: list[dict], orig_trace_id: str, orig_obs_id: str) -> bool:
        copy_id = _copy_id(orig_trace_id, orig_obs_id)
        return all(event["id"] != copy_id for event in batch)

    def test_execution_folds_ms_and_success_into_tool(self) -> None:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-1", "success": True}},
        )

        batch = build_batch([("tr", [tool, execu])], SPOKE)

        assert self._dropped(batch, "tr", "ex")  # the sub-span is no longer a node
        meta = _by_orig(batch, "tr", "tb")["body"]["metadata"]
        assert meta["execution_ms"] == 1000
        assert meta["success"] is True

    def test_execution_error_folds_into_tool(self) -> None:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:00Z",
            metadata={"attributes": {"tool_use_id": "tu-1", "success": False, "error": "boom"}},
        )

        batch = build_batch([("tr", [tool, execu])], SPOKE)

        meta = _by_orig(batch, "tr", "tb")["body"]["metadata"]
        assert meta["success"] is False
        assert meta["error"] == "boom"

    def test_blocked_on_user_folds_ms_and_decision(self) -> None:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        blocked = _obs(
            "bl",
            "claude_code.tool.blocked_on_user",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:03Z",
            metadata={
                "attributes": {"tool_use_id": "tu-1", "decision": "accept", "source": "user"}
            },
        )

        batch = build_batch([("tr", [tool, blocked])], SPOKE)

        assert self._dropped(batch, "tr", "bl")
        meta = _by_orig(batch, "tr", "tb")["body"]["metadata"]
        assert meta["blocked_on_user_ms"] == 3000
        assert meta["decision"] == "accept"
        assert meta["decision_source"] == "user"

    def test_tool_decision_audit_event_folds_into_tool(self) -> None:
        # A #93 tool_decision audit event now folds (decision/decision_source) instead of nesting.
        tool = _obs("t7", "Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-7"}})
        decision = _obs(
            "d7",
            "tool_decision:reject",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-7", "decision": "reject", "decision_source": "rule"},
        )

        batch = build_batch([("trace-tool", [tool]), ("trace-audit", [decision])], SPOKE)

        assert self._dropped(batch, "trace-audit", "d7")
        meta = _by_orig(batch, "trace-tool", "t7")["body"]["metadata"]
        assert meta["decision"] == "reject"
        assert meta["decision_source"] == "rule"

    def test_gate_hook_under_tool_is_not_folded(self) -> None:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        hook = _obs(
            "hk",
            "PreToolUse.sh",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-1"}},
        )

        batch = build_batch([("tr", [tool, hook])], SPOKE)

        # the hook survives as a node (not folded) — nested under its tool's `guards` group (#157).
        assert not self._dropped(batch, "tr", "hk")
        group = _guards_group(batch)
        assert _by_orig(batch, "tr", "hk")["body"]["parentObservationId"] == group["body"]["id"]
        assert group["body"]["parentObservationId"] == _copy_id("tr", "tb")

    def test_tool_result_event_is_not_folded(self) -> None:
        tool = _obs("tb", "Read", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}})
        result = _obs(
            "r1", "tool_result", type_="EVENT", parent=None, metadata={"tool_use_id": "tu-1"}
        )

        batch = build_batch([("trace-tool", [tool]), ("trace-audit", [result])], SPOKE)

        assert not self._dropped(batch, "trace-audit", "r1")
        assert _by_orig(batch, "trace-audit", "r1")["body"]["parentObservationId"] == _copy_id(
            "trace-tool", "tb"
        )

    def test_mixed_naive_aware_timestamps_do_not_crash_the_build(self) -> None:
        # One end aware (Z), the other naive: the duration is omitted, not raised; other attrs fold.
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01",
            metadata={"attributes": {"tool_use_id": "tu-1", "success": True}},
        )

        batch = build_batch([("tr", [tool, execu])], SPOKE)

        meta = _by_orig(batch, "tr", "tb")["body"]["metadata"]
        assert "execution_ms" not in meta
        assert meta["success"] is True
        assert self._dropped(batch, "tr", "ex")

    def test_subspan_whose_parent_is_not_a_tool_is_not_folded(self) -> None:
        # An execution span with no tool_use_id, nested under an interaction (not a tool), must NOT
        # fold onto that non-tool node — it keeps its node nested under the interaction.
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"success": True}},
        )

        batch = build_batch([("tr", [interaction, execu])], SPOKE)

        assert not self._dropped(batch, "tr", "ex")
        assert _by_orig(batch, "tr", "ex")["body"]["parentObservationId"] == _copy_id("tr", "i1")
        assert "success" not in _by_orig(batch, "tr", "i1")["body"].get("metadata", {})

    def test_unmatched_tool_decision_folds_onto_synthesized_blocked_tool(self) -> None:
        # #157: an unmatched tool_decision now synthesizes a blocked-tool node and folds its
        # decision onto that node (previously it kept its own node at the root).
        decision = _obs(
            "d0",
            "tool_decision:reject",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-absent", "decision": "reject"},
        )

        batch = build_batch([("trace-audit", [decision])], SPOKE)

        assert self._dropped(batch, "trace-audit", "d0")
        blocked = _one_blocked(batch)
        assert blocked["body"]["metadata"]["decision"] == "reject"
        assert blocked["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_child_of_a_folded_subspan_is_rehomed_onto_the_tool(self) -> None:
        # A resume interaction nests under the tool.execution via TRACEPARENT; when the execution
        # folds away, the resume (and its tokens) must re-home onto the tool, not dangle.
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-1", "success": True}},
        )
        resume = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            parent="ex",
            usageDetails={"cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
        )

        batch = build_batch([("tr", [tool, execu, resume])], SPOKE)

        assert self._dropped(batch, "tr", "ex")  # the execution sub-span folded away
        tool_copy = _by_orig(batch, "tr", "tb")
        # the resume re-homes onto the tool (not the deleted execution id)...
        assert _by_orig(batch, "tr", "g1")["body"]["parentObservationId"] == tool_copy["id"]
        # ...so its tokens still roll up under the tool container.
        assert tool_copy["body"]["metadata"]["rollup"]["reused"] == 100


class TestScoreEvents:
    """#100 amendment: two numeric Langfuse SCORES make time-budget chartable —
    permission_wait_ms (per blocked tool observation) and gate_park_ms (trace-level PLAN park).
    """

    def _scores(self, traces: list[tuple[str, list[dict]]]) -> list[dict]:
        batch = build_batch(traces, SPOKE)
        return build_score_events(SPOKE, traces, batch, base_ts="2026-01-01T00:00:00Z")

    def _by_name(self, scores: list[dict], name: str) -> list[dict]:
        return [s for s in scores if s["body"]["name"] == name]

    def _blocked_tool_traces(self) -> list[tuple[str, list[dict]]]:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        blocked = _obs(
            "bl",
            "claude_code.tool.blocked_on_user",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:03Z",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        return [("tr", [tool, blocked])]

    def _gate(self, start: str, end: str) -> dict:
        return _obs(
            "gt",
            "script:gate",
            parent=None,
            startTime=start,
            endTime=end,
            metadata={"attributes": {"workflow.kind": "script", "workflow.phase": "gate"}},
        )

    def test_permission_wait_score_on_the_tool_observation(self) -> None:
        scores = self._scores(self._blocked_tool_traces())

        perm = self._by_name(scores, "permission_wait_ms")
        assert len(perm) == 1
        assert perm[0]["type"] == "score-create"
        body = perm[0]["body"]
        assert body["dataType"] == "NUMERIC"
        assert body["value"] == 3000
        assert body["observationId"] == _copy_id("tr", "tb")
        assert body["traceId"] == trace_id_for(SPOKE)

    def test_no_permission_score_without_a_blocked_subspan(self) -> None:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )

        scores = self._scores([("tr", [tool])])

        assert self._by_name(scores, "permission_wait_ms") == []

    def test_tool_result_size_score_on_the_tool_observation(self, tmp_path: Path) -> None:
        # #101 part 4: a numeric tool_result_size (bytes of the reconstructed tool_result)
        # is emitted as a Langfuse score on the tool node, so "which outputs bloat context"
        # is a one-click chart.
        span = _tool_obs("t1", "tool:Read", "tu-1")
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Read", {"file_path": "/a"}), _tool_result("tu-1", "hello")],
        )
        traces = [("trace", [span])]
        batch = build_batch(traces, SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        scores = build_score_events(SPOKE, traces, batch, base_ts="2026-01-01T00:00:00Z")

        sized = self._by_name(scores, "tool_result_size")
        assert len(sized) == 1
        body = sized[0]["body"]
        assert body["dataType"] == "NUMERIC"
        assert body["value"] == 5  # len(b"hello")
        assert body["observationId"] == _copy_id("trace", "t1")
        assert body["traceId"] == trace_id_for(SPOKE)

    def test_no_tool_result_size_score_without_reconstructed_output(self) -> None:
        # A tool span with no transcript output carries no size, so no score is emitted.
        span = _tool_obs("t1", "tool:Read", "tu-1")
        traces = [("trace", [span])]
        batch = build_batch(traces, SPOKE)

        scores = build_score_events(SPOKE, traces, batch, base_ts="2026-01-01T00:00:00Z")

        assert self._by_name(scores, "tool_result_size") == []

    def test_tool_result_size_counts_utf8_bytes_not_characters(self, tmp_path: Path) -> None:
        # A multi-byte glyph ("é" = 2 UTF-8 bytes) is measured by byte length, not char count.
        span = _tool_obs("t1", "tool:Read", "tu-1")
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Read", {"file_path": "/a"}), _tool_result("tu-1", "café")],
        )
        traces = [("trace", [span])]
        batch = build_batch(traces, SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        scores = build_score_events(SPOKE, traces, batch, base_ts="2026-01-01T00:00:00Z")

        assert self._by_name(scores, "tool_result_size")[0]["body"]["value"] == 5  # c-a-f-é(2)

    def test_empty_reconstructed_output_scores_zero(self, tmp_path: Path) -> None:
        # An empty (but present) tool_result is a real 0-byte measurement — distinct from a
        # tool with no reconstructed output (which carries no score at all).
        span = _tool_obs("t1", "tool:Read", "tu-1")
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Read", {"file_path": "/a"}), _tool_result("tu-1", "")],
        )
        traces = [("trace", [span])]
        batch = build_batch(traces, SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        scores = build_score_events(SPOKE, traces, batch, base_ts="2026-01-01T00:00:00Z")

        sized = self._by_name(scores, "tool_result_size")
        assert len(sized) == 1
        assert sized[0]["body"]["value"] == 0

    def test_gate_park_score_is_trace_level_gap_to_first_activity(self) -> None:
        gate = self._gate("2026-01-02T00:00:00Z", "2026-01-02T00:00:10Z")
        turn = _obs("i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:01:10Z")

        scores = self._scores([("tr", [gate, turn])])

        park = self._by_name(scores, "gate_park_ms")
        assert len(park) == 1
        body = park[0]["body"]
        assert body["dataType"] == "NUMERIC"
        assert body["value"] == 60000  # 00:00:10 -> 00:01:10
        assert body["traceId"] == trace_id_for(SPOKE)
        assert "observationId" not in body  # trace-level

    def test_gate_park_uses_first_genuine_activity_not_a_marker(self) -> None:
        gate = self._gate("2026-01-02T00:00:00Z", "2026-01-02T00:00:10Z")
        marker = _obs("sp", "spoke-push", parent=None, startTime="2026-01-02T00:00:20Z")
        turn = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:01:10Z",
            usageDetails={"cache_read_input_tokens": 1},
        )

        scores = self._scores([("tr", [gate, marker, turn])])

        # gap is measured to the llm_request, not the spoke-push marker that fired earlier.
        assert self._by_name(scores, "gate_park_ms")[0]["body"]["value"] == 60000

    def test_no_gate_park_score_without_a_gate(self) -> None:
        turn = _obs("i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:01:10Z")

        scores = self._scores([("tr", [turn])])

        assert self._by_name(scores, "gate_park_ms") == []

    def test_score_ids_are_deterministic(self) -> None:
        first = {s["id"] for s in self._scores(self._blocked_tool_traces())}
        second = {s["id"] for s in self._scores(self._blocked_tool_traces())}

        assert first == second and first

    def test_two_blocked_tools_get_distinct_permission_scores(self) -> None:
        tool_a = _obs("ta", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "a"}})
        block_a = _obs(
            "ba",
            "claude_code.tool.blocked_on_user",
            parent="ta",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "a"}},
        )
        tool_b = _obs(
            "tbb", "tool:Read", parent=None, metadata={"attributes": {"tool_use_id": "b"}}
        )
        block_b = _obs(
            "bb",
            "claude_code.tool.blocked_on_user",
            parent="tbb",
            startTime="2026-01-02T00:00:05Z",
            endTime="2026-01-02T00:00:07Z",
            metadata={"attributes": {"tool_use_id": "b"}},
        )

        scores = self._scores([("tr", [tool_a, block_a, tool_b, block_b])])

        perm = self._by_name(scores, "permission_wait_ms")
        assert {s["body"]["value"] for s in perm} == {1000, 2000}
        assert len({s["id"] for s in perm}) == 2  # distinct ids, no collision

    def test_gate_detected_by_workflow_attributes_without_the_name(self) -> None:
        # Robust to a label-format change: kind=script + phase=gate is enough, no "script:gate".
        gate = _obs(
            "gt",
            "some-other-name",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"workflow.kind": "script", "workflow.phase": "gate"}},
        )
        turn = _obs("i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:01:10Z")

        scores = self._scores([("tr", [gate, turn])])

        assert self._by_name(scores, "gate_park_ms")[0]["body"]["value"] == 60000

    def test_no_gate_park_when_activity_only_precedes_the_gate(self) -> None:
        early = _obs("i0", "claude_code.interaction", parent=None, startTime="2026-01-02T00:00:00Z")
        gate = self._gate("2026-01-02T00:05:00Z", "2026-01-02T00:05:10Z")

        scores = self._scores([("tr", [early, gate])])

        assert self._by_name(scores, "gate_park_ms") == []

    def test_gate_park_orders_by_parsed_time_not_string(self) -> None:
        # Fractional seconds + Z: a naive string sort would mis-order; parsing keeps it correct.
        gate = self._gate("2026-01-02T00:00:00Z", "2026-01-02T00:00:09.500Z")
        turn = _obs("i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:00:10Z")

        scores = self._scores([("tr", [gate, turn])])

        assert self._by_name(scores, "gate_park_ms")[0]["body"]["value"] == 500


class TestCarryCostScores:
    """#232 subtask carry: per-rule and per-tooldef carry-cost scores from the loaded-context
    rows x n_requests x cache-read price (+ a one-time cache-write share). Carry cost is what a
    rule / tool schema costs EVERY request just by being loaded, whether it is ever invoked.
    """

    _BASE_TS = "2026-01-01T00:00:00Z"

    def _rule_scores(
        self, rows: list[dict], n_requests: int = 10, price: float = 0.001
    ) -> list[dict]:
        return build_rule_carry_cost_scores(
            SPOKE, rows, n_requests, base_ts=self._BASE_TS, price=price
        )

    def _tooldef_scores(
        self, rows: list[dict], n_requests: int = 10, price: float = 0.001
    ) -> list[dict]:
        return build_tooldef_carry_cost_scores(
            SPOKE, rows, n_requests, base_ts=self._BASE_TS, price=price
        )

    def test_rule_carry_cost_score_per_rule_file(self) -> None:
        rows = [
            {"category": "rules", "name": "python-style.md", "tokens": 100},
            {"category": "rules", "name": "MEMORY.md", "tokens": 50},
            {"category": "tools", "name": "Bash", "tokens": 120},
        ]

        scores = self._rule_scores(rows)

        names = {s["body"]["name"] for s in scores}
        assert names == {
            "rule_carry_cost_usd:python-style.md",
            "rule_carry_cost_usd:MEMORY.md",
        }

    def test_rule_carry_cost_value_folds_n_requests_reads_and_one_write_share(self) -> None:
        rows = [{"category": "rules", "name": "python-style.md", "tokens": 100}]

        scores = self._rule_scores(rows, n_requests=10, price=0.001)

        body = scores[0]["body"]
        # 100 x (10 reads x 0.001x0.08 + one 0.001 write share) = 100 x 0.0018 = 0.18
        assert body["value"] == pytest.approx(0.18)
        assert body["dataType"] == "NUMERIC"
        assert body["traceId"] == trace_id_for(SPOKE)
        assert "observationId" not in body  # trace-level

    def test_no_rule_scores_without_rule_rows(self) -> None:
        scores = self._rule_scores([{"category": "tools", "name": "Bash", "tokens": 120}])

        assert scores == []

    def test_disk_fallback_memory_category_is_scored(self) -> None:
        # The disk fallback splits the auto-memory into its own 'memory' category; MEMORY.md must
        # still get a carry-cost score, mirroring the request-body path where it lands under 'rules'.
        rows = [{"category": "memory", "name": "MEMORY.md", "tokens": 4350}]

        scores = self._rule_scores(rows)

        assert {s["body"]["name"] for s in scores} == {"rule_carry_cost_usd:MEMORY.md"}

    def test_duplicate_rule_names_are_summed(self) -> None:
        rows = [
            {"category": "rules", "name": "CLAUDE.md", "tokens": 100},
            {"category": "rules", "name": "CLAUDE.md", "tokens": 40},
        ]

        scores = self._rule_scores(rows, n_requests=1, price=0.001)

        assert len(scores) == 1
        assert scores[0]["body"]["value"] == pytest.approx(140 * (0.001 * 0.08 + 0.001))

    def test_rule_score_ids_are_deterministic(self) -> None:
        rows = [{"category": "rules", "name": "python-style.md", "tokens": 100}]

        first = {s["id"] for s in self._rule_scores(rows)}
        second = {s["id"] for s in self._rule_scores(rows)}

        assert first == second and first

    def test_tooldef_carry_cost_score_per_tool(self) -> None:
        rows = [
            {"category": "tools", "name": "Bash", "tokens": 120},
            {"category": "mcp", "name": "mcp__x__y", "tokens": 80},
            {"category": "rules", "name": "a.md", "tokens": 10},
        ]

        scores = self._tooldef_scores(rows)

        names = {s["body"]["name"] for s in scores}
        assert names == {
            "tooldef_carry_cost_usd:Bash",
            "tooldef_carry_cost_usd:mcp__x__y",
        }

    def test_tooldef_scores_capped_to_top_n_with_other_bucket(self) -> None:
        rows = [{"category": "tools", "name": f"T{i}", "tokens": 1000 - i} for i in range(20)]

        scores = self._tooldef_scores(rows)

        names = [s["body"]["name"] for s in scores]
        assert len(scores) == 16  # top 15 by cost + one folded :other bucket
        assert "tooldef_carry_cost_usd:other" in names
        assert "tooldef_carry_cost_usd:T0" in names  # largest is named
        assert "tooldef_carry_cost_usd:T19" not in names  # smallest is folded

    def test_tooldef_other_bucket_sums_folded_tokens(self) -> None:
        # 17 equal-token tools: exactly two are folded regardless of tie-break order.
        rows = [{"category": "tools", "name": f"T{i}", "tokens": 100} for i in range(17)]

        scores = self._tooldef_scores(rows, n_requests=1, price=0.001)

        other = [s for s in scores if s["body"]["name"] == "tooldef_carry_cost_usd:other"]
        assert len(other) == 1
        assert other[0]["body"]["value"] == pytest.approx(200 * (0.001 * 0.08 + 0.001))

    def test_main_loop_request_count_excludes_sub_agent_calls(self) -> None:
        # A rule/tool sits in the MAIN loop's prefix only; sub-agent:llm calls run their own prefix
        # and must not inflate the read multiplier.
        main = _obs(
            "mg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            usageDetails={"cache_read_input_tokens": 1},
        )
        sub_agent = _obs(
            "sag",
            "sub-agent:llm",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:05Z",
            usageDetails={"cache_read_input_tokens": 1},
        )

        assert main_loop_request_count([("tr", [main, sub_agent])]) == 1


class TestRuleInvocationScores:
    """#232 subtask invoke: a glob-scoped rule entering context on a file-match is an ADDED
    context-delta item; classify those and count them as rule_invocations:<rule>, so a dashboard
    can rank rules by carry cost filtered to zero invocations (the same <rule> suffix as carry cost).
    """

    _BASE_TS = "2026-01-01T00:00:00Z"

    def _reminder_msg(self, *rule_basenames: str) -> str:
        # A genuine injection: the rule header(s) sit INSIDE a <system-reminder>.
        headers = "\n".join(
            f"Contents of /repo/.claude/rules/{name}:\n# heading\nbody" for name in rule_basenames
        )
        content = f"<system-reminder>\n{headers}\n</system-reminder>"
        return json.dumps({"role": "user", "content": content})

    def _quote_msg(self, rule_basename: str) -> str:
        # A tool-result / diff that merely QUOTES the header, not wrapped in a reminder.
        content = f"tool output: Contents of /repo/.claude/rules/{rule_basename}: ..."
        return json.dumps({"role": "user", "content": content})

    def test_load_scoped_rules_returns_only_glob_scoped(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / "shared" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "metadata.yml").write_text(
            "code-quality:\n  globs: '**/*.py'\n  alwaysApply: false\n"
            "security:\n  alwaysApply: true\n"
            "operational-gotchas:\n  globs: '**'\n  alwaysApply: false\n",
            encoding="utf-8",
        )

        assert load_scoped_rules(tmp_path) == {"code-quality.md"}

    def test_load_scoped_rules_missing_metadata_is_empty(self, tmp_path: Path) -> None:
        assert load_scoped_rules(tmp_path) == set()

    def test_label_tags_added_message_injecting_a_scoped_rule(self) -> None:
        curr = [ContextItem("messages", "msg[3]:user", self._reminder_msg("code-quality.md"))]
        added = [{"category": "messages", "name": "msg[3]:user", "tokens": 10}]

        _label_rule_injections(added, curr, {"code-quality.md"})

        assert added[0]["rules"] == ["code-quality.md"]

    def test_label_collects_all_rules_in_one_reminder(self) -> None:
        # Editing a test file injects several scoped rules in ONE reminder; all must be counted.
        curr = [
            ContextItem(
                "messages",
                "msg[3]:user",
                self._reminder_msg("python-style.md", "pytest-conventions.md"),
            )
        ]
        added = [{"category": "messages", "name": "msg[3]:user", "tokens": 10}]

        _label_rule_injections(added, curr, {"python-style.md", "pytest-conventions.md"})

        assert added[0]["rules"] == ["pytest-conventions.md", "python-style.md"]

    def test_label_ignores_header_quoted_outside_a_reminder(self) -> None:
        # A tool-result quoting the header is not a real injection and must not be counted.
        curr = [ContextItem("messages", "msg[3]:user", self._quote_msg("code-quality.md"))]
        added = [{"category": "messages", "name": "msg[3]:user", "tokens": 10}]

        _label_rule_injections(added, curr, {"code-quality.md"})

        assert "rules" not in added[0]

    def test_label_ignores_an_unscoped_rule_mention(self) -> None:
        curr = [ContextItem("messages", "msg[3]:user", self._reminder_msg("security.md"))]
        added = [{"category": "messages", "name": "msg[3]:user", "tokens": 10}]

        _label_rule_injections(added, curr, {"code-quality.md"})

        assert "rules" not in added[0]

    def test_label_noop_when_no_scoped_rules(self) -> None:
        curr = [ContextItem("messages", "msg[3]:user", self._reminder_msg("code-quality.md"))]
        added = [{"category": "messages", "name": "msg[3]:user", "tokens": 10}]

        _label_rule_injections(added, curr, set())

        assert "rules" not in added[0]

    def _delta_event(self, *rules: str) -> dict:
        added = [{"category": "messages", "name": "m", "tokens": 1, "rules": list(rules)}]
        return {"body": {"id": "e", "metadata": {"context_delta": {"added": added}}}}

    def test_build_rule_invocation_scores_counts_per_rule(self) -> None:
        batch = [
            self._delta_event("code-quality.md", "python-style.md"),
            self._delta_event("code-quality.md"),
        ]

        scores = build_rule_invocation_scores(SPOKE, batch, base_ts=self._BASE_TS)

        by_name = {s["body"]["name"]: s["body"]["value"] for s in scores}
        assert by_name == {
            "rule_invocations:code-quality.md": 2,
            "rule_invocations:python-style.md": 1,
        }

    def test_invocation_scores_are_trace_level_numeric(self) -> None:
        scores = build_rule_invocation_scores(
            SPOKE, [self._delta_event("code-quality.md")], base_ts=self._BASE_TS
        )

        body = scores[0]["body"]
        assert body["dataType"] == "NUMERIC"
        assert body["traceId"] == trace_id_for(SPOKE)
        assert "observationId" not in body

    def test_no_invocation_scores_without_rule_labels(self) -> None:
        event = {
            "body": {
                "id": "e",
                "metadata": {"context_delta": {"added": [{"category": "messages", "tokens": 1}]}},
            }
        }

        assert build_rule_invocation_scores(SPOKE, [event], base_ts=self._BASE_TS) == []


class TestEnforcementFireScores:
    """#232 subtask enforce: count hook enforcement blocks per (event:tool) SURFACE as
    enforcement_fires:<event>:<tool>.

    Per-script hook identity is blocked upstream (#110 AC3) — Claude Code emits one
    hook_execution_complete per (event x tool) with hook_name '<event>:<tool>' — and every surface
    is guarded by many hooks across different rules + workflow mechanics, so a block cannot be
    attributed to one rule. The surface is the honest granularity; each blocked call counts once.
    """

    _BASE_TS = "2026-01-01T00:00:00Z"

    def _hook_event(self, hook_name: str, num_blocking: int | str) -> dict:
        return {
            "body": {
                "id": "h",
                "name": "hook_execution_complete",
                "metadata": {"hook_name": hook_name, "num_blocking": num_blocking},
            }
        }

    def _scores(self, batch: list[dict]) -> list[dict]:
        return build_enforcement_fire_scores(SPOKE, batch, base_ts=self._BASE_TS)

    def _by_name(self, scores: list[dict]) -> dict[str, float]:
        return {s["body"]["name"]: s["body"]["value"] for s in scores}

    def test_block_scores_the_surface(self) -> None:
        scores = self._scores([self._hook_event("PreToolUse:Edit", 1)])

        assert self._by_name(scores) == {"enforcement_fires:PreToolUse:Edit": 1}

    def test_no_score_when_nothing_blocked(self) -> None:
        assert self._scores([self._hook_event("PreToolUse:Edit", 0)]) == []

    def test_stringified_num_blocking_still_counts(self) -> None:
        # OTel span attributes are frequently flattened to strings during ingestion.
        scores = self._scores([self._hook_event("PreToolUse:Edit", "1")])

        assert self._by_name(scores) == {"enforcement_fires:PreToolUse:Edit": 1}

    def test_bash_surface_is_scored_too(self) -> None:
        scores = self._scores([self._hook_event("PreToolUse:Bash", 1)])

        assert self._by_name(scores) == {"enforcement_fires:PreToolUse:Bash": 1}

    def test_two_hooks_blocking_one_call_count_once(self) -> None:
        # num_blocking=2 means two hooks blocked the SAME tool call — that is one blocked call.
        scores = self._scores([self._hook_event("PreToolUse:Edit", 2)])

        assert self._by_name(scores) == {"enforcement_fires:PreToolUse:Edit": 1}

    def test_repeated_surface_blocks_sum(self) -> None:
        batch = [
            self._hook_event("PreToolUse:Edit", 1),
            self._hook_event("PreToolUse:Edit", 3),
            self._hook_event("PostToolUse:Write", 1),
        ]

        scores = self._scores(batch)

        assert self._by_name(scores) == {
            "enforcement_fires:PreToolUse:Edit": 2,
            "enforcement_fires:PostToolUse:Write": 1,
        }

    def test_enforcement_scores_are_trace_level_numeric(self) -> None:
        scores = self._scores([self._hook_event("PreToolUse:Edit", 1)])

        body = scores[0]["body"]
        assert body["dataType"] == "NUMERIC"
        assert body["traceId"] == trace_id_for(SPOKE)
        assert "observationId" not in body

    def test_non_hook_events_are_ignored(self) -> None:
        other = {"body": {"id": "x", "name": "tool:Edit", "metadata": {"num_blocking": 9}}}

        assert self._scores([other]) == []


class TestContainerRollups:
    """Every container node (and the synthetic root) carries a subtree token rollup."""

    def test_interaction_container_rolls_up_its_subtree(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        rollup = _by_orig(batch, "trace-int", "i1")["body"]["metadata"]["rollup"]
        assert rollup == {
            "reused": 900,
            "written": 300,
            "input": 120,
            "output": 45,
            "duration": _dur(1_000, {"llm_request": 1_000}),
        }

    def test_synthetic_root_rolls_up_the_whole_tree(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"] == {
            "reused": 900,
            "written": 300,
            "input": 120,
            "output": 45,
            "duration": _dur(1_000, {"llm_request": 1_000}),
        }

    def test_leaf_generation_keeps_metadata_verbatim_without_rollup(self) -> None:
        # g1 is a leaf (no children) so it is NOT a container and is left untouched.
        batch = build_batch(_traces(), SPOKE)

        metadata = _by_orig(batch, "trace-int", "g1")["body"]["metadata"]
        assert metadata == {"kind": "turn", "rollup": {"input": 120}}

    def test_container_written_includes_the_1h_cache_write_tier(self) -> None:
        # Issue #97 regression: a generation whose usageDetails split cache writes across
        # the 5m (cache_creation_input_tokens) and 1h (input_cache_creation_1h) tiers must
        # roll up into a ``written`` that totals both, not just the 5m tier.
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        gen = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            parent="i1",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 120,
                "input_cache_creation_1h": 180,
            },
        )

        batch = build_batch([("trace-int", [interaction, gen])], SPOKE)

        assert _by_orig(batch, "trace-int", "i1")["body"]["metadata"]["rollup"]["written"] == 300

    def test_nested_subagent_container_sums_only_its_descendants(self) -> None:
        # tool:Agent over a sub-agent interaction holding one generation.
        agent = _obs("a1", "tool:Agent", parent=None)
        sub = _obs("s1", "claude_code.interaction", parent="a1")
        gen = _obs(
            "sg1",
            "llm_request",
            type_="GENERATION",
            parent="s1",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 2,
            },
        )

        batch = build_batch([("trace-a", [agent, sub, gen])], SPOKE)

        assert _by_orig(batch, "trace-a", "a1")["body"]["metadata"]["rollup"] == {
            "reused": 7,
            "written": 2,
            "input": 10,
            "output": 4,
            "duration": _dur(0),
        }


def _timed_traces() -> list[tuple[str, list[dict]]]:
    """One fully-timed turn: a 4s LLM call, a 3s tool, a 2s hook, and a 1s unattributed gap."""
    interaction = _obs(
        "i1",
        "claude_code.interaction",
        parent=None,
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-02T00:00:10Z",
    )
    gen = _obs(
        "g1",
        "claude_code.llm_request",
        type_="GENERATION",
        parent="i1",
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-02T00:00:04Z",
    )
    tool = _obs(
        "t1",
        "tool:Bash",
        parent="i1",
        startTime="2026-01-02T00:00:04Z",
        endTime="2026-01-02T00:00:07Z",
        metadata={"attributes": {"tool_use_id": "tu-1"}},
    )
    hook = _obs(
        "h1",
        "post-tool.sh",
        parent="i1",
        startTime="2026-01-02T00:00:07Z",
        endTime="2026-01-02T00:00:09Z",
    )
    return [("tr", [interaction, gen, tool, hook])]


class TestDurationRollups:
    """#128: every container's ``metadata.rollup`` also carries a ``duration`` whose
    exclusive-time components (each node's own duration minus its direct children's,
    clamped >= 0, attributed to exactly one class bucket) sum to the observed subtree
    wall-clock. Mirrors the token-rollup subtree-sum pattern."""

    def test_interaction_components_sum_to_wall_clock(self) -> None:
        batch = build_batch(_timed_traces(), SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(
            10_000, {"llm_request": 4_000, "tool": 3_000, "hook": 2_000, "self": 1_000}
        )
        assert sum(duration["components"].values()) == duration["total_ms"]

    def test_root_spans_whole_spoke_and_inter_turn_idle_is_self(self) -> None:
        # Two childless turns with a 10s gap between them: the synthetic root (no endTime of
        # its own) spans min start -> max end, and the un-attributed inter-turn idle — the
        # human-wait between turns — lands in its own ``self`` bucket.
        turn_a = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        turn_b = _obs(
            "i2",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:20Z",
            endTime="2026-01-02T00:00:30Z",
        )

        batch = build_batch([("tr-a", [turn_a]), ("tr-b", [turn_b])], SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"]["duration"] == _dur(
            30_000, {"turn": 20_000, "self": 10_000}
        )

    def test_blocked_on_user_time_counts_as_wait_not_tool(self) -> None:
        # The blocked_on_user sub-span folds onto its tool (#100) as blocked_on_user_ms; the
        # duration rollup carves that human-wait out of the tool bucket into ``wait``.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        tool = _obs(
            "t1",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        blocked = _obs(
            "b1",
            "claude_code.tool.blocked_on_user",
            parent="t1",
            startTime="2026-01-02T00:00:02Z",
            endTime="2026-01-02T00:00:08Z",
        )

        batch = build_batch([("tr", [interaction, tool, blocked])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 4_000, "wait": 6_000})

    def test_gate_script_counts_as_wait(self) -> None:
        turn = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        gate = _obs(
            "gt",
            "script:gate",
            parent=None,
            startTime="2026-01-02T00:00:10Z",
            endTime="2026-01-02T00:00:20Z",
        )

        batch = build_batch([("tr-int", [turn]), ("tr-gate", [gate])], SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"]["duration"] == _dur(
            20_000, {"turn": 10_000, "wait": 10_000}
        )

    def test_non_gate_script_counts_as_script(self) -> None:
        script = _obs(
            "sc",
            "script:worktree-land",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"workflow.kind": "script", "workflow.phase": "land"}},
        )

        batch = build_batch([("tr", [script])], SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"]["duration"] == _dur(5_000, {"script": 5_000})

    def test_leaf_nodes_carry_no_duration(self) -> None:
        batch = build_batch(_timed_traces(), SPOKE)

        metadata = _by_orig(batch, "tr", "g1")["body"].get("metadata") or {}
        assert "rollup" not in metadata

    def test_untimed_tree_yields_zero_duration(self) -> None:
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        tool = _obs("t1", "tool:Bash", parent="i1")

        batch = build_batch([("tr", [interaction, tool])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(0)

    def test_child_exceeding_parent_clamps_self_to_zero(self) -> None:
        # Malformed timing (child outlives its parent): total stays the observed container
        # wall-clock, the child's full time is still attributed, and self clamps to 0 rather
        # than going negative.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:05Z",
        )
        gen = _obs(
            "g1",
            "claude_code.llm_request",
            type_="GENERATION",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:08Z",
        )

        batch = build_batch([("tr", [interaction, gen])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(5_000, {"llm_request": 8_000})

    def test_parallel_children_keep_the_gap_and_book_full_span_time(self) -> None:
        # Two tools run CONCURRENTLY (00:00-00:06) inside a 10s turn. The union of the
        # children covers 6s, so the turn's own gap stays 4s — never erased by summing the
        # overlap twice. Class buckets are span-time: under concurrency they may sum past
        # the wall-clock (12s of tool time in a 10s turn), while gap buckets stay true.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        tool_a = _obs(
            "t1",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:06Z",
        )
        tool_b = _obs(
            "t2",
            "tool:Read",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:06Z",
        )

        batch = build_batch([("tr", [interaction, tool_a, tool_b])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 12_000, "self": 4_000})

    def test_mixed_timestamp_formats_compare_parsed_not_lexicographic(self) -> None:
        # turn_a is stamped in +02:00 (06:00Z-06:30Z, the true earliest); turn_b in Z
        # (07:00Z-07:30Z). A lexicographic min would pick turn_b's "07:..." string as the
        # earliest start and undercount the root span by an hour.
        turn_a = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T08:00:00+02:00",
            endTime="2026-01-02T08:30:00+02:00",
        )
        turn_b = _obs(
            "i2",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T07:00:00Z",
            endTime="2026-01-02T07:30:00Z",
        )

        batch = build_batch([("tr-a", [turn_a]), ("tr-b", [turn_b])], SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"]["duration"] == _dur(
            5_400_000, {"turn": 3_600_000, "self": 1_800_000}
        )

    def test_unmatched_blocked_on_user_span_counts_as_wait(self) -> None:
        # A blocked-on-user span whose tool never materialized (denied/cancelled) keeps its
        # node (#110) instead of folding; its time is human wait, not "other".
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        blocked = _obs(
            "b1",
            "claude_code.tool.blocked_on_user",
            parent="i1",
            startTime="2026-01-02T00:00:02Z",
            endTime="2026-01-02T00:00:08Z",
        )

        batch = build_batch([("tr", [interaction, blocked])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"wait": 6_000, "self": 4_000})

    def test_flat_kind_hook_counts_as_hook(self) -> None:
        # Hook detection reuses _is_hook, including its flat top-level kind == "hook"
        # fallback (no .sh name, no workflow.kind attribute).
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        hook = _obs(
            "h1",
            "guard",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:03Z",
            metadata={"kind": "hook"},
        )

        batch = build_batch([("tr", [interaction, hook])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"hook": 3_000, "self": 7_000})

    def test_non_numeric_blocked_on_user_ms_is_ignored_not_fatal(self) -> None:
        # A verbatim-copied tool span may carry blocked_on_user_ms in a shape the #100 fold
        # never writes (e.g. a string); the carve-out must degrade to 0, not crash the build.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        tool = _obs(
            "t1",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
            metadata={"blocked_on_user_ms": "6000.0"},
        )

        batch = build_batch([("tr", [interaction, tool])], SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 10_000})

    def test_token_rollup_keys_unchanged_alongside_duration(self) -> None:
        # The duration extends the existing token rollup in place — same metadata.rollup
        # object, token keys untouched.
        batch = build_batch(_traces(), SPOKE)

        rollup = _by_orig(batch, "trace-int", "i1")["body"]["metadata"]["rollup"]
        assert rollup == {
            "reused": 900,
            "written": 300,
            "input": 120,
            "output": 45,
            "duration": _dur(1_000, {"llm_request": 1_000}),
        }


SESSION = "sess-idem"


def _stub_get(fetched: Sequence[tuple[str, str | None, list[dict]]]):
    """Stub a Langfuse ``get`` over one page of traces plus each trace's observations.

    Args:
        fetched: Each session trace as ``(trace_id, name, observations)``.

    Returns:
        A path-to-JSON callable matching the spoke-tree fetch paths.
    """
    session = urllib.parse.quote(SESSION)
    pages: dict[str, dict] = {
        f"/traces?sessionId={session}&limit=100&page=1": {
            "data": [{"id": tid, "name": name} for tid, name, _ in fetched],
            "meta": {"totalPages": 1},
        }
    }
    for tid, _name, observations in fetched:
        pages[f"/observations?traceId={tid}&limit=100&page=1"] = {
            "data": observations,
            "meta": {"totalPages": 1},
        }
    return lambda path: pages[path]


class TestExcludesOwnOutput:
    """A re-run must not re-source the synthesizer's own assembled trace (idempotency)."""

    def test_fetch_session_drops_prior_synthetic_traces(self) -> None:
        target_id = trace_id_for(SESSION)
        native = [(tid, None, obs) for tid, obs in _traces()]
        # The assembled trace reappears in the session: once under the deterministic target
        # id, and once (defensively) under an older id but the spoke-tree: name.
        prior_self = (target_id, f"spoke-tree:{SESSION}", [_obs("spokeroot-x", f"spoke:{SESSION}")])
        prior_old = ("spoketree-legacy", f"spoke-tree:{SESSION}", [_obs("tree-y", "Bash")])

        fetched = fetch_session(SESSION, _stub_get(native + [prior_self, prior_old]))

        # Only the native traces survive; the synthetic observations were never sourced.
        assert fetched == [(tid, obs) for tid, _name, obs in native]

    def test_fetch_session_drops_prior_view_b_trace(self) -> None:
        # #156: View B (spokecycle-<id>, name spoke-cycle:<id>) also carries
        # sessionId == spoke_run_id, so a rebuild would source its ~2,100 copies as if
        # native and multiply them. It must be self-excluded exactly like View A.
        native = [(tid, None, obs) for tid, obs in _traces()]
        prior_b = (
            cycle_trace_id_for(SESSION),
            f"spoke-cycle:{SESSION}",
            [_obs("cyc-y", "Bash")],
        )
        prior_b_old = ("spokecycle-legacy", f"spoke-cycle:{SESSION}", [_obs("cyc-z", "Bash")])

        fetched = fetch_session(SESSION, _stub_get([*native, prior_b, prior_b_old]))

        assert fetched == [(tid, obs) for tid, _name, obs in native]

    def test_is_own_output_excludes_view_b_by_id(self) -> None:
        trace = {"id": cycle_trace_id_for(SESSION), "name": None}

        assert _is_own_output(trace, SESSION) is True

    def test_is_own_output_excludes_view_b_by_name(self) -> None:
        trace = {"id": "spokecycle-legacy", "name": f"spoke-cycle:{SESSION}"}

        assert _is_own_output(trace, SESSION) is True

    def test_is_own_output_keeps_a_native_trace(self) -> None:
        trace = {"id": "trace-native", "name": "claude_code.interaction"}

        assert _is_own_output(trace, SESSION) is False

    def test_rerun_with_prior_output_in_session_is_idempotent(self) -> None:
        target_id = trace_id_for(SESSION)
        native = [(tid, None, obs) for tid, obs in _traces()]

        # Run 1: the session holds only the native traces.
        first = build_batch(fetch_session(SESSION, _stub_get(native)), SESSION)

        # Run 2: the session now ALSO holds run 1's output (same target id) and an older-id
        # spoke-tree trace whose spans WOULD become extra copies if they were sourced.
        prior_self = (target_id, f"spoke-tree:{SESSION}", [_obs("spokeroot-x", f"spoke:{SESSION}")])
        prior_old = ("spoketree-legacy", f"spoke-tree:{SESSION}", [_obs("tree-y", "Bash")])
        second = build_batch(
            fetch_session(SESSION, _stub_get(native + [prior_self, prior_old])), SESSION
        )

        # Same node set and no growth: the tree does not multiply across re-runs.
        assert {event["id"] for event in second} == {event["id"] for event in first}
        assert len(second) == len(first)

    def test_rerun_with_prior_output_does_not_double_count_durations(self) -> None:
        # #128: run 2's session also holds run 1's assembled output, whose synthetic root span
        # covers the whole spoke — sourcing it would double every duration component. The
        # rebuild must yield the exact same duration rollup as the first build.
        target_id = trace_id_for(SESSION)
        native = [(tid, None, obs) for tid, obs in _timed_traces()]
        first = build_batch(fetch_session(SESSION, _stub_get(native)), SESSION)
        prior_self = (
            target_id,
            f"spoke-tree:{SESSION}",
            [
                _obs(
                    "spokeroot-x",
                    f"spoke:{SESSION}",
                    startTime="2026-01-02T00:00:00Z",
                    endTime="2026-01-02T01:00:00Z",
                )
            ],
        )

        second = build_batch(fetch_session(SESSION, _stub_get(native + [prior_self])), SESSION)

        root_first = next(e for e in first if e["id"] == root_id_for(SESSION))
        root_second = next(e for e in second if e["id"] == root_id_for(SESSION))
        assert (
            root_second["body"]["metadata"]["rollup"]["duration"]
            == root_first["body"]["metadata"]["rollup"]["duration"]
        )


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


def _traces_page(*ids: str) -> dict:
    """A single-page /traces response listing the given trace ids."""
    return {"data": [{"id": tid} for tid in ids], "meta": {"totalPages": 1}}


class TestPurgeOwnViews:
    """#156: --rebuild deletes the two deterministic view traces, then polls them gone."""

    def test_deletes_both_view_ids_then_polls_until_gone(self) -> None:
        a_id, b_id = trace_id_for(SESSION), cycle_trace_id_for(SESSION)
        deleted: list[list[str]] = []
        sleeps: list[float] = []
        # First poll still lists both views (async delete not yet applied); second is clean.
        responses = iter([_traces_page(a_id, b_id, "native"), _traces_page("native")])

        purge_own_views(
            SESSION,
            lambda _path: next(responses),
            deleted.append,
            sleep=sleeps.append,
        )

        assert deleted == [[a_id, b_id]]
        assert len(sleeps) == 1  # slept once between the present→gone polls

    def test_returns_immediately_when_views_already_absent(self) -> None:
        deleted: list[list[str]] = []
        sleeps: list[float] = []

        purge_own_views(
            SESSION,
            lambda _path: _traces_page("native"),
            deleted.append,
            sleep=sleeps.append,
        )

        assert len(deleted) == 1  # delete is always issued (idempotent no-op server-side)
        assert sleeps == []  # already gone → no polling wait

    def test_raises_when_views_never_disappear(self) -> None:
        a_id = trace_id_for(SESSION)

        with pytest.raises(RuntimeError, match="not deleted"):
            purge_own_views(
                SESSION,
                lambda _path: _traces_page(a_id),
                lambda _ids: None,
                sleep=lambda _s: None,
                attempts=3,
            )


class TestRebuildIdempotency:
    """#156: a rebuild with prior views in the store is byte-identical to a fresh build."""

    def test_build_batch_byte_identical_with_prior_views_present(self) -> None:
        native = [(tid, None, obs) for tid, obs in _traces()]
        prior_a = (trace_id_for(SESSION), f"spoke-tree:{SESSION}", [_obs("spokeroot-x", "spoke:x")])
        prior_b = (cycle_trace_id_for(SESSION), f"spoke-cycle:{SESSION}", [_obs("cyc-y", "Bash")])

        fresh = build_batch(fetch_session(SESSION, _stub_get(native)), SESSION)
        rebuilt = build_batch(
            fetch_session(SESSION, _stub_get([*native, prior_a, prior_b])), SESSION
        )

        assert json.dumps(rebuilt) == json.dumps(fresh)

    def test_build_cycle_batch_byte_identical_with_prior_views_present(self) -> None:
        native = [(tid, None, obs) for tid, obs in _traces()]
        prior_a = (trace_id_for(SESSION), f"spoke-tree:{SESSION}", [_obs("spokeroot-x", "spoke:x")])
        prior_b = (cycle_trace_id_for(SESSION), f"spoke-cycle:{SESSION}", [_obs("cyc-y", "Bash")])

        fresh = build_cycle_batch(fetch_session(SESSION, _stub_get(native)), SESSION)
        rebuilt = build_cycle_batch(
            fetch_session(SESSION, _stub_get([*native, prior_a, prior_b])), SESSION
        )

        assert json.dumps(rebuilt) == json.dumps(fresh)


class TestSchemaRev:
    """#156: both view trace-create bodies stamp a schema_rev so a consumer can tell
    which builder generation produced a stored view."""

    def test_view_a_trace_create_carries_schema_rev(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        assert isinstance(batch[0]["body"]["metadata"]["schema_rev"], int)

    def test_view_b_trace_create_carries_schema_rev(self) -> None:
        batch = build_cycle_batch(_traces(), SPOKE)

        assert isinstance(batch[0]["body"]["metadata"]["schema_rev"], int)

    def test_both_views_stamp_the_same_schema_rev(self) -> None:
        a_batch = build_batch(_traces(), SPOKE)
        b_batch = build_cycle_batch(_traces(), SPOKE)

        assert (
            a_batch[0]["body"]["metadata"]["schema_rev"]
            == b_batch[0]["body"]["metadata"]["schema_rev"]
        )

    def test_schema_rev_survives_mode_lane_tagging(self) -> None:
        # apply_mode_lane_tags merges mode/lane into the same body.metadata via setdefault;
        # schema_rev must coexist, not be clobbered.
        batch = build_batch(_traces(), SPOKE)

        apply_mode_lane_tags(batch, "afk", "spoke")

        metadata = batch[0]["body"]["metadata"]
        assert isinstance(metadata["schema_rev"], int)
        assert metadata["mode"] == "afk"
        assert metadata["lane"] == "spoke"


def _tool_obs(obs_id: str, name: str, tool_use_id: str, **extra) -> dict:
    """Build a source observation carrying a tool-call id under metadata["attributes"]."""
    return _obs(
        obs_id, name, parent="i1", metadata={"attributes": {"tool_use_id": tool_use_id}}, **extra
    )


def _write_transcript(root: Path, records: list[dict]) -> None:
    """Write a Claude Code transcript (one JSON record per line) under a project subdir."""
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / "session.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _tool_use(tool_use_id: str, name: str, tool_input: dict) -> dict:
    """Build an assistant transcript line carrying one tool_use block."""
    block = {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def _tool_result(tool_use_id: str, content: object) -> dict:
    """Build a user transcript line carrying one tool_result block."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    return {"type": "user", "message": {"content": [block]}}


def _ledger_obs(obs_id: str, name: str, tool_use_id: str, *, start: str, end: str) -> dict:
    """A root-level tool:Task* span carrying its tool_use_id and a time window."""
    return _obs(
        obs_id,
        name,
        parent=None,
        startTime=start,
        endTime=end,
        metadata={"attributes": {"tool_use_id": tool_use_id}},
    )


def _marker(obs_id: str, phase: str, start: str, *, end: str | None = None) -> dict:
    """A solo-cycle marker span (kind=step, name=step:<phase>) as cycle-step-mark.sh emits it."""
    return _obs(
        obs_id,
        f"step:{phase}",
        parent=None,
        startTime=start,
        endTime=end or start,
        metadata={"attributes": {"workflow.kind": "step", "workflow.phase": phase}},
    )


def _step_node(batch: list[dict]) -> dict | None:
    """Return the first synthetic step node in a batch, or None when there is none."""
    return next((e for e in batch if e["id"].startswith(_STEP_PREFIX)), None)


def _only_step(batch: list[dict]) -> dict:
    """Return the single synthetic step node, asserting it exists."""
    step = _step_node(batch)
    assert step is not None
    return step


class TestBuildStepWindows:
    """#100: derive cycle-step windows from the TaskCreate/TaskUpdate ledger."""

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: failing test", "description": "x"},
                "Task #1 created successfully: S1 RED: failing test",
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "Updated task #1"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "Updated task #1"),
        }

    def _traces(self) -> list[tuple[str, list[dict]]]:
        create = _ledger_obs(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )
        started = _ledger_obs(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01Z",
        )
        done = _ledger_obs(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            start="2026-01-02T00:00:09Z",
            end="2026-01-02T00:00:10Z",
        )
        return [("tr", [create, started, done])]

    def test_pairs_create_subject_with_update_window(self) -> None:
        windows = build_step_windows(self._traces(), self._content())

        assert len(windows) == 1
        win = windows[0]
        assert win.subject == "S1 RED: failing test"
        # window = in_progress start → completed end.
        assert win.start == "2026-01-02T00:00:01Z"
        assert win.end == "2026-01-02T00:00:10Z"
        assert win.status == "completed"

    def test_non_ledger_spoke_yields_no_windows(self) -> None:
        assert build_step_windows(_traces(), {}) == []

    def test_task_id_parsed_from_block_list_output(self) -> None:
        # A TaskCreate result can arrive as a list of content blocks, not a bare string.
        content = {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: blocks"},
                [{"type": "text", "text": "Task #1 created successfully: S1 RED: blocks"}],
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }
        create = _ledger_obs(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )
        started = _ledger_obs(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01Z",
        )
        done = _ledger_obs(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            start="2026-01-02T00:00:09Z",
            end="2026-01-02T00:00:10Z",
        )

        windows = build_step_windows([("tr", [create, started, done])], content)

        assert len(windows) == 1
        assert windows[0].subject == "S1 RED: blocks"

    def test_create_without_an_in_progress_update_is_skipped(self) -> None:
        # A task that was created but never started has no window to draw.
        content = {
            "tu-c1": ToolContent({"subject": "S2 GREEN"}, "Task #2 created successfully: S2 GREEN")
        }
        create = _ledger_obs(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )

        assert build_step_windows([("tr", [create])], content) == []


class TestBuildCycleWindows:
    """#235: the cycle spine prefers the mechanical marker spans; the ledger only labels."""

    def _markers(self) -> list[tuple[str, list[dict]]]:
        # A trailing generation so the final (push) window has real width beyond its marker start.
        trailing = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:11Z",
            endTime="2026-01-02T00:00:12Z",
        )
        return [
            (
                "tr",
                [
                    _marker("m1", "red", "2026-01-02T00:00:00Z"),
                    _marker("m2", "green", "2026-01-02T00:00:05Z"),
                    _marker("m3", "review", "2026-01-02T00:00:08Z"),
                    _marker("m4", "push", "2026-01-02T00:00:10Z"),
                    trailing,
                ],
            )
        ]

    def test_marker_only_yields_one_window_per_phase_bounded_by_next_marker(self) -> None:
        windows = build_cycle_windows(self._markers(), {})

        assert [_step_phase(w.subject) for w in windows] == ["RED", "GREEN", "REVIEW", "PUSH"]
        assert [w.start for w in windows] == [
            "2026-01-02T00:00:00Z",
            "2026-01-02T00:00:05Z",
            "2026-01-02T00:00:08Z",
            "2026-01-02T00:00:10Z",
        ]
        # Each window ends where the next marker starts; the last clamps to the latest activity.
        assert windows[0].end == "2026-01-02T00:00:05Z"
        assert windows[3].end == "2026-01-02T00:00:12Z"

    def test_marker_window_borrows_an_overlapping_same_phase_ledger_subject(self) -> None:
        content = {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: failing test"},
                "Task #1 created successfully: S1 RED: failing test",
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }
        create = _ledger_obs(
            "tc1", "tool:TaskCreate", "tu-c1", start="2026-01-02T00:00:00Z", end="0"
        )
        started = _ledger_obs(
            "tu1", "tool:TaskUpdate", "tu-u1", start="2026-01-02T00:00:00Z", end="0"
        )
        done = _ledger_obs(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            start="2026-01-02T00:00:04Z",
            end="2026-01-02T00:00:04Z",
        )
        traces = [
            (
                "tr",
                [
                    create,
                    started,
                    done,
                    _marker("m1", "red", "2026-01-02T00:00:01Z"),
                    _marker("m2", "green", "2026-01-02T00:00:06Z"),
                ],
            )
        ]

        windows = build_cycle_windows(traces, content)

        assert windows[0].subject == "S1 RED: failing test"  # borrowed from the ledger
        assert _step_phase(windows[1].subject) == "GREEN"  # no ledger match -> generic phase label

    def test_falls_back_to_ledger_windows_when_no_markers(self) -> None:
        content = {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: failing test"},
                "Task #1 created successfully: S1 RED: failing test",
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }
        create = _ledger_obs(
            "tc1", "tool:TaskCreate", "tu-c1", start="2026-01-02T00:00:00Z", end="0"
        )
        started = _ledger_obs(
            "tu1", "tool:TaskUpdate", "tu-u1", start="2026-01-02T00:00:01Z", end="0"
        )
        done = _ledger_obs(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            start="2026-01-02T00:00:09Z",
            end="2026-01-02T00:00:10Z",
        )
        ledger = [("tr", [create, started, done])]

        assert build_cycle_windows(ledger, content) == build_step_windows(ledger, content)

    def test_no_markers_and_no_ledger_yields_no_windows(self) -> None:
        bare = [("tr", [_obs("t1", "tool:Bash", parent=None, startTime="2026-01-02T00:00:00Z")])]
        assert build_cycle_windows(bare, {}) == []

    def test_single_marker_window_clamps_to_latest_activity(self) -> None:
        traces = [
            (
                "tr",
                [
                    _marker("m1", "green", "2026-01-02T00:00:03Z"),
                    _obs(
                        "t1",
                        "tool:Bash",
                        parent=None,
                        startTime="2026-01-02T00:00:07Z",
                        endTime="2026-01-02T00:00:08Z",
                    ),
                ],
            )
        ]

        windows = build_cycle_windows(traces, {})

        assert len(windows) == 1
        assert windows[0].start == "2026-01-02T00:00:03Z"
        assert windows[0].end == "2026-01-02T00:00:08Z"

    def test_duplicate_phase_markers_yield_distinct_windows(self) -> None:
        # Two GREEN markers (subtask A, subtask B) each open their own window.
        traces = [
            (
                "tr",
                [
                    _marker("m1", "green", "2026-01-02T00:00:01Z"),
                    _marker("m2", "green", "2026-01-02T00:00:09Z", end="2026-01-02T00:00:10Z"),
                ],
            )
        ]

        windows = build_cycle_windows(traces, {})

        assert [w.task_id for w in windows] == ["marker0", "marker1"]
        assert [_step_phase(w.subject) for w in windows] == ["GREEN", "GREEN"]

    def test_malformed_marker_without_a_phase_never_borrows_a_ledger_subject(self) -> None:
        # A kind=step span with no workflow.phase and no step:<phase> label must not word-boundary
        # match (an empty phase) and steal an unrelated ledger subject.
        content = {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: failing test"},
                "Task #1 created successfully: S1 RED: failing test",
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }
        create = _ledger_obs(
            "tc1", "tool:TaskCreate", "tu-c1", start="2026-01-02T00:00:00Z", end="0"
        )
        started = _ledger_obs(
            "tu1", "tool:TaskUpdate", "tu-u1", start="2026-01-02T00:00:00Z", end="0"
        )
        done = _ledger_obs(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        blank_marker = _obs(
            "m1", "weird", parent=None, startTime="2026-01-02T00:00:01Z", metadata={"kind": "step"}
        )

        windows = build_cycle_windows([("tr", [create, started, done, blank_marker])], content)

        assert len(windows) == 1
        assert windows[0].subject != "S1 RED: failing test"


class TestCycleMarkerSpine:
    """#235: a ledger-untouched spoke still yields a full step spine + per-phase scores."""

    _BASE_TS = "2026-01-02T00:00:00Z"

    def _marker_only_traces(self) -> list[tuple[str, list[dict]]]:
        # No TaskCreate/TaskUpdate at all — only the mechanical marker spans plus a token-bearing
        # generation inside the GREEN window (the crash/relaunch case from #225).
        work_gen = _obs(
            "wg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:12Z",
            endTime="2026-01-02T00:00:13Z",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 50,
            },
        )
        return [
            (
                "tr",
                [
                    _marker("m1", "red", "2026-01-02T00:00:02Z"),
                    _marker("m2", "green", "2026-01-02T00:00:10Z"),
                    _marker("m3", "review", "2026-01-02T00:00:25Z"),
                    _marker("m4", "push", "2026-01-02T00:00:30Z"),
                    work_gen,
                ],
            )
        ]

    def test_marker_only_spoke_yields_a_full_four_step_axis(self) -> None:
        cycle = build_cycle_batch(self._marker_only_traces(), SPOKE, {})

        phases = {
            _step_phase(e["body"].get("metadata", {}).get("subject") or e["body"]["name"])
            for e in cycle
            if e["body"]["id"].startswith(_CYCLE_STEP_PREFIX)
        }
        assert {"RED", "GREEN", "REVIEW", "PUSH"} <= phases

    def test_marker_only_spoke_emits_per_phase_cost_scores(self) -> None:
        cycle = build_cycle_batch(self._marker_only_traces(), SPOKE, {})

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        assert "step_cache_write_usd:GREEN" in {s["body"]["name"] for s in scores}

    def test_marker_span_not_rendered_as_orphan_node_in_cycle_view(self) -> None:
        cycle = build_cycle_batch(self._marker_only_traces(), SPOKE, {})

        names = {e["body"].get("name") for e in cycle}
        assert not (names & {"step:red", "step:green", "step:review", "step:push"})

    def test_marker_span_not_rendered_as_orphan_node_in_nested_view(self) -> None:
        batch = build_batch(self._marker_only_traces(), SPOKE, {})

        names = {e["body"].get("name") for e in batch}
        assert not (names & {"step:red", "step:green", "step:review", "step:push"})


class TestStepTotalCostScores:
    """#230: ``step_total_cost_usd:<PHASE>`` windows EVERY generation's true cost into its step.

    Unlike ``step_cache_write_usd`` (cache-write cost only), this sums each generation's full
    Langfuse ``costDetails`` — main-loop ``claude_code.llm_request`` AND ``sub-agent:llm`` — into
    the cycle-step that contains it (nearest step ancestor), so the per-phase scores sum to the
    trace's total cost with only the pre-first-step spend left in a ``:pre`` residual.
    """

    _BASE_TS = "2026-01-02T00:00:00Z"

    def _traces(self) -> list[tuple[str, list[dict]]]:
        # Marker spine: red@02 green@10 review@25 push@30. A pre-step generation, a main-loop
        # generation + a sub-agent (container + sub-agent:llm) in GREEN, and a PUSH generation.
        pre_gen = _obs(
            "pg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:01Z",
            endTime="2026-01-02T00:00:01Z",
            usageDetails={"input": 5},
            costDetails={"total": 0.10},
        )
        main_gen = _obs(
            "mg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:12Z",
            endTime="2026-01-02T00:00:13Z",
            usageDetails={"input": 30, "output": 70},
            # component-only costDetails (no explicit total) — exercises the summed-components path.
            costDetails={"input": 0.30, "output": 0.70},
        )
        sa_container = _obs(
            "sac",
            "sub-agent:code-review",
            parent=None,
            startTime="2026-01-02T00:00:13Z",
            endTime="2026-01-02T00:00:20Z",
        )
        sa_gen = _obs(
            "sag",
            "sub-agent:llm",
            type_="GENERATION",
            parent="sac",
            startTime="2026-01-02T00:00:14Z",
            endTime="2026-01-02T00:00:19Z",
            usageDetails={"input": 200, "output": 40},
            costDetails={"total": 2.00},
        )
        push_gen = _obs(
            "pushg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent=None,
            startTime="2026-01-02T00:00:31Z",
            endTime="2026-01-02T00:00:32Z",
            usageDetails={"input": 8},
            costDetails={"total": 0.50},
        )
        return [
            (
                "tr",
                [
                    _marker("m1", "red", "2026-01-02T00:00:02Z"),
                    _marker("m2", "green", "2026-01-02T00:00:10Z"),
                    _marker("m3", "review", "2026-01-02T00:00:25Z"),
                    _marker("m4", "push", "2026-01-02T00:00:30Z"),
                    pre_gen,
                    main_gen,
                    sa_container,
                    sa_gen,
                    push_gen,
                ],
            )
        ]

    def _by_name(self, scores: list[dict]) -> dict[str, float]:
        return {s["body"]["name"]: s["body"]["value"] for s in scores}

    def test_green_step_sums_main_loop_and_sub_agent_cost(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, {})

        scores = build_step_total_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS)

        # main_gen (0.30 + 0.70) + sa_gen (2.00) both fall in the GREEN window.
        assert self._by_name(scores)["step_total_cost_usd:GREEN"] == pytest.approx(3.00)

    def test_green_score_is_observation_scoped_to_the_green_step(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, {})
        green = _cycle_step(cycle, "step:GREEN")

        scores = build_step_total_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS)

        green_score = next(s for s in scores if s["body"]["name"] == "step_total_cost_usd:GREEN")
        assert green_score["body"]["observationId"] == green["body"]["id"]

    def test_pre_first_step_spend_is_reported_as_pre(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, {})

        scores = build_step_total_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS)

        assert self._by_name(scores)["step_total_cost_usd:pre"] == pytest.approx(0.10)

    def test_phase_scores_sum_to_the_whole_trace_cost(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, {})

        scores = build_step_total_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS)

        total = sum(
            s["body"]["value"]
            for s in scores
            if s["body"]["name"].startswith("step_total_cost_usd:")
        )
        # 0.10 (pre) + 3.00 (green) + 0.50 (push) == every generation's costDetails.
        assert total == pytest.approx(3.60)

    def test_green_step_duration_score_is_the_window_length(self) -> None:
        # GREEN spans green@10 -> review@25 = 15s, dashboardable as a numeric score.
        cycle = build_cycle_batch(self._traces(), SPOKE, {})

        scores = build_step_duration_scores(SPOKE, cycle, base_ts=self._BASE_TS)

        by_name = {s["body"]["name"]: s["body"]["value"] for s in scores}
        assert by_name["step_duration_ms:GREEN"] == 15000


def _root_marker(obs_id: str, name: str, start: str) -> dict:
    """A synthetic-root-level satellite span (marker / lifecycle / script / unmatched hook)."""
    return _obs(obs_id, name, parent=None, startTime=start)


def _ledger_child(
    obs_id: str, name: str, tool_use_id: str, *, parent: str, start: str, end: str
) -> dict:
    """A tool:Task* span nested under an interaction, carrying its tool_use_id and time window."""
    return _obs(
        obs_id,
        name,
        parent=parent,
        startTime=start,
        endTime=end,
        metadata={"attributes": {"tool_use_id": tool_use_id}},
    )


def _steps_by_parent(batch: list[dict]) -> dict[str, str]:
    """Map each step node's parent copy id to the step node's own id."""
    return {
        e["body"]["parentObservationId"]: e["id"] for e in batch if e["id"].startswith(_STEP_PREFIX)
    }


class TestStepGrouping:
    """#113 View A: a ``step:<subject>`` node is inserted INSIDE the interaction, wrapping the
    contiguous run of same-parent siblings between the task's ``in_progress`` and ``completed``
    ``TaskUpdate`` markers (absorbing those two markers and the ``TaskCreate``). The wrap never
    crosses an interaction boundary; a cross-turn task produces one partial wrap per
    anchor-holding interaction; a wrap with zero non-marker siblings is suppressed. Root-level
    satellites are no longer grouped here (View B is the cycle lens).
    """

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _traces(self) -> list[tuple[str, list[dict]]]:
        # One interaction holding the full ledger cycle plus two work spans between the markers.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:30Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01Z",
        )
        work_tool = _ledger_child(
            "wt",
            "tool:Edit",
            "tu-w",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:06Z",
        )
        work_gen = _obs(
            "wg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent="i1",
            startTime="2026-01-02T00:00:07Z",
            endTime="2026-01-02T00:00:08Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        return [("tr", [interaction, create, started, work_tool, work_gen, done])]

    def test_step_node_parented_under_the_interaction_not_root(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        step = _only_step(batch)
        assert step["body"]["name"] == "step:S1 RED: x"
        assert step["body"]["parentObservationId"] == _by_orig(batch, "tr", "i1")["id"]

    def test_in_window_siblings_rehome_under_the_step(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        step = _only_step(batch)
        assert _by_orig(batch, "tr", "wt")["body"]["parentObservationId"] == step["id"]
        assert _by_orig(batch, "tr", "wg")["body"]["parentObservationId"] == step["id"]

    def test_ledger_markers_are_absorbed_under_the_step(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        step = _only_step(batch)
        for oid in ("tc1", "tu1", "tu2"):
            assert _by_orig(batch, "tr", oid)["body"]["parentObservationId"] == step["id"]

    def test_interaction_remains_under_the_synthetic_root(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        assert _by_orig(batch, "tr", "i1")["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_step_node_carries_window_metadata(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        meta = _only_step(batch)["body"]["metadata"]
        assert meta["subject"] == "S1 RED: x"
        assert meta["status"] == "completed"

    def test_step_with_only_ledger_markers_is_suppressed(self) -> None:
        # An interaction holding just the 3 markers (no work spans between) wraps zero siblings.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:30Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )

        batch = build_batch([("tr", [interaction, create, started, done])], SPOKE, self._content())

        assert _step_node(batch) is None

    def test_non_ledger_spoke_emits_no_step_nodes(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        assert _step_node(batch) is None
        assert _by_orig(batch, "trace-int", "i1")["body"]["parentObservationId"] == root_id_for(
            SPOKE
        )

    def test_root_level_satellite_is_not_wrapped(self) -> None:
        # A root-level marker (parent=None) is not an interaction-internal sibling, so View A
        # leaves it at the synthetic root — the cycle lens (View B) is what places it by time.
        traces = self._traces()
        traces[0][1].append(_root_marker("m1", "spoke-push", "2026-01-02T00:00:10Z"))

        batch = build_batch(traces, SPOKE, self._content())

        assert _by_orig(batch, "tr", "m1")["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_sibling_in_a_non_anchor_interaction_is_not_wrapped(self) -> None:
        # The wrap never crosses an interaction boundary: a work span living in a different
        # interaction (which holds no anchor marker) keeps its own interaction parent.
        traces = self._traces()
        other = _obs(
            "iX",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:09Z",
            endTime="2026-01-02T00:00:11Z",
        )
        stray = _ledger_child(
            "wx",
            "tool:Read",
            "tu-x",
            parent="iX",
            start="2026-01-02T00:00:10Z",
            end="2026-01-02T00:00:10Z",
        )
        traces[0][1].extend([other, stray])

        batch = build_batch(traces, SPOKE, self._content())

        assert (
            _by_orig(batch, "tr", "wx")["body"]["parentObservationId"]
            == _by_orig(batch, "tr", "iX")["id"]
        )

    def _cross_turn_content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent({"subject": "S4 GREEN"}, "Task #4 created successfully: S4 GREEN"),
            "tu-u1": ToolContent({"taskId": "4", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "4", "status": "completed"}, "ok"),
        }

    def _cross_turn_traces(self, *, with_b_work: bool = True) -> list[tuple[str, list[dict]]]:
        # Turn A: create + in_progress + a work span. Turn B: (a work span +) completed.
        turn_a = _obs(
            "iA",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="iA",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="iA",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01Z",
        )
        work_a = _ledger_child(
            "wa",
            "tool:Edit",
            "tu-wa",
            parent="iA",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:06Z",
        )
        turn_b = _obs(
            "iB",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:12Z",
            endTime="2026-01-02T00:00:25Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="iB",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        objs = [turn_a, create, started, work_a, turn_b]
        if with_b_work:
            objs.append(
                _ledger_child(
                    "wb",
                    "tool:Edit",
                    "tu-wb",
                    parent="iB",
                    start="2026-01-02T00:00:15Z",
                    end="2026-01-02T00:00:16Z",
                )
            )
        objs.append(done)
        return [("tr", objs)]

    def test_cross_turn_task_wraps_in_each_anchor_interaction(self) -> None:
        batch = build_batch(self._cross_turn_traces(), SPOKE, self._cross_turn_content())

        steps = [e for e in batch if e["id"].startswith(_STEP_PREFIX)]
        assert len(steps) == 2
        assert {e["body"]["parentObservationId"] for e in steps} == {
            _by_orig(batch, "tr", "iA")["id"],
            _by_orig(batch, "tr", "iB")["id"],
        }

    def test_cross_turn_wraps_only_local_siblings(self) -> None:
        batch = build_batch(self._cross_turn_traces(), SPOKE, self._cross_turn_content())

        by_parent = _steps_by_parent(batch)
        assert (
            _by_orig(batch, "tr", "wa")["body"]["parentObservationId"]
            == by_parent[_by_orig(batch, "tr", "iA")["id"]]
        )
        assert (
            _by_orig(batch, "tr", "wb")["body"]["parentObservationId"]
            == by_parent[_by_orig(batch, "tr", "iB")["id"]]
        )

    def test_cross_turn_markers_absorbed_into_their_local_step(self) -> None:
        # Each partial wrap absorbs only the ledger markers physically under it: TaskCreate +
        # in_progress land under turn A's step, completed under turn B's step.
        batch = build_batch(self._cross_turn_traces(), SPOKE, self._cross_turn_content())

        by_parent = _steps_by_parent(batch)
        step_a = by_parent[_by_orig(batch, "tr", "iA")["id"]]
        step_b = by_parent[_by_orig(batch, "tr", "iB")["id"]]
        assert _by_orig(batch, "tr", "tc1")["body"]["parentObservationId"] == step_a
        assert _by_orig(batch, "tr", "tu1")["body"]["parentObservationId"] == step_a
        assert _by_orig(batch, "tr", "tu2")["body"]["parentObservationId"] == step_b

    def test_audit_instant_under_the_interaction_is_not_window_placed(self) -> None:
        # A skill_activated that resolves to the anchor interaction (shared prompt.id) must NOT be
        # pulled into the step by its lagging startTime — it stays directly under the interaction.
        traces = self._traces()
        traces[0][1][0]["metadata"] = {"attributes": {"prompt.id": "p1"}}  # interaction i1
        skill = _audit_event(
            "sk1",
            "skill_activated",
            start="2026-01-02T00:00:06Z",
            **{"prompt.id": "p1", "skill.name": "source-task"},
        )
        traces[0][1].append(skill)

        batch = build_batch(traces, SPOKE, self._content())

        assert (
            _by_orig(batch, "tr", "sk1")["body"]["parentObservationId"]
            == _by_orig(batch, "tr", "i1")["id"]
        )

    def test_cross_turn_interaction_without_local_siblings_is_suppressed(self) -> None:
        # Turn B holds only the completed marker (no work span), so it gets no step node.
        batch = build_batch(
            self._cross_turn_traces(with_b_work=False), SPOKE, self._cross_turn_content()
        )

        steps = [e for e in batch if e["id"].startswith(_STEP_PREFIX)]
        assert len(steps) == 1
        assert steps[0]["body"]["parentObservationId"] == _by_orig(batch, "tr", "iA")["id"]

    def test_innermost_window_wins_within_one_interaction(self) -> None:
        # Two ledger windows overlap inside ONE interaction; a work span inside both nests under
        # the inner (later-starting) step.
        content = {
            "tu-co": ToolContent({"subject": "outer"}, "Task #1 created successfully: outer"),
            "tu-os": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-oe": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
            "tu-ci": ToolContent({"subject": "inner"}, "Task #2 created successfully: inner"),
            "tu-is": ToolContent({"taskId": "2", "status": "in_progress"}, "ok"),
            "tu-ie": ToolContent({"taskId": "2", "status": "completed"}, "ok"),
        }
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:35Z",
        )
        spans = [
            interaction,
            _ledger_child(
                "co",
                "tool:TaskCreate",
                "tu-co",
                parent="i1",
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:00Z",
            ),
            _ledger_child(
                "os",
                "tool:TaskUpdate",
                "tu-os",
                parent="i1",
                start="2026-01-02T00:00:01Z",
                end="2026-01-02T00:00:01Z",
            ),
            _ledger_child(
                "ci",
                "tool:TaskCreate",
                "tu-ci",
                parent="i1",
                start="2026-01-02T00:00:04Z",
                end="2026-01-02T00:00:04Z",
            ),
            _ledger_child(
                "is",
                "tool:TaskUpdate",
                "tu-is",
                parent="i1",
                start="2026-01-02T00:00:05Z",
                end="2026-01-02T00:00:05Z",
            ),
            _ledger_child(
                "wk",
                "tool:Edit",
                "tu-wk",
                parent="i1",
                start="2026-01-02T00:00:10Z",
                end="2026-01-02T00:00:11Z",
            ),
            _ledger_child(
                "ie",
                "tool:TaskUpdate",
                "tu-ie",
                parent="i1",
                start="2026-01-02T00:00:20Z",
                end="2026-01-02T00:00:20Z",
            ),
            _ledger_child(
                "oe",
                "tool:TaskUpdate",
                "tu-oe",
                parent="i1",
                start="2026-01-02T00:00:30Z",
                end="2026-01-02T00:00:30Z",
            ),
        ]

        batch = build_batch([("tr", spans)], SPOKE, content)

        steps = {e["body"]["name"]: e["id"] for e in batch if e["id"].startswith(_STEP_PREFIX)}
        assert _by_orig(batch, "tr", "wk")["body"]["parentObservationId"] == steps["step:inner"]


def _audit_event(obs_id: str, name: str, *, start: str | None = None, **metadata) -> dict:
    """Build a span-less audit observation (EVENT type, flat metadata, no parent).

    Mirrors how :mod:`telemetry.langfuse_audit_events` mints lifecycle events on the per-spoke
    ``spoke-audit:`` trace — a startTime carries the LAGGING OTel-logs flush time, never used
    for placement.
    """
    return _obs(obs_id, name, type_="EVENT", parent=None, startTime=start, metadata=metadata)


def _root_meta(batch: list[dict]) -> dict:
    """Return the synthetic root span's metadata (empty dict when absent)."""
    root = next(e for e in batch if e["id"] == root_id_for(SPOKE))
    return root["body"].get("metadata") or {}


def _has_copy(batch: list[dict], orig_trace_id: str, orig_obs_id: str) -> bool:
    """Whether a copy node exists for one source observation."""
    copy_id = _copy_id(orig_trace_id, orig_obs_id)
    return any(event["id"] == copy_id for event in batch)


class TestStartupInstantCollapse:
    """#104: session-startup lifecycle instants (mcp_server_connection, plugin_loaded) are
    DEMOTED to the synthetic root's ``session_init`` metadata instead of standing as N sibling
    span nodes placed by their lagging log timestamp.
    """

    def test_mcp_connection_collapses_to_root_metadata(self) -> None:
        mcp = _audit_event(
            "mc1",
            "mcp_server_connection:connected",
            start="2026-01-02T00:00:09Z",
            status="connected",
            server_name="langfuse",
        )

        batch = build_batch([("trace-audit", [mcp])], SPOKE)

        init = _root_meta(batch)["session_init"]
        assert init == [
            {
                "name": "mcp_server_connection:connected",
                "status": "connected",
                "server_name": "langfuse",
            }
        ]

    def test_collapsed_startup_instant_emits_no_node(self) -> None:
        mcp = _audit_event("mc2", "mcp_server_connection:connected", status="connected")

        batch = build_batch([("trace-audit", [mcp])], SPOKE)

        assert not _has_copy(batch, "trace-audit", "mc2")

    def test_plugin_loaded_collapses_to_root_metadata(self) -> None:
        plugin = _audit_event(
            "pl1", "plugin_loaded", start="2026-01-02T00:00:02Z", **{"plugin.name": "claude-hud"}
        )

        batch = build_batch([("trace-audit", [plugin])], SPOKE)

        assert _root_meta(batch)["session_init"] == [
            {"name": "plugin_loaded", "plugin.name": "claude-hud"}
        ]
        assert not _has_copy(batch, "trace-audit", "pl1")

    def test_multiple_startup_instants_list_under_one_field(self) -> None:
        # Two startup instants collapse into ONE metadata list, not two sibling nodes.
        events = [
            _audit_event("mc3", "mcp_server_connection:connected", status="connected"),
            _audit_event("mc4", "mcp_server_connection:failed", status="failed"),
        ]

        batch = build_batch([("trace-audit", events)], SPOKE)

        names = [entry["name"] for entry in _root_meta(batch)["session_init"]]
        assert names == ["mcp_server_connection:connected", "mcp_server_connection:failed"]

    def test_no_session_init_field_without_startup_instants(self) -> None:
        # A spoke with no startup instants carries no session_init key on the root.
        batch = build_batch(_traces(), SPOKE)

        assert "session_init" not in _root_meta(batch)


class TestAuditInstantPlacement:
    """#104: span-less audit/lifecycle instants are placed by CAUSAL id-joins, never by their
    lagging OTel-logs ``startTime``. Tool-scoped events nest under their tool (tool_use_id);
    api_error / api_refusal nest under their llm_request (request_id); the genuinely
    unresolvable instants (skill_activated, permission_mode_changed, compaction) fall to the
    synthetic root as a last resort — never re-homed into a step window by the lagging time.
    """

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _ledger_traces(self) -> list[tuple[str, list[dict]]]:
        # A solo-cycle ledger window spanning 00:00..00:20 under one interaction.
        interaction = _obs(
            "i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:00:00Z"
        )
        create = _obs(
            "tc1",
            "tool:TaskCreate",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:00Z",
            metadata={"attributes": {"tool_use_id": "tu-c1"}},
        )
        started = _obs(
            "tu1",
            "tool:TaskUpdate",
            parent="i1",
            startTime="2026-01-02T00:00:01Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-u1"}},
        )
        done = _obs(
            "tu2",
            "tool:TaskUpdate",
            parent="i1",
            startTime="2026-01-02T00:00:20Z",
            endTime="2026-01-02T00:00:20Z",
            metadata={"attributes": {"tool_use_id": "tu-u2"}},
        )
        return [("tr", [interaction, create, started, done])]

    def test_skill_activated_inside_a_window_stays_at_root(self) -> None:
        # The anti-lag guarantee: a skill_activated whose LAGGING startTime falls inside the
        # ledger window must NOT be re-homed under the step node — it stays at the root.
        traces = self._ledger_traces()
        skill = _audit_event(
            "sk1", "skill_activated", start="2026-01-02T00:00:05Z", **{"skill.name": "source-task"}
        )
        traces[0][1].append(skill)

        batch = build_batch(traces, SPOKE, self._content())

        assert _by_orig(batch, "tr", "sk1")["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_compaction_falls_to_root(self) -> None:
        compaction = _audit_event("cp1", "compaction", trigger="auto", pre_tokens="9000")

        batch = build_batch([("trace-audit", [compaction])], SPOKE)

        assert _by_orig(batch, "trace-audit", "cp1")["body"]["parentObservationId"] == root_id_for(
            SPOKE
        )

    def test_permission_mode_change_falls_to_root(self) -> None:
        mode = _audit_event("pm1", "permission_mode_changed:plan", to_mode="plan")

        batch = build_batch([("trace-audit", [mode])], SPOKE)

        assert _by_orig(batch, "trace-audit", "pm1")["body"]["parentObservationId"] == root_id_for(
            SPOKE
        )

    def test_api_error_nests_under_llm_request_via_request_id(self) -> None:
        gen = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            parent="i1",
            metadata={"attributes": {"client_request_id": "req-1"}},
        )
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        err = _audit_event("e1", "api_error", request_id="req-1", status_code="500")
        traces = [("trace-int", [interaction, gen]), ("trace-audit", [err])]

        batch = build_batch(traces, SPOKE)

        assert _by_orig(batch, "trace-audit", "e1")["body"]["parentObservationId"] == _copy_id(
            "trace-int", "g1"
        )

    def test_api_refusal_nests_under_llm_request_via_request_id(self) -> None:
        gen = _obs(
            "g2",
            "llm_request",
            type_="GENERATION",
            parent=None,
            metadata={"attributes": {"client_request_id": "req-2"}},
        )
        refusal = _audit_event("rf1", "api_refusal", request_id="req-2", category="value")
        traces = [("trace-int", [gen]), ("trace-audit", [refusal])]

        batch = build_batch(traces, SPOKE)

        assert _by_orig(batch, "trace-audit", "rf1")["body"]["parentObservationId"] == _copy_id(
            "trace-int", "g2"
        )

    def test_api_error_without_a_matching_request_falls_to_root(self) -> None:
        err = _audit_event("e9", "api_error", request_id="req-absent", status_code="500")

        batch = build_batch([("trace-audit", [err])], SPOKE)

        assert _by_orig(batch, "trace-audit", "e9")["body"]["parentObservationId"] == root_id_for(
            SPOKE
        )

    def test_tool_decision_still_folds_under_tool_with_a_ledger_present(self) -> None:
        # Regression: a tool-scoped audit event must keep nesting/folding under its tool even
        # when step windows exist — it is never pulled into a step by its lagging time.
        traces = self._ledger_traces()
        tool = _obs(
            "tb",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:03Z",
            endTime="2026-01-02T00:00:03Z",
            metadata={"attributes": {"tool_use_id": "tu-b"}},
        )
        decision = _audit_event(
            "db",
            "tool_decision:reject",
            start="2026-01-02T00:00:30Z",
            tool_use_id="tu-b",
            decision="reject",
        )
        traces[0][1].extend([tool, decision])

        batch = build_batch(traces, SPOKE, self._content())

        # The decision folds into the tool's metadata and its own node is dropped.
        assert not _has_copy(batch, "tr", "db")
        assert _by_orig(batch, "tr", "tb")["body"]["metadata"]["decision"] == "reject"

    def test_real_marker_inside_a_window_is_still_grouped(self) -> None:
        # The exclusion must not over-capture: a genuine duration marker that is an
        # interaction-internal sibling within the window still re-homes under its step (#113).
        traces = self._ledger_traces()
        marker = _obs("m1", "spoke-push", parent="i1", startTime="2026-01-02T00:00:05Z")
        traces[0][1].append(marker)

        batch = build_batch(traces, SPOKE, self._content())

        step = _step_node(batch)
        assert step is not None
        assert _by_orig(batch, "tr", "m1")["body"]["parentObservationId"] == step["id"]

    def test_audit_events_are_idempotent_across_reruns(self) -> None:
        traces = [
            (
                "trace-audit",
                [
                    _audit_event("sk2", "skill_activated", **{"skill.name": "hub"}),
                    _audit_event("cp2", "compaction", trigger="manual"),
                ],
            )
        ]

        first = build_batch(traces, SPOKE)
        second = build_batch(traces, SPOKE)

        assert [e["id"] for e in first] == [e["id"] for e in second]


class TestSkillActivatedNesting:
    """#110 AC2: a span-less ``skill_activated`` event nests under the ``tool:Skill`` span that
    activated it — matched by the turn's ``prompt.id``, disambiguated by ``skill.name`` then the
    nearest timestamp when a turn ran more than one skill. With no ``tool:Skill`` in the turn it
    falls back to the enclosing turn (#110 AC1); with no turn at all it stays at the root.
    """

    def _skill_tool(self, obs_id: str, tuid: str, parent: str = "i1", start: str | None = None):
        return _obs(
            obs_id,
            "tool:Skill",
            parent=parent,
            startTime=start,
            metadata={"attributes": {"tool_use_id": tuid}},
        )

    def test_skill_activated_nests_under_its_tool_skill_by_prompt_id(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        tool = self._skill_tool("sk_tool", "tu-sk1")
        skill = _audit_event(
            "act1", "skill_activated", **{"skill.name": "source-task", "prompt.id": "p1"}
        )
        traces = [("trace-int", [interaction, tool]), ("trace-audit", [skill])]
        content = {"tu-sk1": ToolContent({"skill": "source-task"}, "ok")}

        batch = build_batch(traces, SPOKE, content)

        copy = _by_orig(batch, "trace-audit", "act1")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_tool")

    def test_skill_activated_recovers_via_bridge_stamped_interaction(self) -> None:
        # #111 end-to-end: the real production shape. The tool:Skill span carries NO prompt.id
        # of its own, and the interaction carries prompt.id only as a FLAT metadata key (the
        # message bridge's span-update output, not nested under "attributes"). _build_skill_index
        # must still recover the turn key by walking tool:Skill -> enclosing interaction, so the
        # span-less skill_activated nests under its tool:Skill instead of floating to the root.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"prompt.id": "p1", "attributes": {"interaction.sequence": "3"}},
        )
        tool = self._skill_tool("sk_tool", "tu-sk1")  # no prompt.id on the tool span
        skill = _audit_event(
            "act_e2e", "skill_activated", **{"skill.name": "source-task", "prompt.id": "p1"}
        )
        traces = [("trace-int", [interaction, tool]), ("trace-audit", [skill])]
        content = {"tu-sk1": ToolContent({"skill": "source-task"}, "ok")}

        batch = build_batch(traces, SPOKE, content)

        copy = _by_orig(batch, "trace-audit", "act_e2e")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_tool")

    def test_two_skills_in_one_turn_disambiguated_by_skill_name(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        hub = self._skill_tool("sk_hub", "tu-hub")
        source = self._skill_tool("sk_src", "tu-src")
        skill = _audit_event(
            "act2", "skill_activated", **{"skill.name": "source-task", "prompt.id": "p1"}
        )
        traces = [("trace-int", [interaction, hub, source]), ("trace-audit", [skill])]
        content = {
            "tu-hub": ToolContent({"skill": "hub"}, "ok"),
            "tu-src": ToolContent({"skill": "source-task"}, "ok"),
        }

        batch = build_batch(traces, SPOKE, content)

        copy = _by_orig(batch, "trace-audit", "act2")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_src")

    def test_same_skill_twice_disambiguated_by_nearest_timestamp(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        early = self._skill_tool("sk_a", "tu-a", start="2026-01-02T00:00:00Z")
        late = self._skill_tool("sk_b", "tu-b", start="2026-01-02T00:00:20Z")
        # The activation's lagging time sits closest to the second call.
        skill = _audit_event(
            "act3",
            "skill_activated",
            start="2026-01-02T00:00:21Z",
            **{"skill.name": "hub", "prompt.id": "p1"},
        )
        traces = [("trace-int", [interaction, early, late]), ("trace-audit", [skill])]
        content = {
            "tu-a": ToolContent({"skill": "hub"}, "ok"),
            "tu-b": ToolContent({"skill": "hub"}, "ok"),
        }

        batch = build_batch(traces, SPOKE, content)

        copy = _by_orig(batch, "trace-audit", "act3")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_b")

    def test_skill_activated_without_a_tool_skill_falls_back_to_turn(self) -> None:
        # No tool:Skill in the turn (e.g. the skill content was unavailable): the event still
        # attaches to its enclosing turn by prompt.id, never the synthetic root (#110 invariant).
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        skill = _audit_event("act4", "skill_activated", **{"skill.name": "hub", "prompt.id": "p1"})
        traces = [("trace-int", [interaction]), ("trace-audit", [skill])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "act4")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "i1")

    def test_skill_activated_with_no_turn_stays_at_root(self) -> None:
        skill = _audit_event("act5", "skill_activated", **{"skill.name": "hub"})

        batch = build_batch([("trace-audit", [skill])], SPOKE)

        copy = _by_orig(batch, "trace-audit", "act5")
        assert copy["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_skill_activated_never_crosses_into_another_turn(self) -> None:
        # A tool:Skill in turn A must never adopt a skill_activated emitted in turn B.
        turn_a = _obs(
            "ia",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "pa"}},
        )
        turn_b = _obs(
            "ib",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "pb"}},
        )
        tool = self._skill_tool("sk_a", "tu-a", parent="ia")
        skill = _audit_event("act6", "skill_activated", **{"skill.name": "hub", "prompt.id": "pb"})
        traces = [("trace-a", [turn_a, tool]), ("trace-b", [turn_b]), ("trace-audit", [skill])]
        content = {"tu-a": ToolContent({"skill": "hub"}, "ok")}

        batch = build_batch(traces, SPOKE, content)

        # Turn B holds no tool:Skill, so the event homes to turn B itself, not turn A's skill.
        copy = _by_orig(batch, "trace-audit", "act6")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-b", "ib")

    def test_lone_skill_tool_adopts_event_even_without_transcript_content(self) -> None:
        # No tool_content (skill name unknown): the turn's single tool:Skill still adopts it.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        tool = self._skill_tool("sk_tool", "tu-sk1")
        skill = _audit_event("act7", "skill_activated", **{"skill.name": "hub", "prompt.id": "p1"})
        traces = [("trace-int", [interaction, tool]), ("trace-audit", [skill])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "act7")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_tool")

    def test_event_without_skill_name_nearest_of_two_candidates(self) -> None:
        # No skill.name on the event: it falls straight to the nearest-timestamp tiebreak.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        early = self._skill_tool("sk_a", "tu-a", start="2026-01-02T00:00:00Z")
        late = self._skill_tool("sk_b", "tu-b", start="2026-01-02T00:00:20Z")
        skill = _audit_event(
            "act8", "skill_activated", start="2026-01-02T00:00:01Z", **{"prompt.id": "p1"}
        )
        traces = [("trace-int", [interaction, early, late]), ("trace-audit", [skill])]
        content = {
            "tu-a": ToolContent({"skill": "hub"}, "ok"),
            "tu-b": ToolContent({"skill": "land"}, "ok"),
        }

        batch = build_batch(traces, SPOKE, content)

        copy = _by_orig(batch, "trace-audit", "act8")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-int", "sk_a")


class TestScanTranscripts:
    def test_joins_tool_use_input_and_tool_result_output(self, tmp_path: Path) -> None:
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "TaskCreate", {"subject": "ship it"}), _tool_result("tu-1", "ok")],
        )

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert contents["tu-1"].input == {"subject": "ship it"}
        assert contents["tu-1"].output == "ok"

    def test_ids_absent_from_session_are_skipped(self, tmp_path: Path) -> None:
        _write_transcript(tmp_path, [_tool_use("tu-stray", "Read", {"file_path": "/b"})])

        assert scan_transcripts(tmp_path, {"tu-1"}) == {}

    def test_empty_wanted_set_reads_nothing(self, tmp_path: Path) -> None:
        _write_transcript(tmp_path, [_tool_use("tu-1", "Read", {"file_path": "/a"})])

        assert scan_transcripts(tmp_path, set()) == {}


class TestTranscriptScanRoot:
    """Issue #98: scope the transcript scan to the spoke's own CC project dir.

    The default rglobbed EVERY session under ~/.claude/projects on each land. Matching is by
    globally-unique tool_use_id so it never cross-attached (unlike #92's reasoning bug), but
    scoping it to the worktree's project dir avoids the all-projects rglob. Falls back to the
    full root when that dir is absent (a standalone run from a non-worktree cwd).
    """

    def _seed(self, tmp_path: Path) -> tuple[Path, Path]:
        projects = tmp_path / "projects"
        worktree = tmp_path / "Repos" / "ai-toolkit-cycle-demo"
        worktree.mkdir(parents=True)
        wt_proj = project_dir_for_worktree(worktree, projects)
        wt_proj.mkdir(parents=True)
        (wt_proj / "s.jsonl").write_text(
            json.dumps(_tool_use("tu-wt", "Read", {"file_path": "/a"})) + "\n", encoding="utf-8"
        )
        sibling = projects / "-hub-driver"
        sibling.mkdir(parents=True)
        (sibling / "s.jsonl").write_text(
            json.dumps(_tool_use("tu-hub", "Read", {"file_path": "/b"})) + "\n", encoding="utf-8"
        )
        return projects, worktree

    def test_returns_the_worktree_project_dir_when_present(self, tmp_path: Path) -> None:
        projects, worktree = self._seed(tmp_path)
        assert transcript_scan_root(projects, worktree) == project_dir_for_worktree(
            worktree, projects
        )

    def test_scoped_scan_excludes_sibling_project_tool_ids(self, tmp_path: Path) -> None:
        projects, worktree = self._seed(tmp_path)
        root = transcript_scan_root(projects, worktree)

        found = scan_transcripts(root, {"tu-wt", "tu-hub"})

        assert set(found) == {"tu-wt"}

    def test_falls_back_to_full_root_when_project_dir_absent(self, tmp_path: Path) -> None:
        projects, _ = self._seed(tmp_path)
        absent = tmp_path / "Repos" / "never-ran-here"

        assert transcript_scan_root(projects, absent) == projects


class TestToolContentFilledIntoCreateBody:
    def test_tool_span_input_and_output_set_from_transcript(self, tmp_path: Path) -> None:
        # A tool:TaskCreate span arrives with input=None; the transcript supplies both fields.
        span = _tool_obs("t1", "tool:TaskCreate", "tu-1")
        _write_transcript(
            tmp_path,
            [
                _tool_use("tu-1", "TaskCreate", {"subject": "ship it"}),
                _tool_result("tu-1", "Task #1 created"),
            ],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        body = _by_orig(batch, "trace", "t1")["body"]
        assert body["input"] == {"subject": "ship it"}
        assert body["output"] == "Task #1 created"

    def test_existing_bash_input_is_not_overwritten(self, tmp_path: Path) -> None:
        # Bash's input is collector-provided; the transcript output still fills the gap.
        span = _tool_obs("t1", "tool:Bash", "tu-1", input="ls -la")
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Bash", {"command": "rm -rf /"}), _tool_result("tu-1", "files")],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        body = _by_orig(batch, "trace", "t1")["body"]
        assert body["input"] == "ls -la"
        assert body["output"] == "files"

    def test_non_tool_span_sharing_a_tool_use_id_is_untouched(self, tmp_path: Path) -> None:
        # An execution sibling shares tu-1 but is not a tool: span, so it gains no content.
        sibling = _tool_obs("e1", "claude_code.tool.execution", "tu-1")
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Read", {"file_path": "/a"}), _tool_result("tu-1", "data")],
        )

        batch = build_batch([("trace", [sibling])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        body = _by_orig(batch, "trace", "e1")["body"]
        assert "input" not in body
        assert "output" not in body

    def test_large_tool_output_is_truncated_with_marker(self, tmp_path: Path) -> None:
        span = _tool_obs("t1", "tool:Read", "tu-1")
        huge = "x" * (_MAX_CONTENT_CHARS + 500)
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Read", {"file_path": "/a"}), _tool_result("tu-1", huge)],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        output = _by_orig(batch, "trace", "t1")["body"]["output"]
        assert output.endswith(_TRUNCATION_MARKER)
        assert len(output) == _MAX_CONTENT_CHARS + len(_TRUNCATION_MARKER)

    def test_no_tool_content_leaves_bodies_unchanged(self) -> None:
        # build_batch without a content map (the default) fills nothing.
        span = _tool_obs("t1", "tool:TaskCreate", "tu-1")

        batch = build_batch([("trace", [span])], SPOKE)

        body = _by_orig(batch, "trace", "t1")["body"]
        assert "input" not in body
        assert "output" not in body


class TestSubAgentContentGraft:
    """#161: ``sub-agent:*`` container spans graft transcript content like ``tool:`` spans.

    The otelcol renames ``tool:Agent`` → ``sub-agent:<type>``, so the review verdict the
    sub-agent returned (its transcript ``tool_result``) must still be grafted as ``output``.
    """

    def test_subagent_output_set_from_transcript(self, tmp_path: Path) -> None:
        span = _tool_obs("sa1", "sub-agent:code-review", "tu-1")
        _write_transcript(
            tmp_path,
            [
                _tool_use("tu-1", "Agent", {"prompt": "review the diff"}),
                _tool_result("tu-1", "REVIEW: SHIP - no blocking issues"),
            ],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        body = _by_orig(batch, "trace", "sa1")["body"]
        assert body["output"] == "REVIEW: SHIP - no blocking issues"
        assert body["input"] == {"prompt": "review the diff"}

    def test_subagent_graft_does_not_overwrite_native_output(self, tmp_path: Path) -> None:
        # Non-destructive fill: a sub-agent span already carrying output keeps it.
        span = _tool_obs("sa1", "sub-agent:code-review", "tu-1", output="native verdict")
        _write_transcript(
            tmp_path,
            [
                _tool_use("tu-1", "Agent", {"prompt": "x"}),
                _tool_result("tu-1", "transcript verdict"),
            ],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        assert _by_orig(batch, "trace", "sa1")["body"]["output"] == "native verdict"

    def test_large_subagent_output_is_truncated_with_marker(self, tmp_path: Path) -> None:
        span = _tool_obs("sa1", "sub-agent:general-purpose", "tu-1")
        huge = "x" * (_MAX_CONTENT_CHARS + 500)
        _write_transcript(
            tmp_path,
            [_tool_use("tu-1", "Agent", {"prompt": "x"}), _tool_result("tu-1", huge)],
        )

        batch = build_batch([("trace", [span])], SPOKE, scan_transcripts(tmp_path, {"tu-1"}))

        output = _by_orig(batch, "trace", "sa1")["body"]["output"]
        assert output.endswith(_TRUNCATION_MARKER)
        assert len(output) == _MAX_CONTENT_CHARS + len(_TRUNCATION_MARKER)

    def test_subagent_tool_use_id_included_in_scan_set(self) -> None:
        # _tool_span_ids scopes scan_transcripts; a sub-agent id absent here is never fetched.
        span = _tool_obs("sa1", "sub-agent:code-review", "tu-1")

        assert _tool_span_ids([("trace", [span])]) == {"tu-1"}


class TestSubAgentRollupPinning:
    """#161: pin the existing sub-agent nesting/rollup behavior against regressions."""

    def test_subagent_container_usage_absent_but_rollup_present(self) -> None:
        # A sub-agent container holding one generation: the copy carries no own usageDetails
        # (double-count guard) but a subtree token rollup.
        agent = _obs("sa1", "sub-agent:code-review", parent=None)
        gen = _obs(
            "sg1",
            "llm_request",
            type_="GENERATION",
            parent="sa1",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 2,
            },
        )

        batch = build_batch([("trace-a", [agent, gen])], SPOKE)

        body = _by_orig(batch, "trace-a", "sa1")["body"]
        assert not body.get("usageDetails")
        assert body["metadata"]["rollup"] == {
            "reused": 7,
            "written": 2,
            "input": 10,
            "output": 4,
            "duration": _dur(0),
        }

    def test_container_with_generation_descendant_strips_native_usage(self) -> None:
        # Future-proof guard: even if the collector someday stamps usage on the container
        # span, the copy must drop it so trace cost never double-counts the generation child.
        agent = _obs(
            "sa1", "sub-agent:code-review", parent=None, usageDetails={"input": 999, "output": 999}
        )
        gen = _obs(
            "sg1",
            "llm_request",
            type_="GENERATION",
            parent="sa1",
            usageDetails={"input": 10, "output": 4},
        )

        batch = build_batch([("trace-a", [agent, gen])], SPOKE)

        assert not _by_orig(batch, "trace-a", "sa1")["body"].get("usageDetails")

    def test_nested_subagent_trees_roll_up_recursively(self) -> None:
        # sub-agent -> nested sub-agent -> generation: the outer rollup sums the whole subtree.
        outer = _obs("sa1", "sub-agent:code-review", parent=None)
        inner = _obs("sa2", "sub-agent:general-purpose", parent="sa1")
        gen = _obs(
            "sg1",
            "llm_request",
            type_="GENERATION",
            parent="sa2",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 2,
            },
        )

        batch = build_batch([("trace-a", [outer, inner, gen])], SPOKE)

        assert _by_orig(batch, "trace-a", "sa1")["body"]["metadata"]["rollup"] == {
            "reused": 7,
            "written": 2,
            "input": 10,
            "output": 4,
            "duration": _dur(0),
        }


class TestPrefixTotal:
    def test_sums_cache_read_and_creation_of_earliest_call(self) -> None:
        early = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            startTime="2026-01-02T00:00:00Z",
            usageDetails={"cache_read_input_tokens": 4000, "cache_creation_input_tokens": 1000},
        )
        late = _obs(
            "g2",
            "llm_request",
            type_="GENERATION",
            startTime="2026-01-02T00:05:00Z",
            usageDetails={"cache_creation_input_tokens": 80},
        )

        assert prefix_total([("tr", [late, early])]) == 5000

    def test_counts_creation_only_when_read_absent(self) -> None:
        cold = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            startTime="2026-01-02T00:00:00Z",
            usageDetails={"cache_creation_input_tokens": 5000},
        )

        assert prefix_total([("tr", [cold])]) == 5000

    def test_zero_when_no_usage_present(self) -> None:
        assert prefix_total([("tr", [_obs("m1", "step:green")])]) == 0


def _only_node(events: list[dict]) -> dict:
    """Return the body of the single loaded-context event, asserting there is exactly one."""
    assert len(events) == 1
    return events[0]["body"]


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TestLoadedContextDiskFallback:
    """The disk-fallback path: one collapsed node whose metadata folds in the remainder."""

    def _rows(self) -> list[dict]:
        return [
            {
                "category": "rules",
                "name": "CLAUDE.md",
                "tokens": 100,
                "cost_usd": 0.1,
                "source": "CLAUDE.md",
                "estimated": False,
            },
            {
                "category": "rules",
                "name": "python-style.md",
                "tokens": 50,
                "cost_usd": 0.05,
                "source": ".claude/rules/python-style.md",
                "estimated": False,
            },
            {
                "category": "skills",
                "name": "afk",
                "tokens": 20,
                "cost_usd": 0.02,
                "source": ".claude/skills/afk/SKILL.md",
                "estimated": True,
            },
        ]

    def _build(self, prefix_total: int = 1000) -> list[dict]:
        return build_loaded_context_events(
            SPOKE,
            self._rows(),
            category_order=_DISK_CATEGORY_ORDER,
            base_ts="2026-01-01T00:00:00Z",
            prefix_total=prefix_total,
            price=0.001,
        )

    def test_emits_exactly_one_node_under_spoke_root(self) -> None:
        node = _only_node(self._build())
        assert node["parentObservationId"] == root_id_for(SPOKE)

    def test_headline_tokens_fold_in_the_remainder(self) -> None:
        # measured 170 + reconciled remainder 830 == prefix 1000
        assert _only_node(self._build())["metadata"]["tokens"] == 1000

    def test_breakdown_groups_item_tokens_by_category(self) -> None:
        breakdown = _only_node(self._build())["metadata"]["breakdown"]
        assert breakdown["rules"] == {"CLAUDE.md": 100, "python-style.md": 50}
        assert breakdown["skills"] == {"afk": 20}

    def test_remainder_preserved_in_metadata_not_as_a_node(self) -> None:
        assert _only_node(self._build())["metadata"]["remainder"] == 830

    def test_remainder_clamped_to_zero_when_measured_exceeds_prefix(self) -> None:
        node = _only_node(self._build(prefix_total=100))
        assert node["metadata"]["remainder"] == 0
        assert node["metadata"]["tokens"] == 170

    def test_aggregate_cost_includes_the_remainder_cost(self) -> None:
        # measured 0.17 + remainder 830 * 0.001 == 1.0
        assert abs(_only_node(self._build())["metadata"]["cost_usd"] - 1.0) < 1e-9

    def test_breakdown_omits_floor_and_mcp_categories(self) -> None:
        breakdown = _only_node(self._build())["metadata"]["breakdown"]
        assert not any(c.startswith("built-in") or c.startswith("mcp") for c in breakdown)

    def test_node_id_is_deterministic_across_runs(self) -> None:
        assert self._build()[0]["id"] == self._build()[0]["id"]

    def test_node_attaches_to_the_assembled_trace_as_span_create(self) -> None:
        event = self._build()[0]
        assert event["body"]["traceId"] == trace_id_for(SPOKE)
        assert event["type"] == "span-create"


class TestRequestContextSubtree:
    """The primary path: the whole prefix itemized from the raw request body, no remainder."""

    def _rows(self) -> list[dict]:
        return [
            {
                "category": "tools",
                "name": "Bash",
                "tokens": 120,
                "cost_usd": 0.12,
                "source": "request-body",
                "cached": False,
                "estimated": False,
            },
            {
                "category": "mcp",
                "name": "mcp__x__y",
                "tokens": 40,
                "cost_usd": 0.04,
                "source": "request-body",
                "cached": False,
                "estimated": False,
            },
            {
                "category": "system",
                "name": "base system prompt",
                "tokens": 200,
                "cost_usd": 0.2,
                "source": "request-body",
                "cached": True,
                "estimated": False,
            },
            {
                "category": "context",
                "name": "skills",
                "tokens": 80,
                "cost_usd": 0.08,
                "source": "request-body",
                "cached": False,
                "estimated": False,
            },
        ]

    def _build(self) -> list[dict]:
        return build_loaded_context_events(
            SPOKE,
            self._rows(),
            category_order=_REQUEST_CATEGORY_ORDER,
            base_ts="2026-01-01T00:00:00Z",
        )

    def test_breakdown_covers_all_request_categories(self) -> None:
        breakdown = _only_node(self._build())["metadata"]["breakdown"]
        assert set(breakdown) == {"tools", "mcp", "system", "context"}
        assert breakdown["tools"] == {"Bash": 120}
        assert breakdown["system"] == {"base system prompt": 200}

    def test_no_remainder_key_on_the_request_path(self) -> None:
        assert "remainder" not in _only_node(self._build())["metadata"]

    def test_node_total_is_full_itemized_prefix(self) -> None:
        assert _only_node(self._build())["metadata"]["tokens"] == 440  # 120 + 40 + 200 + 80

    def test_breakdown_surfaces_per_file_rules_and_per_skill_entries(self) -> None:
        # Arrange: the turn-0 combined-block router (#159) yields rules/skills/environment rows.
        rows = [
            {"category": "rules", "name": "CLAUDE.md", "tokens": 300, "cost_usd": 0.3},
            {"category": "rules", "name": "MEMORY.md", "tokens": 250, "cost_usd": 0.25},
            {"category": "skills", "name": "afk", "tokens": 30, "cost_usd": 0.03},
            {"category": "environment", "name": "environment", "tokens": 20, "cost_usd": 0.02},
        ]

        # Act: render the breakdown with the request-path category order.
        node = build_loaded_context_events(
            SPOKE, rows, category_order=_REQUEST_CATEGORY_ORDER, base_ts="2026-01-01T00:00:00Z"
        )
        breakdown = _only_node(node)["metadata"]["breakdown"]

        # Assert: per-file rules and per-skill entries are itemized (previously invisible).
        assert breakdown["rules"] == {"CLAUDE.md": 300, "MEMORY.md": 250}
        assert breakdown["skills"] == {"afk": 30}
        assert breakdown["environment"] == {"environment": 20}


class TestRequestContextRows:
    """Sourcing rows from a per-spoke dir of raw request bodies."""

    def _bodies_dir(self, tmp_path: Path) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        shutil.copy(_FIXTURES / "sample.request.json", bodies)
        shutil.copy(_FIXTURES / "degenerate.request.json", bodies)
        return bodies

    def test_find_request_files_globs_request_dumps(self, tmp_path: Path) -> None:
        files = find_request_files(self._bodies_dir(tmp_path))
        assert {p.name for p in files} == {"sample.request.json", "degenerate.request.json"}

    def test_rows_sourced_from_first_real_request(self, tmp_path: Path) -> None:
        rows = request_context_rows(self._bodies_dir(tmp_path), counter=len, price=1.0)
        assert rows is not None
        names = {row["name"] for row in rows}
        assert "Bash" in names and "Workflow" in names

    def test_none_when_no_request_bodies_present(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert request_context_rows(empty, counter=len, price=1.0) is None


class TestContextEvolutionRemoved:
    """#100 part 4: the #98 context-evolution subtree is deleted (subsumed by #99's per-turn
    cache_creation now carried as llm_request metadata). Its builders no longer exist.
    """

    def test_evolution_builders_are_gone(self) -> None:
        import telemetry.langfuse_spoke_tree as module

        for symbol in (
            "build_context_evolution_events",
            "context_evolution_deltas",
            "_reconciliation_map",
            "per_turn_cache_creation",
        ):
            assert not hasattr(module, symbol), symbol


class TestLlmDecompositionMetadata:
    """#100 part 3: the #99 cache_read/cache_creation decomposition is folded onto each
    llm_request copy as metadata (per-component -> per-item, reconciled), NOT nested nodes.
    """

    _TOOL = {"name": "Bash", "description": "d" * 40, "input_schema": {"type": "object"}}

    def _write(self, bodies: Path, index: int, obj: dict) -> None:
        (bodies / f"{index:02d}-body.request.json").write_text(json.dumps(obj), encoding="utf-8")

    def _bodies_dir(self, tmp_path: Path, obj: dict) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        self._write(bodies, 0, obj)
        return bodies

    def _obj(self) -> dict:
        return {
            "tools": [self._TOOL],
            "system": [{"type": "text", "text": "sys"}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "the newest message"}]},
            ],
        }

    def _gen(self, obs_id: str, start: str, *, read: int, creation: int) -> dict:
        return _obs(
            obs_id,
            "llm_request",
            type_="GENERATION",
            parent="i1",
            startTime=start,
            usageDetails={
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": creation,
            },
        )

    def _decompose(self, traces, bodies: Path) -> tuple[list[dict], int]:
        batch = build_batch(traces, SPOKE)
        count = apply_llm_decomposition(batch, traces, bodies, counter=len, price=1.0)
        return batch, count

    def _meta(self, batch: list[dict], orig_trace: str, obs_id: str) -> dict:
        return _by_orig(batch, orig_trace, obs_id)["body"].get("metadata", {})

    def test_decomposition_is_metadata_not_nodes(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=30, creation=10)

        batch, count = self._decompose([("tr", [gen])], bodies)

        # No decomposition NODES are added — only the trace, root, and the single llm_request copy.
        assert count == 1
        assert len(batch) == 3
        meta = self._meta(batch, "tr", "g1")
        assert set(meta) >= {"cache_read", "cache_creation"}
        assert isinstance(meta["cache_read"]["components"], dict)

    def test_cold_turn_puts_everything_in_cache_creation(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=5000)

        batch, _ = self._decompose([("tr", [gen])], bodies)

        meta = self._meta(batch, "tr", "g1")
        assert meta["cache_read"]["components"] == {}  # nothing reused
        assert "tools" in meta["cache_creation"]["components"]

    def test_warm_turn_puts_newest_message_in_cache_creation(self, tmp_path: Path) -> None:
        # Size read to cover every item except the newest message; creation takes exactly it.
        bodies = self._bodies_dir(tmp_path, self._obj())
        rows = measure_request_items(
            decompose_request_body(bodies / "00-body.request.json"), counter=len, price=1.0
        )
        newest = next(r for r in rows if r["name"] == "msg[1]:assistant")
        read = sum(int(r["tokens"]) for r in rows) - int(newest["tokens"])  # type: ignore[arg-type, call-overload]
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=read, creation=int(newest["tokens"]))  # type: ignore[arg-type]

        batch, _ = self._decompose([("tr", [gen])], bodies)

        creation = self._meta(batch, "tr", "g1")["cache_creation"]
        assert "msg[1]:assistant" in creation["components"].get("messages", {})

    def test_each_bucket_reconciles_observed_measured_remainder(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=100000, creation=7)

        batch, _ = self._decompose([("tr", [gen])], bodies)

        read = self._meta(batch, "tr", "g1")["cache_read"]
        assert read["observed"] == 100000
        assert read["measured"] + read["remainder"] == read["observed"]
        # per-item token counts live under components[category][name]
        item_sum = sum(tok for comp in read["components"].values() for tok in comp.values())
        assert item_sum == read["measured"]

    def test_count_mismatch_skips_decomposition(self, tmp_path: Path) -> None:
        # Two bodies but a single llm_request — positional alignment is unsafe → no metadata.
        bodies = self._bodies_dir(tmp_path, self._obj())
        self._write(bodies, 1, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=30, creation=10)

        batch, count = self._decompose([("tr", [gen])], bodies)

        assert count == 0
        assert "cache_read" not in self._meta(batch, "tr", "g1")

    def test_duplicate_named_items_are_summed_not_clobbered(self) -> None:
        # Two rules with the same basename (e.g. nested CLAUDE.md) must SUM, so the per-item
        # itemization still reconciles to measured (a plain {name: tokens} dict would drop one).
        rows = [
            {
                "category": "rules",
                "name": "CLAUDE.md",
                "tokens": 10,
                "cost_usd": 0.0,
                "source": "s",
            },
            {"category": "rules", "name": "CLAUDE.md", "tokens": 7, "cost_usd": 0.0, "source": "s"},
        ]

        meta = _decomp_metadata(rows, observed=20)

        assert meta["components"]["rules"]["CLAUDE.md"] == 17
        assert meta["measured"] == 17
        item_sum = sum(tok for comp in meta["components"].values() for tok in comp.values())
        assert item_sum == meta["measured"]


def _canonical_msg(message: dict) -> str:
    """The canonical ``{role, content}`` JSON a snapshot item carries (mirrors _full_message_items)."""
    return json.dumps(
        {"role": message["role"], "content": message["content"]},
        ensure_ascii=False,
        sort_keys=True,
    )


class TestMemoizedCounter:
    """#160: one token counter memoized by content hash, shared by deltas + decomposition."""

    def test_counts_each_distinct_text_once(self) -> None:
        seen: list[str] = []

        def spy(text: str) -> int:
            seen.append(text)
            return len(text)

        memo = _memoized_counter(spy)

        assert memo("abc") == 3
        assert memo("abc") == 3  # cached — the underlying counter is not called again
        assert memo("de") == 2
        assert seen == ["abc", "de"]


class TestContextDeltas:
    """#160: per-llm_request context deltas from consecutive raw bodies (View A, single-emit)."""

    _TOOL = {"name": "Bash", "description": "d" * 40, "input_schema": {"type": "object"}}

    def _bodies(self, tmp_path: Path, *objs: dict) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        for index, obj in enumerate(objs):
            (bodies / f"{index:02d}-body.request.json").write_text(
                json.dumps(obj), encoding="utf-8"
            )
        return bodies

    def _body(self, messages: list[dict]) -> dict:
        return {
            "tools": [self._TOOL],
            "system": [{"type": "text", "text": "sys"}],
            "messages": messages,
        }

    def _gen(self, obs_id: str, start: str, *, read: int, creation: int) -> dict:
        return _obs(
            obs_id,
            "llm_request",
            type_="GENERATION",
            parent="i1",
            startTime=start,
            usageDetails={"cache_read_input_tokens": read, "cache_creation_input_tokens": creation},
        )

    def _meta(self, batch: list[dict], obs_id: str) -> dict:
        return _by_orig(batch, "tr", obs_id)["body"].get("metadata", {})

    def test_stamps_added_message_delta_on_the_later_call(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        m2 = {"role": "assistant", "content": [{"type": "text", "text": "the newest turn"}]}
        added_tokens = len(_canonical_msg(m2))
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1, m2]))
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:01Z", read=100, creation=added_tokens)
        traces = [("tr", [g1, g2])]
        batch = build_batch(traces, SPOKE)

        apply_context_deltas(batch, traces, bodies, counter=len, price=1.0, tool_content={})

        delta = self._meta(batch, "g2")["context_delta"]
        # net_tokens reconciles (remainder 0 here) against the call's observed cache_creation.
        assert delta["net_tokens"] == added_tokens
        assert any(row["category"] == "messages" for row in delta["added"])

    def test_first_call_has_no_delta(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        m2 = {"role": "assistant", "content": [{"type": "text", "text": "next"}]}
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1, m2]))
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:01Z", read=100, creation=50)
        traces = [("tr", [g1, g2])]
        batch = build_batch(traces, SPOKE)

        apply_context_deltas(batch, traces, bodies, counter=len, price=1.0, tool_content={})

        assert "context_delta" not in self._meta(batch, "g1")

    def test_count_gate_mismatch_skips_deltas(self, tmp_path: Path) -> None:
        # Two bodies but a single llm_request — positional alignment is unsafe → nothing stamped.
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1]))
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        traces = [("tr", [g1])]
        batch = build_batch(traces, SPOKE)

        stamped = apply_context_deltas(
            batch, traces, bodies, counter=len, price=1.0, tool_content={}
        )

        assert not stamped
        assert "context_delta" not in self._meta(batch, "g1")

    def test_skill_added_message_labeled_with_skill_name(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        skill_msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu-sk", "content": "SKILL BODY"}],
        }
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1, skill_msg]))
        skill_span = _tool_obs("sk", "tool:Skill", "tu-sk")
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:01Z", read=100, creation=40)
        traces = [("tr", [skill_span, g1, g2])]
        tool_content = {"tu-sk": ToolContent({"skill": "langfuse"}, "SKILL BODY")}
        batch = build_batch(traces, SPOKE, tool_content)

        apply_context_deltas(
            batch, traces, bodies, counter=len, price=1.0, tool_content=tool_content
        )

        added = self._meta(batch, "g2")["context_delta"]["added"]
        skill_rows = [row for row in added if row.get("skill") == "langfuse"]
        assert len(skill_rows) == 1

    def test_compaction_turn_labeled(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        big = {"role": "assistant", "content": [{"type": "text", "text": "x" * 12000}]}
        bodies = self._bodies(tmp_path, self._body([m1, big]), self._body([m1]))
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:01Z", read=100, creation=5)
        traces = [("tr", [g1, g2])]
        batch = build_batch(traces, SPOKE)

        apply_context_deltas(batch, traces, bodies, counter=len, price=1.0, tool_content={})

        assert self._meta(batch, "g2")["context_delta"]["label"] == "compaction"

    def test_context_delta_is_metadata_only(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        m2 = {"role": "assistant", "content": [{"type": "text", "text": "next"}]}
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1, m2]))
        g1 = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:01Z", read=100, creation=50)
        traces = [("tr", [g1, g2])]
        batch = build_batch(traces, SPOKE)

        apply_context_deltas(batch, traces, bodies, counter=len, price=1.0, tool_content={})

        body = _by_orig(batch, "tr", "g2")["body"]
        # The delta rides metadata only; the call's billed usage is untouched.
        assert body["usageDetails"] == {
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        }
        assert "context_delta" in body["metadata"]


class TestContextRollup:
    """#160: aggregate rollup.context onto each step node so per-cycle context cost reads local."""

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    _TOOL = {"name": "Bash", "description": "d" * 40, "input_schema": {"type": "object"}}

    def _bodies(self, tmp_path: Path, *objs: dict) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        for index, obj in enumerate(objs):
            (bodies / f"{index:02d}-body.request.json").write_text(
                json.dumps(obj), encoding="utf-8"
            )
        return bodies

    def _body(self, messages: list[dict]) -> dict:
        return {
            "tools": [self._TOOL],
            "system": [{"type": "text", "text": "sys"}],
            "messages": messages,
        }

    def _gen(self, obs_id: str, start: str, *, read: int, creation: int) -> dict:
        return _obs(
            obs_id,
            "claude_code.llm_request",
            type_="GENERATION",
            parent="i1",
            startTime=start,
            endTime=start,
            usageDetails={"cache_read_input_tokens": read, "cache_creation_input_tokens": creation},
        )

    def _traces(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:40Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        g1 = self._gen("g1", "2026-01-02T00:00:06Z", read=0, creation=100)
        g2 = self._gen("g2", "2026-01-02T00:00:12Z", read=100, creation=40)
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        return [("tr", [interaction, create, started, g1, g2, done])]

    def test_step_node_carries_context_rollup_both_views(self, tmp_path: Path) -> None:
        m1 = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        m2 = {"role": "assistant", "content": [{"type": "text", "text": "the newest turn"}]}
        added_tokens = len(_canonical_msg(m2))
        bodies = self._bodies(tmp_path, self._body([m1]), self._body([m1, m2]))
        traces = self._traces()
        batch = build_batch(traces, SPOKE, self._content())
        cycle = build_cycle_batch(traces, SPOKE, self._content())

        by_orig = {
            (o, i): s
            for (o, i), s in apply_context_deltas(
                batch, traces, bodies, counter=len, price=1.0, tool_content=self._content()
            ).items()
        }
        assert by_orig  # a delta was produced

        from telemetry.langfuse_spoke_tree import _apply_context_rollups, _copy_id

        _apply_context_rollups(batch, {_copy_id(o, i): s for (o, i), s in by_orig.items()})
        _apply_context_rollups(cycle, {cycle_copy_id_for(o, i): s for (o, i), s in by_orig.items()})

        view_a_step = _only_step(batch)
        view_b_step = _cycle_step(cycle, "step:S1 RED: x")
        expected = {"net_tokens": added_tokens, "added": added_tokens, "removed": 0}
        assert view_a_step["body"]["metadata"]["rollup"]["context"] == expected
        assert view_b_step["body"]["metadata"]["rollup"]["context"] == expected


class TestRequestBodyMetadata:
    """#101 parts 1 + 3: request-body-derived signals folded onto each llm_request copy.

    output_config.effort becomes ``metadata.effort`` + a trace-level ``effort:<value>`` tag
    (``ultra`` is the harness mode, diverted to a separate ``ultracode`` tag, never an effort),
    and the ``cache_control`` breakpoint positions become ``metadata.cache_breakpoints``.
    """

    _TOOL = {"name": "Bash", "description": "d" * 40, "input_schema": {"type": "object"}}

    def _bodies_dir(self, tmp_path: Path, *objs: dict) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        for index, obj in enumerate(objs):
            (bodies / f"{index:02d}-body.request.json").write_text(
                json.dumps(obj), encoding="utf-8"
            )
        return bodies

    def _obj(self, effort: str | None = None) -> dict:
        obj: dict = {
            "tools": [self._TOOL],
            "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        if effort is not None:
            obj["output_config"] = {"effort": effort}
        return obj

    def _gen(self, obs_id: str, start: str) -> dict:
        return _obs(
            obs_id,
            "llm_request",
            type_="GENERATION",
            parent="i1",
            startTime=start,
            usageDetails={"cache_read_input_tokens": 30, "cache_creation_input_tokens": 10},
        )

    def _meta(self, batch: list[dict], orig_trace: str, obs_id: str) -> dict:
        return _by_orig(batch, orig_trace, obs_id)["body"].get("metadata", {})

    def _trace_tags(self, batch: list[dict]) -> list[str]:
        trace = next(event for event in batch if event["type"] == "trace-create")
        return trace["body"].get("tags", [])

    def test_effort_attached_as_metadata_and_trace_tag(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj("high"))
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        count = apply_request_body_metadata(batch, traces, bodies)

        assert count == 1
        assert self._meta(batch, "tr", "g1")["effort"] == "high"
        assert "effort:high" in self._trace_tags(batch)

    def test_ultra_is_ultracode_tag_never_an_effort(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj("ultra"))
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        apply_request_body_metadata(batch, traces, bodies)

        # ultra never lands on effort metadata nor as an effort:* tag.
        assert "effort" not in self._meta(batch, "tr", "g1")
        tags = self._trace_tags(batch)
        assert "ultracode" in tags
        assert "effort:ultra" not in tags

    def test_absent_output_config_attaches_nothing(self, tmp_path: Path) -> None:
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        count = apply_request_body_metadata(batch, traces, bodies)

        assert count == 0
        assert "effort" not in self._meta(batch, "tr", "g1")
        assert self._trace_tags(batch) == []

    def test_count_mismatch_skips_effort(self, tmp_path: Path) -> None:
        # Two bodies but a single llm_request — positional alignment is unsafe → nothing attached.
        bodies = self._bodies_dir(tmp_path, self._obj("high"), self._obj("high"))
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        count = apply_request_body_metadata(batch, traces, bodies)

        assert count == 0
        assert "effort" not in self._meta(batch, "tr", "g1")

    def _obj_with_breakpoint(self) -> dict:
        # A body whose system block carries a cache_control marker → one CacheBoundary.
        return {
            "tools": [self._TOOL],
            "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }

    def test_cache_breakpoints_surfaced_on_llm_request_metadata(self, tmp_path: Path) -> None:
        # #101 part 3: the cache_control breakpoint positions are surfaced on the
        # llm_request node metadata (diagnoses "a moved breakpoint busted cache").
        bodies = self._bodies_dir(tmp_path, self._obj_with_breakpoint())
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        apply_request_body_metadata(batch, traces, bodies)

        assert self._meta(batch, "tr", "g1")["cache_breakpoints"] == [
            {"location": "system", "index": 0}
        ]

    def test_no_breakpoints_attaches_empty_list(self, tmp_path: Path) -> None:
        # A body with no cache_control marker still gets the key, as an empty list — so the
        # llm_request unambiguously records "no breakpoints" rather than "not measured".
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z")
        traces = [("tr", [gen])]
        batch = build_batch(traces, SPOKE)

        apply_request_body_metadata(batch, traces, bodies)

        assert self._meta(batch, "tr", "g1")["cache_breakpoints"] == []


class TestModeLaneTags:
    """#102: stamp a spoke's execution mode + lane on its reconstructed trace.

    ``read_mode_lane`` reads the ``.ai-toolkit/mode`` / ``.ai-toolkit/lane`` pointer files
    written at launch, defaulting safely (``attended`` / ``spoke``) when a pointer is missing,
    blank, or carries an unknown value. ``apply_mode_lane_tags`` attaches ``mode:<v>`` and
    ``lane:<v>`` as trace-level tags (groupable in Langfuse) and mirrors the bare values into
    trace metadata.
    """

    def _write_pointers(self, root: Path, *, mode: str | None, lane: str | None) -> Path:
        (root / ".ai-toolkit").mkdir(parents=True, exist_ok=True)
        if mode is not None:
            (root / ".ai-toolkit" / "mode").write_text(mode, encoding="utf-8")
        if lane is not None:
            (root / ".ai-toolkit" / "lane").write_text(lane, encoding="utf-8")
        return root

    def _trace(self, batch: list[dict]) -> dict:
        return next(event for event in batch if event["type"] == "trace-create")

    def test_reads_mode_and_lane_from_pointers(self, tmp_path: Path) -> None:
        root = self._write_pointers(tmp_path, mode="afk\n", lane="express\n")

        assert read_mode_lane(root) == ("afk", "express")

    def test_missing_pointers_default_to_attended_spoke(self, tmp_path: Path) -> None:
        # A legacy worktree with no pointer files must not crash — safe defaults.
        assert read_mode_lane(tmp_path) == ("attended", "spoke")

    def test_unknown_pointer_value_falls_back_to_default(self, tmp_path: Path) -> None:
        # A garbage/legacy value is not propagated as a mislabel — it defaults.
        root = self._write_pointers(tmp_path, mode="bogus", lane="micro")

        assert read_mode_lane(root) == ("attended", "micro")

    def test_blank_pointer_defaults(self, tmp_path: Path) -> None:
        root = self._write_pointers(tmp_path, mode="", lane="   ")

        assert read_mode_lane(root) == ("attended", "spoke")

    def test_apply_adds_mode_and_lane_trace_tags(self) -> None:
        batch = build_batch([], SPOKE)

        apply_mode_lane_tags(batch, "afk", "quick")

        tags = self._trace(batch)["body"]["tags"]
        assert "mode:afk" in tags
        assert "lane:quick" in tags

    def test_apply_mirrors_bare_values_into_trace_metadata(self) -> None:
        batch = build_batch([], SPOKE)

        apply_mode_lane_tags(batch, "attended", "spoke")

        metadata = self._trace(batch)["body"]["metadata"]
        assert metadata["mode"] == "attended"
        assert metadata["lane"] == "spoke"


def _by_cycle(batch: list[dict], orig_trace_id: str, orig_obs_id: str) -> dict:
    """Return the View B copy event for one source observation by its cycle copy id."""
    cid = cycle_copy_id_for(orig_trace_id, orig_obs_id)
    return next(event for event in batch if event["id"] == cid)


def _has_cycle_copy(batch: list[dict], orig_trace_id: str, orig_obs_id: str) -> bool:
    """Whether a View B copy node exists for one source observation."""
    cid = cycle_copy_id_for(orig_trace_id, orig_obs_id)
    return any(event["id"] == cid for event in batch)


def _cycle_step(batch: list[dict], name: str) -> dict:
    """Return the View B cycle-axis node (preStep / step:N / postStep) with the given name."""
    return next(
        e for e in batch if e["id"].startswith(_CYCLE_STEP_PREFIX) and e["body"]["name"] == name
    )


class TestCycleView:
    """#113 View B: a second ``spokecycle-<spoke>`` trace whose top level is the cycle axis —
    ``preStep`` + ``step:<subject>`` + ``postStep``. Real spans (``tool:*`` /
    ``claude_code.llm_request``) are placed under the step window containing their startTime; an
    audit instant rides along under its tool / llm_request by causal key (never its lagging
    timestamp); top-level interactions are flattened to childless leaf turn-markers. The copies
    carry distinct ids from View A.
    """

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _traces(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:40Z",
        )
        pre_tool = _ledger_child(
            "pt",
            "tool:Read",
            "tu-pt",
            parent="i1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:01Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        work_tool = _ledger_child(
            "wt",
            "tool:Edit",
            "tu-w",
            parent="i1",
            start="2026-01-02T00:00:10Z",
            end="2026-01-02T00:00:11Z",
        )
        work_gen = _obs(
            "wg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent="i1",
            startTime="2026-01-02T00:00:12Z",
            endTime="2026-01-02T00:00:13Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        post_tool = _ledger_child(
            "post",
            "tool:Bash",
            "tu-post",
            parent="i1",
            start="2026-01-02T00:00:30Z",
            end="2026-01-02T00:00:31Z",
        )
        # An audit instant (PostToolUse hook) joined to the work tool by tool_use_id; its own
        # startTime (00:35) lags far past the tool, so it must ride the tool, not be placed late.
        hook = _audit_event(
            "hk",
            "hook_execution_complete:PostToolUse",
            start="2026-01-02T00:00:35Z",
            tool_use_id="tu-w",
        )
        return [
            (
                "tr",
                [
                    interaction,
                    pre_tool,
                    create,
                    started,
                    work_tool,
                    work_gen,
                    done,
                    post_tool,
                    hook,
                ],
            )
        ]

    def test_emits_a_distinct_cycle_trace_and_root(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        trace = next(e for e in batch if e["type"] == "trace-create")
        assert trace["id"] == cycle_trace_id_for(SPOKE)
        assert trace["id"] != trace_id_for(SPOKE)
        assert any(e["id"] == cycle_root_id_for(SPOKE) for e in batch)

    def test_cycle_copy_ids_differ_from_view_a(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert cycle_copy_id_for("tr", "wt") != _copy_id("tr", "wt")
        assert _has_cycle_copy(batch, "tr", "wt")

    def test_pre_step_and_post_step_and_step_nodes_present_under_root(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        croot = cycle_root_id_for(SPOKE)
        for name in ("preStep", "step:S1 RED: x", "postStep"):
            assert _cycle_step(batch, name)["body"]["parentObservationId"] == croot

    def test_in_window_real_spans_placed_under_their_step(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        step = _cycle_step(batch, "step:S1 RED: x")["id"]
        assert _by_cycle(batch, "tr", "wt")["body"]["parentObservationId"] == step
        assert _by_cycle(batch, "tr", "wg")["body"]["parentObservationId"] == step

    def test_pre_step_span_lands_in_pre_step(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert (
            _by_cycle(batch, "tr", "pt")["body"]["parentObservationId"]
            == _cycle_step(batch, "preStep")["id"]
        )

    def test_post_step_span_lands_in_post_step(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert (
            _by_cycle(batch, "tr", "post")["body"]["parentObservationId"]
            == _cycle_step(batch, "postStep")["id"]
        )

    def test_audit_instant_rides_its_tool_not_a_step(self) -> None:
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert _by_cycle(batch, "tr", "hk")["body"]["parentObservationId"] == cycle_copy_id_for(
            "tr", "wt"
        )

    def test_top_level_interaction_kept_as_childless_leaf(self) -> None:
        # #114: the top-level turn is no longer dropped — it survives as a flat marker with no
        # children (its former children re-home onto their own steps, becoming its siblings).
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert _has_cycle_copy(batch, "tr", "i1")
        leaf_id = cycle_copy_id_for("tr", "i1")
        children = [e for e in batch if e.get("body", {}).get("parentObservationId") == leaf_id]
        assert children == []

    def test_interaction_leaf_lands_in_step_window_of_its_own_start(self) -> None:
        # #114: the marker's own start (00:00:00) precedes the task window (in_progress at 00:05),
        # so it lands in preStep — the cycle-axis window containing its start.
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        assert (
            _by_cycle(batch, "tr", "i1")["body"]["parentObservationId"]
            == _cycle_step(batch, "preStep")["id"]
        )

    def test_interaction_leaf_carries_turn_rollup_aggregate(self) -> None:
        # #114: a native interaction span carries no usageDetails — the tokens live on its
        # llm_request descendants (here `wg`). Once flattened to a childless leaf it would lose the
        # per-turn total, so the marker is stamped with metadata.rollup computed from its pre-flatten
        # View A subtree, recovering per-turn cost reading.
        traces = self._traces()
        work_gen = next(o for o in traces[0][1] if o["id"] == "wg")
        work_gen["usageDetails"] = {"input": 1200, "output": 340}

        batch = build_cycle_batch(traces, SPOKE, self._content())

        leaf = _by_cycle(batch, "tr", "i1")["body"]
        assert leaf["metadata"]["rollup"] == {
            "reused": 0,
            "written": 0,
            "input": 1200,
            "output": 340,
            "duration": _dur(40_000, {"tool": 4_000, "llm_request": 1_000, "self": 35_000}),
        }

    def test_interaction_leaf_rollup_is_not_double_counted_in_root(self) -> None:
        # #114: the marker carries the aggregate as metadata.rollup (not usageDetails), and its
        # generation re-homes onto a step as a sibling — so the cycle root counts the turn's tokens
        # exactly once, never the marker's aggregate plus the descendant again.
        traces = self._traces()
        work_gen = next(o for o in traces[0][1] if o["id"] == "wg")
        work_gen["usageDetails"] = {"input": 1200, "output": 340}

        batch = build_cycle_batch(traces, SPOKE, self._content())

        root = next(e for e in batch if e["id"] == cycle_root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"] == {
            "reused": 0,
            "written": 0,
            "input": 1200,
            "output": 340,
            "duration": _dur(40_000, {"tool": 4_000, "llm_request": 1_000, "step": 35_000}),
        }

    def test_interaction_leaf_carries_turn_duration_rollup(self) -> None:
        # #128, same rule as tokens: the childless marker's duration is computed from its
        # pre-flatten View A subtree — the turn's own 40s wall-clock split over its former
        # children (5 timed tools = 4s, one 1s llm_request) with the rest as its own gap.
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        leaf = _by_cycle(batch, "tr", "i1")["body"]
        assert leaf["metadata"]["rollup"]["duration"] == _dur(
            40_000, {"tool": 4_000, "llm_request": 1_000, "self": 35_000}
        )

    def test_cycle_root_duration_counts_each_span_once(self) -> None:
        # #128: on the cycle axis the marker's span overlaps its former children (now its step
        # siblings), so the marker is excluded from duration attribution — the root counts each
        # real span exactly once, and the steps' own gap time carries the remainder.
        batch = build_cycle_batch(self._traces(), SPOKE, self._content())

        root = next(e for e in batch if e["id"] == cycle_root_id_for(SPOKE))
        duration = root["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(40_000, {"tool": 4_000, "llm_request": 1_000, "step": 35_000})
        assert sum(duration["components"].values()) == duration["total_ms"]

    def _straddle_content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent({"subject": "one"}, "Task #1 created successfully: one"),
            "tu-s1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-e1": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
            "tu-c2": ToolContent({"subject": "two"}, "Task #2 created successfully: two"),
            "tu-s2": ToolContent({"taskId": "2", "status": "in_progress"}, "ok"),
            "tu-e2": ToolContent({"taskId": "2", "status": "completed"}, "ok"),
        }

    def _straddle_traces(self) -> list[tuple[str, list[dict]]]:
        # Step one: [00:05, 00:15]; step two: [00:25, 00:35]. One interaction whose own start
        # (00:00:10) sits inside step one, but whose work spans straddle BOTH steps.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:10Z",
            endTime="2026-01-02T00:00:40Z",
        )
        spans = [
            interaction,
            _ledger_child(
                "c1",
                "tool:TaskCreate",
                "tu-c1",
                parent="i1",
                start="2026-01-02T00:00:01Z",
                end="2026-01-02T00:00:01Z",
            ),
            _ledger_child(
                "s1",
                "tool:TaskUpdate",
                "tu-s1",
                parent="i1",
                start="2026-01-02T00:00:05Z",
                end="2026-01-02T00:00:05Z",
            ),
            _ledger_child(
                "wa",
                "tool:Edit",
                "tu-wa",
                parent="i1",
                start="2026-01-02T00:00:12Z",
                end="2026-01-02T00:00:13Z",
            ),
            _ledger_child(
                "e1",
                "tool:TaskUpdate",
                "tu-e1",
                parent="i1",
                start="2026-01-02T00:00:15Z",
                end="2026-01-02T00:00:15Z",
            ),
            _ledger_child(
                "c2",
                "tool:TaskCreate",
                "tu-c2",
                parent="i1",
                start="2026-01-02T00:00:20Z",
                end="2026-01-02T00:00:20Z",
            ),
            _ledger_child(
                "s2",
                "tool:TaskUpdate",
                "tu-s2",
                parent="i1",
                start="2026-01-02T00:00:25Z",
                end="2026-01-02T00:00:25Z",
            ),
            _ledger_child(
                "wb",
                "tool:Bash",
                "tu-wb",
                parent="i1",
                start="2026-01-02T00:00:30Z",
                end="2026-01-02T00:00:31Z",
            ),
            _ledger_child(
                "e2",
                "tool:TaskUpdate",
                "tu-e2",
                parent="i1",
                start="2026-01-02T00:00:35Z",
                end="2026-01-02T00:00:35Z",
            ),
        ]
        return [("tr", spans)]

    def test_straddle_marker_in_start_step_children_stay_distributed(self) -> None:
        # #114 straddle: the interaction marker lands in its START step (step:one), while its
        # former work spans stay distributed across their own steps (wa->one, wb->two).
        batch = build_cycle_batch(self._straddle_traces(), SPOKE, self._straddle_content())

        assert (
            _by_cycle(batch, "tr", "i1")["body"]["parentObservationId"]
            == _cycle_step(batch, "step:one")["id"]
        )
        assert (
            _by_cycle(batch, "tr", "wa")["body"]["parentObservationId"]
            == _cycle_step(batch, "step:one")["id"]
        )
        assert (
            _by_cycle(batch, "tr", "wb")["body"]["parentObservationId"]
            == _cycle_step(batch, "step:two")["id"]
        )

    def test_non_ledger_spoke_emits_no_cycle_step_nodes(self) -> None:
        batch = build_cycle_batch(_traces(), SPOKE)

        assert not any(e["id"].startswith(_CYCLE_STEP_PREFIX) for e in batch)

    def test_non_ledger_spoke_keeps_interaction_as_leaf_under_root(self) -> None:
        # #114: even without a ledger (no cycle axis), the top-level turn survives as a childless
        # leaf hanging under the cycle root rather than being dropped.
        batch = build_cycle_batch(_traces(), SPOKE)

        assert _has_cycle_copy(batch, "trace-int", "i1")
        leaf = _by_cycle(batch, "trace-int", "i1")["body"]
        assert leaf["parentObservationId"] == cycle_root_id_for(SPOKE)
        leaf_id = cycle_copy_id_for("trace-int", "i1")
        assert not [e for e in batch if e.get("body", {}).get("parentObservationId") == leaf_id]

    def _gap_content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent({"subject": "one"}, "Task #1 created successfully: one"),
            "tu-s1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-e1": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
            "tu-c2": ToolContent({"subject": "two"}, "Task #2 created successfully: two"),
            "tu-s2": ToolContent({"taskId": "2", "status": "in_progress"}, "ok"),
            "tu-e2": ToolContent({"taskId": "2", "status": "completed"}, "ok"),
        }

    def _gap_traces(self) -> list[tuple[str, list[dict]]]:
        # Step one: [00:01, 00:10]; step two: [00:30, 00:40]. A span at 00:20 lands in the gap.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:45Z",
        )
        spans = [
            interaction,
            _ledger_child(
                "c1",
                "tool:TaskCreate",
                "tu-c1",
                parent="i1",
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:00Z",
            ),
            _ledger_child(
                "s1",
                "tool:TaskUpdate",
                "tu-s1",
                parent="i1",
                start="2026-01-02T00:00:01Z",
                end="2026-01-02T00:00:01Z",
            ),
            _ledger_child(
                "e1",
                "tool:TaskUpdate",
                "tu-e1",
                parent="i1",
                start="2026-01-02T00:00:10Z",
                end="2026-01-02T00:00:10Z",
            ),
            _ledger_child(
                "gap",
                "tool:Edit",
                "tu-gap",
                parent="i1",
                start="2026-01-02T00:00:20Z",
                end="2026-01-02T00:00:20Z",
            ),
            _ledger_child(
                "c2",
                "tool:TaskCreate",
                "tu-c2",
                parent="i1",
                start="2026-01-02T00:00:29Z",
                end="2026-01-02T00:00:29Z",
            ),
            _ledger_child(
                "s2",
                "tool:TaskUpdate",
                "tu-s2",
                parent="i1",
                start="2026-01-02T00:00:30Z",
                end="2026-01-02T00:00:30Z",
            ),
            _ledger_child(
                "e2",
                "tool:TaskUpdate",
                "tu-e2",
                parent="i1",
                start="2026-01-02T00:00:40Z",
                end="2026-01-02T00:00:40Z",
            ),
        ]
        return [("tr", spans)]

    def test_gap_span_attaches_to_the_preceding_step(self) -> None:
        batch = build_cycle_batch(self._gap_traces(), SPOKE, self._gap_content())

        assert (
            _by_cycle(batch, "tr", "gap")["body"]["parentObservationId"]
            == _cycle_step(batch, "step:one")["id"]
        )

    def _subagent_traces(self) -> list[tuple[str, list[dict]]]:
        # A tool:Agent (in-window) owns a nested sub-agent interaction + sub-tool via parent links.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:30Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        agent = _ledger_child(
            "wa",
            "tool:Agent",
            "tu-wa",
            parent="i1",
            start="2026-01-02T00:00:08Z",
            end="2026-01-02T00:00:18Z",
        )
        sub_i = _obs(
            "subi",
            "claude_code.interaction",
            parent="wa",
            startTime="2026-01-02T00:00:09Z",
            endTime="2026-01-02T00:00:17Z",
        )
        sub_tool = _ledger_child(
            "st",
            "tool:Read",
            "tu-st",
            parent="subi",
            start="2026-01-02T00:00:10Z",
            end="2026-01-02T00:00:11Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        return [("tr", [interaction, create, started, agent, sub_i, sub_tool, done])]

    def test_nested_interaction_survives_and_rides_its_agent_tool(self) -> None:
        # Only TOP-LEVEL interactions flatten to leaf markers: a sub-agent interaction nested under a
        # tool:Agent survives as a container and rides the (timestamp-placed) agent tool, and its
        # sub-tool rides it in turn.
        batch = build_cycle_batch(self._subagent_traces(), SPOKE, self._content())

        step = _cycle_step(batch, "step:S1 RED: x")["id"]
        assert _by_cycle(batch, "tr", "wa")["body"]["parentObservationId"] == step
        assert _has_cycle_copy(batch, "tr", "subi")
        assert _by_cycle(batch, "tr", "subi")["body"]["parentObservationId"] == cycle_copy_id_for(
            "tr", "wa"
        )
        assert _by_cycle(batch, "tr", "st")["body"]["parentObservationId"] == cycle_copy_id_for(
            "tr", "subi"
        )

    def test_audit_instant_on_flattened_turn_anchored_by_turn_start(self) -> None:
        # A skill_activated that resolves to a flattened top-level interaction (shared prompt.id) is
        # anchored by the TURN's start (here -> preStep), never by its own lagging timestamp (00:38,
        # which would land it in postStep). Exercises _resolve_cycle_parent's anchor branch.
        traces = self._traces()
        traces[0][1][0]["metadata"] = {"attributes": {"prompt.id": "p1"}}  # interaction i1
        skill = _audit_event(
            "sk1",
            "skill_activated",
            start="2026-01-02T00:00:38Z",
            **{"prompt.id": "p1", "skill.name": "source-task"},
        )
        traces[0][1].append(skill)

        batch = build_cycle_batch(traces, SPOKE, self._content())

        assert (
            _by_cycle(batch, "tr", "sk1")["body"]["parentObservationId"]
            == _cycle_step(batch, "preStep")["id"]
        )


class TestStepPhaseParser:
    """#158: map an arbitrary step subject into the closed phase set (cardinality pin)."""

    @pytest.mark.parametrize(
        "subject,phase",
        [
            ("S1 RED: failing test", "RED"),
            ("A-RED: red first", "RED"),
            ("ANCHOR #154 source the issue", "ANCHOR"),
            ("S2 GREEN: implement", "GREEN"),
            ("S1 REVIEW + PUSH", "REVIEW"),
            ("S4 PUSH final subtask", "PUSH"),
            ("miscellaneous chore", "other"),
        ],
    )
    def test_maps_subject_into_closed_set(self, subject: str, phase: str) -> None:
        assert _step_phase(subject) == phase

    def test_unknown_subject_yields_other_never_free_text(self) -> None:
        assert _step_phase("totally unrelated subject 123") == "other"


class TestStepCostScores:
    """#158: per-phase ``step_cache_write_usd:<PHASE>`` / ``step_tokens_written:<PHASE>`` scores.

    ``step:*`` nodes carry token rollups only in ``metadata.rollup`` (no ``usageDetails`` — the
    #114 double-count guard), so per-step cost is invisible to the Metrics API. Score NAMES are a
    metrics dimension, so each View B step emits its cost/written from the rollup (cost = written x
    price). Single-emit on View B only, so a Scores sum never doubles a phase across both views.
    """

    _BASE_TS = "2026-01-02T00:00:00Z"

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _traces(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:40Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        work_gen = _obs(
            "wg",
            "claude_code.llm_request",
            type_="GENERATION",
            parent="i1",
            startTime="2026-01-02T00:00:12Z",
            endTime="2026-01-02T00:00:13Z",
            usageDetails={
                "input": 10,
                "output": 4,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 50,
            },
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        return [("tr", [interaction, create, started, work_gen, done])]

    def test_emits_cost_and_tokens_scores_for_the_red_step(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        names = {s["body"]["name"] for s in scores}
        assert "step_cache_write_usd:RED" in names
        assert "step_tokens_written:RED" in names

    def test_boundary_partition_maps_to_pre_phase(self) -> None:
        # preStep (the pre-first-window partition) carries ledger tools, so it emits its own
        # boundary-phase score — locking the closed-set pre/post branch of the parser.
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        pre = _cycle_step(cycle, "preStep")
        assert any(
            s["body"]["name"] == "step_cache_write_usd:pre"
            and s["body"]["observationId"] == pre["body"]["id"]
            for s in scores
        )

    def test_cost_is_written_times_price_and_observation_scoped(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())
        step = _cycle_step(cycle, "step:S1 RED: x")
        written = step["body"]["metadata"]["rollup"]["written"]

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        cost = next(s for s in scores if s["body"]["name"] == "step_cache_write_usd:RED")
        assert cost["body"]["value"] == pytest.approx(written * 0.001)
        assert cost["body"]["observationId"] == step["body"]["id"]

    def test_tokens_written_score_equals_rollup_written(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())
        step = _cycle_step(cycle, "step:S1 RED: x")

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        tokens = next(s for s in scores if s["body"]["name"] == "step_tokens_written:RED")
        assert tokens["body"]["value"] == step["body"]["metadata"]["rollup"]["written"]

    def test_exactly_one_cost_score_for_the_ledger_step(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())

        scores = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        red_costs = [s for s in scores if s["body"]["name"] == "step_cache_write_usd:RED"]
        assert len(red_costs) == 1

    def test_view_a_score_events_carry_no_step_cost_scores(self) -> None:
        # Single-emit: View A's build_score_events never emits a step cost score.
        batch = build_batch(self._traces(), SPOKE, self._content())

        view_a = build_score_events(SPOKE, self._traces(), batch, base_ts=self._BASE_TS)

        assert not any(s["body"]["name"].startswith("step_cache_write_usd:") for s in view_a)

    def test_scores_are_deterministic_across_reruns(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())

        first = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)
        second = build_step_cost_scores(SPOKE, cycle, base_ts=self._BASE_TS, price=0.001)

        assert [e["id"] for e in first] == [e["id"] for e in second]

    def test_no_step_node_carries_usage_or_model(self) -> None:
        # Double-count pin: step nodes in BOTH views stay free of usageDetails / model.
        batch = build_batch(self._traces(), SPOKE, self._content())
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content())

        step_nodes = [
            e
            for e in batch + cycle
            if e["id"].startswith(_STEP_PREFIX) or e["id"].startswith(_CYCLE_STEP_PREFIX)
        ]
        assert step_nodes  # the fixture actually produces step nodes
        for event in step_nodes:
            assert "usageDetails" not in event["body"]
            assert "model" not in event["body"]


def _commit(sha: str, message: str, at: str, files: list[str], add: int, dele: int) -> dict:
    """A parsed commit record as the builder consumes it."""
    return {
        "sha": sha,
        "message": message,
        "authored_at": at,
        "files": files,
        "additions": add,
        "deletions": dele,
    }


def _commit_node(batch: list[dict], sha7: str) -> dict:
    """Return the synthesized commit:<sha7> node, asserting it exists."""
    return next(e for e in batch if e["body"]["name"] == f"commit:{sha7}")


def _gate_park_node(batch: list[dict]) -> dict | None:
    """Return the synthesized wait:gate-park node, or None when absent."""
    return next((e for e in batch if e["body"]["name"] == "wait:gate-park"), None)


class TestParseCommits:
    """#162: parse a ``git log --numstat`` dump into commit records."""

    def test_parses_sha_message_time_and_numstat(self) -> None:
        sep = "\x1f"
        dump = (
            f"commit{sep}abcdef1234567{sep}2026-01-02T00:00:05+00:00{sep}feat: a thing\n"
            "3\t1\tsrc/a.py\n"
            "5\t0\tsrc/b.py\n"
            "\n"
            f"commit{sep}0123456abcdef{sep}2026-01-02T00:00:20+00:00{sep}fix: b thing\n"
            "-\t-\tbin/blob\n"
        )

        commits = _parse_commits(dump)

        assert commits[0] == _commit(
            "abcdef1234567",
            "feat: a thing",
            "2026-01-02T00:00:05+00:00",
            ["src/a.py", "src/b.py"],
            8,
            1,
        )
        # Binary files show "-" for add/del and contribute 0.
        assert commits[1]["additions"] == 0
        assert commits[1]["deletions"] == 0
        assert commits[1]["files"] == ["bin/blob"]

    def test_empty_dump_yields_no_commits(self) -> None:
        assert _parse_commits("") == []


class TestCommitNodes:
    """#162: synthesize commit:<sha7> timeline nodes placed by author time."""

    _SHA = "abcdef1234567890"
    _COMMIT = _commit(_SHA, "feat: land it", "2026-01-02T00:00:12Z", ["a.py"], 4, 2)

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _traces(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:40Z",
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        return [("tr", [interaction, create, started, done])]

    def test_commit_node_carries_metadata_and_no_usage_view_a(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])

        node = _commit_node(batch, "abcdef1")["body"]
        assert node["metadata"] == {
            "sha": self._SHA,
            "message": "feat: land it",
            "files": ["a.py"],
            "additions": 4,
            "deletions": 2,
        }
        assert "usageDetails" not in node

    def test_commit_node_placed_in_containing_step_window_view_b(self) -> None:
        cycle = build_cycle_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])

        # Author time 00:12 falls inside the task window [00:05, 00:21] → under step:S1 RED: x.
        step = _cycle_step(cycle, "step:S1 RED: x")
        assert _commit_node(cycle, "abcdef1")["body"]["parentObservationId"] == step["body"]["id"]

    def test_no_commits_arg_emits_no_commit_node(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        assert not any(e["body"]["name"].startswith("commit:") for e in batch)

    def test_double_build_is_byte_identical(self) -> None:
        first = build_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])
        second = build_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])

        assert first == second

    def test_cycle_double_build_is_byte_identical(self) -> None:
        first = build_cycle_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])
        second = build_cycle_batch(self._traces(), SPOKE, self._content(), commits=[self._COMMIT])

        assert first == second

    def test_out_of_window_commit_does_not_inflate_root_duration(self) -> None:
        # A commit authored long after the last captured span must not stretch the end-time-less
        # root's subtree interval — commit instants are excluded from duration attribution.
        far = _commit("f" * 12, "chore: late", "2026-01-02T00:59:00Z", ["z.py"], 1, 0)

        without = build_batch(self._traces(), SPOKE, self._content())
        withcommit = build_batch(self._traces(), SPOKE, self._content(), commits=[far])

        def _root_total(batch: list[dict]) -> int:
            root = next(e for e in batch if e["id"] == root_id_for(SPOKE))
            return root["body"]["metadata"]["rollup"]["duration"]["total_ms"]

        assert _root_total(withcommit) == _root_total(without)


class TestGateParkNode:
    """#162: synthesize a wait:gate-park timeline block from the gate-park bounds."""

    def _traces(self) -> list[tuple[str, list[dict]]]:
        gate = _obs(
            "g1",
            "script:gate",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
        )
        resume = _obs(
            "t1",
            "tool:Edit",
            parent=None,
            startTime="2026-01-02T00:00:11Z",
            endTime="2026-01-02T00:00:12Z",
        )
        return [("tr", [gate, resume])]

    def test_gate_park_node_duration_equals_gate_park_ms(self) -> None:
        traces = self._traces()
        batch = build_batch(traces, SPOKE)

        node = _gate_park_node(batch)
        assert node is not None
        assert node["body"]["parentObservationId"] == root_id_for(SPOKE)
        assert "usageDetails" not in node["body"]
        # The block spans the gate's end to the resume activity's start (== gate_park_ms).
        assert node["body"]["startTime"] == "2026-01-02T00:00:01Z"
        assert node["body"]["endTime"] == "2026-01-02T00:00:11Z"
        assert _gate_park_ms(traces) == 10000

    def test_park_time_moves_from_root_self_to_wait(self) -> None:
        traces = self._traces()
        batch = build_batch(traces, SPOKE)

        root = next(e for e in batch if e["id"] == root_id_for(SPOKE))
        duration = root["body"]["metadata"]["rollup"]["duration"]
        # base_ts 00:00 → latest 00:12 = 12000ms, fully attributed; the 10s park is wait, not self.
        assert duration["total_ms"] == 12000
        assert duration["components"]["wait"] == 11000  # 1s gate span + 10s park
        assert duration["components"]["self"] == 0

    def test_no_gate_emits_no_wait_node(self) -> None:
        traces = [
            (
                "tr",
                [
                    _obs(
                        "t1",
                        "tool:Edit",
                        parent=None,
                        startTime="2026-01-02T00:00:00Z",
                        endTime="2026-01-02T00:00:01Z",
                    )
                ],
            )
        ]

        batch = build_batch(traces, SPOKE)

        assert _gate_park_node(batch) is None


def _guard(
    obs_id: str,
    name: str,
    *,
    tool_use_id: str | None = None,
    start: str,
    end: str,
    decision: str = "allow",
    status: str = "success",
    **attrs: object,
) -> dict:
    """Build a ``.sh`` guard-hook source span with decision/status under metadata['attributes']."""
    attributes: dict[str, object] = {
        "workflow.kind": "hook",
        "decision": decision,
        "status": status,
    }
    if tool_use_id:
        attributes["tool_use_id"] = tool_use_id
    attributes.update(attrs)
    return _obs(
        obs_id, name, parent=None, startTime=start, endTime=end, metadata={"attributes": attributes}
    )


def _guards_group(batch: list[dict]) -> dict:
    """Return the single per-tool ``guards`` group node, asserting exactly one exists."""
    groups = [event for event in batch if event["body"].get("name") == "guards"]
    assert len(groups) == 1
    return groups[0]


def _session_guards(batch: list[dict]) -> dict:
    """Return the single root ``guards:session`` group node, asserting exactly one exists."""
    groups = [event for event in batch if event["body"].get("name") == "guards:session"]
    assert len(groups) == 1
    return groups[0]


def _guarded_tool_traces() -> list[tuple[str, list[dict]]]:
    """One turn: a 5s tool with two no-op (allow/success, <1s) ``.sh`` guards sharing its id."""
    interaction = _obs(
        "i1",
        "claude_code.interaction",
        parent=None,
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-02T00:00:10Z",
    )
    tool = _obs(
        "t1",
        "tool:Bash",
        parent="i1",
        startTime="2026-01-02T00:00:00Z",
        endTime="2026-01-02T00:00:05Z",
        metadata={"attributes": {"tool_use_id": "tu-1"}},
    )
    pre = _guard(
        "h1",
        "PreToolUse.sh",
        tool_use_id="tu-1",
        start="2026-01-02T00:00:00Z",
        end="2026-01-02T00:00:00.400Z",  # 400ms noop
    )
    post = _guard(
        "h2",
        "PostToolUse.sh",
        tool_use_id="tu-1",
        start="2026-01-02T00:00:04Z",
        end="2026-01-02T00:00:04.500Z",  # 500ms noop
    )
    return [("tr", [interaction, tool, pre, post])]


class TestGuardGroups:
    """#157: `.sh` guard spans join a per-tool ``guards`` group (and a root ``guards:session``);
    no-op raw spans drop by default but their stats survive in the group's ``by_hook`` rollup."""

    def test_guards_group_created_under_the_tool(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        assert _guards_group(batch)["body"]["parentObservationId"] == _copy_id("tr", "t1")

    def test_noop_guards_dropped_by_default(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        ids = {event["id"] for event in batch}
        assert _copy_id("tr", "h1") not in ids
        assert _copy_id("tr", "h2") not in ids

    def test_keep_noop_guards_retains_children_under_the_group(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE, keep_noop_guards=True)

        group_id = _guards_group(batch)["body"]["id"]
        assert _by_orig(batch, "tr", "h1")["body"]["parentObservationId"] == group_id
        assert _by_orig(batch, "tr", "h2")["body"]["parentObservationId"] == group_id

    def test_by_hook_rollup_counts_all_raw_including_dropped(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        metadata = _guards_group(batch)["body"]["metadata"]
        assert metadata["count"] == 2
        assert metadata["total_ms"] == 900
        assert metadata["by_hook"] == {
            "PostToolUse.sh": {"count": 1, "ms": 500},
            "PreToolUse.sh": {"count": 1, "ms": 400},
        }
        assert metadata["decisions"] == ["allow"]

    def test_non_allow_guard_kept_even_by_default(self) -> None:
        interaction, tool, _pre, _post = _guarded_tool_traces()[0][1]
        deny = _guard(
            "h3",
            "PreToolUse.sh",
            tool_use_id="tu-1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01.200Z",
            decision="deny",
        )

        batch = build_batch([("tr", [interaction, tool, deny])], SPOKE)

        assert (
            _by_orig(batch, "tr", "h3")["body"]["parentObservationId"]
            == _guards_group(batch)["body"]["id"]
        )

    def test_slow_allow_guard_kept_even_by_default(self) -> None:
        interaction, tool, _pre, _post = _guarded_tool_traces()[0][1]
        slow = _guard(
            "h4",
            "PreToolUse.sh",
            tool_use_id="tu-1",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:02Z",  # 2s allow/success
        )

        batch = build_batch([("tr", [interaction, tool, slow])], SPOKE)

        assert (
            _by_orig(batch, "tr", "h4")["body"]["parentObservationId"]
            == _guards_group(batch)["body"]["id"]
        )

    def test_group_body_carries_no_usage_or_model(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        body = _guards_group(batch)["body"]
        assert "usageDetails" not in body
        assert "model" not in body

    def test_session_guards_group_under_root(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        start_hook = _guard(
            "s1",
            "SessionStart.sh",
            start="2026-01-02T00:00:00Z",
            end="2026-01-02T00:00:00.300Z",
        )
        stop_hook = _guard(
            "s2",
            "Stop.sh",
            start="2026-01-02T00:00:09Z",
            end="2026-01-02T00:00:09.200Z",
        )

        batch = build_batch([("tr", [interaction, start_hook, stop_hook])], SPOKE)

        session = _session_guards(batch)
        assert session["body"]["parentObservationId"] == root_id_for(SPOKE)
        assert session["body"]["metadata"]["count"] == 2

    def test_guards_group_present_in_cycle_view(self) -> None:
        batch = build_cycle_batch(_guarded_tool_traces(), SPOKE)

        assert len([e for e in batch if e["body"].get("name") == "guards"]) == 1

    def test_double_build_is_byte_identical(self) -> None:
        first = json.dumps(build_batch(_guarded_tool_traces(), SPOKE))
        second = json.dumps(build_batch(_guarded_tool_traces(), SPOKE))

        assert first == second


class TestGuardGroupDuration:
    """#157 AC2: the ``hook`` duration bucket reflects real guard time (sum of raw ``.sh``
    durations), and ``rollup.duration.components`` is identical with --keep-noop-guards on/off."""

    def test_components_identical_keep_noop_on_off(self) -> None:
        default = build_batch(_guarded_tool_traces(), SPOKE)
        kept = build_batch(_guarded_tool_traces(), SPOKE, keep_noop_guards=True)

        default_dur = _by_orig(default, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        kept_dur = _by_orig(kept, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert default_dur == kept_dur

    def test_hook_bucket_is_sum_of_raw_guard_durations(self) -> None:
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration["components"]["hook"] == 900

    def test_guard_grouping_preserves_tool_execution_time(self) -> None:
        # The Pre+Post-with-gap guards must NOT erase the tool's inter-guard execution: the
        # group covers only its real 900ms of guard time, so the 5s tool still books 4.1s and
        # the components sum to the interaction wall-clock (the #128 invariant).
        batch = build_batch(_guarded_tool_traces(), SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 4_100, "hook": 900, "self": 5_000})
        assert sum(duration["components"].values()) == duration["total_ms"]


def _blocked_tools(batch: list[dict]) -> list[dict]:
    """Return every synthesized ``blocked-tool:*`` node in a batch."""
    return [e for e in batch if (e["body"].get("name") or "").startswith("blocked-tool:")]


def _one_blocked(batch: list[dict]) -> dict:
    """Return the single synthesized ``blocked-tool:*`` node, asserting exactly one exists."""
    nodes = _blocked_tools(batch)
    assert len(nodes) == 1
    return nodes[0]


class TestBlockedToolSynthesis:
    """#157: an orphaned tool_use_id (satellites but no tool span — a denied/never-run call)
    gets a synthesized `blocked-tool:<Name>` node, WARNING, no usage/model, never a `tool:`
    prefix, parented to its enclosing turn, that its hooks / audit events / decision adopt."""

    def _orphan_audit(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        hook = _obs(
            "h1",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={
                "hook_event": "PreToolUse",
                "hook_name": "PreToolUse:Edit",
                "tool_use_id": "tu-denied",
                "prompt.id": "p1",
            },
        )
        return [("trace-int", [interaction]), ("trace-audit", [hook])]

    def test_orphaned_audit_event_synthesizes_blocked_tool_under_turn(self) -> None:
        batch = build_batch(self._orphan_audit(), SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        assert (
            _by_orig(batch, "trace-audit", "h1")["body"]["parentObservationId"]
            == blocked["body"]["id"]
        )

    def test_blocked_tool_name_from_hook_name_suffix(self) -> None:
        batch = build_batch(self._orphan_audit(), SPOKE)

        assert _one_blocked(batch)["body"]["name"] == "blocked-tool:Edit"

    def test_blocked_tool_is_warning_and_carries_no_usage(self) -> None:
        batch = build_batch(self._orphan_audit(), SPOKE)

        body = _one_blocked(batch)["body"]
        assert body["level"] == "WARNING"
        assert "usageDetails" not in body
        assert "model" not in body

    def test_blocked_tool_never_uses_tool_prefix(self) -> None:
        batch = build_batch(self._orphan_audit(), SPOKE)

        assert not _one_blocked(batch)["body"]["name"].startswith("tool:")

    def test_matched_tool_produces_no_blocked_node(self) -> None:
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        tool = _obs(
            "t1", "tool:Edit", parent="i1", metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        hook = _obs(
            "h1",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            metadata={"hook_name": "PreToolUse:Edit", "tool_use_id": "tu-1"},
        )

        batch = build_batch([("tr", [interaction, tool, hook])], SPOKE)

        assert _blocked_tools(batch) == []

    def test_orphaned_gate_hook_adopts_blocked_tool_via_guards_group(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        gate = _guard(
            "h1",
            "PreToolUse.sh",
            tool_use_id="tu-denied",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05.300Z",
            decision="deny",
            **{"tool_name": "Bash"},
        )

        batch = build_batch([("trace-int", [interaction]), ("trace-hook", [gate])], SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["name"] == "blocked-tool:Bash"
        assert blocked["body"]["parentObservationId"] == _copy_id("trace-int", "i1")
        group = _guards_group(batch)
        assert group["body"]["parentObservationId"] == blocked["body"]["id"]

    def test_orphaned_tool_decision_folds_onto_blocked_tool(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        decision = _obs(
            "d1",
            "tool_decision:deny",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-denied", "decision": "deny", "prompt.id": "p1"},
        )

        batch = build_batch([("trace-int", [interaction]), ("trace-audit", [decision])], SPOKE)

        blocked = _one_blocked(batch)
        assert blocked["body"]["metadata"]["decision"] == "deny"
        # the decision sub-span folds into the node and is dropped
        assert all(e["id"] != _copy_id("trace-audit", "d1") for e in batch)

    def test_blocked_tool_name_falls_back_to_unknown(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        result = _obs(
            "r1",
            "tool_result",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-denied", "prompt.id": "p1"},
        )

        batch = build_batch([("trace-int", [interaction]), ("trace-audit", [result])], SPOKE)

        assert _one_blocked(batch)["body"]["name"] == "blocked-tool:unknown"

    def test_blocked_tool_present_in_cycle_view(self) -> None:
        batch = build_cycle_batch(self._orphan_audit(), SPOKE)

        assert len(_blocked_tools(batch)) == 1

    def test_blocked_tool_double_build_is_byte_identical(self) -> None:
        first = json.dumps(build_batch(self._orphan_audit(), SPOKE))
        second = json.dumps(build_batch(self._orphan_audit(), SPOKE))

        assert first == second

    def test_blocked_tool_in_cycle_view_anchors_to_turn_start_not_lagging_time(self) -> None:
        # Regression: the blocked node's own start is a LAGGING audit timestamp (35s, in the
        # postStep). In the ledgered cycle view it must anchor to its turn's start (0s -> preStep),
        # never land in the step its lag happens to fall in.
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:40Z",
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        create = _ledger_child(
            "tc1",
            "tool:TaskCreate",
            "tu-c1",
            parent="i1",
            start="2026-01-02T00:00:02Z",
            end="2026-01-02T00:00:02Z",
        )
        started = _ledger_child(
            "tu1",
            "tool:TaskUpdate",
            "tu-u1",
            parent="i1",
            start="2026-01-02T00:00:05Z",
            end="2026-01-02T00:00:05Z",
        )
        done = _ledger_child(
            "tu2",
            "tool:TaskUpdate",
            "tu-u2",
            parent="i1",
            start="2026-01-02T00:00:20Z",
            end="2026-01-02T00:00:21Z",
        )
        decision = _obs(
            "d1",
            "tool_decision:deny",
            type_="EVENT",
            parent=None,
            startTime="2026-01-02T00:00:35Z",
            metadata={"tool_use_id": "tu-denied", "decision": "deny", "prompt.id": "p1"},
        )
        content = {
            "tu-c1": ToolContent({"subject": "S1"}, "Task #1 created successfully: S1"),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

        batch = build_cycle_batch(
            [("tr", [interaction, create, started, done, decision])], SPOKE, content
        )

        assert (
            _one_blocked(batch)["body"]["parentObservationId"]
            == _cycle_step(batch, "preStep")["id"]
        )

    def test_multiple_orphans_get_distinct_blocked_tools(self) -> None:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            metadata={"attributes": {"prompt.id": "p1"}},
        )
        d_a = _obs(
            "da",
            "tool_decision:deny",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-a", "decision": "deny", "prompt.id": "p1"},
        )
        d_b = _obs(
            "db",
            "tool_decision:ask",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-b", "decision": "ask", "prompt.id": "p1"},
        )

        batch = build_batch([("trace-int", [interaction]), ("trace-audit", [d_a, d_b])], SPOKE)

        blocked = _blocked_tools(batch)
        assert len(blocked) == 2
        assert len({b["body"]["id"] for b in blocked}) == 2


class TestHookEndTimeStamping:
    """#157: hook_execution_complete events carry total_duration_ms but no endTime; stamp
    endTime = startTime + total_duration_ms (time_source: lagging) and EXCLUDE them from
    duration attribution, since that duration duplicates the .sh spans already in the hook
    bucket — so rollup.duration.components is identical before vs after stamping (AC2 pin)."""

    def _hook_under_tool(self, *, total_duration_ms: int | None = 2000) -> list:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        tool = _obs(
            "t1",
            "tool:Edit",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        meta: dict = {"tool_use_id": "tu-1"}
        if total_duration_ms is not None:
            meta["total_duration_ms"] = total_duration_ms
        hook = _obs(
            "h1",
            "hook_execution_complete:PostToolUse",
            type_="EVENT",
            parent=None,
            startTime="2026-01-02T00:00:01Z",
            metadata=meta,
        )
        return [("tr", [interaction, tool, hook])]

    def test_endtime_stamped_from_total_duration_ms(self) -> None:
        batch = build_batch(self._hook_under_tool(), SPOKE)

        body = _by_orig(batch, "tr", "h1")["body"]
        assert body["endTime"] == "2026-01-02T00:00:03Z"
        assert body["metadata"]["time_source"] == "lagging"

    def test_hook_event_without_total_duration_is_not_stamped(self) -> None:
        batch = build_batch(self._hook_under_tool(total_duration_ms=None), SPOKE)

        body = _by_orig(batch, "tr", "h1")["body"]
        assert body.get("endTime") is None
        assert "time_source" not in (body.get("metadata") or {})

    def test_stamped_hook_event_excluded_from_view_a_components(self) -> None:
        # The pin: the stamped 2s duration must NOT appear in any bucket — the tool books its
        # full 5s and there is no "other", exactly as when the event had no endTime.
        batch = build_batch(self._hook_under_tool(), SPOKE)

        duration = _by_orig(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 5_000, "self": 5_000})

    def test_stamped_hook_event_excluded_from_cycle_turn_marker_rollup(self) -> None:
        # The same pin for View B: a flattened turn-marker's own rollup.duration (#114) must also
        # exclude the stamped hook event.
        batch = build_cycle_batch(self._hook_under_tool(), SPOKE)

        duration = _by_cycle(batch, "tr", "i1")["body"]["metadata"]["rollup"]["duration"]
        assert duration == _dur(10_000, {"tool": 5_000, "self": 5_000})


class TestFailureLevels:
    """#157: fold failure data onto Langfuse levels (was all DEFAULT). Tool success=false/error ->
    ERROR; a guard whose decision is deny/ask/block or whose status != success -> WARNING (span AND
    its group); hook_execution_complete with num_blocking>0 -> WARNING; blocked-tool:* -> WARNING.
    Precedence ERROR > WARNING > DEFAULT."""

    def _tool_with_execution(self, **exec_attrs: object) -> list:
        tool = _obs(
            "tb", "tool:Bash", parent=None, metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        execu = _obs(
            "ex",
            "claude_code.tool.execution",
            parent="tb",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-1", **exec_attrs}},
        )
        return [("tr", [tool, execu])]

    def _guarded_tool(self, **guard_attrs: str) -> list:
        interaction = _obs(
            "i1",
            "claude_code.interaction",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:10Z",
        )
        tool = _obs(
            "t1",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        guard = _guard(
            "h1",
            "PreToolUse.sh",
            tool_use_id="tu-1",
            start="2026-01-02T00:00:01Z",
            end="2026-01-02T00:00:01.200Z",
            **guard_attrs,
        )
        return [("tr", [interaction, tool, guard])]

    def test_tool_success_false_is_error(self) -> None:
        batch = build_batch(self._tool_with_execution(success=False), SPOKE)

        assert _by_orig(batch, "tr", "tb")["body"]["level"] == "ERROR"

    def test_tool_error_is_error(self) -> None:
        batch = build_batch(self._tool_with_execution(success=True, error="boom"), SPOKE)

        assert _by_orig(batch, "tr", "tb")["body"]["level"] == "ERROR"

    def test_successful_tool_is_not_error(self) -> None:
        batch = build_batch(self._tool_with_execution(success=True), SPOKE)

        assert _by_orig(batch, "tr", "tb")["body"].get("level") != "ERROR"

    def test_deny_guard_is_warning_on_span_and_group(self) -> None:
        batch = build_batch(self._guarded_tool(decision="deny"), SPOKE)

        assert _by_orig(batch, "tr", "h1")["body"]["level"] == "WARNING"
        assert _guards_group(batch)["body"]["level"] == "WARNING"

    def test_failed_status_guard_is_warning(self) -> None:
        batch = build_batch(self._guarded_tool(status="failure"), SPOKE)

        assert _by_orig(batch, "tr", "h1")["body"]["level"] == "WARNING"
        assert _guards_group(batch)["body"]["level"] == "WARNING"

    @pytest.mark.parametrize("decision", ["ask", "block"])
    def test_ask_and_block_guard_decisions_are_warning(self, decision: str) -> None:
        batch = build_batch(self._guarded_tool(decision=decision), SPOKE)

        assert _by_orig(batch, "tr", "h1")["body"]["level"] == "WARNING"
        assert _guards_group(batch)["body"]["level"] == "WARNING"

    def test_allow_success_guard_is_not_warning(self) -> None:
        # A kept-but-benign guard (slow allow/success) must not be flagged.
        batch = build_batch(self._guarded_tool(), SPOKE, keep_noop_guards=True)

        assert _by_orig(batch, "tr", "h1")["body"].get("level") != "WARNING"
        assert _guards_group(batch)["body"].get("level") != "WARNING"

    def test_hook_event_num_blocking_is_warning(self) -> None:
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        tool = _obs(
            "t1", "tool:Edit", parent="i1", metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        hook = _obs(
            "h1",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            startTime="2026-01-02T00:00:01Z",
            metadata={"tool_use_id": "tu-1", "num_blocking": 1},
        )

        batch = build_batch([("tr", [interaction, tool, hook])], SPOKE)

        assert _by_orig(batch, "tr", "h1")["body"]["level"] == "WARNING"

    def test_non_blocking_hook_event_is_not_warning(self) -> None:
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        tool = _obs(
            "t1", "tool:Edit", parent="i1", metadata={"attributes": {"tool_use_id": "tu-1"}}
        )
        hook = _obs(
            "h1",
            "hook_execution_complete:PreToolUse",
            type_="EVENT",
            parent=None,
            startTime="2026-01-02T00:00:01Z",
            metadata={"tool_use_id": "tu-1", "num_blocking": 0},
        )

        batch = build_batch([("tr", [interaction, tool, hook])], SPOKE)

        assert _by_orig(batch, "tr", "h1")["body"].get("level") != "WARNING"


class TestMcpGrouping:
    """#234: fold tool:mcp__<server>__<tool> spans into one mcp:<server> group per server."""

    def _traces(self) -> list[tuple[str, list[dict]]]:
        interaction = _obs("i1", "claude_code.interaction", parent=None)
        nav = _obs(
            "t1",
            "tool:mcp__chrome__navigate",
            parent="i1",
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-1"}},
        )
        read = _obs(
            "t2",
            "tool:mcp__chrome__read_page",
            parent="i1",
            startTime="2026-01-02T00:00:01Z",
            endTime="2026-01-02T00:00:02Z",
            metadata={"attributes": {"tool_use_id": "tu-2"}},
        )
        return [("tr", [interaction, nav, read])]

    def test_mcp_tools_group_under_one_server_node(self) -> None:
        batch = build_batch(self._traces(), SPOKE)

        groups = [e for e in batch if (e["body"].get("name") or "") == "mcp:chrome"]
        assert len(groups) == 1
        group_id = groups[0]["body"]["id"]
        assert _by_orig(batch, "tr", "t1")["body"]["parentObservationId"] == group_id
        assert _by_orig(batch, "tr", "t2")["body"]["parentObservationId"] == group_id
        assert groups[0]["body"]["metadata"]["calls"] == 2
