"""Context as per-turn input-state: ONE named node per turn (Issue #65, S4 — RED).

The old tree rendered loaded context as N bare ``rule ~2 tokens`` leaves under the
spawn bucket. S4 collapses it to a single ``context`` child on each main turn that
carries the turn's **input state**: the named rules / CLAUDE.md / memory / tool-schemas
that composed its prompt, with per-item token estimates, plus the real cached-prefix
total (``cache_read + cache_creation``) and the history remainder.

The parser already emits one ``rule``-kind span per loaded item (``phase`` =
``rule``/``CLAUDE.md``/``memory``/``tool-schema``, ``name`` = the item, ``summary`` =
its ``~N tokens`` estimate); the builder groups them per session and folds them into
the turn's context node instead of scattering them as leaves.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal import validate_causal_tree
from telemetry.causal_tree import build_causal_forest

SID = "sess-c"


def _turn(uuid, parent, *, source, agent_id, ts, cache_read=0, cache_creation=0) -> dict:
    return {
        "uuid": uuid,
        "parent_uuid": parent,
        "session_id": SID,
        "ts": ts,
        "source": source,
        "agent_id": agent_id,
        "is_sidechain": source == "subagent",
        "model": "claude-opus-4-8",
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.1,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }


def _rule(span_id, name, phase, est, ts="2026-06-12T23:00:01Z") -> dict:
    return {
        "span_id": span_id,
        "parent_id": None,
        "kind": "rule",
        "name": name,
        "phase": phase,
        "summary": f"~{est:,} tokens",
        "ts_start": ts,
        "ts_end": ts,
        "duration_ms": 0,
        "status": "success",
        "session_id": SID,
    }


def _scenario() -> tuple[list[dict], list[dict], dict[str, str]]:
    turns = [
        _turn(
            "m1",
            "u1",
            source="main",
            agent_id=None,
            ts="2026-06-12T23:00:10Z",
            cache_read=12000,
            cache_creation=0,
        ),
        _turn("s1", None, source="subagent", agent_id="AG1", ts="2026-06-12T23:00:22Z"),
    ]
    spans = [
        _rule("c_cq", "code-quality", "rule", 1800),
        _rule("c_py", "python-style", "rule", 1200),
        _rule("c_md", "CLAUDE.md", "CLAUDE.md", 1100),
        _rule("c_mem", "dashboard-v3-roadmap", "memory", 80),
        _rule("c_t1", "Bash", "tool-schema", 300),
        _rule("c_t2", "Edit", "tool-schema", 300),
        {
            "span_id": "st_red",
            "parent_id": None,
            "kind": "step",
            "name": "solo-cycle",
            "phase": "red",
            "ts_start": "2026-06-12T23:00:05Z",
            "ts_end": "2026-06-12T23:00:30Z",
            "duration_ms": 25000,
            "status": "success",
        },
        {
            "span_id": "sp_ag1",
            "parent_id": None,
            "kind": "agent",
            "name": "Explore",
            "phase": None,
            "ts_start": "2026-06-12T23:00:21Z",
            "ts_end": "2026-06-12T23:00:25Z",
            "duration_ms": 4000,
            "status": "success",
            "agent_link": "AG1",
        },
    ]
    return turns, spans, {"sp_ag1": "m1"}


def _build() -> list[dict]:
    turns, spans, tool_parents = _scenario()
    return build_causal_forest(turns, spans, tool_parents)


def _by_id(forest: list[dict]) -> dict[str, dict]:
    index: dict[str, dict] = {}

    def walk(nodes):
        for node in nodes:
            index[node["node_id"]] = node
            walk(node["children"])

    walk(forest)
    return index


class TestContextNode:
    def test_forest_conforms(self) -> None:
        validate_causal_tree(_build())

    def test_main_turn_has_exactly_one_context_child(self) -> None:
        m1 = _by_id(_build())["m1"]
        contexts = [c for c in m1["children"] if c["kind"] == "context"]
        assert len(contexts) == 1

    def test_no_bare_rule_leaves_anywhere(self) -> None:
        # The defect this kills: loaded context must NOT appear as scattered `rule` leaves.
        assert "rule" not in {n["kind"] for n in _by_id(_build()).values()}

    def test_context_names_its_items_with_token_ints(self) -> None:
        m1 = _by_id(_build())["m1"]
        ctx = next(c for c in m1["children"] if c["kind"] == "context")["input_context"]
        rule_names = {r["name"] for r in ctx["rules"]}
        assert {"code-quality", "python-style"} <= rule_names
        assert all(isinstance(r["tokens"], int) and r["tokens"] > 0 for r in ctx["rules"])
        assert ctx["claude_md"] == {"name": "CLAUDE.md", "tokens": 1100}
        assert ctx["memory"] == [{"name": "dashboard-v3-roadmap", "tokens": 80}]
        assert ctx["schemas"] == {"count": 2, "tokens": 600}

    def test_context_total_is_the_real_cached_prefix(self) -> None:
        m1 = _by_id(_build())["m1"]
        ctx = next(c for c in m1["children"] if c["kind"] == "context")["input_context"]
        # Real cached prefix = cache_read + cache_creation = 12000; history is the
        # remainder after the named items (1800+1200+1100+80+600 = 4780).
        assert ctx["total_tokens"] == 12000
        assert ctx["history_tokens"] == 12000 - 4780

    def test_subturn_has_no_context_node(self) -> None:
        # Sub-agent loaded context is not surfaced by the parser, so a sub-turn carries
        # no context node (rather than mislabelling the main session's items).
        s1 = _by_id(_build())["s1"]
        assert not [c for c in s1["children"] if c["kind"] == "context"]

    def test_context_node_owns_no_cost(self) -> None:
        ctx = next(c for c in _by_id(_build())["m1"]["children"] if c["kind"] == "context")
        assert ctx["own_cost_usd"] == 0.0
        assert ctx["own_tokens_in"] == 0 and ctx["own_tokens_out"] == 0


class TestContextEdgeCases:
    def test_main_turn_with_no_context_items_still_gets_a_node(self) -> None:
        # A session with no loaded-context spans: the turn still carries a context node
        # whose real cached prefix is pure history (no named items).
        turns = [
            _turn("m", "u", source="main", agent_id=None, ts="2026-06-12T23:00:10Z", cache_read=500)
        ]
        spans = [
            {
                "span_id": "st",
                "parent_id": None,
                "kind": "step",
                "name": "solo-cycle",
                "phase": "red",
                "ts_start": "2026-06-12T23:00:05Z",
                "ts_end": "2026-06-12T23:00:30Z",
                "duration_ms": 25000,
                "status": "success",
            },
        ]
        ctx = next(
            c
            for c in _by_id(build_causal_forest(turns, spans, {}))["m"]["children"]
            if c["kind"] == "context"
        )["input_context"]
        assert ctx["rules"] == [] and ctx["claude_md"] is None
        assert ctx["total_tokens"] == 500 and ctx["history_tokens"] == 500

    def test_per_session_grouping_across_a_resume(self) -> None:
        # Two main turns in different sessions each name only their own session's items.
        turns = [
            {
                **_turn(
                    "m1",
                    "u1",
                    source="main",
                    agent_id=None,
                    ts="2026-06-12T23:00:10Z",
                    cache_read=9000,
                ),
                "session_id": "sess-a",
            },
            {
                **_turn(
                    "m2",
                    "m1",
                    source="main",
                    agent_id=None,
                    ts="2026-06-13T00:00:10Z",
                    cache_read=9000,
                ),
                "session_id": "sess-b",
            },
        ]
        spans = [
            {**_rule("ra", "rule-a", "rule", 100), "session_id": "sess-a"},
            {**_rule("rb", "rule-b", "rule", 200), "session_id": "sess-b"},
            {
                "span_id": "st",
                "parent_id": None,
                "kind": "step",
                "name": "solo-cycle",
                "phase": "red",
                "ts_start": "2026-06-12T23:00:05Z",
                "ts_end": "2026-06-13T00:01:00Z",
                "duration_ms": 1,
                "status": "success",
            },
        ]
        nodes = _by_id(build_causal_forest(turns, spans, {}))
        names1 = {
            r["name"]
            for r in next(c for c in nodes["m1"]["children"] if c["kind"] == "context")[
                "input_context"
            ]["rules"]
        }
        names2 = {
            r["name"]
            for r in next(c for c in nodes["m2"]["children"] if c["kind"] == "context")[
                "input_context"
            ]["rules"]
        }
        assert names1 == {"rule-a"}
        assert names2 == {"rule-b"}
