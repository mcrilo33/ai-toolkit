"""Per-spoke causal forest assembly + dividers (Issue #65, S5).

``causal_forest_from_parsed`` is the per-spoke entry point the dashboard wires in: it
turns one parsed session (pull spans + per-turn rows + ``tool_parents``) plus its push
spans into the full causal forest, with idle/resume dividers appended. These tests drive
it on the real demo transcript fixtures (the same data a live spoke would parse) so the
end-to-end build — not just the unit builder — is covered, plus the divider logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal import validate_causal_tree
from telemetry.causal_tree import causal_dividers, causal_forest_from_parsed
from telemetry.session_parser import parse_session_file

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "projects"
PROJECT = FIXTURES / "-Users-demo-Repos-proj"
TASK_SESSION = PROJECT / "11111111-1111-1111-1111-111111111111.jsonl"
WF_SESSION = PROJECT / "22222222-2222-2222-2222-222222222222.jsonl"


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _t(uuid: str, session: str, ts: str, *, source: str = "main", cache_creation: int = 0) -> dict:
    return {
        "uuid": uuid,
        "session_id": session,
        "ts": ts,
        "source": source,
        "cache_creation": cache_creation,
    }


class TestDividers:
    def test_resume_emits_a_session_divider_with_cold_note(self) -> None:
        turns = [
            _t("a", "s1", "2026-06-12T23:00:00Z"),
            _t("b", "s2", "2026-06-13T00:00:00Z", cache_creation=5000),
        ]
        divs = causal_dividers(turns)
        assert [d["kind"] for d in divs] == ["session"]
        assert "5,000" in divs[0]["summary"]

    def test_long_idle_emits_a_gap_divider(self) -> None:
        turns = [_t("a", "s1", "2026-06-12T23:00:00Z"), _t("b", "s1", "2026-06-12T23:10:00Z")]
        assert [d["kind"] for d in causal_dividers(turns)] == ["gap"]

    def test_adjacent_turns_no_divider(self) -> None:
        turns = [_t("a", "s1", "2026-06-12T23:00:00Z"), _t("b", "s1", "2026-06-12T23:00:30Z")]
        assert causal_dividers(turns) == []

    def test_subagent_turns_do_not_drive_dividers(self) -> None:
        # Only the main timeline gets dividers; a sub-agent turn between two main turns
        # must not be read as a resume/idle on the main spine.
        turns = [
            _t("m1", "s1", "2026-06-12T23:00:00Z"),
            _t("s", "s2", "2026-06-12T23:00:01Z", source="subagent"),
            _t("m2", "s1", "2026-06-12T23:00:30Z"),
        ]
        assert causal_dividers(turns) == []


class TestForestFromParsedSession:
    def test_task_session_builds_a_conformant_resolved_forest(self) -> None:
        parsed = parse_session_file(TASK_SESSION)
        forest = causal_forest_from_parsed(parsed, [], {})
        validate_causal_tree(forest)
        kinds = {n["kind"] for n in _walk(forest)}
        assert "unresolved" not in kinds  # AC: every node resolves by id
        assert "turn" in kinds and "agent" in kinds  # the Task sub-agent reconstructed

    def test_task_subagent_recurses_under_its_issuing_turn(self) -> None:
        parsed = parse_session_file(TASK_SESSION)
        forest = causal_forest_from_parsed(parsed, [], {})
        agents = [n for n in _walk(forest) if n["kind"] == "agent"]
        # The Task agent carries the sub-agent's own sub-turns (recursion), not a flat leaf.
        assert any(any(c["kind"] == "turn" for c in a["children"]) for a in agents)

    def test_workflow_session_has_context_and_workflow_agents(self) -> None:
        parsed = parse_session_file(WF_SESSION)
        forest = causal_forest_from_parsed(parsed, [], {})
        validate_causal_tree(forest)
        nodes = list(_walk(forest))
        assert any(n["kind"] == "context" for n in nodes)  # loaded context folded per turn
        agent_names = {n["name"] for n in nodes if n["kind"] == "agent"}
        assert agent_names  # workflow fan-out agents reconstructed
