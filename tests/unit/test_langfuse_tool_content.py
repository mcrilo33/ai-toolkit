"""Unit tests for the transcript-sourced tool-content filler (Issue #83).

The filler (:mod:`telemetry.langfuse_tool_content`) joins a session's Langfuse tool spans
(which arrive with ``input=None`` for every non-Bash tool) to the tool-call content in the
session transcripts, keyed by ``tool_use_id``, and PATCHes ``input``/``output`` back onto the
matching observation. These tests run with NO network: the index/batch helpers are pure, and
the transcript scan reads a hand-built ``*.jsonl`` file under ``tmp_path``. They assert the
input/output join, that only the visible ``tool:`` span is indexed (not the
``claude_code.tool.execution`` / ``*.sh`` hook siblings that share its ``tool_use_id``),
GENERATION-vs-SPAN event routing, skipping of ids absent from Langfuse,
``metadata["attributes"]`` nesting, deterministic ids, and large-output truncation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_tool_content import (
    _EVENT_PREFIX,
    _MAX_CONTENT_CHARS,
    _TRUNCATION_MARKER,
    ToolContent,
    ToolSpan,
    build_batch,
    build_span_index,
    content_event,
    scan_transcripts,
)


def _obs(
    obs_id: str,
    *,
    type_: str = "SPAN",
    tool_use_id: str | None = None,
    name: str = "tool:Generic",
    **attrs,
) -> dict:
    """Build a Langfuse observation carrying a tool-call id under metadata["attributes"].

    ``name`` defaults to a ``tool:`` span (the visible tool observation, which is the patch
    target); pass an execution/hook name to build a sibling that must be excluded.
    """
    attributes: dict[str, object] = dict(attrs)
    if tool_use_id is not None:
        attributes["tool_use_id"] = tool_use_id
    return {"id": obs_id, "type": type_, "name": name, "metadata": {"attributes": attributes}}


def _write_transcript(root: Path, name: str, records: list[dict]) -> None:
    """Write a Claude Code transcript (one JSON record per line) under a project subdir."""
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / name).write_text(
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


class TestBuildSpanIndex:
    def test_maps_tool_use_id_to_observation_and_type(self) -> None:
        traces = [("trace-int", [_obs("o-task", type_="SPAN", tool_use_id="tu-1")])]

        index = build_span_index(traces)

        assert index == {"tu-1": ToolSpan("o-task", "SPAN")}

    def test_reads_gen_ai_tool_call_id_key(self) -> None:
        obs = {
            "id": "o-g",
            "type": "GENERATION",
            "name": "tool:WebSearch",
            "metadata": {"attributes": {"gen_ai.tool.call.id": "tu-9"}},
        }

        index = build_span_index([("trace", [obs])])

        assert index == {"tu-9": ToolSpan("o-g", "GENERATION")}

    def test_observation_without_tool_use_id_is_skipped(self) -> None:
        obs = {"id": "o-int", "type": "SPAN", "name": "tool:Bash", "metadata": {"attributes": {}}}

        index = build_span_index([("trace", [obs])])

        assert index == {}

    def test_only_the_tool_span_is_indexed_not_hook_or_execution_siblings(self) -> None:
        # All four observations share tu-1; the execution/hook/blocked siblings appear BEFORE
        # the visible tool: span, yet only the tool: span must end up in the index.
        execution = _obs("o-exec", name="claude_code.tool.execution", tool_use_id="tu-1")
        hook = _obs("o-hook", name="pre-tool-use.sh", tool_use_id="tu-1")
        blocked = _obs("o-blocked", name="claude_code.tool.blocked_on_user", tool_use_id="tu-1")
        tool = _obs("o-tool", name="tool:TaskCreate", tool_use_id="tu-1")

        index = build_span_index([("trace", [execution, hook, blocked, tool])])

        assert index == {"tu-1": ToolSpan("o-tool", "SPAN")}


class TestScanTranscripts:
    def test_joins_tool_use_input_and_tool_result_content(self, tmp_path: Path) -> None:
        _write_transcript(
            tmp_path,
            "session.jsonl",
            [
                _tool_use("tu-1", "TaskCreate", {"subject": "ship it"}),
                _tool_result("tu-1", "Task #1 created"),
            ],
        )

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert contents == {"tu-1": ToolContent({"subject": "ship it"}, "Task #1 created")}

    def test_ids_absent_from_langfuse_are_skipped(self, tmp_path: Path) -> None:
        _write_transcript(
            tmp_path,
            "session.jsonl",
            [
                _tool_use("tu-1", "Read", {"file_path": "/a"}),
                _tool_use("tu-stray", "Read", {"file_path": "/b"}),
            ],
        )

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert set(contents) == {"tu-1"}

    def test_tool_use_without_a_result_keeps_input_and_none_output(self, tmp_path: Path) -> None:
        _write_transcript(tmp_path, "s.jsonl", [_tool_use("tu-1", "Edit", {"k": "v"})])

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert contents == {"tu-1": ToolContent({"k": "v"}, None)}

    def test_bare_tool_result_without_tool_use_is_dropped(self, tmp_path: Path) -> None:
        _write_transcript(tmp_path, "s.jsonl", [_tool_result("tu-1", "orphan output")])

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert contents == {}

    def test_malformed_lines_are_ignored(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir(parents=True)
        good = json.dumps(_tool_use("tu-1", "Read", {"file_path": "/a"}))
        (project / "s.jsonl").write_text(f"not json\n\n{good}\n", encoding="utf-8")

        contents = scan_transcripts(tmp_path, {"tu-1"})

        assert set(contents) == {"tu-1"}

    def test_empty_wanted_set_reads_nothing(self, tmp_path: Path) -> None:
        _write_transcript(tmp_path, "s.jsonl", [_tool_use("tu-1", "Read", {"file_path": "/a"})])

        assert scan_transcripts(tmp_path, set()) == {}


class TestContentEvent:
    def test_span_observation_gets_span_update(self) -> None:
        event = content_event(ToolSpan("o-1", "SPAN"), ToolContent({"a": 1}, "out"))

        assert event["type"] == "span-update"
        assert event["body"] == {"id": "o-1", "input": {"a": 1}, "output": "out"}

    def test_generation_observation_gets_generation_update(self) -> None:
        event = content_event(ToolSpan("o-2", "GENERATION"), ToolContent({"a": 1}, None))

        assert event["type"] == "generation-update"

    def test_output_omitted_when_absent(self) -> None:
        event = content_event(ToolSpan("o-3", "SPAN"), ToolContent({"a": 1}, None))

        assert "output" not in event["body"]

    def test_event_id_is_deterministic_from_observation_id(self) -> None:
        content = ToolContent({"a": 1}, "out")

        first = content_event(ToolSpan("o-4", "SPAN"), content)
        second = content_event(ToolSpan("o-4", "SPAN"), content)

        assert first["id"] == second["id"] == f"{_EVENT_PREFIX}o-4"

    def test_large_output_is_truncated_with_marker(self) -> None:
        huge = "x" * (_MAX_CONTENT_CHARS + 500)

        event = content_event(ToolSpan("o-5", "SPAN"), ToolContent({"a": 1}, huge))

        output = event["body"]["output"]
        assert output.endswith(_TRUNCATION_MARKER)
        assert len(output) == _MAX_CONTENT_CHARS + len(_TRUNCATION_MARKER)

    def test_small_structured_content_is_passed_through_unchanged(self) -> None:
        event = content_event(ToolSpan("o-6", "SPAN"), ToolContent({"a": 1}, [{"type": "text"}]))

        assert event["body"]["input"] == {"a": 1}
        assert event["body"]["output"] == [{"type": "text"}]


class TestBuildBatch:
    def test_emits_one_event_per_matched_id(self) -> None:
        index = {
            "tu-1": ToolSpan("o-1", "SPAN"),
            "tu-2": ToolSpan("o-2", "GENERATION"),
            "tu-orphan": ToolSpan("o-3", "SPAN"),
        }
        contents = {
            "tu-1": ToolContent({"a": 1}, "out1"),
            "tu-2": ToolContent({"b": 2}, None),
        }

        batch = build_batch(index, contents)

        assert [event["body"]["id"] for event in batch] == ["o-1", "o-2"]

    def test_join_lands_content_on_the_right_observation(self, tmp_path: Path) -> None:
        index = build_span_index(
            [("trace", [_obs("o-task", tool_use_id="tu-1"), _obs("o-read", tool_use_id="tu-2")])]
        )
        _write_transcript(
            tmp_path,
            "s.jsonl",
            [
                _tool_use("tu-1", "TaskCreate", {"subject": "S"}),
                _tool_result("tu-1", "created"),
                _tool_use("tu-2", "Read", {"file_path": "/p"}),
            ],
        )

        batch = build_batch(index, scan_transcripts(tmp_path, set(index)))

        by_obs = {event["body"]["id"]: event["body"] for event in batch}
        assert by_obs["o-task"] == {"id": "o-task", "input": {"subject": "S"}, "output": "created"}
        assert by_obs["o-read"] == {"id": "o-read", "input": {"file_path": "/p"}}

    def test_content_patches_the_tool_span_not_its_execution_or_hook_siblings(
        self, tmp_path: Path
    ) -> None:
        # tool:TaskCreate, claude_code.tool.execution, and a *.sh hook all share tu-1.
        index = build_span_index(
            [
                (
                    "trace",
                    [
                        _obs("o-exec", name="claude_code.tool.execution", tool_use_id="tu-1"),
                        _obs("o-hook", name="post-tool-use.sh", tool_use_id="tu-1"),
                        _obs("o-task", name="tool:TaskCreate", tool_use_id="tu-1"),
                    ],
                )
            ]
        )
        _write_transcript(
            tmp_path,
            "s.jsonl",
            [
                _tool_use("tu-1", "TaskCreate", {"subject": "ship it"}),
                _tool_result("tu-1", "Task #1 created"),
            ],
        )

        batch = build_batch(index, scan_transcripts(tmp_path, set(index)))

        # Exactly one event, targeting the tool: span -- never the execution/hook siblings.
        assert [event["body"]["id"] for event in batch] == ["o-task"]
        assert batch[0]["type"] == "span-update"
        assert batch[0]["body"] == {
            "id": "o-task",
            "input": {"subject": "ship it"},
            "output": "Task #1 created",
        }
