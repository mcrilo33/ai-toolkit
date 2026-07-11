"""The enrichment registry in ``main`` drives the passes in order over an EnrichmentContext (#166)."""

from __future__ import annotations

import sys
from pathlib import Path

from spoke_tree_helpers import SPOKE, _traces

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.langfuse_spoke_tree import (
    _ENRICHMENTS,
    EnrichmentContext,
    build_batch,
    build_cycle_batch,
    build_score_events,
    build_step_cost_scores,
)

_BASE_TS = "2026-01-02T00:00:00Z"


def _ctx(tmp_path: Path) -> EnrichmentContext:
    traces = _traces()
    return EnrichmentContext(
        spoke_run_id=SPOKE,
        traces=traces,
        batch=build_batch(traces, SPOKE),
        cycle_batch=build_cycle_batch(traces, SPOKE),
        tool_content={},
        bodies_dir=tmp_path / "absent",  # no request bodies -> the count-gated passes no-op
        counter=len,  # stub counter (no network) for the disk-fallback measurement
        price=0.0,
        base_ts=_BASE_TS,
        root=tmp_path,
    )


class TestEnrichmentRegistry:
    def test_registry_order_is_the_documented_sequence(self) -> None:
        assert [name for name, _ in _ENRICHMENTS] == [
            "loaded-context",
            "llm-decomposition",
            "request-body-metadata",
            "context-deltas",
            "scores",
            "step-scores",
            "carry-cost",
            "invocation-scores",
            "enforcement-scores",
        ]

    def test_running_the_loop_populates_scores_matching_direct_calls(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        for _name, enrich in _ENRICHMENTS:
            enrich(ctx)

        assert ctx.score_events == build_score_events(
            SPOKE, ctx.traces, ctx.batch, base_ts=_BASE_TS
        )
        assert ctx.step_scores == build_step_cost_scores(
            SPOKE, ctx.cycle_batch, base_ts=_BASE_TS, price=0.0
        )

    def test_loop_collapses_loaded_context_into_one_node(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)

        for _name, enrich in _ENRICHMENTS:
            enrich(ctx)

        assert len(ctx.context_events) == 1
        assert ctx.source == "disk"  # no request body dir -> disk fallback
