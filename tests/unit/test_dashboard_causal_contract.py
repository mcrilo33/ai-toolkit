"""The causal-node contract + golden causal-tree fixture (Issue #65, S1 — RED).

Phase 1 of the dashboard causal trace replaces timestamp correlation with the real
causal ids already in the data. Before any builder code exists, S1 freezes two
artifacts the later subtasks develop against:

1. **The node-shape contract** — :mod:`telemetry.causal` defines the ``CausalNode``
   TypedDict every node in the causal tree carries, the union of allowed ``kind``
   values, and :func:`validate_causal_tree` (a structural conformance check).
2. **The golden causal tree** — ``fixtures/causal_tree_golden.json`` is ``feature/47``'s
   *expected* causal tree (the output Phase 3 builds against). It must conform to the
   contract and exhibit the causal-model invariants the acceptance criteria name:
   idle→prompt→turn→tool→hook→sub-agent (recursive), context as ONE named real-token
   node per turn, no ``(unresolved)`` node, and cost only on turn/agent leaves.

These tests are the coverage contract for the shape; the builder (S3) and the parser
(S2) must keep producing a tree that passes them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.causal import (
    CAUSAL_KINDS,
    REQUIRED_NODE_KEYS,
    validate_causal_tree,
)
from telemetry.spans import SPAN_KINDS, SYNTHETIC_KINDS

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = _FIXTURES / "causal_tree_golden.json"
SPOKE_RUN_ID = "feature/47+1700000000"


def _golden() -> list[dict]:
    return json.loads(GOLDEN.read_text())


def _walk(nodes: list[dict]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))


class TestContractModule:
    def test_causal_kinds_union_spans_and_synthetics(self) -> None:
        # Every real-span and synthetic kind is a legal causal-node kind, so a node
        # built from either source validates against one contract.
        assert set(SPAN_KINDS) <= set(CAUSAL_KINDS)
        assert set(SYNTHETIC_KINDS) <= set(CAUSAL_KINDS)

    def test_required_keys_cover_the_view_columns(self) -> None:
        # The contract must carry every column the v3 view renders (Node/Time/Dur/
        # Cost/Tokens/H/Actor) plus the causal parentage + recursion keys.
        for key in (
            "node_id",
            "parent_id",
            "kind",
            "name",
            "actor",
            "ts_start",
            "duration_ms",
            "own_cost_usd",
            "own_tokens_in",
            "own_tokens_out",
            "human_count",
            "synthetic",
            "children",
        ):
            assert key in REQUIRED_NODE_KEYS


class TestGoldenConformsToContract:
    def test_golden_is_a_non_empty_forest(self) -> None:
        assert _golden(), "golden causal tree is empty"

    def test_every_node_conforms(self) -> None:
        # validate_causal_tree raises with a path-qualified message on the first
        # non-conforming node; a clean pass is the assertion.
        validate_causal_tree(_golden())

    def test_synthetic_flag_matches_kind(self) -> None:
        for node in _walk(_golden()):
            expected = node["kind"] in SYNTHETIC_KINDS
            assert node["synthetic"] is expected, (
                f"{node['node_id']}: synthetic={node['synthetic']} but kind={node['kind']}"
            )


class TestCausalInvariants:
    def test_no_unresolved_node(self) -> None:
        # Acceptance: the causal build resolves every node — nothing falls to the
        # off-spine ``(unresolved)`` catch-all the timestamp builder produced.
        kinds = {n["kind"] for n in _walk(_golden())}
        assert "unresolved" not in kinds

    def test_context_is_one_named_real_token_node_per_turn(self) -> None:
        # Each turn carries exactly ONE ``context`` child holding the input-state
        # (named rules/memory + real tokens) — never N bare "rule" leaves.
        turns = [n for n in _walk(_golden()) if n["kind"] == "turn"]
        assert turns, "golden has no turn nodes"
        for turn in turns:
            contexts = [c for c in turn["children"] if c["kind"] == "context"]
            assert len(contexts) == 1, f"{turn['node_id']}: expected 1 context child"
            ctx = contexts[0]["input_context"]
            assert ctx["rules"], "context must name its rules"
            assert all(r["name"] and r["tokens"] >= 0 for r in ctx["rules"])
            assert ctx["total_tokens"] > 0, "context must carry real tokens"
            # No bare per-rule leaf nodes hanging off the turn (the defect this kills).
            assert not [c for c in turn["children"] if c["kind"] == "rule"]

    def test_recursive_subagent_containment(self) -> None:
        # idle→…→sub-agent recursion: an agent nested under an agent nested under an
        # agent (the feature/47 Explore→Explore→general-purpose chain).
        by_id = {n["node_id"]: n for n in _walk(_golden())}

        def agent_depth(node: dict) -> int:
            depth, cur = 1, node
            while cur["parent_id"] in by_id and by_id[cur["parent_id"]]["kind"] == "agent":
                cur = by_id[cur["parent_id"]]
                depth += 1
            return depth

        agents = [n for n in _walk(_golden()) if n["kind"] == "agent"]
        assert max((agent_depth(a) for a in agents), default=0) >= 3

    def test_tool_scoped_hook_nested_under_its_tool(self) -> None:
        # A tool-scoped hook hangs UNDER the tool that triggered it (parent =
        # tool_use_id), not as a sibling of the turn.
        tools = [n for n in _walk(_golden()) if n["kind"] == "tool"]
        assert any(any(child["kind"] == "hook" for child in tool["children"]) for tool in tools), (
            "no tool has a tool-scoped hook child"
        )

    def test_cost_lives_only_on_turn_and_agent_leaves(self) -> None:
        # Conservation: a container owns nothing; only turn/agent leaves carry cost.
        for node in _walk(_golden()):
            if node["own_cost_usd"] > 0:
                assert node["kind"] in ("turn", "agent"), (
                    f"{node['node_id']} ({node['kind']}) owns cost but is not a turn/agent leaf"
                )

    def test_phase_spine_buckets_the_turns(self) -> None:
        # Hybrid model: the lifecycle/step markers still form the L1 spine, and the
        # causal turns live INSIDE those interval buckets.
        top_intervals = [n for n in _golden() if n["kind"] == "interval"]
        assert top_intervals, "no phase-interval spine at the top level"
        assert any(any(c["kind"] == "turn" for c in iv["children"]) for iv in top_intervals), (
            "no interval brackets a turn"
        )

    def test_idle_renders_as_a_divider(self) -> None:
        # Idle time is a divider row (gap/session), not a phase.
        kinds = {n["kind"] for n in _golden()}
        assert kinds & {"gap", "session"}, "no idle/resume divider at the top level"
