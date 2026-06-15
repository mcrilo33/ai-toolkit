"""Unit tests for the Claude session-log parser (Issue #22, subtask 1 — RED).

The parser reads ``~/.claude/projects/*/*.jsonl`` session transcripts and emits
*pull* spans (``skill``/``agent``/``todo``/``human``) matching the frozen #21
span schema verbatim. For ``agent`` spans it walks the subagent transcript
(``<session>/subagents/agent-<id>.jsonl``) so those tokens can be attributed to
the parent agent span.

Privacy contract (issue constraint): spans carry metadata only — never the raw
prompt / answer / tool-output text that the session logs contain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.session_parser import ParsedSession, parse_projects_dir, parse_session_file
from telemetry.spans import Span

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
PROJECT = FIXTURES / "-Users-demo-Repos-proj"
SESSION = PROJECT / "11111111-1111-1111-1111-111111111111.jsonl"
SESSION_ID = "11111111-1111-1111-1111-111111111111"

SCHEMA_KEYS = {
    "span_id",
    "parent_id",
    "spoke_run_id",
    "session_id",
    "workflow_rev",
    "repo",
    "branch",
    "kind",
    "name",
    "phase",
    "ts_start",
    "ts_end",
    "duration_ms",
    "status",
    "human",
    "summary",
    "tokens_in",
    "tokens_out",
    "cost_usd",
}

# Strings planted in the fixtures that must NEVER appear in span data. Issue #47
# lifts the filter for short *intent* metadata only — the todo item, the agent's
# task description, and a trimmed prompt/question snippet become the node summary.
# The long-form content stays filtered: extended thinking, the agent's full task
# prompt, agent output / work, and the human's answer.
SECRETS = (
    "SECRET_THINKING",
    "SECRET_TASK_PROMPT",
    "SECRET_AGENT_OUTPUT",
    "SECRET_AGENT_WORK",
    "SECRET_ANSWER",
    # Tool inputs/outputs (Issue #47 S2b): tool leaf spans are name-only — the
    # Bash command, Read/Write path & content, Edit text, Grep pattern, and the
    # tool result body must never appear in a span.
    "SECRET_COMMAND",
    "SECRET_PATH",
    "SECRET_EDIT_OLD",
    "SECRET_PATTERN",
    "SECRET_WRITE_CONTENT",
    "SECRET_FILE_CONTENT",
)

# Tool names planted in the fixture's tool_use blocks (Issue #47 S2b).
TOOL_NAMES = {"Bash", "Read", "Edit", "Grep", "Write"}


@pytest.fixture()
def parsed() -> ParsedSession:
    return parse_session_file(SESSION)


def _by_kind(parsed: ParsedSession, kind: str) -> list[Span]:
    return [s for s in parsed.spans if s.kind == kind]


class TestSpanKinds:
    def test_emits_one_skill_span_named_for_the_skill(self, parsed: ParsedSession) -> None:
        skills = _by_kind(parsed, "skill")
        assert len(skills) == 1
        assert skills[0].name == "source-task"

    def test_skill_span_brackets_tool_use_to_tool_result(self, parsed: ParsedSession) -> None:
        span = _by_kind(parsed, "skill")[0]
        assert span.ts_start == "2026-06-13T12:00:01.000Z"
        assert span.ts_end == "2026-06-13T12:00:03.000Z"
        assert span.duration_ms == 2000

    def test_emits_agent_span_named_for_subagent_type(self, parsed: ParsedSession) -> None:
        agents = _by_kind(parsed, "agent")
        assert len(agents) == 1
        assert agents[0].name == "Explore"

    def test_agent_summary_is_the_task_description(self, parsed: ParsedSession) -> None:
        # Issue #47: the agent node's few-word summary is the Task tool's short
        # `description` (a 3-5 word task summary), NOT the long private `prompt`.
        # `name` stays the subagent_type so the Aggregate / A-B views still group.
        agent = _by_kind(parsed, "agent")[0]
        assert agent.summary == "explore code"
        assert agent.name == "Explore"

    def test_emits_todo_span(self, parsed: ParsedSession) -> None:
        todos = _by_kind(parsed, "todo")
        assert len(todos) == 1
        assert todos[0].kind == "todo"

    def test_todo_summary_is_its_in_progress_item(self, parsed: ParsedSession) -> None:
        # Issue #47: the todo node's summary is the in-progress ledger item it
        # advances (the L1 step label); `name` stays the bare tool for grouping.
        todo = _by_kind(parsed, "todo")[0]
        assert todo.summary == "Add RED telemetry test"
        assert todo.name == "TodoWrite"

    def test_human_prompt_summary_is_a_short_snippet(self, parsed: ParsedSession) -> None:
        # Issue #47: the human-prompt node summarises the prompt in a few words
        # (a trimmed first-line snippet); `name` stays "prompt" for grouping.
        prompt = next(
            s for s in _by_kind(parsed, "human") if s.human and s.human["type"] == "prompt"
        )
        assert prompt.summary == "Wire the parser and add tests"
        assert prompt.name == "prompt"

    def test_question_summary_is_a_short_snippet(self, parsed: ParsedSession) -> None:
        question = next(
            s for s in _by_kind(parsed, "human") if s.human and s.human["type"] == "question"
        )
        assert question.summary == "Which carrier field should we use"

    def test_emits_human_prompt_span(self, parsed: ParsedSession) -> None:
        prompts = [s for s in _by_kind(parsed, "human") if s.human and s.human["type"] == "prompt"]
        assert len(prompts) == 1

    def test_emits_human_question_span_with_wait_ms(self, parsed: ParsedSession) -> None:
        questions = [
            s for s in _by_kind(parsed, "human") if s.human and s.human["type"] == "question"
        ]
        assert len(questions) == 1
        human = questions[0].human
        assert human is not None
        # AskUserQuestion fired at 12:01:10, answered at 12:01:40 → 30s wait.
        assert human["wait_ms"] == 30000


class TestToolSpans:
    """Issue #47 S2b: every tool_use becomes a name-only `tool` leaf span."""

    def test_every_tool_use_emits_a_leaf_span_named_by_tool(self, parsed: ParsedSession) -> None:
        tools = _by_kind(parsed, "tool")
        assert {t.name for t in tools} == TOOL_NAMES

    def test_specific_tools_keep_their_own_kinds(self, parsed: ParsedSession) -> None:
        # Skill/Agent/Todo/AskUserQuestion are NOT generic `tool` leaves.
        assert not [t for t in _by_kind(parsed, "tool") if t.name in {"Skill", "Task", "TodoWrite"}]
        assert len(_by_kind(parsed, "skill")) == 1
        assert len(_by_kind(parsed, "agent")) == 1

    def test_tool_span_brackets_tool_use_to_tool_result(self, parsed: ParsedSession) -> None:
        bash = next(t for t in _by_kind(parsed, "tool") if t.name == "Bash")
        assert bash.ts_start == "2026-06-13T12:02:00.000Z"
        assert bash.ts_end == "2026-06-13T12:02:05.000Z"
        assert bash.duration_ms == 5000

    def test_tool_span_carries_no_summary_or_cost_at_parse_time(
        self, parsed: ParsedSession
    ) -> None:
        for tool in _by_kind(parsed, "tool"):
            assert tool.summary is None
            assert tool.tokens_in is None
            assert tool.tokens_out is None
            assert tool.cost_usd is None

    def test_no_tool_input_or_output_leaks_into_tool_spans(self, parsed: ParsedSession) -> None:
        blob = "".join(str(t.to_dict()) for t in _by_kind(parsed, "tool"))
        for secret in SECRETS:
            assert secret not in blob

    def test_tool_kind_is_accepted_by_the_schema(self) -> None:
        assert Span(span_id="x", kind="tool", name="Bash").kind == "tool"


class TestSubagentWalk:
    def test_links_agent_span_to_its_subagent_transcript(self, parsed: ParsedSession) -> None:
        agent = _by_kind(parsed, "agent")[0]
        assert parsed.agent_links[agent.span_id] == "aaaa1111bbbb2222"

    def test_walks_subagent_usage_into_usage_events(self, parsed: ParsedSession) -> None:
        sub = [e for e in parsed.usage_events if e.source == "subagent"]
        assert {e.agent_id for e in sub} == {"aaaa1111bbbb2222"}
        # Subagent transcript: 500+200 input, 300+150 output, 1000 cache_read.
        assert sum(e.input_tokens for e in sub) == 700
        assert sum(e.output_tokens for e in sub) == 450
        assert sum(e.cache_read for e in sub) == 1000

    def test_main_session_usage_events_captured(self, parsed: ParsedSession) -> None:
        main = [e for e in parsed.usage_events if e.source == "main"]
        # Four assistant turns carry usage in the fixture.
        assert len(main) == 4
        assert all(e.session_id == SESSION_ID for e in main)


class TestSchemaConformance:
    def test_every_span_dict_has_exactly_the_frozen_schema_keys(
        self, parsed: ParsedSession
    ) -> None:
        for span in parsed.spans:
            assert set(span.to_dict().keys()) == SCHEMA_KEYS

    def test_tokens_and_cost_are_null_at_parse_time(self, parsed: ParsedSession) -> None:
        for span in parsed.spans:
            assert span.tokens_in is None
            assert span.tokens_out is None
            assert span.cost_usd is None

    def test_session_metadata_attributed(self, parsed: ParsedSession) -> None:
        for span in parsed.spans:
            assert span.session_id == SESSION_ID
            assert span.repo == "proj"
            assert span.branch == "feature/22-demo"

    def test_span_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="unknown span kind"):
            Span(span_id="x", kind="bogus", name="n")


class TestPrivacy:
    def test_no_prompt_or_output_text_leaks_into_spans(self, parsed: ParsedSession) -> None:
        blob = "".join(str(s.to_dict()) for s in parsed.spans)
        for secret in SECRETS:
            assert secret not in blob


def _assistant_todo(uuid: str, ts: str, tool_id: str, todos: list[dict]) -> dict:
    return {
        "type": "assistant",
        "sessionId": "diff-sess",
        "cwd": "/Users/demo/Repos/proj",
        "gitBranch": "feature/47-demo",
        "timestamp": ts,
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "TodoWrite", "input": {"todos": todos}}
            ],
        },
    }


class TestTodoLedgerDiff:
    """Issue #47: the in-progress item per ledger write drives the L1 step label."""

    def test_newly_in_progress_item_is_chosen_across_consecutive_writes(
        self, tmp_path: Path
    ) -> None:
        # Two snapshots: write 1 marks A in_progress; write 2 advances to B. The
        # diff must pick the item that NEWLY entered in_progress on each write.
        records = [
            _assistant_todo(
                "w1",
                "2026-06-15T12:00:01.000Z",
                "tool_w1",
                [
                    {"content": "Add RED test", "status": "in_progress"},
                    {"content": "Implement GREEN", "status": "pending"},
                ],
            ),
            _assistant_todo(
                "w2",
                "2026-06-15T12:00:30.000Z",
                "tool_w2",
                [
                    {"content": "Add RED test", "status": "completed"},
                    {"content": "Implement GREEN", "status": "in_progress"},
                ],
            ),
        ]
        path = tmp_path / "diff-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todos = sorted(_by_kind(parse_session_file(path), "todo"), key=lambda s: s.ts_start or "")

        assert [t.summary for t in todos] == ["Add RED test", "Implement GREEN"]
        assert [t.name for t in todos] == ["TodoWrite", "TodoWrite"]

    def test_todo_write_without_in_progress_item_has_no_summary(self, tmp_path: Path) -> None:
        # A snapshot with nothing in_progress has no item to advance → no summary
        # is derived; the span keeps its bare tool name (never crashes, never blank).
        records = [
            _assistant_todo(
                "w1",
                "2026-06-15T12:00:01.000Z",
                "tool_w1",
                [{"content": "Add RED test", "status": "pending"}],
            )
        ]
        path = tmp_path / "diff-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todo = _by_kind(parse_session_file(path), "todo")[0]

        assert todo.name == "TodoWrite"
        assert todo.summary is None


def _assistant_tasks(uuid: str, ts: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "sessionId": "task-sess",
        "cwd": "/Users/demo/Repos/proj",
        "gitBranch": "feature/47-demo",
        "timestamp": ts,
        "uuid": uuid,
        "message": {"role": "assistant", "model": "claude-opus-4-8", "content": blocks},
    }


class TestSummaryIsFullText:
    """Issue #47: node labels show the full text — never truncated with an ellipsis."""

    def test_long_subject_is_not_truncated(self, tmp_path: Path) -> None:
        # A long ledger item must render in full so it is readable in the L1 label;
        # no word/char cap and no trailing ellipsis.
        long_subject = (
            "PLAN gate explore the code and present the full implementation plan then park"
        )
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": long_subject},
                    }
                ],
            )
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todo = _by_kind(parse_session_file(path), "todo")[0]

        assert todo.summary == long_subject
        assert "…" not in (todo.summary or "")

    def test_multiline_prompt_keeps_its_full_first_line(self, tmp_path: Path) -> None:
        # A multi-line prompt scopes to its first line (the gist), but that line is
        # never truncated — internal whitespace is just collapsed.
        text = "Refactor the parser to retain the   in-progress todo\nplus some more detail below"
        records = [
            {
                "type": "user",
                "sessionId": "p-sess",
                "cwd": "/Users/demo/Repos/proj",
                "gitBranch": "feature/47-demo",
                "timestamp": "2026-06-15T12:00:00.000Z",
                "uuid": "u1",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        ]
        path = tmp_path / "p-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        prompt = _by_kind(parse_session_file(path), "human")[0]

        assert prompt.summary == "Refactor the parser to retain the in-progress todo"


class TestTaskLedgerResolution:
    """Issue #47: TaskCreate/TaskUpdate are id-keyed; resolve to the task subject."""

    def test_task_create_summary_is_its_subject(self, tmp_path: Path) -> None:
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": "Add RED parser test", "description": "x"},
                    }
                ],
            )
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todo = _by_kind(parse_session_file(path), "todo")[0]

        assert todo.summary == "Add RED parser test"
        assert todo.name == "TaskCreate"

    def test_task_update_resolves_to_the_subject_of_the_task_it_updates(
        self, tmp_path: Path
    ) -> None:
        # Two TaskCreates assign ids 1, 2 in creation order; a later TaskUpdate
        # referencing taskId "2" must name the second task, never a generic label.
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": "First task"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tc2",
                        "name": "TaskCreate",
                        "input": {"subject": "Second task"},
                    },
                ],
            ),
            _assistant_tasks(
                "a2",
                "2026-06-15T12:01:00.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "TaskUpdate",
                        "input": {"taskId": "2", "status": "in_progress"},
                    }
                ],
            ),
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        update = next(
            s for s in _by_kind(parse_session_file(path), "todo") if s.name == "TaskUpdate"
        )

        assert update.summary == "Second task"


class TestProjectsDirWalk:
    def test_parse_projects_dir_finds_the_session_spans(self) -> None:
        merged = parse_projects_dir(FIXTURES)
        kinds = {s.kind for s in merged.spans}
        assert {"skill", "agent", "todo", "human"} <= kinds

    def test_parse_projects_dir_does_not_treat_subagent_file_as_a_session(self) -> None:
        # The subagent transcript must be walked for tokens, never parsed as a
        # top-level session (which would double-count and mis-kind its spans).
        merged = parse_projects_dir(FIXTURES)
        assert len(_by_kind(merged, "agent")) == 1
