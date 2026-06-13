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

from telemetry.cost import attribute, load_ccusage_costs
from telemetry.session_parser import parse_session_file

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
