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

from telemetry.session_parser import (
    ParsedSession,
    _usage_event,
    _walk_transcript,
    parse_project_dir,
    parse_projects_dir,
    parse_session_file,
    project_dir_for_worktree,
    thinking_by_turn,
)
from telemetry.spans import Span

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
PROJECT = FIXTURES / "-Users-demo-Repos-proj"
SESSION = PROJECT / "11111111-1111-1111-1111-111111111111.jsonl"
SESSION_ID = "11111111-1111-1111-1111-111111111111"

# Issue #51 (track B) v3 scenarios live in their own session so the frozen #22/#47
# token math on SESSION stays untouched. Its subagents/workflows/ holds a Workflow
# fan-out (S1): two agents under one workflow ``wf_review01``.
WF_SESSION = PROJECT / "22222222-2222-2222-2222-222222222222.jsonl"
WF_SESSION_ID = "22222222-2222-2222-2222-222222222222"
WF_AGENT_IDS = frozenset({"cccc3333dddd4444", "eeee5555ffff6666"})

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
    # The hook's raising condition (Issue #82) — set on hook spans, null on pull spans.
    "hook_event",
    "summary",
    # v3 link fields (Issue #50) — additive, pull-only; null on push spans.
    "emits",
    "sidecar_session",
    "agent_link",
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
    # Tool leaves surface only their MAIN identifying parameter (Bash command,
    # Read/Edit/Write path, Grep pattern). Bulk/secondary fields stay filtered —
    # Edit's replacement text, Write's content body, and tool result output must
    # never appear in a span.
    "SECRET_EDIT_OLD",
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

    def test_tool_span_summary_is_its_main_parameter(self, parsed: ParsedSession) -> None:
        # The tool leaf names what it acted on: Bash → command, Read/Edit/Write →
        # file path, Grep → pattern. So the trace reads "what over what".
        by_name = {t.name: t for t in _by_kind(parsed, "tool")}
        assert by_name["Bash"].summary == "pytest tests/unit -q"
        assert by_name["Read"].summary == "/repo/dashboard/queries.py"
        assert by_name["Edit"].summary == "/repo/app.py"
        assert by_name["Grep"].summary == "_bucket_traces"
        assert by_name["Write"].summary == "/repo/notes.md"

    def test_tool_span_carries_no_cost_at_parse_time(self, parsed: ParsedSession) -> None:
        for tool in _by_kind(parsed, "tool"):
            assert tool.tokens_in is None
            assert tool.tokens_out is None
            assert tool.cost_usd is None

    def test_only_the_main_parameter_leaks_not_bulk_input_or_output(
        self, parsed: ParsedSession
    ) -> None:
        # Edit's replacement text, Write's content body, and the tool result output
        # are secondary/bulk fields — surfacing the main param must not drag them in.
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


def _agent_span(parsed: ParsedSession) -> Span:
    return _by_kind(parsed, "agent")[0]


def _subagent_spans(parsed: ParsedSession) -> list[Span]:
    """Spans the parser reconstructs from the walked sub-agent transcript."""
    agent_id = _agent_span(parsed).span_id
    return [s for s in parsed.spans if s.parent_id == agent_id]


class TestSubagentSpans:
    """Issue #47 S3: the sub-agent's own steps become spans under the agent span."""

    def test_subagent_tool_use_emits_a_span(self, parsed: ParsedSession) -> None:
        reads = [s for s in _subagent_spans(parsed) if s.kind == "tool" and s.name == "Read"]
        assert len(reads) == 1

    def test_subagent_span_inherits_parent_session_and_repo(self, parsed: ParsedSession) -> None:
        # The sub-agent span joins the spoke via the PARENT session id, not the
        # sub-agent transcript's own session metadata.
        sub = _subagent_spans(parsed)[0]
        assert sub.session_id == SESSION_ID
        assert sub.repo == "proj"
        assert sub.branch == "feature/22-demo"

    def test_subagent_span_parent_id_is_the_agent_span(self, parsed: ParsedSession) -> None:
        sub = next(s for s in _subagent_spans(parsed) if s.name == "Read")
        assert sub.parent_id == _agent_span(parsed).span_id

    def test_subagent_span_summary_is_its_main_parameter(self, parsed: ParsedSession) -> None:
        sub = next(s for s in _subagent_spans(parsed) if s.name == "Read")
        assert sub.summary == "/repo/sub/code.py"

    def test_subagent_span_ids_are_idempotent(self) -> None:
        a = {s.span_id for s in _subagent_spans(parse_session_file(SESSION))}
        b = {s.span_id for s in _subagent_spans(parse_session_file(SESSION))}
        assert a == b and a

    def test_subagent_spans_carry_no_tokens_or_cost_at_parse_time(
        self, parsed: ParsedSession
    ) -> None:
        for sub in _subagent_spans(parsed):
            assert sub.tokens_in is None
            assert sub.tokens_out is None
            assert sub.cost_usd is None

    def test_subagent_usage_event_totals_unchanged(self, parsed: ParsedSession) -> None:
        # Emitting sub-agent spans must not add usage events: s2b carries no usage,
        # so the agent's token pool stays 700/450/1000.
        sub = [e for e in parsed.usage_events if e.source == "subagent"]
        assert sum(e.input_tokens for e in sub) == 700
        assert sum(e.output_tokens for e in sub) == 450

    def test_subagent_emits_no_human_prompt_span(self, parsed: ParsedSession) -> None:
        # The sub-agent transcript's leading user record is the orchestrator's task
        # prompt — it must NEVER become a human-prompt span (it would leak the
        # prompt). Locked structurally, not only via the secret-string scan.
        agent_id = _agent_span(parsed).span_id
        assert not [s for s in parsed.spans if s.kind == "human" and s.parent_id == agent_id]


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

    def test_ledger_creation_write_is_summarised_by_its_lead_item(self, tmp_path: Path) -> None:
        # Issue #51 S3: a pure ledger-creation write (items created, nothing yet in
        # progress) is no longer a bare `todo` — it carries its lead item as a summary.
        records = [
            _assistant_todo(
                "w1",
                "2026-06-15T12:00:01.000Z",
                "tool_w1",
                [
                    {"content": "Seed RED test", "status": "pending"},
                    {"content": "Implement GREEN", "status": "pending"},
                ],
            )
        ]
        path = tmp_path / "diff-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todo = _by_kind(parse_session_file(path), "todo")[0]

        assert todo.name == "TodoWrite"
        assert todo.summary == "Seed RED test"

    def test_redundant_write_with_no_new_item_has_no_summary(self, tmp_path: Path) -> None:
        # A later snapshot that introduces no new item and has nothing in progress has
        # nothing to label → no summary (the span keeps its bare tool name).
        records = [
            _assistant_todo(
                "w1",
                "2026-06-15T12:00:01.000Z",
                "tool_w1",
                [{"content": "Seed RED test", "status": "in_progress"}],
            ),
            _assistant_todo(
                "w2",
                "2026-06-15T12:00:30.000Z",
                "tool_w2",
                [{"content": "Seed RED test", "status": "completed"}],
            ),
        ]
        path = tmp_path / "diff-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        todos = sorted(_by_kind(parse_session_file(path), "todo"), key=lambda s: s.ts_start or "")

        assert todos[1].name == "TodoWrite"
        assert todos[1].summary is None


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

        # The subject anchors the summary (id 2 → "Second task", not "First task");
        # Issue #81 then appends the status transition.
        assert update.summary == "Second task: pending → in_progress"

    def test_task_update_summary_shows_the_status_transition(self, tmp_path: Path) -> None:
        # Issue #81: a TaskUpdate must read as progress, not just the bare subject —
        # else it is indistinguishable from the seed. The summary records the status
        # flip (prior → new) alongside the subtask label.
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": "Add RED test"},
                    }
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
                        "input": {"taskId": "1", "status": "in_progress"},
                    }
                ],
            ),
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        spans = _by_kind(parse_session_file(path), "todo")
        seed = next(s for s in spans if s.name == "TaskCreate")
        update = next(s for s in spans if s.name == "TaskUpdate")

        assert seed.summary == "Add RED test"
        assert update.summary == "Add RED test: pending → in_progress"

    def test_task_update_without_a_status_flip_keeps_the_bare_subject(self, tmp_path: Path) -> None:
        # Issue #81: an update that carries no status (e.g. an owner/subject edit) has
        # no transition to show, so it falls back to the bare subject — unchanged.
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": "Add RED test"},
                    }
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
                        "input": {"taskId": "1", "owner": "alice"},
                    }
                ],
            ),
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        update = next(
            s for s in _by_kind(parse_session_file(path), "todo") if s.name == "TaskUpdate"
        )

        assert update.summary == "Add RED test"

    def test_seed_then_update_sequence_parses_into_ordered_todo_spans(self, tmp_path: Path) -> None:
        # Issue #81: a create-then-advance sequence yields ordered todo spans whose
        # summaries read as a progression through the lifecycle.
        records = [
            _assistant_tasks(
                "a1",
                "2026-06-15T12:00:01.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tc1",
                        "name": "TaskCreate",
                        "input": {"subject": "Ship feature"},
                    }
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
                        "input": {"taskId": "1", "status": "in_progress"},
                    }
                ],
            ),
            _assistant_tasks(
                "a3",
                "2026-06-15T12:02:00.000Z",
                [
                    {
                        "type": "tool_use",
                        "id": "tu2",
                        "name": "TaskUpdate",
                        "input": {"taskId": "1", "status": "completed"},
                    }
                ],
            ),
        ]
        path = tmp_path / "task-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        spans = sorted(_by_kind(parse_session_file(path), "todo"), key=lambda s: s.ts_start or "")

        assert [s.name for s in spans] == ["TaskCreate", "TaskUpdate", "TaskUpdate"]
        assert [s.summary for s in spans] == [
            "Ship feature",
            "Ship feature: pending → in_progress",
            "Ship feature: in_progress → completed",
        ]


@pytest.fixture()
def wf_parsed() -> ParsedSession:
    return parse_session_file(WF_SESSION)


def _wf_agent(parsed: ParsedSession, agent_id: str) -> Span:
    return next(
        s
        for s in parsed.spans
        if s.kind == "agent" and parsed.agent_links.get(s.span_id) == agent_id
    )


class TestWorkflowAgentDiscovery:
    """Issue #51 S1: Workflow fan-out agents live one level deeper than today's
    ``subagents/agent-*.jsonl`` — at ``subagents/workflows/wf_*/agent-*.jsonl`` — so
    they are discovered by walking that tree (no Task ``tool_use`` links them) and
    bracketed by their own ``agent`` span. They no longer orphan to ``(unresolved)``.
    """

    def test_discovers_an_agent_span_per_workflow_agent(self, wf_parsed: ParsedSession) -> None:
        # The two workflow agents are identified by their workflow-name summary; the
        # session also holds a Task-spawned recursion chain (S2), so this scopes to
        # the workflow fan-out rather than asserting global agent-span exclusivity.
        wf_agents = [
            s for s in wf_parsed.spans if s.kind == "agent" and s.summary == "review-changes"
        ]
        assert {wf_parsed.agent_links[s.span_id] for s in wf_agents} == set(WF_AGENT_IDS)
        assert len(wf_agents) == 2

    def test_workflow_agent_name_is_its_agent_type(self, wf_parsed: ParsedSession) -> None:
        # ``name`` stays the stable grouping key — the meta.json agentType, mirroring
        # how a Task agent's name is its subagent_type.
        assert _wf_agent(wf_parsed, "cccc3333dddd4444").name == "code-review"
        assert _wf_agent(wf_parsed, "eeee5555ffff6666").name == "Explore"

    def test_workflow_agent_summary_is_the_workflow_name(self, wf_parsed: ParsedSession) -> None:
        # The few-word display label is the workflow's name (the "+ workflow name"
        # half of the issue's "agentType + workflow name" label).
        assert _wf_agent(wf_parsed, "cccc3333dddd4444").summary == "review-changes"
        assert _wf_agent(wf_parsed, "eeee5555ffff6666").summary == "review-changes"

    def test_window_brackets_transcript_first_and_last_ts(self, wf_parsed: ParsedSession) -> None:
        agent = _wf_agent(wf_parsed, "cccc3333dddd4444")
        assert agent.ts_start == "2026-06-14T12:01:10.000Z"
        assert agent.ts_end == "2026-06-14T12:01:25.000Z"
        assert agent.duration_ms == 15000

    def test_agent_link_field_mirrors_the_links_map(self, wf_parsed: ParsedSession) -> None:
        # Issue #50: every agent span carries its own ``agent_link`` (the per-span
        # half of agent_links) so agent→agent recursion composes into a chain.
        for span in (s for s in wf_parsed.spans if s.kind == "agent"):
            assert span.agent_link == wf_parsed.agent_links.get(span.span_id)
            assert span.agent_link is not None

    def test_links_registered_for_cost_attribution(self, wf_parsed: ParsedSession) -> None:
        assert set(WF_AGENT_IDS) <= set(wf_parsed.agent_links.values())

    def test_workflow_agent_usage_events_tagged_as_subagent(self, wf_parsed: ParsedSession) -> None:
        events = [e for e in wf_parsed.usage_events if e.agent_id in WF_AGENT_IDS]
        assert events
        assert all(e.source == "subagent" for e in events)
        # cccc transcript: 400+100 input, 200+50 output across two usage turns.
        cccc = [e for e in events if e.agent_id == "cccc3333dddd4444"]
        assert sum(e.input_tokens for e in cccc) == 500
        assert sum(e.output_tokens for e in cccc) == 250

    def test_span_rehomed_onto_the_spoke_session(self, wf_parsed: ParsedSession) -> None:
        agent = _wf_agent(wf_parsed, "cccc3333dddd4444")
        assert agent.session_id == WF_SESSION_ID
        assert agent.repo == "proj"
        assert agent.branch == "feature/51-demo"

    def test_workflow_agent_child_tool_spans_nest_under_it(self, wf_parsed: ParsedSession) -> None:
        agent = _wf_agent(wf_parsed, "cccc3333dddd4444")
        children = [s for s in wf_parsed.spans if s.parent_id == agent.span_id]
        grep = next(c for c in children if c.kind == "tool" and c.name == "Grep")
        assert grep.summary == "_walk_workflow_agents"

    def test_fan_out_groups_under_a_workflow_phase_subtree(self, wf_parsed: ParsedSession) -> None:
        # Issue #58: the canonical fixture's fan-out renders as workflow → phase →
        # agent. ``cccc`` ran in "Review", ``eeee`` in "Scan" (per workflowProgress).
        workflow = next(s for s in wf_parsed.spans if s.kind == "workflow")
        assert workflow.name == "review-changes" and workflow.parent_id is None
        phase_of = {
            s.name: s.span_id
            for s in wf_parsed.spans
            if s.kind == "workflow_phase" and s.parent_id == workflow.span_id
        }
        assert phase_of.keys() == {"Scan", "Review"}
        assert _wf_agent(wf_parsed, "cccc3333dddd4444").parent_id == phase_of["Review"]
        assert _wf_agent(wf_parsed, "eeee5555ffff6666").parent_id == phase_of["Scan"]

    def test_span_ids_are_idempotent(self) -> None:
        a = {s.span_id for s in parse_session_file(WF_SESSION).spans if s.kind == "agent"}
        b = {s.span_id for s in parse_session_file(WF_SESSION).spans if s.kind == "agent"}
        assert a == b and len(a) >= 2

    def test_no_task_prompt_or_agent_work_leaks(self, wf_parsed: ParsedSession) -> None:
        blob = "".join(str(s.to_dict()) for s in wf_parsed.spans)
        for secret in SECRETS:
            assert secret not in blob

    def test_no_human_prompt_span_from_a_workflow_agent(self, wf_parsed: ParsedSession) -> None:
        # The agent transcript's leading user record is the orchestrator's task
        # prompt — never a human-prompt span (it would leak the prompt).
        agent_span_ids = {s.span_id for s in wf_parsed.spans if s.kind == "agent"}
        leaked = [s for s in wf_parsed.spans if s.kind == "human" and s.parent_id in agent_span_ids]
        assert not leaked


CHAIN_IDS = ("a1a1a1a1a1a1a1a1", "b2b2b2b2b2b2b2b2", "c3c3c3c3c3c3c3c3")


def _agents_by_link(parsed: ParsedSession) -> dict[str, Span]:
    return {
        parsed.agent_links[s.span_id]: s
        for s in parsed.spans
        if s.kind == "agent" and s.span_id in parsed.agent_links
    }


class TestRecursiveAgents:
    """Issue #51 S2: a sub-agent that spawns another sub-agent chains agent_links
    parent→child so agent→agent→… reconstructs at any depth. Each level's turns are
    tagged with that level's own agentId, and each agent span's window nests inside
    its parent's so the tree re-homes them by containment.
    """

    def test_chains_agent_links_to_unbounded_depth(self, wf_parsed: ParsedSession) -> None:
        assert set(CHAIN_IDS) <= set(wf_parsed.agent_links.values())

    def test_each_nested_agent_has_its_own_linked_span(self, wf_parsed: ParsedSession) -> None:
        by_link = _agents_by_link(wf_parsed)
        for chain_id in CHAIN_IDS:
            assert chain_id in by_link
            assert by_link[chain_id].agent_link == chain_id

    def test_nested_agent_names_are_their_subagent_types(self, wf_parsed: ParsedSession) -> None:
        by_link = _agents_by_link(wf_parsed)
        assert by_link["a1a1a1a1a1a1a1a1"].name == "general-purpose"
        assert by_link["b2b2b2b2b2b2b2b2"].name == "Explore"
        assert by_link["c3c3c3c3c3c3c3c3"].name == "tdd-red"

    def test_windows_nest_by_containment(self, wf_parsed: ParsedSession) -> None:
        # the forest builder homes a subagent turn under the tightest enclosing agent by
        # time window, so each child's [ts_start, ts_end] must sit inside its parent's.
        windows = [
            (a.ts_start, a.ts_end) for a in (_agents_by_link(wf_parsed)[c] for c in CHAIN_IDS)
        ]
        assert all(start and end for start, end in windows)
        (d1s, d1e), (d2s, d2e), (d3s, d3e) = windows
        # Narrow each bound from ``str | None`` to ``str`` (the assert above already
        # proved them non-null) so the ordering comparisons type-check.
        assert d1s and d1e and d2s and d2e and d3s and d3e
        assert d1s <= d2s and d2e <= d1e
        assert d2s <= d3s and d3e <= d2e

    def test_each_level_usage_tagged_with_its_own_agent_id(self, wf_parsed: ParsedSession) -> None:
        for chain_id in CHAIN_IDS:
            events = [e for e in wf_parsed.usage_events if e.agent_id == chain_id]
            assert events
            assert all(e.source == "subagent" for e in events)

    def test_no_secret_leaks_across_the_chain(self, wf_parsed: ParsedSession) -> None:
        blob = "".join(str(s.to_dict()) for s in wf_parsed.spans)
        for secret in SECRETS:
            assert secret not in blob

    def test_recursion_terminates_on_a_cyclic_link(self, tmp_path: Path) -> None:
        # Two agents referencing each other must not loop forever — a seen-set guard
        # stops the walk after each agent id is visited once.
        session_id = "cycle-sess"
        main = tmp_path / f"{session_id}.jsonl"
        subagents = tmp_path / session_id / "subagents"
        subagents.mkdir(parents=True)

        def task(uuid: str, ts: str, tool_id: str, sub_type: str) -> dict:
            return {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": "/Users/demo/Repos/proj",
                "gitBranch": "feature/51-demo",
                "timestamp": ts,
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Task",
                            "input": {"description": "spawn", "subagent_type": sub_type},
                        }
                    ],
                },
            }

        def result(uuid: str, ts: str, tool_id: str, agent_id: str) -> dict:
            return {
                "type": "user",
                "sessionId": session_id,
                "timestamp": ts,
                "uuid": uuid,
                "toolUseResult": {"agentId": agent_id},
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": False}],
                },
            }

        main.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    task("m1", "2026-06-14T13:00:00.000Z", "t_x", "Explore"),
                    result("m2", "2026-06-14T13:00:10.000Z", "t_x", "agentX"),
                ]
            ),
            encoding="utf-8",
        )
        (subagents / "agent-agentX.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    task("x1", "2026-06-14T13:00:01.000Z", "t_y", "Explore"),
                    result("x2", "2026-06-14T13:00:05.000Z", "t_y", "agentY"),
                ]
            ),
            encoding="utf-8",
        )
        (subagents / "agent-agentY.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    task("y1", "2026-06-14T13:00:02.000Z", "t_x2", "Explore"),
                    result("y2", "2026-06-14T13:00:03.000Z", "t_x2", "agentX"),
                ]
            ),
            encoding="utf-8",
        )

        parsed = parse_session_file(main)  # must return, not hang

        assert {"agentX", "agentY"} <= set(parsed.agent_links.values())


class TestReasoningRefs:
    """Issue #51 S3: an extended-thinking block surfaces a privacy-safe reasoning
    ref — a transcript-link locator + timestamp, never the thinking text itself.
    """

    def test_main_thinking_emits_a_reasoning_ref(self, parsed: ParsedSession) -> None:
        # The #22 main session has exactly one assistant turn with a thinking block.
        refs = parsed.reasoning_refs
        assert len(refs) == 1
        assert refs[0].source == "main"
        assert refs[0].session_id == SESSION_ID

    def test_reasoning_ref_carries_a_locator_and_timestamp(self, parsed: ParsedSession) -> None:
        ref = parsed.reasoning_refs[0]
        assert ref.ts == "2026-06-13T12:00:01.000Z"
        assert SESSION_ID in ref.ref  # transcript-link locator points at the session

    def test_reasoning_ref_never_carries_thinking_text(self, parsed: ParsedSession) -> None:
        assert "SECRET_THINKING" not in str(parsed.reasoning_refs[0])

    def test_reasoning_ref_summary_is_the_turn_narration(self, parsed: ParsedSession) -> None:
        # Issue #59: the per-turn reasoning summary is the turn's *visible* narration
        # (the user-facing text block), never the redacted thinking — real extended
        # thinking is signature-only, so the narration is the privacy-safe gist.
        assert parsed.reasoning_refs[0].summary == "Running skill"

    def test_parse_projects_dir_merges_reasoning_refs(self) -> None:
        merged = parse_projects_dir(FIXTURES)
        assert any(r.source == "main" for r in merged.reasoning_refs)


class TestThinkingExtraction:
    """Issue #92: the transcript→Langfuse backfill needs the extended-thinking BODY
    (redacted in every OTel raw API body) keyed by turn ``uuid`` — the causal node id.

    It is read ONLY by this opt-in extractor; the default parse (spans + reasoning
    refs) never surfaces a thinking body (privacy / volume), so the backfill's caller
    gates the call behind its flag. Only turns whose thinking block carries real text
    are keyed — signature-only extended thinking and narration-only turns are absent.
    """

    def test_thinking_body_keyed_by_turn_uuid(self) -> None:
        thinking = thinking_by_turn(SESSION)
        assert thinking["a1"] == "SECRET_THINKING"

    def test_only_turns_that_carry_thinking_are_keyed(self) -> None:
        thinking = thinking_by_turn(SESSION)
        # The #22 main session has exactly one assistant turn with a thinking block;
        # narration-only turns are never keyed, and every value is non-empty text.
        assert set(thinking) == {"a1"}
        assert all(isinstance(text, str) and text for text in thinking.values())

    def test_default_parse_never_surfaces_the_thinking_body(self, parsed: ParsedSession) -> None:
        # The opt-in extractor is the ONLY reader of the thinking body: the default
        # parser output stays byte-for-byte free of it (reasoning refs are gist-only).
        assert "SECRET_THINKING" not in str(parsed.spans)
        assert "SECRET_THINKING" not in str(parsed.reasoning_refs)

    def test_walks_thinking_from_a_subagent_transcript(self, tmp_path: Path) -> None:
        # The walk reaches sub-agent transcripts under ``<stem>/``: a thinking body in a
        # sub-agent turn is keyed by that turn's (globally unique) uuid, not the main one.
        main = tmp_path / "s.jsonl"
        main.write_text(json.dumps(_assistant_thinking("m1", "MAIN_THINK")), encoding="utf-8")
        sub = tmp_path / "s" / "subagents" / "agent-x.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps(_assistant_thinking("s1", "SUB_THINK")), encoding="utf-8")

        thinking = thinking_by_turn(main)

        assert thinking == {"m1": "MAIN_THINK", "s1": "SUB_THINK"}


def _assistant_thinking(uuid: str, thinking: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": "think-sess",
        "timestamp": "2026-06-15T12:00:01.000Z",
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "thinking", "thinking": thinking}],
        },
    }


def _assistant_bash(uuid: str, ts: str, tool_id: str, command: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": "side-sess",
        "cwd": "/Users/demo/Repos/proj",
        "gitBranch": "feature/51-demo",
        "timestamp": ts,
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
            ],
        },
    }


class TestSidecarSeam:
    """Issue #51 S3: a hook/script that shells out to a separate ``claude -p`` session
    is linked via ``sidecar_session`` (seam only — none in-repo yet).
    """

    def _bash(self, tmp_path: Path, command: str) -> Span:
        path = tmp_path / "side-sess.jsonl"
        path.write_text(
            json.dumps(_assistant_bash("w1", "2026-06-15T12:00:01.000Z", "tb", command)),
            encoding="utf-8",
        )
        return next(t for t in _by_kind(parse_session_file(path), "tool") if t.name == "Bash")

    def test_claude_dash_p_invocation_sets_sidecar_session(self, tmp_path: Path) -> None:
        bash = self._bash(tmp_path, "claude -p --session-id side-abc123 'review the diff'")
        assert bash.sidecar_session == "side-abc123"

    def test_resume_flag_form_is_recognised(self, tmp_path: Path) -> None:
        bash = self._bash(tmp_path, "claude --print --resume side-xyz789 'judge this'")
        assert bash.sidecar_session == "side-xyz789"

    def test_plain_bash_has_no_sidecar_session(self, tmp_path: Path) -> None:
        bash = self._bash(tmp_path, "pytest tests/unit -q")
        assert bash.sidecar_session is None

    def test_claude_without_print_flag_is_not_a_sidecar(self, tmp_path: Path) -> None:
        # An interactive `claude` (no -p/--print) is not a headless sidecar session.
        bash = self._bash(tmp_path, "claude --session-id side-nope chat")
        assert bash.sidecar_session is None

    def test_short_r_flag_does_not_match_the_tail_of_another_flag(self, tmp_path: Path) -> None:
        # `-r` must be left-word-bounded: a trailing `-r` inside `--foo-r` is not the
        # resume flag, so the real --session-id later in the command wins.
        bash = self._bash(tmp_path, "claude -p --foo-r bad --session-id side-real go")
        assert bash.sidecar_session == "side-real"


class TestRuleAndContextLoads:
    """Issue #59: loaded context is read from the session log's real `attachment`
    records — `nested_memory` (rules / CLAUDE.md / memory, path + body) and
    `deferred_tools_delta` (tool schemas). Every item becomes a `rule`-kind span
    whose `phase` names its subtype (`rule` / `CLAUDE.md` / `memory` / `tool-schema`),
    so the dashboard groups them into one `context` node per subtype. Only the name
    and a body-size token estimate are read — never the rule/memory/CLAUDE.md body.

    This supersedes #51 S4, whose `Contents of <path>` system-reminder detection only
    ever matched the hand-built fixture: real transcripts carry no such header, so the
    surface measured zero on all 31 audited spokes (the defect #59 closes).
    """

    BODY_SECRETS = ("CLAUDEMD_BODY_SECRET", "RULE_BODY_SECRET", "MEMORY_BODY_SECRET")

    def _ctx(self, parsed: ParsedSession, phase: str) -> list[Span]:
        return [s for s in parsed.spans if s.kind == "rule" and s.phase == phase]

    def test_emits_a_rule_span_per_loaded_rule(self, wf_parsed: ParsedSession) -> None:
        names = {s.name for s in self._ctx(wf_parsed, "rule")}
        assert {"python-style", "code-quality"} <= names

    def test_rule_span_window_is_at_load_time(self, wf_parsed: ParsedSession) -> None:
        rule = next(s for s in self._ctx(wf_parsed, "rule") if s.name == "python-style")
        assert rule.ts_start == "2026-06-14T12:00:59.000Z"
        assert rule.session_id == WF_SESSION_ID
        assert rule.repo == "proj"

    def test_claude_md_is_a_context_span(self, wf_parsed: ParsedSession) -> None:
        names = {s.name for s in self._ctx(wf_parsed, "CLAUDE.md")}
        assert "CLAUDE.md" in names

    def test_memory_is_a_context_span(self, wf_parsed: ParsedSession) -> None:
        assert self._ctx(wf_parsed, "memory")

    def test_tool_schemas_become_context_spans(self, wf_parsed: ParsedSession) -> None:
        names = {s.name for s in self._ctx(wf_parsed, "tool-schema")}
        assert {"WebFetch", "WebSearch", "Bash"} <= names

    def test_context_subtypes_cover_every_kind(self, wf_parsed: ParsedSession) -> None:
        subtypes = {s.phase for s in wf_parsed.spans if s.kind == "rule"}
        assert {"rule", "memory", "CLAUDE.md", "tool-schema"} <= subtypes

    def test_each_context_item_carries_a_per_item_token_estimate(
        self, wf_parsed: ParsedSession
    ) -> None:
        # "drillable to per-item tokens": each item's summary is a token estimate so a
        # reader can weigh its context cost. The longer rule estimates more than memory.
        rule = next(s for s in self._ctx(wf_parsed, "rule") if s.name == "python-style")
        assert rule.summary is not None and "token" in rule.summary
        assert any(char.isdigit() for char in rule.summary)

    def test_each_rule_loaded_once(self, wf_parsed: ParsedSession) -> None:
        rules = [s for s in self._ctx(wf_parsed, "rule") if s.name == "python-style"]
        assert len(rules) == 1

    def test_no_rule_or_context_body_text_leaks(self, wf_parsed: ParsedSession) -> None:
        blob = "".join(str(s.to_dict()) for s in wf_parsed.spans if s.kind == "rule")
        for secret in self.BODY_SECRETS:
            assert secret not in blob

    def test_parse_projects_dir_merges_context_spans(self) -> None:
        merged = parse_projects_dir(FIXTURES)
        assert any(s.kind == "rule" and s.phase == "memory" for s in merged.spans)

    def test_prose_quoting_a_contents_header_is_not_a_load(self, tmp_path: Path) -> None:
        # Only `attachment` records mint context spans; ordinary prose that merely
        # quotes a "Contents of …" line must NOT mint a phantom rule span.
        records = [
            {
                "type": "assistant",
                "sessionId": "prose-sess",
                "cwd": "/Users/demo/Repos/proj",
                "gitBranch": "feature/51-demo",
                "timestamp": "2026-06-15T09:00:01.000Z",
                "uuid": "p1",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [
                        {
                            "type": "text",
                            "text": "I read the Contents of /repo/.claude/rules/ghost.md",
                        }
                    ],
                },
            },
        ]
        path = tmp_path / "prose-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        parsed = parse_session_file(path)

        assert not [s for s in parsed.spans if s.kind == "rule"]


class TestProjectsDirWalk:
    def test_parse_projects_dir_finds_the_session_spans(self) -> None:
        merged = parse_projects_dir(FIXTURES)
        kinds = {s.kind for s in merged.spans}
        assert {"skill", "agent", "todo", "human"} <= kinds

    def test_parse_projects_dir_does_not_treat_subagent_file_as_a_session(self) -> None:
        # Subagent transcripts (Task at subagents/agent-*.jsonl and workflow agents
        # at subagents/workflows/wf_*/agent-*.jsonl) must be walked for tokens, never
        # parsed as top-level sessions (which would double-count and mis-kind them).
        # The project holds exactly six agents: one Task agent (#22 session), the two
        # workflow agents and the three-deep recursion chain (#51 session) — and no
        # more (no subagent transcript re-parsed as a top-level session).
        merged = parse_projects_dir(FIXTURES)
        agents = _by_kind(merged, "agent")
        assert len(agents) == 6
        # Every agent span is linked for cost attribution; none is a stray re-parse.
        assert all(merged.agent_links.get(a.span_id) for a in agents)


class TestWorkflowGroupingNodes:
    """Issue #58: a ``Workflow`` fan-out must render as a ``workflow → workflow_phase
    → agent`` subtree on REAL data, not just the hand-built golden fixture.

    Real workflow metadata lives at ``<session>/workflows/<runId>.json`` (carrying
    ``workflowName``, ``phases`` and the ``workflowProgress`` agent→phase map), one
    directory above the ``subagents/workflows/wf_*/`` agent transcripts; each agent's
    type is in its sidecar ``agent-<id>.meta.json``. The parser must read that layout
    and emit the container spans, nesting phases under the workflow and re-homing each
    workflow agent under its phase. Containers carry no ``agent_link`` (own-cost $0 —
    cost rolls up from their agent children).
    """

    RUN_ID = "wf_grp01"
    SESSION_ID = "33333333-3333-3333-3333-333333333333"

    @pytest.fixture()
    def grouped(self, tmp_path: Path) -> ParsedSession:
        session = self.SESSION_ID
        main = tmp_path / f"{session}.jsonl"
        wf_dir = tmp_path / session / "subagents" / "workflows" / self.RUN_ID
        wf_dir.mkdir(parents=True)
        defs_dir = tmp_path / session / "workflows"
        defs_dir.mkdir(parents=True)

        main.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": session,
                    "cwd": "/Users/demo/Repos/proj",
                    "gitBranch": "feature/58-demo",
                    "timestamp": "2026-06-15T09:00:00.000Z",
                    "uuid": "m1",
                    "message": {"role": "assistant", "model": "claude-opus-4-8", "content": []},
                }
            ),
            encoding="utf-8",
        )

        def agent_transcript(agent_id: str, start: str, end: str) -> None:
            (wf_dir / f"agent-{agent_id}.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {
                            "type": "assistant",
                            "agentId": agent_id,
                            "sessionId": session,
                            "isSidechain": True,
                            "timestamp": ts,
                            "uuid": f"{agent_id}-{ts}",
                            "message": {
                                "role": "assistant",
                                "model": "claude-opus-4-8",
                                "usage": {"input_tokens": 100, "output_tokens": 40},
                                "content": [],
                            },
                        }
                    )
                    for ts in (start, end)
                ),
                encoding="utf-8",
            )

        agents = {
            "aaa1111111111111": ("code-review", "Review"),
            "bbb2222222222222": ("code-review", "Review"),
            "ccc3333333333333": ("general-purpose", "Verify"),
        }
        for i, (agent_id, (agent_type, _phase)) in enumerate(agents.items()):
            agent_transcript(
                agent_id, f"2026-06-15T09:0{i + 1}:00.000Z", f"2026-06-15T09:0{i + 1}:30.000Z"
            )
            (wf_dir / f"agent-{agent_id}.meta.json").write_text(
                json.dumps({"agentType": agent_type}), encoding="utf-8"
            )

        (defs_dir / f"{self.RUN_ID}.json").write_text(
            json.dumps(
                {
                    "runId": self.RUN_ID,
                    "workflowName": "design-panel",
                    "phases": [{"title": "Review"}, {"title": "Verify"}],
                    "workflowProgress": [
                        {"type": "workflow_phase", "index": 1, "title": "Review"},
                        {"type": "workflow_phase", "index": 2, "title": "Verify"},
                        *[
                            {
                                "type": "workflow_agent",
                                "agentId": agent_id,
                                "phaseTitle": phase,
                                "label": f"{phase.lower()}:{agent_id[:3]}",
                            }
                            for agent_id, (_t, phase) in agents.items()
                        ],
                    ],
                }
            ),
            encoding="utf-8",
        )
        return parse_session_file(main)

    def _workflow(self, parsed: ParsedSession) -> Span:
        workflows = [s for s in parsed.spans if s.kind == "workflow"]
        assert len(workflows) == 1, "expected exactly one workflow container span"
        return workflows[0]

    def test_emits_one_workflow_container_span(self, grouped: ParsedSession) -> None:
        workflow = self._workflow(grouped)
        assert workflow.name == "design-panel"
        assert workflow.parent_id is None

    def test_workflow_container_owns_no_cost(self, grouped: ParsedSession) -> None:
        # No ``agent_link`` → the correlation pass attributes it nothing; cost rolls up
        # from its agent children (conservation: Σ owned == Σ turns).
        workflow = self._workflow(grouped)
        assert workflow.agent_link is None
        assert grouped.agent_links.get(workflow.span_id) is None

    def test_each_phase_nests_under_the_workflow(self, grouped: ParsedSession) -> None:
        workflow = self._workflow(grouped)
        phases = [s for s in grouped.spans if s.kind == "workflow_phase"]
        assert {p.name for p in phases} == {"Review", "Verify"}
        assert all(p.parent_id == workflow.span_id for p in phases)
        assert all(p.agent_link is None for p in phases)

    def test_each_agent_nests_under_its_phase(self, grouped: ParsedSession) -> None:
        phase_by_name = {p.name: p.span_id for p in grouped.spans if p.kind == "workflow_phase"}
        agent_phase = {
            "aaa1111111111111": "Review",
            "bbb2222222222222": "Review",
            "ccc3333333333333": "Verify",
        }
        for agent_id, phase in agent_phase.items():
            span = next(
                s
                for s in grouped.spans
                if s.kind == "agent" and grouped.agent_links.get(s.span_id) == agent_id
            )
            assert span.parent_id == phase_by_name[phase]


def _assistant_usage(
    msg_id: str | None,
    uuid: str,
    ts: str,
    *,
    output_tokens: int = 308,
    input_tokens: int = 6556,
) -> dict:
    """One assistant transcript record carrying ``message.usage``.

    ``msg_id`` is the assistant ``message.id`` — the identity of the inference. When
    several records share it (streaming partials / text+tool_use split / re-emit),
    they describe ONE inference. ``msg_id=None`` omits the id so the parser must fall
    back to the record ``uuid``.
    """
    message: dict = {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "..."}],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 15750,
            "cache_creation_input_tokens": 9507,
        },
    }
    if msg_id is not None:
        message["id"] = msg_id
    return {
        "type": "assistant",
        "sessionId": "dup-sess",
        "cwd": "/Users/demo/Repos/proj",
        "gitBranch": "chore/telemetry-smoke",
        "timestamp": ts,
        "uuid": uuid,
        "message": message,
    }


class TestUsageDedupByMessageId:
    """Issue #78: one inference's ``message.usage``, written across several assistant
    records, must be counted exactly once — keyed by ``message.id`` (uuid fallback)."""

    def test_one_message_across_three_records_collapses_to_one_event(self, tmp_path: Path) -> None:
        # The reported triple: 21:32:41/42/42, same message.id + identical usage = ONE
        # inference re-emitted across three records, not three inferences.
        records = [
            _assistant_usage("msg_A", "rec1", "2026-06-16T21:32:41.149Z"),
            _assistant_usage("msg_A", "rec2", "2026-06-16T21:32:42.089Z"),
            _assistant_usage("msg_A", "rec3", "2026-06-16T21:32:42.800Z"),
        ]
        path = tmp_path / "dup-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        main = [e for e in parse_session_file(path).usage_events if e.source == "main"]

        assert len(main) == 1
        assert main[0].output_tokens == 308
        assert main[0].input_tokens == 6556

    def test_dedup_preserves_distinct_inferences(self, tmp_path: Path) -> None:
        # Two real inferences (distinct message.id) survive; only the duplicate collapses.
        records = [
            _assistant_usage("msg_A", "rec1", "2026-06-16T21:32:41.149Z", output_tokens=308),
            _assistant_usage("msg_A", "rec2", "2026-06-16T21:32:42.089Z", output_tokens=308),
            _assistant_usage("msg_B", "rec3", "2026-06-16T21:33:10.000Z", output_tokens=512),
        ]
        path = tmp_path / "dup-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        main = [e for e in parse_session_file(path).usage_events if e.source == "main"]

        assert len(main) == 2
        assert sorted(e.output_tokens for e in main) == [308, 512]

    def test_subagent_message_across_records_collapses_to_one_event(self, tmp_path: Path) -> None:
        # The dedup applies on the subagent walk too: a sub-agent inference re-emitted
        # across records (same message.id) collapses to one source="subagent" event,
        # tagged with the agent's id.
        records = [
            _assistant_usage("msg_sub", "srec1", "2026-06-16T21:40:01.000Z", output_tokens=120),
            _assistant_usage("msg_sub", "srec2", "2026-06-16T21:40:01.500Z", output_tokens=120),
        ]
        events, _spans, _links = _walk_transcript(
            records,
            "agent_xyz",
            main_path=tmp_path / "sess.jsonl",
            parent_meta={"session_id": "parent-sess", "repo": "proj", "branch": "b"},
            agent_span_id="span_agent",
            seen=set(),
            tool_parents={},
        )

        assert len(events) == 1
        assert events[0].source == "subagent"
        assert events[0].agent_id == "agent_xyz"
        assert events[0].output_tokens == 120

    def test_records_without_message_id_fall_back_to_uuid(self, tmp_path: Path) -> None:
        # No message.id → each distinct record uuid is its own inference (no collapse).
        records = [
            _assistant_usage(None, "rec1", "2026-06-16T21:32:41.149Z", output_tokens=308),
            _assistant_usage(None, "rec2", "2026-06-16T21:33:10.000Z", output_tokens=512),
        ]
        path = tmp_path / "noid-sess.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

        main = [e for e in parse_session_file(path).usage_events if e.source == "main"]

        assert len(main) == 2


class TestProjectDirForWorktree:
    """Issue #98: map a spoke's worktree to the single CC project dir holding its sessions.

    Claude Code names ``~/.claude/projects/<encoded>`` by replacing every non-alphanumeric
    character of the worktree's *resolved* absolute path with ``-`` (verified against every
    local project dir). Scoping the #92 backfill to this dir is what stops it ingesting other
    sessions' reasoning/content.
    """

    def test_encodes_every_non_alphanumeric_char_to_dash(self) -> None:
        root = Path("/tmp/projects")
        out = project_dir_for_worktree(Path("/Users/x/Repos/ai-toolkit-cycle-demo"), root)
        assert out == root / "-Users-x-Repos-ai-toolkit-cycle-demo"

    def test_resolves_symlinks_before_encoding(self, tmp_path: Path) -> None:
        # CC encodes the realpath (e.g. /tmp -> /private/tmp), so a symlinked worktree must
        # map to the SAME project dir as its target — else the scoped scan misses everything.
        real = tmp_path / "real-wt"
        real.mkdir()
        link = tmp_path / "link-wt"
        link.symlink_to(real)
        root = tmp_path / "projects"
        assert project_dir_for_worktree(link, root) == project_dir_for_worktree(real, root)


class TestParseProjectDir:
    """Issue #98: parse only ONE project dir's sessions (one level), resumes included."""

    def test_reads_sessions_directly_under_the_project_dir(self, tmp_path: Path) -> None:
        proj = tmp_path / "-Users-x-Repos-wt"
        proj.mkdir()
        (proj / "s1.jsonl").write_text(
            json.dumps(_assistant_usage("m1", "u1", "2026-06-16T21:32:41Z", output_tokens=111)),
            encoding="utf-8",
        )

        merged = parse_project_dir(proj)

        main = [e for e in merged.usage_events if e.source == "main"]
        assert [e.output_tokens for e in main] == [111]

    def test_merges_multiple_resume_sessions(self, tmp_path: Path) -> None:
        proj = tmp_path / "-Users-x-Repos-wt"
        proj.mkdir()
        (proj / "s1.jsonl").write_text(
            json.dumps(_assistant_usage("m1", "u1", "2026-06-16T21:32:41Z", output_tokens=111)),
            encoding="utf-8",
        )
        (proj / "s2.jsonl").write_text(
            json.dumps(_assistant_usage("m2", "u2", "2026-06-16T21:33:41Z", output_tokens=222)),
            encoding="utf-8",
        )

        merged = parse_project_dir(proj)

        tokens = {e.output_tokens for e in merged.usage_events if e.source == "main"}
        assert {111, 222} <= tokens

    def test_does_not_treat_subagent_file_as_a_top_level_session(self, tmp_path: Path) -> None:
        proj = tmp_path / "-Users-x-Repos-wt"
        sub = proj / "s1" / "subagents"
        sub.mkdir(parents=True)
        (proj / "s1.jsonl").write_text(
            json.dumps(_assistant_usage("m1", "u1", "2026-06-16T21:32:41Z", output_tokens=111)),
            encoding="utf-8",
        )
        (sub / "agent-x.jsonl").write_text(
            json.dumps(_assistant_usage("m9", "u9", "2026-06-16T21:34:41Z", output_tokens=999)),
            encoding="utf-8",
        )

        merged = parse_project_dir(proj)

        # The subagent file is never re-parsed as a top-level (main) session.
        tokens = {e.output_tokens for e in merged.usage_events if e.source == "main"}
        assert 999 not in tokens


class TestSignatureOnlyThinkingYieldsNothing:
    """Issue #98 fix (b): signature-only / empty extended thinking -> no reasoning body."""

    def test_signature_only_thinking_is_absent_from_the_map(self, tmp_path: Path) -> None:
        rec = _assistant_thinking("a1", "")
        rec["message"]["content"][0]["signature"] = "SIG=="
        path = tmp_path / "sig.jsonl"
        path.write_text(json.dumps(rec), encoding="utf-8")

        assert thinking_by_turn(path) == {}

    def test_whitespace_only_thinking_is_absent_from_the_map(self, tmp_path: Path) -> None:
        path = tmp_path / "ws.jsonl"
        path.write_text(json.dumps(_assistant_thinking("a1", "   \n")), encoding="utf-8")

        assert thinking_by_turn(path) == {}


def _usage_record(usage: dict) -> dict:
    """A minimal assistant record carrying one ``message.usage`` block."""
    return {"message": {"model": "claude-opus-4-8", "usage": usage}}


class TestCacheCreationTTLSplit:
    """Issue #97: ``_usage_event`` splits cache-creation tokens by ephemeral TTL.

    Anthropic prices a 1-hour cache write at 2x input and a 5-minute write at 1.25x;
    the transcript carries the per-TTL breakdown in a nested ``cache_creation`` object.
    """

    def test_nested_object_splits_into_5m_and_1h(self) -> None:
        event = _usage_event(
            _usage_record(
                {
                    "cache_creation_input_tokens": 12000,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 5000,
                        "ephemeral_1h_input_tokens": 7000,
                    },
                }
            ),
            "main",
            None,
        )

        assert event.cache_creation_5m == 5000
        assert event.cache_creation_1h == 7000

    def test_split_sums_to_the_flat_total(self) -> None:
        event = _usage_event(
            _usage_record(
                {
                    "cache_creation_input_tokens": 12000,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 5000,
                        "ephemeral_1h_input_tokens": 7000,
                    },
                }
            ),
            "main",
            None,
        )

        assert event.cache_creation_5m + event.cache_creation_1h == event.cache_creation

    def test_absent_nested_object_attributes_all_to_5m(self) -> None:
        # Older transcripts / push-only records carry only the flat aggregate. The
        # conservative fallback prices it all at the 5m rate (today's behavior).
        event = _usage_event(
            _usage_record({"cache_creation_input_tokens": 9000}),
            "main",
            None,
        )

        assert event.cache_creation_5m == 9000
        assert event.cache_creation_1h == 0

    def test_no_cache_creation_yields_zero_split(self) -> None:
        event = _usage_event(_usage_record({"input_tokens": 5}), "main", None)

        assert event.cache_creation_5m == 0
        assert event.cache_creation_1h == 0
