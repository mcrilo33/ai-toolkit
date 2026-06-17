"""Per-spoke lazy causal build wired into the SpanStore (Issue #65, S5 — RED).

``SpanStore.spoke_causal_forest`` is the cross-cutting requirement made real: given a
selected spoke, find ITS session ids (from the push spans already loaded) and parse
ONLY those transcripts — never an all-projects walk — then build the causal forest from
the fresh pull spans + the spoke's push markers. This is what keeps cold open per-spoke.

Driven on the demo fixtures: the push log ``events.jsonl`` carries spoke
``feature/22-demo+1700000000`` on session ``11111111…``, whose transcript lives under
the projects fixture root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from _dashboard_helpers import load_queries
from telemetry.causal import validate_causal_tree

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = _FIXTURES / "events.jsonl"
PROJECTS = _FIXTURES / "projects"
SPOKE = "feature/22-demo+1700000000"


def _store():
    return load_queries().SpanStore.from_jsonl(EVENTS)


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


class TestSpokeCausalForest:
    def test_builds_a_conformant_forest_for_the_spoke(self) -> None:
        forest = _store().spoke_causal_forest(SPOKE, PROJECTS)
        assert forest, "no causal forest built for the spoke"
        validate_causal_tree(forest)

    def test_forest_is_resolved_and_has_turns(self) -> None:
        nodes = list(_walk(_store().spoke_causal_forest(SPOKE, PROJECTS)))
        kinds = {n["kind"] for n in nodes}
        assert "turn" in kinds  # the spoke's transcript was parsed
        assert "unresolved" not in kinds

    def test_unknown_spoke_yields_empty(self) -> None:
        assert _store().spoke_causal_forest("nope/0+0", PROJECTS) == []

    def test_only_the_spokes_sessions_are_parsed(self) -> None:
        # The spoke maps to session 11111111… only; session 22222222…'s distinctive
        # workflow agents (design-panel) must NOT appear — proof we parsed per-spoke,
        # not the whole projects root.
        nodes = list(_walk(_store().spoke_causal_forest(SPOKE, PROJECTS)))
        agent_summaries = {n.get("summary") for n in nodes if n["kind"] == "agent"}
        assert "3-approach design panel" not in agent_summaries
