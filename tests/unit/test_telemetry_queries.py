"""DuckDB unified-dataset query module (Issue #22, subtask 4 — RED).

queries.connect() builds an in-memory DuckDB over the unified push + pull span
dataset that Issue #23 (the dashboard) consumes:

- push spans read from the telemetry ``events.jsonl`` source,
- pull spans parsed from session logs and cost-correlated,
- both attributed (tokens/cost) and joined into spoke runs (pull spans inherit
  spoke_run_id from their session's push spans).

Spans are hierarchical (a step span encloses the skill/agent spans that ran
during it), so a wide ``step`` span's tokens include the narrower spans nested
inside it — the views aggregate within a granularity, never summing across.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.queries import build_unified_spans, connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
RUN = "feature/22-demo+1700000000"
CCUSAGE = {SESSION_ID: 2.80}


@pytest.fixture()
def con():
    connection = connect(events_path=EVENTS, projects_root=PROJECTS, ccusage_costs=CCUSAGE)
    yield connection
    connection.close()


def _flat(rows):
    return [r[0] for r in rows]


class TestUnifiedSpans:
    def test_spans_table_holds_both_push_and_pull_kinds(self, con) -> None:
        kinds = set(_flat(con.execute("SELECT DISTINCT kind FROM spans").fetchall()))
        assert {"lifecycle", "step", "hook"} <= kinds  # push
        assert {"skill", "agent", "todo", "human"} <= kinds  # pull

    def test_build_unified_spans_emits_schema_dicts(self) -> None:
        spans = build_unified_spans(
            events_path=EVENTS, projects_root=PROJECTS, ccusage_costs=CCUSAGE
        )
        assert spans
        assert all("span_id" in s and "kind" in s and "cost_usd" in s for s in spans)


class TestSpokeJoin:
    def test_pull_spans_inherit_spoke_run_id_from_session_push_spans(self, con) -> None:
        (n,) = con.execute(
            "SELECT count(*) FROM spans "
            "WHERE kind IN ('skill','agent','todo','human') AND spoke_run_id = ?",
            [RUN],
        ).fetchone()
        assert n >= 4

    def test_ad_hoc_push_span_keeps_null_spoke_run_id(self, con) -> None:
        (run_id,) = con.execute(
            "SELECT spoke_run_id FROM spans WHERE name = 'rm-scope-guard.sh'"
        ).fetchone()
        assert run_id is None


class TestAttribution:
    def test_pull_span_carries_correlated_cost(self, con) -> None:
        (cost,) = con.execute(
            "SELECT cost_usd FROM spans WHERE kind = 'skill' AND name = 'source-task'"
        ).fetchone()
        assert cost is not None and cost > 0

    def test_step_span_brackets_session_usage(self, con) -> None:
        # The wide red step [12:00:00, 12:01:50) brackets all four main turns
        # of the session: 100+80+60+70 in, 50+40+30+20 out.
        row = con.execute(
            "SELECT tokens_in, tokens_out FROM spans WHERE kind = 'step' AND phase = 'red'"
        ).fetchone()
        assert row == (310, 140)


class TestViews:
    def test_spoke_run_summary_aggregates_the_run(self, con) -> None:
        row = con.execute(
            "SELECT span_count, session_count, total_cost_usd "
            "FROM spoke_run_summary WHERE spoke_run_id = ?",
            [RUN],
        ).fetchone()
        span_count, session_count, total_cost = row
        assert span_count >= 8  # 4 push + 4+ pull
        assert session_count == 1
        # The run's one session's ccusage total — not a cross-granularity sum of
        # span costs (which would exceed it by double-counting nested spans).
        assert total_cost == pytest.approx(2.80)

    def test_step_metrics_view_has_red_step_invocation(self, con) -> None:
        (invocations,) = con.execute(
            "SELECT invocations FROM step_metrics "
            "WHERE spoke_run_id = ? AND step_key = 'step:solo-cycle:red'",
            [RUN],
        ).fetchone()
        assert invocations == 1

    def test_step_metrics_counts_human_interactions(self, con) -> None:
        (human_count,) = con.execute(
            "SELECT human_count FROM step_metrics WHERE step_key = 'human:AskUserQuestion'"
        ).fetchone()
        assert human_count == 1


class TestRobustness:
    def test_legacy_non_span_lines_in_events_are_skipped(self, con) -> None:
        # The append-only events.jsonl holds a legacy {ts,hook,decision,repo}
        # line; it must not become a span (no span_id / unknown kind).
        (n,) = con.execute("SELECT count(*) FROM spans WHERE name = 'old-hook.sh'").fetchone()
        assert n == 0

    def test_partial_push_line_keeps_schema_defaults(self, con) -> None:
        # A partial line omitting repo/duration_ms/status must not overwrite the
        # frozen-schema defaults with NULL.
        row = con.execute(
            "SELECT repo, duration_ms, status FROM spans WHERE name = 'partial-hook.sh'"
        ).fetchone()
        assert row == ("unknown", 0, "success")


class TestMissingEvents:
    def test_missing_events_file_yields_pull_only(self) -> None:
        connection = connect(
            events_path=FIXTURES / "does-not-exist.jsonl",
            projects_root=PROJECTS,
            ccusage_costs=CCUSAGE,
        )
        kinds = set(_flat(connection.execute("SELECT DISTINCT kind FROM spans").fetchall()))
        connection.close()
        assert "step" not in kinds and "lifecycle" not in kinds
        assert "skill" in kinds
