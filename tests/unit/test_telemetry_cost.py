"""Token/cost correlation tests (Issue #22, subtask 2 — RED).

Every span gets ``tokens_in`` / ``tokens_out`` / ``cost_usd`` attributed:

- ``agent`` spans take their tokens from the walked subagent transcript.
- ``skill`` / ``todo`` / ``human`` spans take theirs by bracketing the main
  session's per-turn ``message.usage`` between the span's ``ts_start`` /
  ``ts_end`` (joined on ``session_id``).
- Cost reuses ccusage's per-session ``totalCost`` as the pool, distributed by
  each span's share of the session's tokens — an effective rate derived wholly
  from ccusage, never a re-derived price table.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.cost import attribute, load_ccusage_costs, per_turn_rows
from telemetry.session_parser import ParsedSession, UsageEvent, parse_session_file
from telemetry.spans import Span

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
SESSION = FIXTURES / "-Users-demo-Repos-proj" / "11111111-1111-1111-1111-111111111111.jsonl"
SESSION_ID = "11111111-1111-1111-1111-111111111111"

# Session token total across every parsed UsageEvent (input+output+cache):
#   main a1=350 a2=120 a3=90 a4=90 ; subagent s2=1800 s3=350 → 2800.
SESSION_TOTAL_TOKENS = 2800
# Pick a ccusage cost that makes the effective rate a round 0.001 $/token.
CCUSAGE_COST = 2.80
RATE = CCUSAGE_COST / SESSION_TOTAL_TOKENS


@pytest.fixture()
def attributed():
    parsed = parse_session_file(SESSION)
    attribute(parsed, {SESSION_ID: CCUSAGE_COST})
    return parsed


def _one(parsed, kind, **match):
    for span in parsed.spans:
        if span.kind != kind:
            continue
        if all(getattr(span, k) == v for k, v in match.items()):
            return span
    raise AssertionError(f"no {kind} span matching {match}")


class TestTokenAttribution:
    def test_agent_span_tokens_come_from_subagent_transcript(self, attributed) -> None:
        agent = _one(attributed, "agent")
        assert agent.tokens_in == 700
        assert agent.tokens_out == 450

    def test_skill_span_tokens_bracketed_from_main_usage(self, attributed) -> None:
        skill = _one(attributed, "skill")
        # The assistant turn that issued the Skill call: 100 in / 50 out.
        assert skill.tokens_in == 100
        assert skill.tokens_out == 50

    def test_todo_span_tokens_bracketed(self, attributed) -> None:
        todo = _one(attributed, "todo")
        assert todo.tokens_in == 60
        assert todo.tokens_out == 30

    def test_human_question_span_tokens_bracketed(self, attributed) -> None:
        question = _one(attributed, "human", name="AskUserQuestion")
        assert question.tokens_in == 70
        assert question.tokens_out == 20

    def test_human_prompt_span_has_zero_tokens(self, attributed) -> None:
        prompt = _one(attributed, "human", name="prompt")
        assert prompt.tokens_in == 0
        assert prompt.tokens_out == 0


class TestCostAttribution:
    def test_agent_cost_is_token_share_of_ccusage_session_cost(self, attributed) -> None:
        agent = _one(attributed, "agent")
        # agent weight = subagent total = 1800 + 350 = 2150 tokens.
        assert agent.cost_usd == pytest.approx(RATE * 2150)

    def test_skill_cost_is_token_share(self, attributed) -> None:
        skill = _one(attributed, "skill")
        assert skill.cost_usd == pytest.approx(RATE * 350)

    def test_attributed_cost_never_exceeds_ccusage_session_cost(self, attributed) -> None:
        total = sum(s.cost_usd or 0.0 for s in attributed.spans)
        # a2 (the agent's parent turn, 120 tokens) is unattributed by design.
        assert total == pytest.approx(CCUSAGE_COST - RATE * 120)
        assert total <= CCUSAGE_COST + 1e-9

    def test_cost_is_none_when_session_absent_from_ccusage(self) -> None:
        parsed = parse_session_file(SESSION)
        attribute(parsed, {})  # no ccusage data for this session
        assert all(s.cost_usd is None for s in parsed.spans)
        # tokens are still attributed even without cost data.
        assert _one(parsed, "agent").tokens_in == 700


class TestBoundaryBracketing:
    def test_boundary_event_attributed_to_exactly_one_adjacent_span(self) -> None:
        # Span A ends where span B starts; a usage event lands on the shared
        # boundary. Half-open [start, end) must give it to exactly one span so
        # the cost invariant holds.
        session = "S"
        span_a = Span(
            span_id="A",
            kind="skill",
            name="a",
            session_id=session,
            ts_start="2026-01-01T00:00:01.000Z",
            ts_end="2026-01-01T00:00:03.000Z",
        )
        span_b = Span(
            span_id="B",
            kind="skill",
            name="b",
            session_id=session,
            ts_start="2026-01-01T00:00:03.000Z",
            ts_end="2026-01-01T00:00:06.000Z",
        )
        event = UsageEvent(
            session_id=session,
            ts="2026-01-01T00:00:03.000Z",
            model=None,
            input_tokens=100,
            output_tokens=0,
            cache_read=0,
            cache_creation=0,
            source="main",
        )
        parsed = ParsedSession(spans=[span_a, span_b], usage_events=[event], agent_links={})

        attribute(parsed, {session: 1.0})

        assert span_a.tokens_in is not None and span_b.tokens_in is not None
        assert span_a.tokens_in + span_b.tokens_in == 100
        assert (span_a.tokens_in == 0) != (span_b.tokens_in == 0)
        total = sum(s.cost_usd or 0.0 for s in parsed.spans)
        assert total <= 1.0 + 1e-9


class TestRecursiveAgentCost:
    """Issue #51 S2: each agent in an agent→agent→agent chain is attributed only the
    tokens of its own transcript — never its children's — so a depth-N chain never
    double-counts.
    """

    WF_SESSION = FIXTURES / "-Users-demo-Repos-proj" / "22222222-2222-2222-2222-222222222222.jsonl"

    def test_each_agent_gets_only_its_own_transcript_tokens(self) -> None:
        parsed = parse_session_file(self.WF_SESSION)
        attribute(parsed, {})  # tokens are attributed even without ccusage cost data
        by_link = {
            parsed.agent_links[s.span_id]: s
            for s in parsed.spans
            if s.kind == "agent" and s.span_id in parsed.agent_links
        }
        # Each transcript's own usage turns: depth1 200+60/80+25, depth2 150+40/60+15,
        # depth3 100/40. A child's tokens never roll up into its parent's span.
        assert (by_link["a1a1a1a1a1a1a1a1"].tokens_in, by_link["a1a1a1a1a1a1a1a1"].tokens_out) == (
            260,
            105,
        )
        assert (by_link["b2b2b2b2b2b2b2b2"].tokens_in, by_link["b2b2b2b2b2b2b2b2"].tokens_out) == (
            190,
            75,
        )
        assert (by_link["c3c3c3c3c3c3c3c3"].tokens_in, by_link["c3c3c3c3c3c3c3c3"].tokens_out) == (
            100,
            40,
        )


def _diamond_session(root: Path) -> Path:
    """A session where two top-level agents (A, B) both spawn the same child C.

    The same child ``agentId`` is reachable through two parents — the parser must
    walk C's transcript once and let exactly one span own its tokens, or C is
    double-counted. Returns the main session path.
    """
    import json

    session_id = "diamond-sess"
    main = root / f"{session_id}.jsonl"
    subagents = root / session_id / "subagents"
    subagents.mkdir(parents=True)

    def assistant(uuid: str, ts: str, content: list[dict], usage: dict | None = None) -> dict:
        message = {"role": "assistant", "model": "claude-opus-4-8", "content": content}
        if usage is not None:
            message["usage"] = usage
        return {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/Users/demo/Repos/proj",
            "gitBranch": "feature/51-demo",
            "timestamp": ts,
            "uuid": uuid,
            "message": message,
        }

    def task(uuid: str, ts: str, tool_id: str, sub_type: str) -> dict:
        return assistant(
            uuid,
            ts,
            [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Task",
                    "input": {"subagent_type": sub_type},
                }
            ],
        )

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

    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    child_usage = {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    def write(path: Path, records: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    write(
        main,
        [
            task("m1", "2026-06-15T10:00:00.000Z", "t_a", "Explore"),
            result("m2", "2026-06-15T10:00:30.000Z", "t_a", "agentA"),
            task("m3", "2026-06-15T10:01:00.000Z", "t_b", "Explore"),
            result("m4", "2026-06-15T10:01:30.000Z", "t_b", "agentB"),
        ],
    )
    write(
        subagents / "agent-agentA.jsonl",
        [
            assistant("a1", "2026-06-15T10:00:05.000Z", [{"type": "text", "text": "x"}], usage),
            task("a2", "2026-06-15T10:00:10.000Z", "t_c1", "tdd-red"),
            result("a3", "2026-06-15T10:00:25.000Z", "t_c1", "agentC"),
        ],
    )
    write(
        subagents / "agent-agentB.jsonl",
        [
            assistant("b1", "2026-06-15T10:01:05.000Z", [{"type": "text", "text": "x"}], usage),
            task("b2", "2026-06-15T10:01:10.000Z", "t_c2", "tdd-red"),
            result("b3", "2026-06-15T10:01:25.000Z", "t_c2", "agentC"),
        ],
    )
    write(
        subagents / "agent-agentC.jsonl",
        [assistant("c1", "2026-06-15T10:00:15.000Z", [{"type": "text", "text": "x"}], child_usage)],
    )
    return main


class TestDiamondAttribution:
    """Issue #51 S2 regression: a repeated child ``agentId`` reached via two parents
    is walked once and owned by one span, so its tokens are never double-counted.
    """

    def test_shared_child_usage_emitted_once(self, tmp_path: Path) -> None:
        parsed = parse_session_file(_diamond_session(tmp_path))
        child_events = [e for e in parsed.usage_events if e.agent_id == "agentC"]
        assert len(child_events) == 1  # not two — global seen-set walks C once

    def test_shared_child_has_exactly_one_cost_owner(self, tmp_path: Path) -> None:
        parsed = parse_session_file(_diamond_session(tmp_path))
        owners = [sid for sid, aid in parsed.agent_links.items() if aid == "agentC"]
        assert len(owners) == 1

    def test_child_tokens_attributed_once_across_all_spans(self, tmp_path: Path) -> None:
        parsed = parse_session_file(_diamond_session(tmp_path))
        attribute(parsed, {})
        owner_id = next(sid for sid, aid in parsed.agent_links.items() if aid == "agentC")
        c_spans = [s for s in parsed.spans if s.agent_link == "agentC"]
        # Both parents' spans display the link, but only the owner draws C's tokens.
        assert len(c_spans) == 2
        assert sum(s.tokens_in or 0 for s in c_spans) == 100
        assert next(s for s in c_spans if s.span_id == owner_id).tokens_in == 100


class TestPerTurnRows:
    def test_rows_sum_to_ccusage_session_total(self) -> None:
        parsed = parse_session_file(SESSION)
        rows = per_turn_rows(parsed.usage_events, {SESSION_ID: CCUSAGE_COST})
        total = sum(float(r["cost_usd"]) for r in rows if r["session_id"] == SESSION_ID)  # type: ignore[arg-type]
        assert total == pytest.approx(CCUSAGE_COST)

    def test_cost_is_none_when_session_absent_from_ccusage(self) -> None:
        parsed = parse_session_file(SESSION)
        rows = per_turn_rows(parsed.usage_events, {})
        assert rows  # turns still emitted
        assert all(r["cost_usd"] is None for r in rows)
        assert all(int(r["tokens_total"]) > 0 for r in rows)  # type: ignore[arg-type]


class TestCcusageLoader:
    def test_maps_claude_session_period_to_total_cost(self) -> None:
        raw = (
            '{"session":['
            '{"agent":"claude","period":"sess-a","totalCost":1.5},'
            '{"agent":"codex","period":"sess-b","totalCost":9.9},'
            '{"agent":"claude","period":"sess-c","totalCost":0.25}'
            "]}"
        )
        costs = load_ccusage_costs(runner=lambda: raw)
        assert costs == {"sess-a": 1.5, "sess-c": 0.25}
