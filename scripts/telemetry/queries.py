"""Unified push + pull span dataset, exposed as DuckDB views for Issue #23.

Builds one in-memory DuckDB over the whole workflow-observability dataset — no
separate database to run. Push spans (hooks, cycle steps, lifecycle) come from
the telemetry ``events.jsonl`` source; pull spans (skill/agent/todo/human) are
parsed from session logs and cost-correlated. Pull spans inherit their
``spoke_run_id`` from a session-peer push span, and every span — push or pull —
is token/cost attributed.

Spans are hierarchical (a ``step`` span encloses the skill/agent spans that ran
during it), so the ``step_metrics`` and ``spoke_run_summary`` views aggregate
within a granularity; callers must not sum a step's cost together with the
nested spans it already contains.

The raw span's ``human`` struct is flattened to ``human_type`` / ``human_wait_ms``
columns for SQL ergonomics; every other field is the frozen schema verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from telemetry.cost import attribute_spans, per_turn_rows
from telemetry.session_parser import ParsedSession, parse_projects_dir
from telemetry.spans import SPAN_FIELDS, SPAN_KINDS, Span
from telemetry.spoke_runs import backfill_spoke_run_ids

# Table columns: the 18 frozen span fields with `human` flattened to two columns.
_COLUMNS = (
    "span_id VARCHAR",
    "parent_id VARCHAR",
    "spoke_run_id VARCHAR",
    "session_id VARCHAR",
    "workflow_rev VARCHAR",
    "repo VARCHAR",
    "branch VARCHAR",
    "kind VARCHAR",
    "name VARCHAR",
    "phase VARCHAR",
    "ts_start VARCHAR",
    "ts_end VARCHAR",
    "duration_ms BIGINT",
    "status VARCHAR",
    "human_type VARCHAR",
    "human_wait_ms BIGINT",
    "tokens_in BIGINT",
    "tokens_out BIGINT",
    "cost_usd DOUBLE",
)

_STEP_KEY_SQL = (
    "kind || ':' || name || "
    "CASE WHEN phase IS NOT NULL AND phase != '' THEN ':' || phase ELSE '' END"
)

# Per-turn relation: one row per assistant usage event (main + walked subagent),
# carrying model and a per-turn cost that — unlike the overlapping span costs —
# is counted exactly once, so the rows reconcile to the ccusage session total.
_TURN_COLUMNS = (
    "session_id VARCHAR",
    "ts VARCHAR",
    "model VARCHAR",
    "source VARCHAR",
    "agent_id VARCHAR",
    "tokens_in BIGINT",
    "tokens_out BIGINT",
    "tokens_total BIGINT",
    "cost_usd DOUBLE",
)
_TURN_FIELDS = tuple(col.split(" ", 1)[0] for col in _TURN_COLUMNS)


def connect(
    *, events_path: Path, projects_root: Path, ccusage_costs: dict[str, float]
) -> duckdb.DuckDBPyConnection:
    """Build an in-memory DuckDB with the unified ``spans`` table and views.

    Args:
        events_path: Telemetry ``events.jsonl`` (push spans). Missing → pull only.
        projects_root: Claude ``projects`` root holding the session logs.
        ccusage_costs: Map of ``session_id`` to ccusage ``totalCost``.

    Returns:
        A connection exposing the ``spans`` table plus the ``spoke_run_summary``
        and ``step_metrics`` views.
    """
    # Parse the session logs once and reuse for both relations: spans (attributed,
    # joined to push spans) and turns (per-turn usage with model + once-only cost).
    parsed = parse_projects_dir(projects_root)
    spans = _attributed_span_dicts(parsed, events_path, ccusage_costs)
    turns = per_turn_rows(parsed.usage_events, ccusage_costs)

    con = duckdb.connect(":memory:")
    con.execute(f"CREATE TABLE spans ({', '.join(_COLUMNS)})")
    rows = [_row(span) for span in spans]
    if rows:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        con.executemany(f"INSERT INTO spans VALUES ({placeholders})", rows)
    con.execute("CREATE TABLE session_costs (session_id VARCHAR, cost_usd DOUBLE)")
    if ccusage_costs:
        con.executemany("INSERT INTO session_costs VALUES (?, ?)", list(ccusage_costs.items()))
    _create_turns(con, turns)
    _create_views(con)
    return con


def build_unified_spans(
    *, events_path: Path, projects_root: Path, ccusage_costs: dict[str, float]
) -> list[dict[str, object]]:
    """Parse, attribute, and join push + pull spans into one span-dict list."""
    parsed = parse_projects_dir(projects_root)
    return _attributed_span_dicts(parsed, events_path, ccusage_costs)


def build_turns(*, projects_root: Path, ccusage_costs: dict[str, float]) -> list[dict[str, object]]:
    """Per-turn rows (model + once-only cost) parsed from the session logs."""
    parsed = parse_projects_dir(projects_root)
    return per_turn_rows(parsed.usage_events, ccusage_costs)


def _attributed_span_dicts(
    parsed: ParsedSession, events_path: Path, ccusage_costs: dict[str, float]
) -> list[dict[str, object]]:
    push_spans = _load_push_spans(events_path)
    all_spans = parsed.spans + push_spans
    backfill_spoke_run_ids(all_spans)
    attribute_spans(all_spans, parsed.usage_events, ccusage_costs, agent_links=parsed.agent_links)
    return [span.to_dict() for span in all_spans]


def _load_push_spans(events_path: Path) -> list[Span]:
    if not events_path.exists():
        return []
    spans: list[Span] = []
    with open(events_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # The append-only log may hold legacy / partial lines that pre-date
            # the span schema; keep only well-formed spans (identity + valid
            # kind). Skip absent fields so the dataclass defaults apply rather
            # than overwriting them with None (e.g. repo "unknown", status
            # "success") on a partial line.
            if not record.get("span_id") or not record.get("name"):
                continue
            if record.get("kind") not in SPAN_KINDS:
                continue
            present = {f: v for f in SPAN_FIELDS if (v := record.get(f)) is not None}
            spans.append(Span(**present))
    return spans


def _row(span: dict[str, object]) -> tuple[object, ...]:
    human = span.get("human")
    human_type = human.get("type") if isinstance(human, dict) else None
    human_wait_ms = human.get("wait_ms") if isinstance(human, dict) else None
    scalar = {field: span.get(field) for field in SPAN_FIELDS if field != "human"}
    return (
        scalar["span_id"],
        scalar["parent_id"],
        scalar["spoke_run_id"],
        scalar["session_id"],
        scalar["workflow_rev"],
        scalar["repo"],
        scalar["branch"],
        scalar["kind"],
        scalar["name"],
        scalar["phase"],
        scalar["ts_start"],
        scalar["ts_end"],
        scalar["duration_ms"],
        scalar["status"],
        human_type,
        human_wait_ms,
        scalar["tokens_in"],
        scalar["tokens_out"],
        scalar["cost_usd"],
    )


def _create_turns(con: duckdb.DuckDBPyConnection, turns: list[dict[str, object]]) -> None:
    con.execute(f"CREATE TABLE turns ({', '.join(_TURN_COLUMNS)})")
    if not turns:
        return
    placeholders = ", ".join("?" for _ in _TURN_FIELDS)
    con.executemany(
        f"INSERT INTO turns VALUES ({placeholders})",
        [tuple(turn[field] for field in _TURN_FIELDS) for turn in turns],
    )


def _create_views(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"CREATE VIEW span_steps AS SELECT *, {_STEP_KEY_SQL} AS step_key FROM spans")
    # total_cost_usd is the sum of the run's distinct sessions' ccusage totals —
    # NOT sum(spans.cost_usd), which would double-count because a step span's
    # cost already includes the pull spans nested inside it. Sourcing from
    # session_costs makes the run total cross-check against ccusage directly.
    con.execute(
        "CREATE VIEW spoke_run_summary AS "
        "WITH lifetime AS ("
        "  SELECT spoke_run_id, count(*) AS span_count, "
        "         count(DISTINCT session_id) AS session_count, "
        "         min(ts_start) AS ts_start, max(ts_end) AS ts_end "
        "  FROM spans GROUP BY spoke_run_id"
        "), run_cost AS ("
        "  SELECT d.spoke_run_id, sum(sc.cost_usd) AS total_cost_usd "
        "  FROM (SELECT DISTINCT spoke_run_id, session_id FROM spans) d "
        "  JOIN session_costs sc ON d.session_id = sc.session_id "
        "  GROUP BY d.spoke_run_id"
        ") "
        "SELECT l.spoke_run_id, l.span_count, l.session_count, "
        "       c.total_cost_usd, l.ts_start, l.ts_end "
        "FROM lifetime l "
        "LEFT JOIN run_cost c ON l.spoke_run_id IS NOT DISTINCT FROM c.spoke_run_id"
    )
    con.execute(
        "CREATE VIEW step_metrics AS "
        "SELECT spoke_run_id, step_key, count(*) AS invocations, "
        "avg(duration_ms) AS mean_duration_ms, "
        "median(duration_ms) AS median_duration_ms, "
        "sum(cost_usd) AS total_cost_usd, "
        "sum(CASE WHEN human_type IS NOT NULL THEN 1 ELSE 0 END) AS human_count "
        "FROM span_steps GROUP BY spoke_run_id, step_key"
    )
