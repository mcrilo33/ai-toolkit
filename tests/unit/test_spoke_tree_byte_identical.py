"""Byte-identical rebuild pin for the #166 view-builder decoupling refactor.

Issue #166 splits ``langfuse_spoke_tree.py`` into per-family modules. The refactor must not change
a single byte of the assembled ingest batches. This test rebuilds a reference spoke through the
whole pure-function pipeline the orchestrator's ``main`` drives — View A (:func:`build_batch`),
View B (:func:`build_cycle_batch`), the numeric scores (:func:`build_score_events`), and the
per-phase step-cost scores (:func:`build_step_cost_scores`), the #230 true per-step total-cost
(:func:`build_step_total_cost_scores`) and duration (:func:`build_step_duration_scores`) scores, the
#322 per-skill cost (:func:`build_skill_cost_scores`) and #323 per-sub-agent cost
(:func:`build_agent_cost_scores`) scores,
including the #162 commit timeline nodes — and asserts the result equals a golden captured from
pre-refactor code. Any drift in the core assembly, re-parenting, rollups, view lenses, or score
emission fails here immediately.

The golden lives in ``data/spoke_tree_reference_batch.json``; regenerate it only when a *behavior*
change is intended (run this module as a script: ``python3 tests/unit/test_spoke_tree_byte_identical.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from spoke_tree_helpers import SPOKE, _traces
from telemetry.langfuse_spoke_tree import (
    build_agent_cost_scores,
    build_batch,
    build_cycle_batch,
    build_score_events,
    build_skill_cost_scores,
    build_step_cost_scores,
    build_step_duration_scores,
    build_step_total_cost_scores,
)

_GOLDEN = Path(__file__).with_name("data") / "spoke_tree_reference_batch.json"
_BASE_TS = "2026-01-02T00:00:00Z"
_PRICE = 0.00000625


def _reference_commits() -> list[dict[str, object]]:
    """A parsed commit dump (as :func:`_parse_commits` returns) to drive the #162 timeline nodes."""
    return [
        {
            "sha": "a" * 40,
            "message": "test(telemetry): RED for reference spoke",
            "authored_at": "2026-01-02T00:00:00Z",
            "files": ["tests/unit/test_x.py"],
            "additions": 12,
            "deletions": 0,
        },
        {
            "sha": "b" * 40,
            "message": "feat(telemetry): GREEN for reference spoke",
            "authored_at": "2026-01-02T00:00:02Z",
            "files": ["scripts/telemetry/x.py"],
            "additions": 34,
            "deletions": 5,
        },
    ]


def _reference_batches() -> dict[str, list]:
    """Rebuild every batch the orchestrator posts, from a fixed in-memory reference spoke."""
    traces = _traces()
    commits = _reference_commits()
    view_a = build_batch(traces, SPOKE, commits=commits)
    view_b = build_cycle_batch(traces, SPOKE, commits=commits)
    scores = build_score_events(SPOKE, traces, view_a, base_ts=_BASE_TS)
    step_scores = build_step_cost_scores(SPOKE, view_b, base_ts=_BASE_TS, price=_PRICE)
    step_total_cost_scores = build_step_total_cost_scores(SPOKE, view_b, base_ts=_BASE_TS)
    step_duration_scores = build_step_duration_scores(SPOKE, view_b, base_ts=_BASE_TS)
    skill_cost_scores = build_skill_cost_scores(SPOKE, view_a, base_ts=_BASE_TS)
    agent_cost_scores = build_agent_cost_scores(SPOKE, view_a, base_ts=_BASE_TS)
    return {
        "view_a": view_a,
        "view_b": view_b,
        "scores": scores,
        "step_scores": step_scores,
        "step_total_cost_scores": step_total_cost_scores,
        "step_duration_scores": step_duration_scores,
        "skill_cost_scores": skill_cost_scores,
        "agent_cost_scores": agent_cost_scores,
    }


def test_reference_spoke_rebuild_is_byte_identical() -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))

    rebuilt = _reference_batches()

    assert json.dumps(rebuilt, sort_keys=True) == json.dumps(golden, sort_keys=True)


if __name__ == "__main__":  # regenerate the golden from current code
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text(json.dumps(_reference_batches(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote golden -> {_GOLDEN}")
