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
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.measure_context_cost import CountTokensError
from telemetry.langfuse_spoke_tree import (
    _MAX_CONTENT_CHARS,
    _TRUNCATION_MARKER,
    _copy_id,
    build_batch,
    build_loaded_context_events,
    fetch_session,
    itemize_tool_schemas,
    load_tools_from_request,
    prefix_cache_creation,
    root_id_for,
    scan_transcripts,
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


def _fixed_counter(value: int):
    """A token counter returning a constant, ignoring its input text."""

    def counter(_text: str) -> int:
        return value

    return counter


class TestPrefixCacheCreation:
    def test_picks_earliest_generations_cache_creation(self) -> None:
        early = _obs(
            "g1",
            "llm_request",
            type_="GENERATION",
            startTime="2026-01-02T00:00:00Z",
            usageDetails={"cache_creation_input_tokens": 5000},
        )
        late = _obs(
            "g2",
            "llm_request",
            type_="GENERATION",
            startTime="2026-01-02T00:05:00Z",
            usageDetails={"cache_creation_input_tokens": 80},
        )

        assert prefix_cache_creation([("tr", [late, early])]) == 5000

    def test_zero_when_no_usage_present(self) -> None:
        assert prefix_cache_creation([("tr", [_obs("m1", "step:green")])]) == 0


class TestItemizeToolSchemas:
    def test_splits_mcp_from_builtin_and_counts_each_schema(self) -> None:
        tools = [
            {"name": "Read", "description": "read a file", "input_schema": {"a": 1}},
            {"name": "mcp__notion__search", "description": "search", "input_schema": {"b": 2}},
        ]

        rows = itemize_tool_schemas(tools, counter=_fixed_counter(30), price=0.001)

        by_name = {row["name"]: row for row in rows}
        assert by_name["Read"]["category"] == "tools"
        assert by_name["mcp__notion__search"]["category"] == "mcp"
        assert by_name["Read"]["tokens"] == 30
        assert by_name["Read"]["cost_usd"] == 0.03
        assert by_name["Read"]["source"] == "request_tools_array"

    def test_marks_estimated_when_counter_unreachable(self) -> None:
        def failing(_text: str) -> int:
            raise CountTokensError("nope")

        tools = [{"name": "Edit", "description": "x", "input_schema": {}}]

        rows = itemize_tool_schemas(tools, counter=failing, price=0.001)

        assert rows[0]["estimated"] is True

    def test_unnamed_tools_are_skipped(self) -> None:
        rows = itemize_tool_schemas(
            [{"description": "no name"}], counter=_fixed_counter(1), price=1
        )

        assert rows == []


class TestLoadToolsFromRequest:
    def test_reads_tools_array_from_request_body_json(self, tmp_path: Path) -> None:
        path = tmp_path / "req.json"
        path.write_text(json.dumps({"tools": [{"name": "Read"}], "model": "x"}), encoding="utf-8")

        assert load_tools_from_request(path) == [{"name": "Read"}]

    def test_missing_or_unreadable_file_yields_empty(self, tmp_path: Path) -> None:
        assert load_tools_from_request(tmp_path / "nope.json") == []

    def test_malformed_json_yields_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{ not json", encoding="utf-8")

        assert load_tools_from_request(path) == []


def _lc_by_name(events: list[dict]) -> dict[str, dict]:
    """Index loaded-context event bodies by their node name."""
    return {event["body"]["name"]: event["body"] for event in events}


class TestLoadedContextSubtree:
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

    def test_parent_node_sits_under_spoke_root_with_grand_total(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        parent = _lc_by_name(events)["loaded-context"]
        assert parent["parentObservationId"] == root_id_for(SPOKE)
        assert parent["metadata"]["tokens"] == 170

    def test_each_category_node_carries_its_rolled_up_total(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        index = _lc_by_name(events)
        assert index["rules"]["metadata"]["tokens"] == 150
        assert index["skills"]["metadata"]["tokens"] == 20

    def test_one_item_node_per_name_under_its_category(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        index = _lc_by_name(events)
        rules_id = index["rules"]["id"]
        claude = next(b for n, b in index.items() if n.startswith("CLAUDE.md"))
        assert claude["parentObservationId"] == rules_id
        assert claude["metadata"]["tokens"] == 100
        assert claude["metadata"]["source"] == "CLAUDE.md"

    def test_estimated_item_carries_the_flag(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        afk = next(b for n, b in _lc_by_name(events).items() if n.startswith("afk"))
        assert afk["metadata"]["estimated"] is True

    def test_remainder_is_prefix_minus_measured(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        remainder = next(b for n, b in _lc_by_name(events).items() if n.startswith("remainder"))
        assert remainder["metadata"]["tokens"] == 830
        assert remainder["parentObservationId"] == _lc_by_name(events)["loaded-context"]["id"]

    def test_remainder_clamped_to_zero_when_measured_exceeds_prefix(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=100,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        remainder = next(b for n, b in _lc_by_name(events).items() if n.startswith("remainder"))
        assert remainder["metadata"]["tokens"] == 0

    def test_ids_are_deterministic_across_runs(self) -> None:
        first = {
            e["id"]
            for e in build_loaded_context_events(
                SPOKE,
                self._rows(),
                prefix_cache_creation=1000,
                price=0.001,
                base_ts="2026-01-01T00:00:00Z",
            )
        }
        second = {
            e["id"]
            for e in build_loaded_context_events(
                SPOKE,
                self._rows(),
                prefix_cache_creation=1000,
                price=0.001,
                base_ts="2026-01-01T00:00:00Z",
            )
        }

        assert first == second

    def test_all_nodes_attach_to_the_assembled_trace(self) -> None:
        events = build_loaded_context_events(
            SPOKE,
            self._rows(),
            prefix_cache_creation=1000,
            price=0.001,
            base_ts="2026-01-01T00:00:00Z",
        )

        assert all(e["body"]["traceId"] == trace_id_for(SPOKE) for e in events)
        assert all(e["type"] == "span-create" for e in events)
