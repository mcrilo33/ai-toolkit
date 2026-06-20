"""Unit tests for the transcript→Langfuse backfill translator (Issue #92, S3 — RED).

The backfill reuses the parser + causal builder to assemble one spoke's causal forest
from the local transcript, then translates that forest into Langfuse ingestion events —
the SECOND sink for the same forest the dashboard renders. These tests run with NO
network: :func:`forest_to_events` is pure, fed a forest built by ``build_causal_forest``.

They assert one ``trace-create`` + one synthetic root + one event per causal node,
turn/agent leaves becoming ``generation-create`` with usage details, tool/context/
reasoning nodes becoming spans, deterministic ids keyed on ``(spoke_run_id, node_id)``,
parent re-wiring across the tree, container token rollups, and the thinking body joined
onto reasoning nodes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry import langfuse_backfill
from telemetry.causal_tree import build_causal_forest
from telemetry.langfuse_backfill import (
    _gather_thinking,
    backfill_events,
    backfill_node_id,
    backfill_root_id,
    backfill_trace_id,
    forest_to_events,
    is_native_trace,
    main,
    reasoning_only_events,
    session_is_covered,
)
from telemetry.session_parser import project_dir_for_worktree

SPOKE = "feature/92-demo+1700000000"


def _turn(**kw: Any) -> dict:
    row = {
        "uuid": "m1",
        "parent_uuid": "u1",
        "session_id": "s",
        "ts": "2026-06-12T23:00:10Z",
        "source": "main",
        "agent_id": None,
        "is_sidechain": False,
        "model": "claude-opus-4-8",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cache_read": 500,
        "cache_creation": 300,
        "cost_usd": 0.10,
        "reasoning": "weighing the reuse",
    }
    row.update(kw)
    return row


def _forest() -> list[Any]:
    """One main turn with a tool child and a thinking body (so a reasoning node forms)."""
    spans = [
        {
            "span_id": "sp_read",
            "parent_id": None,
            "kind": "tool",
            "name": "Read",
            "phase": None,
            "ts_start": "2026-06-12T23:00:11Z",
            "ts_end": "2026-06-12T23:00:11Z",
            "duration_ms": 0,
            "status": "success",
            "summary": "queries.py",
        }
    ]
    return build_causal_forest([_turn()], spans, {"sp_read": "m1"}, thinking={"m1": "BODY_THINK"})


def _events() -> list[dict]:
    return forest_to_events(_forest(), SPOKE, thinking={"m1": "BODY_THINK"})


def _by_type(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e["type"] == event_type]


def _body_by_id(events: list[dict]) -> dict[str, dict]:
    return {e["body"]["id"]: e["body"] for e in events if e["type"] != "trace-create"}


class TestTraceAndRoot:
    def test_exactly_one_trace_create(self) -> None:
        traces = _by_type(_events(), "trace-create")
        assert len(traces) == 1

    def test_trace_carries_the_session_id(self) -> None:
        trace = _by_type(_events(), "trace-create")[0]
        assert trace["body"]["sessionId"] == SPOKE
        assert trace["body"]["id"] == backfill_trace_id(SPOKE)

    def test_one_synthetic_root_span(self) -> None:
        bodies = _body_by_id(_events())
        assert backfill_root_id(SPOKE) in bodies

    def test_one_event_per_node_plus_trace_and_root(self) -> None:
        # 1 interval + 1 turn + 1 context + 1 reasoning + 1 tool = 5 nodes, + trace + root.
        assert len(_events()) == 5 + 2


class TestNodeTranslation:
    def test_turn_becomes_a_generation_with_usage(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "m1")]
        gen_ids = {e["body"]["id"] for e in _by_type(_events(), "generation-create")}
        assert backfill_node_id(SPOKE, "m1") in gen_ids
        assert body["usageDetails"] == {
            "input": 1000,
            "output": 200,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 300,
        }

    def test_turn_splits_cache_creation_by_ttl(self) -> None:
        # Issue #97: a turn with both 5m and 1h cache writes maps 5m to the existing
        # cache_creation_input_tokens key (1.25x) and 1h to input_cache_creation_1h (2x).
        forest = build_causal_forest(
            [_turn(cache_creation=300, cache_creation_5m=120, cache_creation_1h=180)],
            [],
            {},
            thinking={},
        )
        gen = next(
            e["body"]
            for e in forest_to_events(forest, SPOKE, thinking={})
            if e["type"] == "generation-create" and e["body"]["id"] == backfill_node_id(SPOKE, "m1")
        )
        assert gen["usageDetails"]["cache_creation_input_tokens"] == 120
        assert gen["usageDetails"]["input_cache_creation_1h"] == 180

    def test_turn_without_1h_writes_omits_the_1h_usage_type(self) -> None:
        # No 1h cache writes -> usageDetails stays in the pre-#97 four-component shape
        # (the whole flat total prices at the 5m rate).
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "m1")]
        assert body["usageDetails"]["cache_creation_input_tokens"] == 300
        assert "input_cache_creation_1h" not in body["usageDetails"]

    def test_turn_carries_no_cost_details(self) -> None:
        # Issue #91: cost is retired from the forest; the generation carries only
        # usageDetails and Langfuse computes costDetails from its model-pricing config.
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "m1")]
        assert "costDetails" not in body

    def test_tool_becomes_a_span_under_its_turn(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "sp_read")]
        span_ids = {e["body"]["id"] for e in _by_type(_events(), "span-create")}
        assert backfill_node_id(SPOKE, "sp_read") in span_ids
        assert body["parentObservationId"] == backfill_node_id(SPOKE, "m1")

    def test_reasoning_node_carries_the_thinking_body_as_output(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "reasoning:m1")]
        assert body["output"] == "BODY_THINK"

    def test_node_metadata_carries_kind(self) -> None:
        body = _body_by_id(_events())[backfill_node_id(SPOKE, "sp_read")]
        assert body["metadata"]["kind"] == "tool"


class TestTopLevelParenting:
    def test_top_level_node_parents_under_the_root(self) -> None:
        # The single synthetic interval (run) sits directly under the spoke root.
        events = _events()
        intervals = [
            e for e in events if e["type"] == "span-create" and e["body"].get("name") == "run"
        ]
        assert intervals
        assert intervals[0]["body"]["parentObservationId"] == backfill_root_id(SPOKE)


class TestContainerRollups:
    def test_container_node_carries_subtree_token_rollup(self) -> None:
        # The run interval contains the whole subtree; its rollup sums the turn's usage.
        run = next(
            e["body"]
            for e in _events()
            if e["type"] == "span-create" and e["body"].get("name") == "run"
        )
        assert run["metadata"]["rollup"] == {
            "reused": 500,
            "written": 300,
            "input": 1000,
            "output": 200,
        }


class TestDeterminism:
    def test_ids_are_stable_across_runs(self) -> None:
        first = {e["body"]["id"] for e in _events()}
        second = {e["body"]["id"] for e in _events()}
        assert first == second

    def test_node_id_is_derived_from_spoke_and_node(self) -> None:
        assert backfill_node_id(SPOKE, "m1") == backfill_node_id(SPOKE, "m1")
        assert backfill_node_id(SPOKE, "m1") != backfill_node_id("other", "m1")
        assert backfill_node_id(SPOKE, "m1") != backfill_node_id(SPOKE, "m2")


def _stub_get(traces: list[dict]) -> Any:
    def get(_path: str) -> dict:
        return {"data": traces, "meta": {"totalPages": 1}}

    return get


def _thinking_record(uuid: str, thinking: str) -> dict:
    """One assistant transcript record carrying an extended-thinking block."""
    return {
        "type": "assistant",
        "sessionId": "sess",
        "timestamp": "2026-06-15T12:00:01.000Z",
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "thinking", "thinking": thinking}],
        },
    }


def _write_session(project_dir: Path, name: str, records: list[dict]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / name).write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


class TestGatherThinkingScoping:
    """Issue #98 fix (a): with a project_dir, thinking is read from ONLY that dir."""

    def test_project_dir_reads_only_its_own_sessions(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        wt = projects / "-wt"
        hub = projects / "-hub"
        _write_session(wt, "s1.jsonl", [_thinking_record("w1", "WT_THINK")])
        _write_session(hub, "s1.jsonl", [_thinking_record("h1", "HUB_THINK")])

        thinking = _gather_thinking(None, projects, project_dir=wt)

        assert set(thinking.values()) == {"WT_THINK"}

    def test_without_project_dir_scans_the_whole_root(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _write_session(projects / "-wt", "s1.jsonl", [_thinking_record("w1", "WT_THINK")])
        _write_session(projects / "-hub", "s1.jsonl", [_thinking_record("h1", "HUB_THINK")])

        thinking = _gather_thinking(None, projects, project_dir=None)

        assert set(thinking.values()) == {"WT_THINK", "HUB_THINK"}


class TestReasoningOnlyEmptyThinking:
    """Issue #98 fix (b): no thinking bodies -> zero reasoning observations (no placeholders)."""

    def test_empty_thinking_emits_no_events(self) -> None:
        assert reasoning_only_events(_forest(), SPOKE, {}) == []


class TestMainScopesToTheWorktree:
    """Issue #98 fix (a), end-to-end: --worktree confines the backfill to the spoke's own
    project dir, so a sibling/hub session's reasoning never bleeds in (the confirmed bug).
    """

    def test_only_the_worktree_thinking_is_ingested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects = tmp_path / "projects"
        worktree = tmp_path / "Repos" / "ai-toolkit-cycle-demo"
        worktree.mkdir(parents=True)
        wt_proj = project_dir_for_worktree(worktree, projects)
        _write_session(wt_proj, "s1.jsonl", [_thinking_record("w1", "WT_THINK")])
        _write_session(projects / "-hub-driver", "s1.jsonl", [_thinking_record("h1", "HUB_THINK")])

        posted: list[dict] = []
        monkeypatch.setenv("LANGFUSE_BASIC_AUTH", "Basic x")
        monkeypatch.setattr(langfuse_backfill, "make_get", lambda *a, **k: _stub_get([]))
        monkeypatch.setattr(langfuse_backfill, "make_post", lambda *a, **k: None)
        # Covered → reasoning-only path, sourced straight from the (scoped) thinking map.
        monkeypatch.setattr(langfuse_backfill, "session_is_covered", lambda *a, **k: True)
        monkeypatch.setattr(
            langfuse_backfill, "post_in_chunks", lambda events, _post: posted.extend(events)
        )

        rc = main(
            ["spoke-id", "--worktree", str(worktree), "--projects", str(projects), "--thinking"]
        )

        assert rc == 0
        outputs = {e["body"].get("output") for e in posted if "body" in e}
        assert "WT_THINK" in outputs
        assert "HUB_THINK" not in outputs


class TestCoverageDetection:
    """Issue #92 dedup vs the live push: a session the live OTel push already covered
    has native (non-synthetic) traces; the backfill detects them and does not re-emit a
    competing full tree. The backfill's own trace and langfuse_spoke_tree's assembled
    tree are NOT native — they are synthetic views, recognised by their id/name prefixes.
    """

    def test_live_push_native_trace_is_native(self) -> None:
        assert is_native_trace({"id": "abc123", "name": "claude_code.interaction"})

    def test_backfill_own_trace_is_not_native(self) -> None:
        trace = {"id": backfill_trace_id("s"), "name": "spoke-backfill:s"}
        assert not is_native_trace(trace)

    def test_spoke_tree_assembled_trace_is_not_native(self) -> None:
        assert not is_native_trace({"id": "spoketree-deadbeef", "name": "spoke-tree:s"})

    def test_session_is_covered_when_a_native_trace_exists(self) -> None:
        get = _stub_get([{"id": "abc", "name": "claude_code.interaction"}])
        assert session_is_covered("s", get) is True

    def test_session_not_covered_when_only_synthetic_traces(self) -> None:
        get = _stub_get([{"id": backfill_trace_id("s"), "name": "spoke-backfill:s"}])
        assert session_is_covered("s", get) is False

    def test_session_not_covered_when_no_traces(self) -> None:
        assert session_is_covered("s", _stub_get([])) is False


class TestBackfillDecision:
    """The three-branch dedup guard: uncovered → full forest; covered + thinking →
    reasoning-only (the gap the live push lacks); covered + no thinking → no-op.
    """

    def test_uncovered_emits_the_full_forest(self) -> None:
        events = backfill_events(_forest(), SPOKE, {"m1": "BODY_THINK"}, covered=False)
        assert any(e["type"] == "generation-create" for e in events)
        kinds = {
            e["body"].get("metadata", {}).get("kind") for e in events if "metadata" in e["body"]
        }
        assert {"turn", "tool", "reasoning"} <= kinds

    def test_covered_with_thinking_emits_reasoning_only(self) -> None:
        events = backfill_events(_forest(), SPOKE, {"m1": "BODY_THINK"}, covered=True)
        kinds = {
            e["body"].get("metadata", {}).get("kind") for e in events if "metadata" in e["body"]
        }
        assert "turn" not in kinds and "tool" not in kinds
        assert any(e["body"].get("output") == "BODY_THINK" for e in events)
        assert any(e["type"] == "trace-create" for e in events)

    def test_covered_reasoning_nodes_parent_under_the_root(self) -> None:
        events = backfill_events(_forest(), SPOKE, {"m1": "BODY_THINK"}, covered=True)
        reasoning = next(
            e["body"] for e in events if e["body"].get("metadata", {}).get("kind") == "reasoning"
        )
        assert reasoning["parentObservationId"] == backfill_root_id(SPOKE)

    def test_covered_without_thinking_is_a_noop(self) -> None:
        assert backfill_events(_forest(), SPOKE, {}, covered=True) == []

    def test_covered_emits_one_reasoning_per_thinking_body_when_forest_lacks_reasoning(
        self,
    ) -> None:
        # Real-world: the dedup uuid split means the surviving turn uuid differs from the
        # thinking-record uuid, so the forest carries NO reasoning node. Reasoning-only must
        # still emit one observation per thinking body — sourced from the thinking map, not
        # from forest reasoning nodes (the production "86 bodies → 0 observations" bug).
        forest = build_causal_forest([_turn(uuid="survivor")], [], {}, thinking={})
        thinking = {"think_a": "BODY_A", "think_b": "BODY_B"}
        events = backfill_events(forest, SPOKE, thinking, covered=True)
        reasoning = [e for e in events if e["body"].get("metadata", {}).get("kind") == "reasoning"]
        assert len(reasoning) == 2
        assert {e["body"]["output"] for e in reasoning} == {"BODY_A", "BODY_B"}

    def test_covered_reasoning_ids_are_stable_across_reruns(self) -> None:
        forest = build_causal_forest([_turn(uuid="survivor")], [], {}, thinking={})
        thinking = {"think_a": "BODY_A", "think_b": "BODY_B"}
        first = backfill_events(forest, SPOKE, thinking, covered=True)
        second = backfill_events(forest, SPOKE, thinking, covered=True)
        ids = lambda evs: sorted(e["body"]["id"] for e in evs)  # noqa: E731
        assert ids(first) == ids(second)
        assert backfill_node_id(SPOKE, "reasoning:think_a") in ids(first)
