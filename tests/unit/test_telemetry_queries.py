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

import json
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

    def test_summary_does_not_fragment_step_metrics_grouping(self, con) -> None:
        # Issue #47: the per-instance `summary` must never enter the step_key /
        # GROUP BY. Two spans sharing (kind, name, phase) but differing only in
        # `summary` must collapse to ONE step_metrics row (invocations == 2), or
        # the Aggregate / A-B views would fragment one step into many.
        cols = (
            "span_id, spoke_run_id, kind, name, phase, ts_start, ts_end, "
            "duration_ms, status, summary"
        )
        placeholders = ", ".join("?" for _ in range(10))
        for span_id, summary in (("b1", "ran the linter"), ("b2", "read a file")):
            con.execute(
                f"INSERT INTO spans ({cols}) VALUES ({placeholders})",
                [
                    span_id,
                    "sumrun",
                    "tool",
                    "Bash",
                    None,
                    "2026-06-12T00:00:00Z",
                    "2026-06-12T00:00:01Z",
                    0,
                    "success",
                    summary,
                ],
            )

        (invocations,) = con.execute(
            "SELECT invocations FROM step_metrics "
            "WHERE spoke_run_id = ? AND step_key = 'tool:Bash'",
            ["sumrun"],
        ).fetchone()
        assert invocations == 2


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


class TestTurns:
    """The per-turn relation the v2 dashboard needs for model + once-per-turn cost.

    One row per assistant usage event (``main`` and walked ``subagent`` turns),
    carrying model and a per-turn cost computed from the same session rate the
    span attribution uses. Unlike the overlapping span costs, every turn is
    counted exactly once, so the turn costs sum to the ccusage session total.
    """

    def test_turns_table_holds_main_and_subagent_sources(self, con) -> None:
        sources = set(_flat(con.execute("SELECT DISTINCT source FROM turns").fetchall()))
        assert sources == {"main", "subagent"}

    def test_turns_count_main_and_subagent(self, con) -> None:
        # main a1..a4 (4) + walked subagent s2,s3 (2).
        (n,) = con.execute(
            "SELECT count(*) FROM turns WHERE session_id = ?", [SESSION_ID]
        ).fetchone()
        assert n == 6

    def test_turns_carry_model(self, con) -> None:
        models = set(
            _flat(
                con.execute(
                    "SELECT DISTINCT model FROM turns WHERE session_id = ?", [SESSION_ID]
                ).fetchall()
            )
        )
        assert models == {"claude-opus-4-8"}

    def test_subagent_turns_carry_agent_id(self, con) -> None:
        (n,) = con.execute(
            "SELECT count(*) FROM turns WHERE source = 'subagent' AND agent_id = ?",
            ["aaaa1111bbbb2222"],
        ).fetchone()
        assert n == 2

    def test_per_turn_cost_sums_to_ccusage_session_total(self, con) -> None:
        # Every turn counted once → the session's turn costs reconcile to ccusage
        # exactly (no double-count, no unattributed parent turn).
        (total,) = con.execute(
            "SELECT sum(cost_usd) FROM turns WHERE session_id = ?", [SESSION_ID]
        ).fetchone()
        assert total == pytest.approx(2.80)


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


# ── emission link: script → the marker it produced (Issue #54 track E) ───────


def _span(**fields: object) -> dict[str, object]:
    """A push-span dict with the given fields (others default in the parser)."""
    return fields


def _write_events(path: Path, spans: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(s) for s in spans) + "\n")


def _by_id(spans: list[dict[str, object]], span_id: str) -> dict[str, object]:
    return next(s for s in spans if s["span_id"] == span_id)


RUN_E = "feature/54-track-e+1700000000"


class TestEmissionLink:
    """``emits`` is pull-only: build_unified_spans correlates each ``script`` span
    to the ``step``/``lifecycle`` marker it produced (same spoke_run_id, the
    tightest marker bracketing the script's window — a gate runs at the tail of the
    phase it closes). It reproduces the golden's frozen ``commit-gauntlet`` →
    ``red`` link from spans that carry ``emits=null`` on the push side.
    """

    def test_script_links_to_marker_it_closes(self, tmp_path: Path) -> None:
        # The golden pattern: a commit-gauntlet script span at the tail of the red
        # step interval emits that step marker.
        events = tmp_path / "events.jsonl"
        _write_events(
            events,
            [
                _span(
                    span_id="m_red",
                    spoke_run_id=RUN_E,
                    kind="step",
                    name="solo-cycle",
                    phase="red",
                    ts_start="2026-06-12T23:00:05Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
                _span(
                    span_id="s_cg",
                    spoke_run_id=RUN_E,
                    kind="script",
                    name="commit-gauntlet",
                    ts_start="2026-06-12T23:00:54Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
            ],
        )

        spans = build_unified_spans(
            events_path=events, projects_root=tmp_path / "no-projects", ccusage_costs={}
        )

        assert _by_id(spans, "s_cg")["emits"] == "m_red"
        # The marker itself is never back-stamped; emission lives on the script.
        assert _by_id(spans, "m_red")["emits"] is None

    def test_script_picks_tightest_enclosing_marker(self, tmp_path: Path) -> None:
        # A lifecycle envelope and a phase step both bracket the script; the
        # tightest (latest-starting) wins, so the link is to the phase, not the run.
        events = tmp_path / "events.jsonl"
        _write_events(
            events,
            [
                _span(
                    span_id="m_env",
                    spoke_run_id=RUN_E,
                    kind="lifecycle",
                    name="worktree-new",
                    phase="spawn",
                    ts_start="2026-06-12T23:00:00Z",
                    ts_end="2026-06-12T23:01:00Z",
                ),
                _span(
                    span_id="m_red",
                    spoke_run_id=RUN_E,
                    kind="step",
                    name="solo-cycle",
                    phase="red",
                    ts_start="2026-06-12T23:00:05Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
                _span(
                    span_id="s_cg",
                    spoke_run_id=RUN_E,
                    kind="script",
                    name="commit-gauntlet",
                    ts_start="2026-06-12T23:00:54Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
            ],
        )

        spans = build_unified_spans(
            events_path=events, projects_root=tmp_path / "no-projects", ccusage_costs={}
        )

        assert _by_id(spans, "s_cg")["emits"] == "m_red"

    def test_unbracketed_script_keeps_null_emits(self, tmp_path: Path) -> None:
        # A script span no marker brackets (it runs after the step ends) stays
        # unlinked — emission is never guessed.
        events = tmp_path / "events.jsonl"
        _write_events(
            events,
            [
                _span(
                    span_id="m_red",
                    spoke_run_id=RUN_E,
                    kind="step",
                    name="solo-cycle",
                    phase="red",
                    ts_start="2026-06-12T23:00:05Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
                _span(
                    span_id="s_late",
                    spoke_run_id=RUN_E,
                    kind="script",
                    name="spoke-push",
                    ts_start="2026-06-12T23:01:10Z",
                    ts_end="2026-06-12T23:01:12Z",
                ),
            ],
        )

        spans = build_unified_spans(
            events_path=events, projects_root=tmp_path / "no-projects", ccusage_costs={}
        )

        assert _by_id(spans, "s_late")["emits"] is None

    def test_ad_hoc_null_run_script_does_not_link(self, tmp_path: Path) -> None:
        # An ad-hoc script and marker both carry spoke_run_id=null; emission is
        # scoped to a spoke run, so None==None must NOT be treated as same-run.
        events = tmp_path / "events.jsonl"
        _write_events(
            events,
            [
                _span(
                    span_id="m_adhoc",
                    kind="step",
                    name="solo-cycle",
                    phase="red",
                    ts_start="2026-06-12T23:00:05Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
                _span(
                    span_id="s_adhoc",
                    kind="script",
                    name="commit-gauntlet",
                    ts_start="2026-06-12T23:00:54Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
            ],
        )

        spans = build_unified_spans(
            events_path=events, projects_root=tmp_path / "no-projects", ccusage_costs={}
        )

        assert _by_id(spans, "s_adhoc")["emits"] is None

    def test_emission_does_not_cross_spoke_runs(self, tmp_path: Path) -> None:
        # A marker in a different spoke run never claims this script's emission.
        events = tmp_path / "events.jsonl"
        _write_events(
            events,
            [
                _span(
                    span_id="m_other",
                    spoke_run_id="feature/99-other+1700000000",
                    kind="step",
                    name="solo-cycle",
                    phase="red",
                    ts_start="2026-06-12T23:00:05Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
                _span(
                    span_id="s_cg",
                    spoke_run_id=RUN_E,
                    kind="script",
                    name="commit-gauntlet",
                    ts_start="2026-06-12T23:00:54Z",
                    ts_end="2026-06-12T23:00:55Z",
                ),
            ],
        )

        spans = build_unified_spans(
            events_path=events, projects_root=tmp_path / "no-projects", ccusage_costs={}
        )

        assert _by_id(spans, "s_cg")["emits"] is None
