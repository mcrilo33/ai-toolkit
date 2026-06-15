"""Query layer for the workflow-observability dashboard (Issue #23).

This is the thin layer the Streamlit UI (``app.py``) sits on. It loads the
unified span dataset — the frozen v1 schema from Issue #21
(``docs/telemetry-span-schema.md``) — into DuckDB and answers the dashboard's
three views plus the automatability panel.

Data source: until Issue #22 (the session-log parser + token/cost correlation)
lands its DuckDB views, this module ingests the append-only ``events.jsonl``
span log directly via :meth:`SpanStore.from_jsonl`. When #22 lands, point
:meth:`SpanStore.from_connection` at its views — every query here is plain SQL
over a ``spans``-shaped relation, so the wiring is a one-line swap.

Privacy: spans carry metadata only (timings, statuses, toolkit construct names).
This layer never reads or surfaces prompt content — there is none in the schema.
Token/cost numbers originate from #22's ccusage correlation and are read as-is;
nothing is re-derived here.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

# Column order for the in-memory ``spans`` table. ``human`` is flattened into
# ``human_type`` / ``human_wait_ms`` so the table is purely scalar (no nested
# structs to wrangle in SQL). Timestamps stay as ISO-8601 UTC strings: they sort
# lexicographically, so window filtering and child ordering need no parsing.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("span_id", "VARCHAR"),
    ("parent_id", "VARCHAR"),
    ("spoke_run_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("workflow_rev", "VARCHAR"),
    ("repo", "VARCHAR"),
    ("branch", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("name", "VARCHAR"),
    ("phase", "VARCHAR"),
    ("ts_start", "VARCHAR"),
    ("ts_end", "VARCHAR"),
    ("duration_ms", "BIGINT"),
    ("status", "VARCHAR"),
    ("human_type", "VARCHAR"),
    ("human_wait_ms", "BIGINT"),
    ("summary", "VARCHAR"),
    ("tokens_in", "BIGINT"),
    ("tokens_out", "BIGINT"),
    ("cost_usd", "DOUBLE"),
)
_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _COLUMNS)

# Per-turn relation (mirrors Issue #22's ``turns`` table). One row per assistant
# usage event, carrying model and a per-turn cost counted exactly once — the
# source the v2 spoke view uses for model attribution and once-per-turn cost.
_TURN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("session_id", "VARCHAR"),
    ("ts", "VARCHAR"),
    ("model", "VARCHAR"),
    ("source", "VARCHAR"),
    ("agent_id", "VARCHAR"),
    ("tokens_in", "BIGINT"),
    ("tokens_out", "BIGINT"),
    ("tokens_total", "BIGINT"),
    ("cost_usd", "DOUBLE"),
)
_TURN_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _TURN_COLUMNS)

# Status severity for collapsing many spans into one line: a collapsed hooks row
# must surface the worst outcome, never hide a deny/failure/warn behind success.
_STATUS_SEVERITY: dict[str, int] = {
    "deny": 4,
    "failure": 3,
    "warn": 2,
    "skipped": 1,
    "success": 0,
}

# Synthetic bucket ids for the phase-interval attribution (Issue #46): the leading
# pre-cycle region and the off-spine catch-all. Real intervals key on their marker's
# ``span_id``, so these sentinels never collide with a real span.
_SETUP_KEY = "__setup__"
_UNRESOLVED_KEY = "__unresolved__"


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Parse an append-only span log: one JSON object per non-blank line."""
    spans: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            spans.append(json.loads(line))
    return spans


def _row_tuple(span: dict[str, Any]) -> tuple[Any, ...]:
    """Flatten one span dict into a row matching ``_COLUMNS`` order."""
    human = span.get("human") or {}
    values: dict[str, Any] = {
        **span,
        "human_type": human.get("type"),
        "human_wait_ms": human.get("wait_ms"),
    }
    return tuple(values.get(name) for name in _COLUMN_NAMES)


class SpanStore:
    """A DuckDB-backed view over the span dataset.

    Construct with :meth:`from_jsonl` (ingest the raw log) or
    :meth:`from_connection` (reuse Issue #22's prepared views). Query methods
    return plain Python structures so the Streamlit layer — and the tests — stay
    free of DuckDB types.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con

    @classmethod
    def from_events(
        cls, events: list[dict[str, Any]], turns: list[dict[str, Any]] | None = None
    ) -> SpanStore:
        """Build an in-memory store from already-parsed span dicts.

        ``turns`` (optional per-turn rows) seeds a ``turns`` table so the v2
        spoke view's once-per-turn cost/model attribution has data; the raw
        push-span log has none, so it defaults to an empty table.
        """
        con = duckdb.connect(":memory:")
        ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _COLUMNS)
        con.execute(f"CREATE TABLE spans ({ddl})")
        if events:
            placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
            con.executemany(
                f"INSERT INTO spans VALUES ({placeholders})",
                [_row_tuple(span) for span in events],
            )
        _create_turns_table(con, turns or [])
        return cls(con)

    @classmethod
    def from_jsonl(cls, path: str | Path, turns: list[dict[str, Any]] | None = None) -> SpanStore:
        """Build an in-memory store from a raw ``events.jsonl`` span log."""
        return cls.from_events(load_jsonl(path), turns=turns)

    @classmethod
    def from_connection(cls, con: duckdb.DuckDBPyConnection) -> SpanStore:
        """Wrap an existing connection that already exposes a ``spans`` relation.

        This is the seam for Issue #22: once its parser publishes correlated
        ``spans`` views, hand that connection here instead of re-ingesting JSONL.
        """
        return cls(con)

    @classmethod
    def from_telemetry(
        cls,
        *,
        events_path: str | Path,
        projects_root: str | Path,
        ccusage_costs: dict[str, float] | None = None,
        scripts_dir: str | Path | None = None,
    ) -> SpanStore:
        """Build a store from Issue #22's correlated unified-span dataset.

        Issue #22's ``telemetry.queries.connect`` parses session logs, joins the
        push ``events.jsonl`` spans, and ccusage-attributes tokens/cost — then
        exposes a ``spans`` table whose columns match this module's exactly, so
        every query here runs against it unchanged. This is the live path; the
        JSONL constructors are for fixtures and the raw log.

        The import is lazy and ``scripts_dir`` (the ai-toolkit ``scripts/``
        directory) is added to ``sys.path`` only here, so the fixture path and
        the unit suite never depend on the ``telemetry`` package being importable.
        ``ccusage_costs`` is reused verbatim — costs are never re-derived here.
        """
        if scripts_dir is not None:
            sys.path.insert(0, str(scripts_dir))
        from telemetry.queries import connect

        con = connect(
            events_path=Path(events_path),
            projects_root=Path(projects_root),
            ccusage_costs=ccusage_costs or {},
        )
        return cls.from_connection(con)

    def close(self) -> None:
        self.con.close()

    def _query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run SQL and return rows as column-keyed dicts."""
        cursor = self.con.execute(sql, params or [])
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def spoke_run_ids(self) -> list[str]:
        """All known ``spoke_run_id``s, newest-first by latest activity.

        Ordered by ``max(ts_end)`` descending, tie-broken by ``min(ts_start)``
        descending — not by the ``<branch>+<epoch>`` id string, which sorts
        alphabetically by branch. Ordering happens in Python via :func:`_parse_ts`
        because push spans carry second precision and pull spans millisecond, so a
        lexical sort on the raw timestamps would misorder them.
        """
        rows = self._query(
            "SELECT spoke_run_id, MAX(ts_end) AS last_end, MIN(ts_start) AS first_start "
            "FROM spans WHERE spoke_run_id IS NOT NULL GROUP BY spoke_run_id"
        )
        rows.sort(
            key=lambda r: (
                _parse_ts(r["last_end"]) or 0.0,
                _parse_ts(r["first_start"]) or 0.0,
                r["spoke_run_id"],
            ),
            reverse=True,
        )
        return [row["spoke_run_id"] for row in rows]

    def workflow_revs(self) -> list[str]:
        """All known ``workflow_rev``s, sorted — the A/B view's pick list."""
        rows = self._query(
            "SELECT DISTINCT workflow_rev FROM spans "
            "WHERE workflow_rev IS NOT NULL ORDER BY workflow_rev"
        )
        return [row["workflow_rev"] for row in rows]

    def spoke_tree(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """The step/sub-step tree for one spoke.

        Returns a forest of root nodes (spans with no parent inside this spoke),
        each child list ordered by ``ts_start``. Every node carries its own
        metrics plus a ``subtree`` rollup summing the node and all descendants
        (null cost/token values count as zero).
        """
        rows = self._query(
            "SELECT * FROM spans WHERE spoke_run_id = ? ORDER BY ts_start, span_id",
            [spoke_run_id],
        )
        nodes: dict[str, dict[str, Any]] = {}
        for row in rows:
            nodes[row["span_id"]] = {
                "span_id": row["span_id"],
                "parent_id": row["parent_id"],
                "kind": row["kind"],
                "name": row["name"],
                "phase": row["phase"],
                "status": row["status"],
                "ts_start": row["ts_start"],
                "duration_ms": row["duration_ms"],
                "cost_usd": row["cost_usd"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "human_type": row["human_type"],
                "human_wait_ms": row["human_wait_ms"],
                "human_count": 1 if row["human_type"] else 0,
                "children": [],
            }

        roots: list[dict[str, Any]] = []
        for node in nodes.values():
            parent = nodes.get(node["parent_id"])
            # A node with no in-spoke parent — or a malformed self-reference —
            # is a root, so a bad span can never vanish or recurse forever.
            if parent is None or parent is node:
                roots.append(node)
            else:
                parent["children"].append(node)

        for root in roots:
            _roll_up(root)
        return roots

    def spoke_steps(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """The v2 collapse-to-steps drill-down tree for one spoke.

        Level-1 roots are reconstructed **phase-interval buckets** (Issue #46):
        ``step``/``lifecycle`` spans are point markers that fire at phase
        *completion*, so the marker spine partitions the run into intervals and a
        main turn attributes to the bucket whose interval contains it. The leading
        region up to the first ``step`` marker is one ``setup`` bucket; trailing
        lifecycle markers keep their own labels; main turns outside the lifecycle
        envelope go to ``(unresolved)``. Subagent turns still attribute to their
        ``agent`` span. The real spans nest *under* their bucket by time-bracketing
        (smallest enclosing window), hooks collapse into one ``hooks`` node, and
        each node carries a ``rollup`` of additive subtree metrics. The bucket
        owns no wall-clock — intervals are attribution-only, never durations.

        Returns a forest ordered by ``ts_start``; an unknown spoke yields ``[]``.
        """
        nodes, intervals, buckets, traces = self._attributed_nodes(spoke_run_id)
        if not nodes:
            return []
        forest = _interval_forest(nodes, intervals, buckets, traces)
        for root in forest:
            _roll_up_steps(root)
        return forest

    def spoke_meta_by_kind(self, spoke_run_id: str) -> list[dict[str, Any]]:
        """Aggregate one spoke's spans by ``kind`` to spot "launched too much".

        Per span kind: invocation ``count``, total/mean/median ``duration_ms``,
        total/mean ``cost_usd``, and the distinct ``models`` seen. Since the source
        split (#46) attributes main-agent cost to phase intervals rather than spans,
        only ``agent`` spans carry owned cost here — so summing across kinds equals
        the subagent total (the run total minus every phase bucket's owned main
        cost). The "launched too much" signal lives in the ``count``/``duration``
        columns. Rows sort by total cost then count, descending. Unknown → ``[]``.
        """
        nodes, _, _, _ = self._attributed_nodes(spoke_run_id)
        return _aggregate_by_kind(nodes)

    def _attributed_nodes(
        self, spoke_run_id: str
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, list[dict[str, Any]]],
    ]:
        """Load one spoke's spans as nodes with once-per-turn cost attributed.

        Returns ``(nodes, intervals, bucket_costs, bucket_traces)`` — the flat
        real-span nodes (``agent`` nodes carry their subagent ``own_cost_usd`` /
        ``own_tokens_*`` / ``models``; every other node owns nothing, since main
        cost lives on the phase intervals), the reconstructed phase ``intervals``,
        the per-bucket owned main-turn cost keyed by bucket id, and the per-bucket
        main-turn ``turns_trace`` (Issue #47). Shared by the drill-down (which
        materializes the buckets) and the meta-by-kind view (which ignores them).
        """
        rows = self._query(
            "SELECT * FROM spans WHERE spoke_run_id = ? ORDER BY ts_start, span_id",
            [spoke_run_id],
        )
        nodes = [_step_node(row) for row in rows]
        intervals = _build_intervals(nodes)
        session_ids = sorted({row["session_id"] for row in rows if row["session_id"]})
        turns = self._turns_for_sessions(session_ids)
        buckets = _attribute_turns(nodes, turns, intervals)
        traces = _bucket_traces(turns, intervals)
        return nodes, intervals, buckets, traces

    def _turns_for_sessions(self, session_ids: list[str]) -> list[dict[str, Any]]:
        """Per-turn rows for the spoke's sessions (empty on the raw path).

        A connection handed to :meth:`from_connection` that predates the ``turns``
        relation has no such table; rather than crash, degrade to no owned cost.
        """
        if not session_ids or not self._has_table("turns"):
            return []
        placeholders = ", ".join("?" for _ in session_ids)
        return self._query(
            f"SELECT * FROM turns WHERE session_id IN ({placeholders})", list(session_ids)
        )

    def _has_table(self, name: str) -> bool:
        rows = self._query("SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name])
        return bool(rows)

    def aggregate(
        self,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Roll spans up across all spokes in a time window.

        Groups by ``(kind, name, phase)`` and reports, per group: frequency
        (invocation count), totals, and per-invocation mean/median for time and
        cost, plus human-interaction count normalized per invocation. The window
        is a half-open ``[window_start, window_end)`` interval on ``ts_start``
        (ISO-8601 strings compare lexicographically); ``None`` drops that bound.
        Null cost/token values count as zero. Rows are sorted by total time
        spent, descending — the dashboard's "where does time go" ordering.
        """
        rows = self._query(
            """
            SELECT
                kind, name, phase,
                COUNT(*) AS invocations,
                SUM(duration_ms) AS total_duration_ms,
                AVG(duration_ms) AS mean_duration_ms,
                MEDIAN(duration_ms) AS median_duration_ms,
                SUM(COALESCE(cost_usd, 0)) AS total_cost_usd,
                AVG(COALESCE(cost_usd, 0)) AS mean_cost_usd,
                MEDIAN(COALESCE(cost_usd, 0)) AS median_cost_usd,
                SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) AS total_tokens,
                AVG(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) AS mean_tokens,
                SUM(CASE WHEN human_type IS NOT NULL THEN 1 ELSE 0 END) AS human_count
            FROM spans
            WHERE (? IS NULL OR ts_start >= ?)
              AND (? IS NULL OR ts_start < ?)
            GROUP BY kind, name, phase
            ORDER BY total_duration_ms DESC, kind, name, phase
            """,
            [window_start, window_start, window_end, window_end],
        )
        for row in rows:
            row["frequency"] = row["invocations"]
            row["human_per_invocation"] = row["human_count"] / row["invocations"]
        return rows

    def ab_compare(
        self,
        rev_a: str,
        rev_b: str,
        *,
        low_confidence_n: int = 5,
    ) -> list[dict[str, Any]]:
        """Per-step delta between two ``workflow_rev``s, normalized per invocation.

        For each ``(kind, name, phase)`` present in either rev, reports the
        per-invocation mean time, cost, and human-interaction rate on each side
        and their B-minus-A deltas. Normalizing per invocation makes revs with
        different spoke counts comparable; a negative delta is an improvement
        (less time/cost/human). Each row carries sample sizes ``n_a``/``n_b``
        and a ``low_confidence`` flag set when ``min(n_a, n_b) < low_confidence_n``
        — small spoke counts are noisy and must not imply significance. Rows are
        sorted by the magnitude of the time delta, descending.
        """
        rows = self._query(
            """
            SELECT
                kind, name, phase, workflow_rev,
                COUNT(*) AS n,
                AVG(duration_ms) AS mean_duration,
                AVG(COALESCE(cost_usd, 0)) AS mean_cost,
                SUM(CASE WHEN human_type IS NOT NULL THEN 1 ELSE 0 END) * 1.0
                    / COUNT(*) AS human_per_invocation
            FROM spans
            WHERE workflow_rev IN (?, ?)
            GROUP BY kind, name, phase, workflow_rev
            """,
            [rev_a, rev_b],
        )

        groups: dict[tuple[Any, Any, Any], dict[str, dict[str, Any]]] = {}
        for row in rows:
            key = (row["kind"], row["name"], row["phase"])
            groups.setdefault(key, {})[row["workflow_rev"]] = row

        result = [
            _ab_row(kind, name, phase, per_rev.get(rev_a), per_rev.get(rev_b), low_confidence_n)
            for (kind, name, phase), per_rev in groups.items()
        ]
        result.sort(
            key=lambda r: (-abs(r["delta_duration_ms"]), r["kind"], r["name"], r["phase"] or "")
        )
        return result

    def automatability_candidates(self, *, min_frequency: int = 1) -> list[dict[str, Any]]:
        """Rank human-interaction points by how worth automating they look.

        Groups spans that waited on a human (``human_type`` set) by
        ``(name, phase, human_type)`` and scores each group by
        ``frequency * consistency * on_critical_path``:

        - ``frequency`` — how often the interaction occurs.
        - ``consistency`` — modal-status fraction (the share of the most common
          outcome); high means low decision variance, so a rule could likely
          replace the human.
        - ``on_critical_path`` — fraction of the spans that are ``step`` or
          ``lifecycle`` (blocking the workflow's spine) rather than incidental.

        Reports ``mean_wait_ms`` too. This only SURFACES candidates; judging
        whether one is truly automatable is a later LLM-judge step. Groups below
        ``min_frequency`` are dropped; rows sort by score descending.
        """
        groups = self._query(
            """
            SELECT
                name, phase, human_type,
                COUNT(*) AS frequency,
                AVG(human_wait_ms) AS mean_wait_ms,
                AVG(CASE WHEN kind IN ('step', 'lifecycle') THEN 1.0 ELSE 0.0 END)
                    AS on_critical_path
            FROM spans
            WHERE human_type IS NOT NULL
            GROUP BY name, phase, human_type
            """
        )
        # Modal-status count per group, computed in Python: a SQL self-join on
        # (name, phase, human_type) would drop null-phase groups (NULL != NULL).
        status_rows = self._query(
            """
            SELECT name, phase, human_type, COUNT(*) AS cnt
            FROM spans
            WHERE human_type IS NOT NULL
            GROUP BY name, phase, human_type, status
            """
        )
        modal: dict[tuple[Any, Any, Any], int] = {}
        for row in status_rows:
            key = (row["name"], row["phase"], row["human_type"])
            modal[key] = max(modal.get(key, 0), row["cnt"])

        result: list[dict[str, Any]] = []
        for group in groups:
            if group["frequency"] < min_frequency:
                continue
            key = (group["name"], group["phase"], group["human_type"])
            consistency = modal[key] / group["frequency"]
            group["consistency"] = consistency
            group["score"] = group["frequency"] * consistency * group["on_critical_path"]
            result.append(group)
        result.sort(key=lambda r: (-r["score"], r["name"], r["phase"] or "", r["human_type"]))
        return result


def _ab_row(
    kind: Any,
    name: Any,
    phase: Any,
    side_a: dict[str, Any] | None,
    side_b: dict[str, Any] | None,
    low_confidence_n: int,
) -> dict[str, Any]:
    """Build one A/B comparison row from each side's per-rev aggregate.

    A missing side (the step never ran under that rev) reads as zero metrics
    with ``n = 0``, which always trips ``low_confidence``.
    """
    n_a = side_a["n"] if side_a else 0
    n_b = side_b["n"] if side_b else 0
    dur_a = side_a["mean_duration"] if side_a else 0.0
    dur_b = side_b["mean_duration"] if side_b else 0.0
    cost_a = side_a["mean_cost"] if side_a else 0.0
    cost_b = side_b["mean_cost"] if side_b else 0.0
    human_a = side_a["human_per_invocation"] if side_a else 0.0
    human_b = side_b["human_per_invocation"] if side_b else 0.0
    return {
        "kind": kind,
        "name": name,
        "phase": phase,
        "n_a": n_a,
        "n_b": n_b,
        "mean_duration_a": dur_a,
        "mean_duration_b": dur_b,
        "delta_duration_ms": dur_b - dur_a,
        "mean_cost_a": cost_a,
        "mean_cost_b": cost_b,
        "delta_cost_usd": cost_b - cost_a,
        "human_per_invocation_a": human_a,
        "human_per_invocation_b": human_b,
        "delta_human_per_invocation": human_b - human_a,
        "low_confidence": min(n_a, n_b) < low_confidence_n,
    }


def _create_turns_table(con: duckdb.DuckDBPyConnection, turns: list[dict[str, Any]]) -> None:
    """Create the ``turns`` table and seed it (empty when no turns are given)."""
    ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _TURN_COLUMNS)
    con.execute(f"CREATE TABLE turns ({ddl})")
    if not turns:
        return
    placeholders = ", ".join("?" for _ in _TURN_COLUMN_NAMES)
    con.executemany(
        f"INSERT INTO turns VALUES ({placeholders})",
        [tuple(turn.get(name) for name in _TURN_COLUMN_NAMES) for turn in turns],
    )


def _parse_ts(ts: str | None) -> float | None:
    """ISO-8601 UTC string to epoch seconds (None if missing/malformed).

    Parsed numerically rather than compared lexically because push spans carry
    second precision (``…00Z``) and pull spans millisecond (``…00.000Z``), which
    sort in the wrong order as strings.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _step_node(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_id": row["span_id"],
        "kind": row["kind"],
        "name": row["name"],
        "summary": row["summary"],
        "phase": row["phase"],
        "status": row["status"],
        "ts_start": row["ts_start"],
        "ts_end": row["ts_end"],
        "duration_ms": row["duration_ms"] or 0,
        "human_type": row["human_type"],
        "human_wait_ms": row["human_wait_ms"],
        "human_count": 1 if row["human_type"] else 0,
        # Filled by the once-per-turn attribution pass; the span schema carries
        # no model, so these come from the turns relation, never spans.cost_usd.
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "agent": "subagent" if row["kind"] == "agent" else "main",
        "children": [],
    }


def _sort_key(node: dict[str, Any]) -> tuple[float, str]:
    return (_parse_ts(node["ts_start"]) or 0.0, node["span_id"] or "")


def _nest_by_time(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest each node under the smallest span whose window contains it."""
    bounds = {n["span_id"]: (_parse_ts(n["ts_start"]), _parse_ts(n["ts_end"])) for n in nodes}
    by_id = {n["span_id"]: n for n in nodes}
    roots: list[dict[str, Any]] = []
    for node in nodes:
        parent_id = _smallest_container(node, nodes, bounds)
        if parent_id is None:
            roots.append(node)
        else:
            by_id[parent_id]["children"].append(node)
    return roots


def _smallest_container(
    child: dict[str, Any],
    nodes: list[dict[str, Any]],
    bounds: dict[str, tuple[float | None, float | None]],
) -> str | None:
    """The id of the tightest-windowed span strictly enclosing ``child``.

    A span with an identical window is a sibling, not a parent (its window is not
    *strictly* larger), so equal-window peers like repeated ``TaskCreate`` calls
    stay flat. Ties between equal-window containers break on ``span_id``.
    """
    cs, ce = bounds[child["span_id"]]
    if cs is None or ce is None:
        return None
    best_key: tuple[float, float, str] | None = None
    best_id: str | None = None
    for cand in nodes:
        if cand is child:
            continue
        ps, pe = bounds[cand["span_id"]]
        if ps is None or pe is None:
            continue
        if ps <= cs and pe >= ce and (ps, pe) != (cs, ce):
            key = (pe - ps, ps, cand["span_id"])
            if best_key is None or key < best_key:
                best_key, best_id = key, cand["span_id"]
    return best_id


def _collapse_hooks(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse hook spans among ``siblings`` into one node (recursively)."""
    others = [n for n in siblings if n["kind"] != "hook"]
    hooks = [n for n in siblings if n["kind"] == "hook"]
    for node in others:
        node["children"] = _collapse_hooks(node["children"])
    for hook in hooks:
        hook["children"] = _collapse_hooks(hook["children"])
    result = list(others)
    if hooks:
        result.append(_hooks_node(hooks))
    return sorted(result, key=_sort_key)


def _hooks_node(hooks: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [h["ts_start"] for h in hooks if h["ts_start"]]
    ends = [h["ts_end"] for h in hooks if h["ts_end"]]
    return {
        "span_id": None,
        "kind": "hooks",
        "name": "hooks",
        "phase": None,
        "status": _worst_status(hooks),
        "ts_start": min(starts, key=lambda s: _parse_ts(s) or 0.0) if starts else None,
        "ts_end": max(ends, key=lambda s: _parse_ts(s) or 0.0) if ends else None,
        "duration_ms": sum(h["duration_ms"] for h in hooks),
        "human_type": None,
        "human_wait_ms": None,
        "human_count": 0,
        # A collapsed node owns no turns itself — its hook children carry any.
        "own_cost_usd": 0.0,
        "own_tokens_in": 0,
        "own_tokens_out": 0,
        "models": [],
        "agent": "main",
        "collapsed": True,
        "collapsed_count": len(hooks),
        "children": list(hooks),
    }


def _worst_status(nodes: list[dict[str, Any]]) -> str:
    return max(
        (n["status"] for n in nodes),
        key=lambda s: _STATUS_SEVERITY.get(s, 0),
        default="success",
    )


def _build_intervals(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct contiguous phase intervals from the step/lifecycle marker spine.

    Markers fire at phase *completion*, so the spine is sorted by ``ts_end`` and
    ``interval[i] = (M[i-1].ts_end, M[i].ts_end]`` is the work that culminated in
    marker ``M[i]``; ``interval[0]`` floors at the earliest marker ``ts_start`` (the
    spawn). Sorting by completion time keeps ``hi`` monotonic, so the intervals tile
    ``[earliest_start, Mn.ts_end]`` with no gap or inversion even when a wide marker
    overlaps a later one (point markers make this moot, but the contract is robust).
    Every interval up to and including the first ``step`` marker keys to the
    ``setup`` bucket (the pre-cycle gap has no phase-start signal, so its work is
    honestly coarse rather than mislabelled); the rest key per-phase by their
    marker's ``span_id``.
    """
    markers = sorted(
        (
            n
            for n in nodes
            if n["kind"] in ("step", "lifecycle")
            and _parse_ts(n["ts_start"]) is not None
            and _parse_ts(n["ts_end"]) is not None
        ),
        key=lambda n: (_parse_ts(n["ts_end"]) or 0.0, n["span_id"] or ""),
    )
    if not markers:
        return []
    first_step = next((i for i, m in enumerate(markers) if m["kind"] == "step"), None)
    floor_iso = min(markers, key=lambda n: _parse_ts(n["ts_start"]) or 0.0)["ts_start"]
    intervals: list[dict[str, Any]] = []
    for i, marker in enumerate(markers):
        lo_iso = floor_iso if i == 0 else markers[i - 1]["ts_end"]
        is_setup = first_step is not None and i <= first_step
        intervals.append(
            {
                "lo": _parse_ts(lo_iso),
                "hi": _parse_ts(marker["ts_end"]),
                "lo_iso": lo_iso,
                "hi_iso": marker["ts_end"],
                "first": i == 0,
                "key": _SETUP_KEY if is_setup else marker["span_id"],
                "label": "setup" if is_setup else (marker["phase"] or marker["name"]),
            }
        )
    return intervals


def _interval_containing(ts: float, intervals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The interval whose window holds ``ts`` (right-closed at each marker boundary).

    The first interval is closed at both ends; the rest are left-open, so a turn on
    a shared marker boundary lands in the earlier interval — counted exactly once.
    """
    for iv in intervals:
        lo, hi = iv["lo"], iv["hi"]
        if lo is None or hi is None:
            continue
        if (lo <= ts if iv["first"] else lo < ts) and ts <= hi:
            return iv
    return None


def _main_turn_bucket(turn: dict[str, Any], intervals: list[dict[str, Any]]) -> str:
    """The bucket id a main turn belongs to (``(unresolved)`` when off the spine)."""
    ts = _parse_ts(turn["ts"])
    if ts is None or not intervals:
        return _UNRESOLVED_KEY
    iv = _interval_containing(ts, intervals)
    return iv["key"] if iv is not None else _UNRESOLVED_KEY


def _acc() -> dict[str, Any]:
    return {"cost": 0.0, "in": 0, "out": 0, "models": set()}


def _add_turn(acc: dict[str, Any], turn: dict[str, Any]) -> None:
    acc["cost"] += turn["cost_usd"] or 0.0
    acc["in"] += turn["tokens_in"] or 0
    acc["out"] += turn["tokens_out"] or 0
    if turn["model"]:
        acc["models"].add(turn["model"])


def _fill_owned(node: dict[str, Any], acc: dict[str, Any]) -> None:
    node["own_cost_usd"] = acc["cost"]
    node["own_tokens_in"] = acc["in"]
    node["own_tokens_out"] = acc["out"]
    node["models"] = sorted(acc["models"])


def _attribute_turns(
    nodes: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Source-split once-per-turn attribution; fill agent nodes, return bucket costs.

    A **subagent** turn attaches to the tightest enclosing ``agent`` span (its own
    cost). A **main** turn attaches to the phase interval containing it — a
    synthetic bucket, never a window-nested leaf — so no hook/skill/todo/human can
    own a main turn. Counting each turn once makes the rolled-up cost reconcile to
    the run total. Returns the per-bucket owned cost keyed by bucket id.
    """
    bounds = {n["span_id"]: (_parse_ts(n["ts_start"]), _parse_ts(n["ts_end"])) for n in nodes}
    owned: dict[str, dict[str, Any]] = {n["span_id"]: _acc() for n in nodes}
    buckets: dict[str, dict[str, Any]] = {}
    for turn in turns:
        if turn["source"] == "subagent":
            owner_id = _subagent_owner(turn, nodes, bounds)
            if owner_id is not None:
                target = owned[owner_id]
            else:
                target = buckets.setdefault(_UNRESOLVED_KEY, _acc())
        else:
            target = buckets.setdefault(_main_turn_bucket(turn, intervals), _acc())
        _add_turn(target, turn)
    for node in nodes:
        _fill_owned(node, owned[node["span_id"]])
    return buckets


def _subagent_owner(
    turn: dict[str, Any],
    nodes: list[dict[str, Any]],
    bounds: dict[str, tuple[float | None, float | None]],
) -> str | None:
    """The id of the tightest ``agent`` span containing a subagent turn.

    A parallel-agent caveat: with overlapping agent windows the smallest one wins
    by ``span_id`` tie-break — still counted once, just possibly attributed to a
    sibling agent.
    """
    ts = _parse_ts(turn["ts"])
    if ts is None:
        return None
    best_key: tuple[float, float, str] | None = None
    best_id: str | None = None
    for node in nodes:
        if node["kind"] != "agent":
            continue
        start, end = bounds[node["span_id"]]
        if start is None or end is None or not (start <= ts <= end):
            continue
        key = (end - start, start, node["span_id"])
        if best_key is None or key < best_key:
            best_key, best_id = key, node["span_id"]
    return best_id


def _interval_forest(
    nodes: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    buckets: dict[str, dict[str, Any]],
    traces: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the Level-1 interval-bucket roots with their spans nested beneath.

    Each distinct bucket key becomes one root; real spans nest under the bucket
    whose interval contains their ``ts_start`` (clamped to the envelope so a span
    always displays), reusing the time-bracketing + hooks-collapse inner pass.
    Each bucket also carries its ``turns_trace`` — the per-turn token spikes for
    the main turns it owns (Issue #47). ``(unresolved)`` appears only when an
    off-spine turn or span exists.
    """
    windows = _bucket_windows(intervals)
    spans_by_key: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        spans_by_key.setdefault(_span_bucket_key(node, intervals), []).append(node)

    roots: list[dict[str, Any]] = []
    for key, window in windows.items():
        bucket_spans = spans_by_key.get(key, [])
        children = _collapse_hooks(_nest_by_time(bucket_spans))
        roots.append(
            _bucket_node(
                window,
                buckets.get(key),
                children,
                _bucket_todo_label(bucket_spans),
                traces.get(key, []),
            )
        )

    orphan_spans = spans_by_key.get(_UNRESOLVED_KEY, [])
    if _UNRESOLVED_KEY in buckets or orphan_spans:
        children = _collapse_hooks(_nest_by_time(orphan_spans))
        roots.append(
            _unresolved_node(
                buckets.get(_UNRESOLVED_KEY), children, traces.get(_UNRESOLVED_KEY, [])
            )
        )
    roots.sort(key=_bucket_sort_key)
    return roots


def _bucket_traces(
    turns: list[dict[str, Any]], intervals: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Per-bucket main-turn trace: token spikes for the prompt-cycle, by bucket.

    A divider is one main turn's ``{ts, tokens, model}`` read straight from the
    turns relation — never recomputed from spans and never a span itself, so it
    cannot enter the rollup. Subagent turns are excluded (they belong to their
    ``agent`` node, not the main trace). Entries sort by ``ts``.
    """
    traces: dict[str, list[dict[str, Any]]] = {}
    for turn in turns:
        if turn.get("source") != "main":
            continue
        tokens = turn.get("tokens_total")
        if tokens is None:
            tokens = (turn.get("tokens_in") or 0) + (turn.get("tokens_out") or 0)
        traces.setdefault(_main_turn_bucket(turn, intervals), []).append(
            {
                "kind": "turn_divider",
                "ts": turn["ts"],
                "tokens": tokens,
                "model": turn.get("model"),
                "cost_usd": turn.get("cost_usd") or 0.0,
            }
        )
    for entries in traces.values():
        entries.sort(key=lambda e: _parse_ts(e["ts"]) or 0.0)
    return traces


def _bucket_windows(intervals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One display window per bucket key, merging the leading ``setup`` intervals."""
    windows: dict[str, dict[str, Any]] = {}
    for iv in intervals:
        window = windows.get(iv["key"])
        if window is None:
            windows[iv["key"]] = {
                "label": iv["label"],
                "lo_iso": iv["lo_iso"],
                "hi_iso": iv["hi_iso"],
            }
        else:
            window["hi_iso"] = iv["hi_iso"]  # extend setup over its merged intervals
    return windows


def _span_bucket_key(span: dict[str, Any], intervals: list[dict[str, Any]]) -> str:
    """The bucket a span displays under: its interval, clamped into the envelope."""
    if not intervals:
        return _UNRESOLVED_KEY
    ts = _parse_ts(span["ts_start"])
    if ts is None or ts < intervals[0]["lo"]:
        return intervals[0]["key"]
    if ts > intervals[-1]["hi"]:
        return intervals[-1]["key"]
    iv = _interval_containing(ts, intervals)
    return iv["key"] if iv is not None else intervals[-1]["key"]


def _flatten(nodes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from _flatten(node["children"])


def _bucket_node(
    window: dict[str, Any],
    acc: dict[str, Any] | None,
    children: list[dict[str, Any]],
    todo_label: str | None = None,
    turns_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A synthetic phase-interval root owning its main-turn cost; no own duration.

    ``todo_label`` (Issue #47) names the bucket for the in-progress todo it
    advances, falling back to the phase/``setup`` label when none resolved;
    ``turns_trace`` carries the bucket's per-turn token spikes for the trace.
    """
    return _synthetic_root(
        kind="interval",
        name=todo_label or window["label"],
        ts_start=window["lo_iso"],
        ts_end=window["hi_iso"],
        acc=acc or _acc(),
        children=children,
        turns_trace=turns_trace or [],
    )


def _bucket_todo_label(bucket_spans: list[dict[str, Any]]) -> str | None:
    """The todo item a bucket advances: the latest summarised todo span in it.

    A todo span with no derived ``summary`` (no in-progress item resolved) is
    ignored — the bucket then keeps its phase label.
    """
    todos = sorted(
        (n for n in bucket_spans if n["kind"] == "todo" and n.get("summary")),
        key=_sort_key,
    )
    return todos[-1]["summary"] if todos else None


def _unresolved_node(
    acc: dict[str, Any] | None,
    children: list[dict[str, Any]],
    turns_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A synthetic root for turns/spans off the lifecycle envelope, so totals reconcile.

    Off-spine turns never frame a window (a malformed ts must not format as garbage),
    so the node carries no ``ts_start``/``ts_end``.
    """
    return _synthetic_root(
        kind="unresolved",
        name="(unresolved)",
        ts_start=None,
        ts_end=None,
        acc=acc or _acc(),
        children=children,
        turns_trace=turns_trace or [],
    )


def _synthetic_root(
    *,
    kind: str,
    name: str,
    ts_start: str | None,
    ts_end: str | None,
    acc: dict[str, Any],
    children: list[dict[str, Any]],
    turns_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """A synthetic bucket root: owns its main-turn cost, never a phase duration."""
    return {
        "span_id": None,
        "kind": kind,
        "name": name,
        "phase": None,
        "status": _worst_status(list(_flatten(children))),
        "ts_start": ts_start,
        "ts_end": ts_end,
        "duration_ms": None,  # intervals are attribution-only, never a phase width
        "human_type": None,
        "human_wait_ms": None,
        "human_count": 0,
        "own_cost_usd": acc["cost"],
        "own_tokens_in": acc["in"],
        "own_tokens_out": acc["out"],
        "models": sorted(acc["models"]),
        "agent": "main",
        # Per-turn token-spike dividers for the trace (Issue #47); display-only
        # metadata, never a child node, so it can't enter any rollup.
        "turns_trace": turns_trace,
        "children": children,
    }


def _bucket_sort_key(node: dict[str, Any]) -> tuple[float, str]:
    """Order buckets by interval start; ``(unresolved)`` always sorts last."""
    if node["kind"] == "unresolved":
        return (float("inf"), "")
    return (_parse_ts(node["ts_start"]) or 0.0, node["name"])


def format_spoke_label(spoke_run_id: str) -> str:
    """Human dropdown label for a spoke run: ``<branch> · <YYYY-MM-DD>``.

    The raw id is ``<branch>+<spawn-epoch>``; the trailing epoch renders as a UTC
    spawn date while the id stays the lookup key. A malformed id — no ``+`` epoch,
    a non-numeric one, or an epoch outside the platform's timestamp range — falls
    back to the raw id unchanged, so this never raises in a selectbox format_func.
    """
    branch, sep, epoch = spoke_run_id.rpartition("+")
    if not sep or not epoch.isdigit():
        return spoke_run_id
    try:
        date = datetime.fromtimestamp(int(epoch), tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return spoke_run_id
    return f"{branch} · {date}"


def format_step_label(node: dict[str, Any]) -> str:
    """Human label for a v2 spoke node.

    Prefers the node's few-word ``summary`` (Issue #47: the todo it advances, the
    agent's task, a prompt snippet); else ``name · phase``, or ``hooks xN``.
    """
    if node["kind"] == "hooks":
        return f"hooks x{node['collapsed_count']}"
    summary = node.get("summary")
    # A tool leaf keeps its name visible alongside the parameter it acted on, so
    # the trace reads e.g. "Read · /path"; other kinds show the summary alone.
    if node["kind"] == "tool" and summary:
        return f"{node['name']} · {summary}"
    if summary:
        return summary
    if node.get("phase"):
        return f"{node['name']} · {node['phase']}"
    return node["name"]


def format_step_metrics(node: dict[str, Any]) -> dict[str, str]:
    """Display-ready metrics for a v2 spoke node.

    Time is the node's own wall-clock; cost/tokens/models/humans come from the
    rolled-up once-per-turn subtree totals that ``spoke_steps`` attaches to every
    node (cost and models fall back to the node's own only for a node built
    without a rollup). Zero values render as an em dash.
    """
    rollup = node.get("rollup") or {}
    cost = rollup.get("cost_usd", node.get("own_cost_usd", 0.0))
    tokens = rollup.get("tokens_in", 0) + rollup.get("tokens_out", 0)
    models = rollup.get("models") or node.get("models") or []
    humans = rollup.get("human_count", node.get("human_count", 0))
    return {
        "time": _format_secs(node.get("duration_ms")),
        "cost": _format_cost(cost),
        "tokens": f"{tokens:,}" if tokens else "—",
        "model": ", ".join(_short_model(m) for m in models) if models else "—",
        "agent": node.get("agent", "main"),
        "humans": str(humans) if humans else "—",
        "status": node.get("status", ""),
    }


def _format_secs(ms: int | float | None) -> str:
    return "—" if not ms else f"{ms / 1000:.1f}s"


def _format_cost(usd: float | None) -> str:
    return "—" if not usd else f"${usd:.4f}"


def _short_model(model: str) -> str:
    """Drop the ``claude-`` vendor prefix for compact display."""
    return model.removeprefix("claude-")


def _aggregate_by_kind(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group attributed span nodes by ``kind`` into meta-view rows."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_kind.setdefault(node["kind"], []).append(node)
    rows = [_kind_row(kind, group) for kind, group in by_kind.items()]
    rows.sort(key=lambda r: (-r["total_cost_usd"], -r["count"], r["kind"]))
    return rows


def _kind_row(kind: str, group: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [node["duration_ms"] for node in group]
    costs = [node["own_cost_usd"] for node in group]
    models = sorted({model for node in group for model in node["models"]})
    return {
        "kind": kind,
        "count": len(group),
        "total_duration_ms": sum(durations),
        "mean_duration_ms": statistics.mean(durations),
        "median_duration_ms": statistics.median(durations),
        "total_cost_usd": sum(costs),
        "mean_cost_usd": sum(costs) / len(group),
        "models": models,
    }


def _roll_up_steps(node: dict[str, Any]) -> dict[str, Any]:
    """Attach an additive subtree ``rollup`` to ``node`` (post-order).

    A collapsed ``hooks`` node owns no metrics itself — its hook children carry
    them — so summing self + children never double-counts. The returned dict
    carries ``models`` as a set for merging; the node stores it sorted.
    """
    models: set[str] = set(node.get("models") or [])
    human = node["human_count"]
    cost = node.get("own_cost_usd", 0.0)
    tokens_in = node.get("own_tokens_in", 0)
    tokens_out = node.get("own_tokens_out", 0)
    for child in node["children"]:
        child_rollup = _roll_up_steps(child)
        human += child_rollup["human_count"]
        cost += child_rollup["cost_usd"]
        tokens_in += child_rollup["tokens_in"]
        tokens_out += child_rollup["tokens_out"]
        models |= child_rollup["models"]
    node["rollup"] = {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": sorted(models),
    }
    return {
        "human_count": human,
        "cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "models": models,
    }


def _roll_up(node: dict[str, Any]) -> dict[str, int | float]:
    """Attach a ``subtree`` rollup to ``node`` and return it (post-order)."""
    subtree = {
        "duration_ms": node["duration_ms"] or 0,
        "cost_usd": node["cost_usd"] or 0.0,
        "tokens_in": node["tokens_in"] or 0,
        "tokens_out": node["tokens_out"] or 0,
        "human_count": node["human_count"],
    }
    for child in node["children"]:
        child_subtree = _roll_up(child)
        for key, value in child_subtree.items():
            subtree[key] += value
    node["subtree"] = subtree
    return subtree
