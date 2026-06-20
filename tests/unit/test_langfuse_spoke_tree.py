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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_spoke_tree import (
    _DISK_CATEGORY_ORDER,
    _MAX_CONTENT_CHARS,
    _REQUEST_CATEGORY_ORDER,
    _STEP_PREFIX,
    _TRUNCATION_MARKER,
    ToolContent,
    _copy_id,
    build_batch,
    build_context_evolution_events,
    build_llm_decomposition_events,
    build_loaded_context_events,
    build_step_windows,
    context_evolution_deltas,
    fetch_session,
    find_request_files,
    prefix_total,
    request_context_rows,
    root_id_for,
    scan_transcripts,
    trace_id_for,
    transcript_scan_root,
)
from telemetry.request_body import (
    ContextDelta,
    decompose_request_body,
    measure_request_items,
)
from telemetry.session_parser import project_dir_for_worktree

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

        copy = _by_orig(batch, "trace-hook", "h9")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t9")

    def test_hook_detected_by_workflow_kind_without_sh_name(self) -> None:
        # A hook whose name does not end in ".sh" is still detected via workflow.kind and
        # collapses to the root when nothing matches its id.
        hook = _obs(
            "h8",
            "hook-emit",
            parent=None,
            metadata={"attributes": {"workflow.kind": "hook", "hook_event": "Stop"}},
        )

        batch = build_batch([("trace-hook", [hook])], SPOKE)

        copy = _by_orig(batch, "trace-hook", "h8")
        assert copy["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_tool_decision_audit_event_nests_under_its_tool(self) -> None:
        # A #93 tool_decision audit observation (event-create, type EVENT) carries its
        # tool_use_id in FLAT metadata and no parentObservationId. It must nest under the
        # tool sharing that id, exactly like a gate hook.
        tool = _obs(
            "t7",
            "Bash",
            parent=None,
            metadata={"attributes": {"gen_ai.tool.call.id": "tu-7"}},
        )
        decision = _obs(
            "d7",
            "tool_decision:allow",
            type_="EVENT",
            parent=None,
            metadata={"tool_name": "Bash", "tool_use_id": "tu-7", "decision": "allow"},
        )
        traces = [("trace-tool", [tool]), ("trace-audit", [decision])]

        batch = build_batch(traces, SPOKE)

        copy = _by_orig(batch, "trace-audit", "d7")
        assert copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t7")

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

    def test_audit_event_without_a_match_collapses_to_spoke_root(self) -> None:
        # An unmatched tool_use_id is never dropped — it falls through to the synthetic root.
        decision = _obs(
            "d0",
            "tool_decision:reject",
            type_="EVENT",
            parent=None,
            metadata={"tool_use_id": "tu-absent", "decision": "reject"},
        )

        batch = build_batch([("trace-audit", [decision])], SPOKE)

        copy = _by_orig(batch, "trace-audit", "d0")
        assert copy["body"]["parentObservationId"] == root_id_for(SPOKE)

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

        hook_copy = _by_orig(batch, "trace-hook", "h5")
        assert hook_copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t5")

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

    def test_unmatched_hook_execution_complete_event_collapses_to_root(self) -> None:
        # A Pre/PostToolUse hook whose tool_use_id matches no tool span is never dropped --
        # it falls through to the synthetic root.
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

        copy = _by_orig(batch, "trace-audit", "h10")
        assert copy["body"]["parentObservationId"] == root_id_for(SPOKE)

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

        gate_copy = _by_orig(batch, "trace-hook", "h11")
        assert gate_copy["body"]["parentObservationId"] == _copy_id("trace-tool", "t11")

    def test_ids_are_deterministic_across_runs(self) -> None:
        first = {event["id"] for event in build_batch(_traces(), SPOKE)}

        second = {event["id"] for event in build_batch(_traces(), SPOKE)}

        assert first == second

    def test_empty_session_emits_only_trace_and_root(self) -> None:
        batch = build_batch([], SPOKE)

        assert [event["type"] for event in batch] == ["trace-create", "span-create"]


class TestContainerRollups:
    """Every container node (and the synthetic root) carries a subtree token rollup."""

    def test_interaction_container_rolls_up_its_subtree(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        rollup = _by_orig(batch, "trace-int", "i1")["body"]["metadata"]["rollup"]
        assert rollup == {"reused": 900, "written": 300, "input": 120, "output": 45}

    def test_synthetic_root_rolls_up_the_whole_tree(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        root = next(event for event in batch if event["id"] == root_id_for(SPOKE))
        assert root["body"]["metadata"]["rollup"] == {
            "reused": 900,
            "written": 300,
            "input": 120,
            "output": 45,
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


def _step_node(batch: list[dict]) -> dict | None:
    """Return the first synthetic step node in a batch, or None when there is none."""
    return next((e for e in batch if e["id"].startswith(_STEP_PREFIX)), None)


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


def _turn(obs_id: str, start: str, *, parent: str | None) -> dict:
    """An llm_request turn (the chronological content unit nested under a step)."""
    return _obs(obs_id, "llm_request", type_="GENERATION", parent=parent, startTime=start)


class TestStepGrouping:
    """#100: every timeline node nests under the step whose window contains its startTime,
    regardless of its current parent — root, a dissolved interaction, or a resume buried under
    a tool.execution. Nodes that ride with a tool (parent is a ``tool:`` span) are left nested.
    """

    def _content(self) -> dict[str, ToolContent]:
        return {
            "tu-c1": ToolContent(
                {"subject": "S1 RED: x"}, "Task #1 created successfully: S1 RED: x"
            ),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }

    def _traces(self, turn_start: str = "2026-01-02T00:00:05Z") -> list[tuple[str, list[dict]]]:
        # An interaction container holds the ledger ops + a turn; the interaction is dissolved.
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
            endTime="2026-01-02T00:00:21Z",
            metadata={"attributes": {"tool_use_id": "tu-u2"}},
        )
        turn = _turn("turnA", turn_start, parent="i1")
        return [("tr", [interaction, create, started, done, turn])]

    def test_turn_within_window_nests_under_a_step_node(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        step = _step_node(batch)
        assert step is not None
        assert step["body"]["name"] == "step:S1 RED: x"
        assert step["body"]["parentObservationId"] == root_id_for(SPOKE)
        turn = _by_orig(batch, "tr", "turnA")
        assert turn["body"]["parentObservationId"] == step["id"]

    def test_ledger_ops_nest_under_their_step(self) -> None:
        # The TaskUpdate spans themselves (in_progress and completed) join the step timeline.
        batch = build_batch(self._traces(), SPOKE, self._content())

        step_id = _step_node(batch)["id"]
        assert _by_orig(batch, "tr", "tu1")["body"]["parentObservationId"] == step_id
        assert _by_orig(batch, "tr", "tu2")["body"]["parentObservationId"] == step_id

    def test_interaction_container_is_dissolved(self) -> None:
        # When grouping is active the claude_code.interaction node is dropped entirely.
        batch = build_batch(self._traces(), SPOKE, self._content())

        assert all(e["id"] != _copy_id("tr", "i1") for e in batch)

    def test_step_node_carries_window_metadata(self) -> None:
        batch = build_batch(self._traces(), SPOKE, self._content())

        meta = _step_node(batch)["body"]["metadata"]
        assert meta["subject"] == "S1 RED: x"
        assert meta["status"] == "completed"

    def test_turn_outside_every_window_falls_back_to_root(self) -> None:
        # A turn well after the only step's window re-homes to the synthetic root (its interaction
        # parent is dissolved), never left dangling.
        batch = build_batch(self._traces(turn_start="2026-01-02T01:00:00Z"), SPOKE, self._content())

        turn = _by_orig(batch, "tr", "turnA")
        assert turn["body"]["parentObservationId"] == root_id_for(SPOKE)

    def test_non_ledger_spoke_emits_no_step_nodes(self) -> None:
        batch = build_batch(_traces(), SPOKE)

        assert _step_node(batch) is None
        # the interaction turn still collapses to the synthetic root (interactions untouched).
        assert _by_orig(batch, "trace-int", "i1")["body"]["parentObservationId"] == root_id_for(
            SPOKE
        )

    def test_hook_under_a_tool_rides_with_its_tool_into_the_step(self) -> None:
        # A gate hook nested under a tool stays under that tool (Part 2); the tool re-homes
        # into the step, so the hook ends up under tool-under-step, not a direct step child.
        content = {
            "tu-c1": ToolContent({"subject": "S1 GREEN"}, "Task #1 created successfully: S1 GREEN"),
            "tu-u1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
        }
        create = _obs(
            "tc1",
            "tool:TaskCreate",
            parent=None,
            startTime="2026-01-02T00:00:00Z",
            endTime="2026-01-02T00:00:00Z",
            metadata={"attributes": {"tool_use_id": "tu-c1"}},
        )
        started = _obs(
            "tu1",
            "tool:TaskUpdate",
            parent=None,
            startTime="2026-01-02T00:00:01Z",
            endTime="2026-01-02T00:00:01Z",
            metadata={"attributes": {"tool_use_id": "tu-u1"}},
        )
        done = _obs(
            "tu2",
            "tool:TaskUpdate",
            parent=None,
            startTime="2026-01-02T00:00:30Z",
            endTime="2026-01-02T00:00:30Z",
            metadata={"attributes": {"tool_use_id": "tu-u2"}},
        )
        tool = _obs(
            "tb",
            "tool:Bash",
            parent=None,
            startTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"tool_use_id": "tu-b"}},
        )
        hook = _obs(
            "hb",
            "PreToolUse.sh",
            parent=None,
            startTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"workflow.kind": "hook", "tool_use_id": "tu-b"}},
        )

        batch = build_batch([("tr", [create, started, done, tool, hook])], SPOKE, content)

        step_id = _step_node(batch)["id"]
        tool_copy = _by_orig(batch, "tr", "tb")
        assert tool_copy["body"]["parentObservationId"] == step_id  # tool re-homed into step
        # the hook still rides under its tool, NOT directly under the step.
        assert _by_orig(batch, "tr", "hb")["body"]["parentObservationId"] == tool_copy["id"]

    def test_resume_interactions_flatten_into_step_chronologically(self) -> None:
        # Two interactions (a resume); interaction-2 is buried under a tool.execution sub-span,
        # which natively breaks chronology. Every turn/TaskUpdate across BOTH interactions must
        # sort into the one step window as direct children, ordered by startTime.
        content = {
            "tu-c1": ToolContent({"subject": "S1 PUSH"}, "Task #3 created successfully: S1 PUSH"),
            "tu-u1": ToolContent({"taskId": "3", "status": "in_progress"}, "ok"),
            "tu-u2": ToolContent({"taskId": "3", "status": "completed"}, "ok"),
        }
        # interaction-1 (root): create, in_progress(T10), push turn (T12)
        i1 = _obs("i1", "claude_code.interaction", parent=None, startTime="2026-01-02T00:00:05Z")
        create = _obs(
            "tc1",
            "tool:TaskCreate",
            parent="i1",
            startTime="2026-01-02T00:00:05Z",
            endTime="2026-01-02T00:00:05Z",
            metadata={"attributes": {"tool_use_id": "tu-c1"}},
        )
        started = _obs(
            "tu1",
            "tool:TaskUpdate",
            parent="i1",
            startTime="2026-01-02T00:00:10Z",
            endTime="2026-01-02T00:00:10Z",
            metadata={"attributes": {"tool_use_id": "tu-u1"}},
        )
        push_turn1 = _turn("p1", "2026-01-02T00:00:12Z", parent="i1")
        # a real tool with a tool.execution sub-span; the resume interaction-2 hangs off it.
        tool = _obs(
            "tp",
            "tool:Bash",
            parent="i1",
            startTime="2026-01-02T00:00:15Z",
            metadata={"attributes": {"tool_use_id": "tu-p"}},
        )
        execu = _obs(
            "exec1", "claude_code.tool.execution", parent="tp", startTime="2026-01-02T00:00:15Z"
        )
        # interaction-2 (resume) nested under the tool.execution — out of chronological place.
        i2 = _obs("i2", "claude_code.interaction", parent="exec1", startTime="2026-01-02T00:00:18Z")
        push_turn2 = _turn("p2", "2026-01-02T00:00:22Z", parent="i2")
        done = _obs(
            "tu2",
            "tool:TaskUpdate",
            parent="i2",
            startTime="2026-01-02T00:00:30Z",
            endTime="2026-01-02T00:00:31Z",
            metadata={"attributes": {"tool_use_id": "tu-u2"}},
        )

        batch = build_batch(
            [("tr", [i1, create, started, push_turn1, tool, execu, i2, push_turn2, done])],
            SPOKE,
            content,
        )

        step_id = _step_node(batch)["id"]
        # both interactions are dissolved.
        assert all(e["id"] not in {_copy_id("tr", "i1"), _copy_id("tr", "i2")} for e in batch)
        # the direct children of the step, in startTime order.
        children = sorted(
            (e for e in batch if e["body"].get("parentObservationId") == step_id),
            key=lambda e: e["body"]["startTime"],
        )
        names = [e["body"]["name"] for e in children]
        # task3 in_progress -> push turn 1 -> push turn 2 -> task3 completed.
        assert names == ["tool:TaskUpdate", "llm_request", "llm_request", "tool:TaskUpdate"]
        # the tool itself also re-homes into the step (its execution sub-span rides with it).
        assert _by_orig(batch, "tr", "tp")["body"]["parentObservationId"] == step_id

    def test_innermost_window_wins_on_overlap(self) -> None:
        # Two overlapping steps; a turn inside both nests under the later-starting (inner) one.
        content = {
            "tu-ca": ToolContent({"subject": "outer"}, "Task #1 created successfully: outer"),
            "tu-ua1": ToolContent({"taskId": "1", "status": "in_progress"}, "ok"),
            "tu-ua2": ToolContent({"taskId": "1", "status": "completed"}, "ok"),
            "tu-cb": ToolContent({"subject": "inner"}, "Task #2 created successfully: inner"),
            "tu-ub1": ToolContent({"taskId": "2", "status": "in_progress"}, "ok"),
            "tu-ub2": ToolContent({"taskId": "2", "status": "completed"}, "ok"),
        }
        outer = [
            _ledger_obs(
                "ca",
                "tool:TaskCreate",
                "tu-ca",
                start="2026-01-02T00:00:00Z",
                end="2026-01-02T00:00:00Z",
            ),
            _ledger_obs(
                "ua1",
                "tool:TaskUpdate",
                "tu-ua1",
                start="2026-01-02T00:00:01Z",
                end="2026-01-02T00:00:01Z",
            ),
            _ledger_obs(
                "ua2",
                "tool:TaskUpdate",
                "tu-ua2",
                start="2026-01-02T00:00:30Z",
                end="2026-01-02T00:00:30Z",
            ),
        ]
        inner = [
            _ledger_obs(
                "cb",
                "tool:TaskCreate",
                "tu-cb",
                start="2026-01-02T00:00:05Z",
                end="2026-01-02T00:00:05Z",
            ),
            _ledger_obs(
                "ub1",
                "tool:TaskUpdate",
                "tu-ub1",
                start="2026-01-02T00:00:06Z",
                end="2026-01-02T00:00:06Z",
            ),
            _ledger_obs(
                "ub2",
                "tool:TaskUpdate",
                "tu-ub2",
                start="2026-01-02T00:00:20Z",
                end="2026-01-02T00:00:20Z",
            ),
        ]
        turn = _turn("turnB", "2026-01-02T00:00:10Z", parent=None)

        batch = build_batch([("tr", [*outer, *inner, turn])], SPOKE, content)

        steps = {e["body"]["name"]: e["id"] for e in batch if e["id"].startswith(_STEP_PREFIX)}
        assert _by_orig(batch, "tr", "turnB")["body"]["parentObservationId"] == steps["step:inner"]


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


def _lc_by_name(events: list[dict]) -> dict[str, dict]:
    """Index loaded-context event bodies by their node name."""
    return {event["body"]["name"]: event["body"] for event in events}


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TestLoadedContextDiskFallback:
    """The disk-fallback path: measured on-disk items + a single reconciled remainder."""

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

    def test_parent_node_sits_under_spoke_root_with_grand_total(self) -> None:
        parent = _lc_by_name(self._build())["loaded-context"]
        assert parent["parentObservationId"] == root_id_for(SPOKE)
        assert parent["metadata"]["tokens"] == 170

    def test_each_category_node_carries_its_rolled_up_total(self) -> None:
        index = _lc_by_name(self._build())
        assert index["rules"]["metadata"]["tokens"] == 150
        assert index["skills"]["metadata"]["tokens"] == 20

    def test_one_item_node_per_name_under_its_category(self) -> None:
        index = _lc_by_name(self._build())
        rules_id = index["rules"]["id"]
        claude = next(b for n, b in index.items() if n.startswith("CLAUDE.md"))
        assert claude["parentObservationId"] == rules_id
        assert claude["metadata"]["tokens"] == 100
        assert claude["metadata"]["source"] == "CLAUDE.md"

    def test_estimated_item_carries_the_flag(self) -> None:
        afk = next(b for n, b in _lc_by_name(self._build()).items() if n.startswith("afk"))
        assert afk["metadata"]["estimated"] is True

    def test_remainder_is_prefix_minus_measured(self) -> None:
        events = self._build()
        remainder = next(b for n, b in _lc_by_name(events).items() if n.startswith("remainder"))
        assert remainder["metadata"]["tokens"] == 830
        assert remainder["parentObservationId"] == _lc_by_name(events)["loaded-context"]["id"]

    def test_remainder_clamped_to_zero_when_measured_exceeds_prefix(self) -> None:
        events = self._build(prefix_total=100)
        remainder = next(b for n, b in _lc_by_name(events).items() if n.startswith("remainder"))
        assert remainder["metadata"]["tokens"] == 0

    def test_no_floor_or_mcp_nodes_remain(self) -> None:
        names = list(_lc_by_name(self._build()))
        assert not any(n.startswith("built-in") or n.startswith("mcp") for n in names)

    def test_ids_are_deterministic_across_runs(self) -> None:
        first = {e["id"] for e in self._build()}
        second = {e["id"] for e in self._build()}
        assert first == second

    def test_all_nodes_attach_to_the_assembled_trace(self) -> None:
        events = self._build()
        assert all(e["body"]["traceId"] == trace_id_for(SPOKE) for e in events)
        assert all(e["type"] == "span-create" for e in events)


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

    def test_all_request_categories_rendered(self) -> None:
        names = set(_lc_by_name(self._build()))
        assert {"tools", "mcp", "system", "context"} <= names

    def test_no_remainder_node_on_the_request_path(self) -> None:
        assert not any(n.startswith("remainder") for n in _lc_by_name(self._build()))

    def test_cached_flag_carried_into_item_metadata(self) -> None:
        index = _lc_by_name(self._build())
        base = next(b for n, b in index.items() if n.startswith("base system prompt"))
        assert base["metadata"]["cached"] is True

    def test_parent_total_is_full_itemized_prefix(self) -> None:
        parent = _lc_by_name(self._build())["loaded-context"]
        assert parent["metadata"]["tokens"] == 440  # 120 + 40 + 200 + 80


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


def _added_row(category: str, name: str, tokens: int) -> dict:
    return {"category": category, "name": name, "tokens": tokens, "cost_usd": tokens * 0.1}


def _changed_row(category: str, name: str, tokens: int, delta: int) -> dict:
    row = _added_row(category, name, tokens)
    row["prev_tokens"] = tokens - delta
    row["delta_tokens"] = delta
    return row


class TestContextEvolutionSubtree:
    """The #98 per-turn context-evolution subtree: one node per evolving turn."""

    def _deltas(self) -> list[tuple[int, ContextDelta]]:
        turn1 = ContextDelta(
            added=[
                _added_row("tools", "WebSearch", 300),
                _added_row("messages", "msg[3]:user", 50),
            ],
            removed=[],
            changed=[_changed_row("system", "base system prompt", 220, 20)],
            net_tokens=370,
            label=None,
        )
        turn2 = ContextDelta(
            added=[],
            removed=[_added_row("messages", "msg[1]:user", 15000)],
            changed=[],
            net_tokens=-15000,
            label="compaction",
        )
        return [(1, turn1), (2, turn2)]

    def _build(self) -> list[dict]:
        return build_context_evolution_events(SPOKE, self._deltas(), base_ts="2026-01-01T00:00:00Z")

    def test_parent_sits_under_spoke_root(self) -> None:
        parent = _lc_by_name(self._build())["context-evolution"]
        assert parent["parentObservationId"] == root_id_for(SPOKE)

    def test_one_turn_node_per_evolving_turn_under_the_parent(self) -> None:
        events = self._build()
        parent_id = _lc_by_name(events)["context-evolution"]["id"]
        turn_nodes = [
            e["body"] for e in events if e["body"].get("parentObservationId") == parent_id
        ]
        assert len(turn_nodes) == 2

    def test_turn_node_metadata_carries_added_removed_and_net(self) -> None:
        turn = next(b for n, b in _lc_by_name(self._build()).items() if n.startswith("turn 1"))
        assert turn["metadata"]["net_tokens"] == 370
        added_names = {row["name"] for row in turn["metadata"]["added"]}
        assert "WebSearch" in added_names
        assert turn["metadata"]["removed"] == []

    def test_toolsearch_added_schema_is_a_named_child_node(self) -> None:
        events = self._build()
        turn1 = next(b for n, b in _lc_by_name(events).items() if n.startswith("turn 1"))
        children = [
            e["body"] for e in events if e["body"].get("parentObservationId") == turn1["id"]
        ]
        assert any(c["name"].startswith("WebSearch") for c in children)

    def test_compaction_turn_is_labeled_and_net_negative(self) -> None:
        turn2 = next(b for n, b in _lc_by_name(self._build()).items() if n.startswith("turn 2"))
        assert turn2["metadata"]["label"] == "compaction"
        assert turn2["metadata"]["net_tokens"] < 0

    def test_compaction_removed_message_is_a_child_node(self) -> None:
        events = self._build()
        turn2 = next(b for n, b in _lc_by_name(events).items() if n.startswith("turn 2"))
        children = [
            e["body"] for e in events if e["body"].get("parentObservationId") == turn2["id"]
        ]
        assert any(c["name"].startswith("msg[1]:user") for c in children)

    def test_all_nodes_attach_to_the_assembled_trace_as_spans(self) -> None:
        events = self._build()
        assert all(e["body"]["traceId"] == trace_id_for(SPOKE) for e in events)
        assert all(e["type"] == "span-create" for e in events)

    def test_ids_are_deterministic_across_runs(self) -> None:
        assert {e["id"] for e in self._build()} == {e["id"] for e in self._build()}

    def test_empty_deltas_emit_only_the_parent(self) -> None:
        events = build_context_evolution_events(SPOKE, [], base_ts="2026-01-01T00:00:00Z")
        assert len(events) == 1
        assert events[0]["body"]["name"] == "context-evolution"

    def test_observed_cache_creation_is_stamped_for_reconciliation(self) -> None:
        # Arrange: turn 1 net is 370; pass the turn's observed cache_creation as ~390.
        events = build_context_evolution_events(
            SPOKE,
            self._deltas(),
            base_ts="2026-01-01T00:00:00Z",
            cache_creation_by_turn={1: 390},
        )
        turn1 = next(b for n, b in _lc_by_name(events).items() if n.startswith("turn 1"))

        # Assert: the observed value is recorded so net can be cross-checked against it.
        assert turn1["metadata"]["cache_creation_observed"] == 390


class TestContextEvolutionDeltas:
    """Wiring: diff every consecutive raw request body, ordered, skipping unchanged turns."""

    def _write(self, bodies: Path, index: int, obj: dict) -> None:
        # Names are random UUIDs in practice; ordering is by mtime, so write in turn order.
        (bodies / f"{index:02d}-body.request.json").write_text(json.dumps(obj), encoding="utf-8")

    def _bodies(self, tmp_path: Path) -> Path:
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        bash = {"name": "Bash", "description": "d", "input_schema": {"type": "object"}}
        web = {"name": "WebSearch", "description": "d", "input_schema": {"type": "object"}}
        turn0 = {
            "tools": [bash],
            "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        turn1 = {
            "tools": [bash, web],
            "system": [{"type": "text", "text": "sys"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        self._write(bodies, 0, turn0)
        self._write(bodies, 1, turn1)
        return bodies

    def test_returns_one_delta_per_evolving_transition(self, tmp_path: Path) -> None:
        deltas = context_evolution_deltas(self._bodies(tmp_path), counter=len, price=1.0)
        assert len(deltas) == 1
        turn_index, delta = deltas[0]
        assert turn_index == 1
        assert any(row["name"] == "WebSearch" for row in delta.added)

    def test_empty_when_no_bodies(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert context_evolution_deltas(empty, counter=len, price=1.0) == []

    def test_turn_index_tracks_raw_file_position_across_an_unparseable_body(
        self, tmp_path: Path
    ) -> None:
        # Arrange: a corrupt body at position 0 precedes two valid turns. The turn index must
        # stay the RAW file position (so it aligns with the reconciliation map, which keys off
        # the full file list), NOT the compacted parsed-only position.
        bodies = tmp_path / "bodies"
        bodies.mkdir()
        bash = {"name": "Bash", "description": "d", "input_schema": {"type": "object"}}
        web = {"name": "WebSearch", "description": "d", "input_schema": {"type": "object"}}
        body = {"system": [{"type": "text", "text": "sys"}], "messages": []}
        (bodies / "00-body.request.json").write_text("{ not json", encoding="utf-8")
        self._write(bodies, 1, {**body, "tools": [bash]})
        self._write(bodies, 2, {**body, "tools": [bash, web]})

        # Act
        deltas = context_evolution_deltas(bodies, counter=len, price=1.0)

        # Assert: one diff (positions 1->2), indexed at raw position 2, not compacted 1.
        assert [turn for turn, _delta in deltas] == [2]


class TestLlmDecompositionEvents:
    """#99: per-llm_request cache_read/cache_creation decomposition attached to each call."""

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

    def _build(self, traces, bodies: Path) -> list[dict]:
        return build_llm_decomposition_events(
            traces, bodies, SPOKE, counter=len, price=1.0, base_ts="2026-01-01T00:00:00Z"
        )

    def _bucket(self, events: list[dict], prefix: str) -> dict:
        return next(e for e in events if e["body"]["name"].startswith(prefix))

    def _children(self, events: list[dict], parent_id: str) -> list[dict]:
        return [e for e in events if e["body"].get("parentObservationId") == parent_id]

    def test_buckets_parent_under_the_llm_request_copy(self, tmp_path: Path) -> None:
        # Arrange: one llm_request and its aligned request body.
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=30, creation=10)

        # Act
        events = self._build([("tr", [gen])], bodies)

        # Assert: both cache buckets hang under the copy of the llm_request observation.
        copy_id = _copy_id("tr", "g1")
        parents = {
            e["body"]["parentObservationId"]
            for e in events
            if e["body"]["name"].startswith(("cache_read", "cache_creation"))
        }
        assert parents == {copy_id}

    def test_cold_turn_puts_everything_in_cache_creation(self, tmp_path: Path) -> None:
        # Arrange: a cold call (read=0) — nothing was reused, the whole prefix is written.
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=0, creation=5000)

        # Act
        events = self._build([("tr", [gen])], bodies)

        # Assert: cache_read holds only its remainder; cache_creation carries the components.
        read = self._bucket(events, "cache_read")
        read_children = self._children(events, read["body"]["id"])
        assert all("remainder" in c["body"]["name"] for c in read_children)
        creation = self._bucket(events, "cache_creation")
        creation_children = {
            c["body"]["name"] for c in self._children(events, creation["body"]["id"])
        }
        assert "tools" in creation_children

    def test_warm_turn_puts_newest_message_in_cache_creation(self, tmp_path: Path) -> None:
        # Arrange: a warm call — the stable prefix is read, only the newest message is written.
        # Size read to cover every item except the newest (the last in request order), and
        # creation to exactly that newest message, so cumulative-fit routes it to cache_creation.
        bodies = self._bodies_dir(tmp_path, self._obj())
        rows = measure_request_items(
            decompose_request_body(bodies / "00-body.request.json"), counter=len, price=1.0
        )
        newest = next(r for r in rows if r["name"] == "msg[1]:assistant")
        read = sum(int(r["tokens"]) for r in rows) - int(newest["tokens"])
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=read, creation=int(newest["tokens"]))

        # Act
        events = self._build([("tr", [gen])], bodies)

        # Assert: the newest message item lands under the cache_creation -> messages subtree.
        creation = self._bucket(events, "cache_creation")
        component_ids = {c["body"]["id"] for c in self._children(events, creation["body"]["id"])}
        creation_item_names = {
            e["body"]["name"]
            for e in events
            if e["body"].get("parentObservationId") in component_ids
        }
        assert any("msg[1]:assistant" in name for name in creation_item_names)

    def test_each_bucket_items_plus_remainder_sum_to_the_observed_counter(
        self, tmp_path: Path
    ) -> None:
        # Arrange: generous read budget so all items fall in cache_read; creation gets none.
        bodies = self._bodies_dir(tmp_path, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=100000, creation=7)

        # Act
        events = self._build([("tr", [gen])], bodies)

        # Assert: measured + remainder == observed for the cache_read bucket.
        read = self._bucket(events, "cache_read")
        remainder = next(
            e
            for e in self._children(events, read["body"]["id"])
            if "remainder" in e["body"]["name"]
        )
        measured = int(read["body"]["metadata"]["measured_tokens"])
        assert measured + int(remainder["body"]["metadata"]["tokens"]) == 100000

    def test_count_mismatch_skips_decomposition(self, tmp_path: Path) -> None:
        # Arrange: two bodies but a single llm_request — positional alignment is unsafe.
        bodies = self._bodies_dir(tmp_path, self._obj())
        self._write(bodies, 1, self._obj())
        gen = self._gen("g1", "2026-01-02T00:00:00Z", read=30, creation=10)

        # Act / Assert: no decomposition emitted rather than a misaligned one.
        assert self._build([("tr", [gen])], bodies) == []
