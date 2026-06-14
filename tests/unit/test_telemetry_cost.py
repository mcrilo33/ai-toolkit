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
