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
    ("tokens_in", "BIGINT"),
    ("tokens_out", "BIGINT"),
    ("cost_usd", "DOUBLE"),
)
_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _COLUMNS)


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
    def from_events(cls, events: list[dict[str, Any]]) -> SpanStore:
        """Build an in-memory store from already-parsed span dicts."""
        con = duckdb.connect(":memory:")
        ddl = ", ".join(f"{name} {sqltype}" for name, sqltype in _COLUMNS)
        con.execute(f"CREATE TABLE spans ({ddl})")
        if events:
            placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
            con.executemany(
                f"INSERT INTO spans VALUES ({placeholders})",
                [_row_tuple(span) for span in events],
            )
        return cls(con)

    @classmethod
    def from_jsonl(cls, path: str | Path) -> SpanStore:
        """Build an in-memory store from a raw ``events.jsonl`` span log."""
        return cls.from_events(load_jsonl(path))

    @classmethod
    def from_connection(cls, con: duckdb.DuckDBPyConnection) -> SpanStore:
        """Wrap an existing connection that already exposes a ``spans`` relation.

        This is the seam for Issue #22: once its parser publishes correlated
        ``spans`` views, hand that connection here instead of re-ingesting JSONL.
        """
        return cls(con)

    def close(self) -> None:
        self.con.close()

    def _query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run SQL and return rows as column-keyed dicts."""
        cursor = self.con.execute(sql, params or [])
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def spoke_run_ids(self) -> list[str]:
        """All known ``spoke_run_id``s, sorted."""
        rows = self._query(
            "SELECT DISTINCT spoke_run_id FROM spans "
            "WHERE spoke_run_id IS NOT NULL ORDER BY spoke_run_id"
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
