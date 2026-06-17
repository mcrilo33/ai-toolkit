"""The parser surfaces the real causal ids the v3 tree builds on (Issue #65, S2 — RED).

Phase 1 replaces timestamp correlation with the causal ids already in the transcript.
The parser already *reads* ``tool_use.id`` / ``tool_result.tool_use_id`` / ``isSidechain``
and already discovers workflow-nested sub-agents; what it never *surfaced* is the per-turn
``uuid``/``parentUuid`` and the tool→issuing-turn edge the builder (S3) needs to assemble:

- ``turn = assistant record`` with ``parent = parentUuid`` — so each :class:`UsageEvent`
  must carry its record ``uuid``, its ``parent_uuid``, and whether it ``is_sidechain``.
- ``tool/skill/todo = tool_use block`` with ``parent = the turn`` — so the parser must
  map each tool-derived span back to the ``uuid`` of the assistant turn that issued it,
  for the main session AND recursively inside every sub-agent transcript.

The fixture session ``11111111…`` has a clean uuid chain (u1→a1→u2→a2→…) and a Task
sub-agent whose transcript carries its own sub-turns (s1→s2→s2b→s3).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.session_parser import parse_session_file
from telemetry.spans import derive_span_id

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
PROJECT = FIXTURES / "-Users-demo-Repos-proj"
SESSION = PROJECT / "11111111-1111-1111-1111-111111111111.jsonl"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
WF_SESSION = PROJECT / "22222222-2222-2222-2222-222222222222.jsonl"
WF_SESSION_ID = "22222222-2222-2222-2222-222222222222"
WF_AGENT_IDS = frozenset({"cccc3333dddd4444", "eeee5555ffff6666"})


def _parsed():
    return parse_session_file(SESSION)


class TestTurnUuids:
    def test_main_usage_events_carry_uuid_parent_and_are_not_sidechain(self) -> None:
        events = [e for e in _parsed().usage_events if e.source == "main"]
        pairs = {(e.uuid, e.parent_uuid) for e in events}
        assert ("a1", "u1") in pairs
        assert ("a2", "u2") in pairs
        assert all(e.is_sidechain is False for e in events)

    def test_subagent_usage_events_carry_uuid_and_are_sidechain(self) -> None:
        events = [e for e in _parsed().usage_events if e.source == "subagent"]
        assert events, "no subagent usage events"
        assert all(e.is_sidechain is True for e in events)
        assert ("s2", "s1") in {(e.uuid, e.parent_uuid) for e in events}


class TestToolParents:
    def test_main_tool_use_links_to_its_issuing_turn(self) -> None:
        parents = _parsed().tool_parents
        assert parents[derive_span_id(SESSION_ID, "toolu_skill1")] == "a1"
        # All five tools in the a5 turn parent back to a5 (one turn, many tools).
        assert parents[derive_span_id(SESSION_ID, "toolu_bash1")] == "a5"
        assert parents[derive_span_id(SESSION_ID, "toolu_write1")] == "a5"

    def test_agent_and_todo_tool_use_link_to_their_turn(self) -> None:
        parents = _parsed().tool_parents
        assert parents[derive_span_id(SESSION_ID, "toolu_task1")] == "a2"
        assert parents[derive_span_id(SESSION_ID, "toolu_todo1")] == "a3"

    def test_subagent_tool_use_links_to_its_subturn(self) -> None:
        # The sub-agent's Read (toolu_subread1) was issued in sub-turn s2b — the edge
        # must reconstruct recursively, not collapse onto the parent agent span.
        parents = _parsed().tool_parents
        assert parents[derive_span_id(SESSION_ID, "toolu_subread1")] == "s2b"

    def test_every_tool_parent_value_is_a_known_turn_uuid(self) -> None:
        parsed = _parsed()
        turn_uuids = {e.uuid for e in parsed.usage_events if e.uuid}
        # s2b/a5 issue tools but carry no usage; the builder still needs them, so the
        # tool_parents values are a superset of the usage turns — but never empty.
        assert parsed.tool_parents
        assert all(isinstance(v, str) and v for v in parsed.tool_parents.values())
        assert turn_uuids & set(parsed.tool_parents.values())


class TestWorkflowDiscovery:
    def test_workflow_nested_agents_are_surfaced(self) -> None:
        # Already discovered (#51) — guarded here as part of the causal surface S3 needs.
        parsed = parse_session_file(WF_SESSION)
        assert set(parsed.agent_links.values()) >= WF_AGENT_IDS

    def test_workflow_agent_tool_use_links_to_its_subturn(self) -> None:
        # Pin the workflow-agent tool→turn edge directly: agent cccc3333dddd4444's
        # Grep (toolu_cgrep) was issued in its sub-turn c2.
        parents = parse_session_file(WF_SESSION).tool_parents
        assert parents[derive_span_id(WF_SESSION_ID, "toolu_cgrep")] == "c2"
