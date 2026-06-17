"""The causal tree builder: ids not timestamps (Issue #65, S3 — RED).

Builds one spoke's causal forest from cost-attributed **turn rows** (carrying the
``uuid``/``parent_uuid``/``is_sidechain`` the parser now surfaces, plus the attributed
``cost_usd``), the unified spoke **spans** (pull tool/skill/agent + push hook/script/
step), and the parser's ``tool_parents`` edge map — using the real causal ids rather
than timestamp windows:

- a main turn = a turn row, bucketed by time into the push-marker phase spine (kept);
- a tool/skill/todo = a pull span, parented under the turn that issued it
  (``tool_parents[span_id] -> turn uuid``);
- a tool-scoped hook = a push span whose ``parent_id`` is the tool's id, nested under
  that tool;
- a sub-agent = an ``agent`` span; its sub-turns are the sub-agent turn rows
  (``agent_id == agent_link``), each holding its own sub-tools — recursively, so
  agent→sub-turn→agent→sub-turn→tool reconstructs at any depth;
- a script = a push span, at the spoke root when it has no parent.

The acceptance bars: idle→prompt→turn→tool→hook→sub-agent (recursive), cost only on
turn/agent leaves, and NO ``(unresolved)`` — every node resolves by id.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal import validate_causal_tree
from telemetry.causal_tree import build_causal_forest
from telemetry.spans import derive_span_id

SID = "sess-x"


def _turn(
    uuid: str, parent: str | None, *, source: str, agent_id: str | None, ts: str, cost: float
) -> dict:
    return {
        "uuid": uuid,
        "parent_uuid": parent,
        "session_id": SID,
        "ts": ts,
        "source": source,
        "agent_id": agent_id,
        "is_sidechain": source == "subagent",
        "model": "claude-opus-4-8",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cost_usd": cost,
    }


def _span(
    span_id: str, kind: str, name: str, *, ts: str, parent_id: str | None = None, **kw
) -> dict:
    span = {
        "span_id": span_id,
        "parent_id": parent_id,
        "kind": kind,
        "name": name,
        "phase": None,
        "ts_start": ts,
        "ts_end": ts,
        "duration_ms": 0,
        "status": "success",
    }
    span.update(kw)
    return span


def _scenario() -> tuple[list[dict], list[dict], dict[str, str]]:
    """A spoke with two main turns, a tool carrying a tool-scoped hook, and a recursive
    sub-agent chain (Explore → sub-turn → general-purpose → sub-turn → Grep)."""
    turns = [
        _turn("m1", "u1", source="main", agent_id=None, ts="2026-06-12T23:00:10Z", cost=0.10),
        _turn("m2", "m1", source="main", agent_id=None, ts="2026-06-12T23:00:20Z", cost=0.20),
        _turn("s1", None, source="subagent", agent_id="AG1", ts="2026-06-12T23:00:22Z", cost=0.30),
        _turn("s2", "s1", source="subagent", agent_id="AG2", ts="2026-06-12T23:00:24Z", cost=0.15),
    ]
    spans = [
        # pull spans. The agent spans carry the subagent transcript's *pooled* cost
        # (cost.py attributes it); the builder must drop it once the per-sub-turn rows
        # (which carry the same spend) nest under the agent, else Σ owned double-counts.
        _span("sp_read", "tool", "Read", ts="2026-06-12T23:00:11Z", summary="queries.py"),
        _span(
            "sp_ag1", "agent", "Explore", ts="2026-06-12T23:00:21Z", agent_link="AG1", cost_usd=0.45
        ),
        _span("sp_subwrite", "tool", "Write", ts="2026-06-12T23:00:23Z", parent_id="sp_ag1"),
        _span(
            "sp_ag2",
            "agent",
            "general-purpose",
            ts="2026-06-12T23:00:23Z",
            parent_id="sp_ag1",
            agent_link="AG2",
            cost_usd=0.15,
        ),
        _span("sp_grep", "tool", "Grep", ts="2026-06-12T23:00:25Z", parent_id="sp_ag2"),
        # push spans
        _span("st_red", "step", "solo-cycle", ts="2026-06-12T23:00:05Z", phase="red"),
        _span("hk1", "hook", "commit-gauntlet", ts="2026-06-12T23:00:12Z", parent_id="sp_read"),
        _span("scr1", "script", "commit-gauntlet", ts="2026-06-12T23:00:29Z", emits="st_red"),
    ]
    # The step marker's window closes the red interval at 23:00:30.
    spans[5]["ts_end"] = "2026-06-12T23:00:30Z"
    tool_parents = {
        "sp_read": "m1",
        "sp_ag1": "m2",
        "sp_subwrite": "s1",
        "sp_ag2": "s1",
        "sp_grep": "s2",
    }
    return turns, spans, tool_parents


def _by_id(forest: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            index[node["node_id"]] = node
            walk(node["children"])

    walk(forest)
    return index


def _child_kinds(node: dict) -> set[str]:
    return {c["kind"] for c in node["children"]}


def _build() -> list[dict]:
    turns, spans, tool_parents = _scenario()
    return build_causal_forest(turns, spans, tool_parents)


class TestCausalForest:
    def test_forest_conforms_to_contract(self) -> None:
        validate_causal_tree(_build())

    def test_no_unresolved(self) -> None:
        assert "unresolved" not in {n["kind"] for n in _by_id(_build()).values()}

    def test_main_turns_bucket_into_the_phase_spine(self) -> None:
        forest = _build()
        intervals = [n for n in forest if n["kind"] == "interval"]
        assert intervals, "no phase-interval spine"
        red = next(iv for iv in intervals if iv["phase"] == "red")
        turn_ids = {c["node_id"] for c in red["children"] if c["kind"] == "turn"}
        assert {"m1", "m2"} <= turn_ids

    def test_tool_nests_under_its_issuing_turn(self) -> None:
        nodes = _by_id(_build())
        assert "sp_read" in {c["node_id"] for c in nodes["m1"]["children"]}
        assert "sp_ag1" in {c["node_id"] for c in nodes["m2"]["children"]}

    def test_tool_scoped_hook_nests_under_its_tool(self) -> None:
        nodes = _by_id(_build())
        read = nodes["sp_read"]
        assert "hook" in _child_kinds(read)
        assert "hk1" in {c["node_id"] for c in read["children"]}

    def test_recursive_subagent_chain(self) -> None:
        nodes = _by_id(_build())
        # Explore (sp_ag1) → sub-turn s1 → general-purpose (sp_ag2) → sub-turn s2 → Grep.
        assert "s1" in {c["node_id"] for c in nodes["sp_ag1"]["children"]}
        assert "sp_ag2" in {c["node_id"] for c in nodes["s1"]["children"]}
        assert "s2" in {c["node_id"] for c in nodes["sp_ag2"]["children"]}
        assert "sp_grep" in {c["node_id"] for c in nodes["s2"]["children"]}

    def test_script_at_spoke_root(self) -> None:
        forest = _build()
        assert "scr1" in {n["node_id"] for n in forest if n["kind"] == "script"}

    def test_cost_only_on_turn_and_agent_leaves(self) -> None:
        for node in _by_id(_build()).values():
            if node["own_cost_usd"] > 0:
                assert node["kind"] in ("turn", "agent")

    def test_subturn_and_main_turn_own_their_cost(self) -> None:
        nodes = _by_id(_build())
        assert nodes["s1"]["own_cost_usd"] > 0
        assert nodes["m1"]["own_cost_usd"] > 0

    def test_agent_with_subturns_owns_no_cost(self) -> None:
        # The agent's pooled cost moves onto its sub-turns; the container owns nothing.
        nodes = _by_id(_build())
        assert nodes["sp_ag1"]["own_cost_usd"] == 0.0
        assert nodes["sp_ag2"]["own_cost_usd"] == 0.0

    def test_cost_is_conserved(self) -> None:
        # Σ owned across the whole tree equals Σ of the turn-row costs — no double count
        # despite the agent spans carrying the same pooled spend.
        owned = sum(n["own_cost_usd"] for n in _by_id(_build()).values())
        assert owned == pytest.approx(0.10 + 0.20 + 0.30 + 0.15)

    def test_agent_without_subturns_keeps_its_pooled_cost(self) -> None:
        # An agent whose sub-agent transcript was not parsed (no sub-turn rows) stays a
        # leaf and keeps its attributed cost, so the spend is not lost.
        turns = [_turn("m", "u", source="main", agent_id=None, ts="2026-06-12T23:00:10Z", cost=0.1)]
        spans = [
            _span(
                "ag", "agent", "Explore", ts="2026-06-12T23:00:11Z", agent_link="AGX", cost_usd=0.4
            ),
            _span("mk", "step", "solo-cycle", ts="2026-06-12T23:00:05Z", phase="red"),
        ]
        nodes = _by_id(build_causal_forest(turns, spans, {"ag": "m"}))
        assert nodes["ag"]["own_cost_usd"] == pytest.approx(0.4)


class TestActor:
    def test_subturn_inherits_its_agent_name(self) -> None:
        nodes = _by_id(_build())
        assert nodes["s1"]["actor"] == "Explore"
        assert nodes["s2"]["actor"] == "general-purpose"

    def test_subagent_tool_inherits_the_agent_actor(self) -> None:
        nodes = _by_id(_build())
        assert nodes["sp_subwrite"]["actor"] == "Explore"
        assert nodes["sp_grep"]["actor"] == "general-purpose"

    def test_main_tool_actor_is_main(self) -> None:
        nodes = _by_id(_build())
        assert nodes["sp_read"]["actor"] == "main"


# The human prompt record's uuid (a turn's ``parent_uuid`` points at this); the parser
# derives the human span's id from it, so the builder reconnects turn → prompt by id.
HREC = "hrec-uuid"


def _trigger_scenario() -> tuple[list[dict], list[dict], dict[str, str]]:
    """A human prompt triggers a turn, a continuation turn follows it (its parent is a
    tool_result record, not the prompt), and a session-level hook + a ready script carry
    no ``parent_id`` — the root-dump cases issue #76 must bucket into the phase interval."""
    human_id = derive_span_id(SID, HREC)
    turns = [
        # t1.parent_uuid == the human record uuid ⇒ genuine trigger ⇒ nests under the prompt.
        _turn("t1", HREC, source="main", agent_id=None, ts="2026-06-12T20:57:52Z", cost=0.10),
        # t2.parent_uuid is a tool_result record (not in any map) ⇒ continuation ⇒ sibling.
        _turn("t2", "tr1", source="main", agent_id=None, ts="2026-06-12T20:57:54Z", cost=0.20),
    ]
    spans = [
        _span(
            human_id,
            "human",
            "prompt",
            ts="2026-06-12T20:57:50Z",
            human={"type": "prompt", "wait_ms": None},
        ),
        _span("hk_sess", "hook", "todo-ledger-nudge", ts="2026-06-12T20:57:11Z"),
        _span("scr_sess", "script", "spoke-ready", ts="2026-06-12T20:57:58Z"),
        _span("st_spawn", "step", "solo-cycle", ts="2026-06-12T20:57:05Z", phase="spawn"),
    ]
    spans[-1]["ts_end"] = "2026-06-12T20:58:00Z"  # the interval closes after every span above
    return turns, spans, {}


def _build_trigger() -> list[dict]:
    turns, spans, tool_parents = _trigger_scenario()
    return build_causal_forest(turns, spans, tool_parents)


class TestGenuineTriggerNesting:
    def test_forest_conforms_to_contract(self) -> None:
        validate_causal_tree(_build_trigger())

    def test_human_prompt_nests_its_triggered_turn(self) -> None:
        nodes = _by_id(_build_trigger())
        human = nodes[derive_span_id(SID, HREC)]
        assert "t1" in {c["node_id"] for c in human["children"]}

    def test_continuation_turn_is_a_sibling_under_the_prompt(self) -> None:
        nodes = _by_id(_build_trigger())
        human = nodes[derive_span_id(SID, HREC)]
        child_ids = [c["node_id"] for c in human["children"]]
        assert "t2" in child_ids
        # Time-ordered siblings, not one nested under the other.
        assert child_ids.index("t1") < child_ids.index("t2")

    def test_turn_loop_stays_flat(self) -> None:
        nodes = _by_id(_build_trigger())
        assert "turn" not in _child_kinds(nodes["t1"])

    def test_parentless_human_buckets_into_interval_not_root(self) -> None:
        forest = _build_trigger()
        human_id = derive_span_id(SID, HREC)
        assert human_id not in {n["node_id"] for n in forest}, "human dumped to root"
        interval = next(n for n in forest if n["kind"] == "interval")
        assert human_id in {c["node_id"] for c in interval["children"]}

    def test_parentless_hook_buckets_into_interval_not_root(self) -> None:
        forest = _build_trigger()
        assert "hk_sess" not in {n["node_id"] for n in forest}, "hook dumped to root"
        interval = next(n for n in forest if n["kind"] == "interval")
        assert "hk_sess" in {c["node_id"] for c in interval["children"]}

    def test_parentless_script_buckets_into_interval_not_root(self) -> None:
        forest = _build_trigger()
        assert "scr_sess" not in {n["node_id"] for n in forest}, "script dumped to root"
        interval = next(n for n in forest if n["kind"] == "interval")
        assert "scr_sess" in {c["node_id"] for c in interval["children"]}

    def test_no_node_dumps_to_root(self) -> None:
        # The whole point of #76: top-level holds only the phase spine, nothing orphaned.
        forest = _build_trigger()
        assert {n["kind"] for n in forest} == {"interval"}

    def test_cost_conserved(self) -> None:
        owned = sum(n["own_cost_usd"] for n in _by_id(_build_trigger()).values())
        assert owned == pytest.approx(0.10 + 0.20)
