"""Persisted, incrementally-materialized telemetry store (Issue #62, Phase A).

A CQRS read model: the correlated push+pull span dataset is materialized once into a
persisted DuckDB at ``~/.ai-toolkit/telemetry/store.duckdb`` instead of being re-parsed
from every historical session log on each dashboard open (the old 146s startup). The
dashboard then attaches this store read-only and queries it.

**No migration / no backfill.** The store is created *empty at a watermark* — its
init timestamp — and only session transcripts modified at/after that watermark are ever
parsed. The 252 MB / 748-file historical backlog is never read; that is what removes the
146s entirely. Consequence (accepted, not a bug): spokes that ran before the store
existed do not appear; the store populates as new spokes run.

Ingest is incremental and idempotent:

- a per-session cursor (``session_file -> mtime_ns/size``) skips unchanged transcripts;
- span ids are deterministic (:func:`telemetry.spans.derive_span_id`), so re-ingesting a
  grown transcript replaces that session's rows without double-counting;
- ``events.jsonl`` stays the append-only WAL — push spans are read from it in full each
  ingest (cheap) and the script→marker emission link is computed over them globally, so a
  cross-session emission inside a spoke run stays correct.

Correlation (spoke_run_id backfill, token/cost attribution, per-turn rows) is genuinely
per-session, so re-correlating one changed session yields byte-identical results to the
old whole-dataset parse — see ``tests/unit/test_dashboard_persisted_store.py``.

Rebuilding the store means deleting the ``.duckdb`` file and re-initialising empty at a
new watermark — never a historical re-parse.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import duckdb

from telemetry.cost import attribute_spans, per_turn_rows
from telemetry.queries import (
    _COLUMNS,
    _TURN_COLUMNS,
    _TURN_FIELDS,
    _create_views,
    _link_emissions,
    _load_push_spans,
    _row,
)
from telemetry.session_parser import parse_session_file
from telemetry.spans import Span
from telemetry.spoke_runs import backfill_spoke_run_ids

_COLUMN_NAMES: tuple[str, ...] = tuple(col.split(" ", 1)[0] for col in _COLUMNS)
_WATERMARK_KEY = "watermark"


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the store's tables if absent (idempotent)."""
    con.execute(f"CREATE TABLE IF NOT EXISTS spans ({', '.join(_COLUMNS)})")
    con.execute(f"CREATE TABLE IF NOT EXISTS turns ({', '.join(_TURN_COLUMNS)})")
    con.execute("CREATE TABLE IF NOT EXISTS session_costs (session_id VARCHAR, cost_usd DOUBLE)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS ingest_cursor "
        "(session_file VARCHAR PRIMARY KEY, mtime_ns BIGINT, size BIGINT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS store_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")


def _get_watermark(con: duckdb.DuckDBPyConnection) -> float | None:
    row = con.execute("SELECT value FROM store_meta WHERE key = ?", [_WATERMARK_KEY]).fetchone()
    return float(row[0]) if row else None


def store_watermark(store_path: str | Path) -> float | None:
    """The store's init watermark (epoch seconds), or ``None`` if uninitialised."""
    if not Path(store_path).exists():
        return None
    con = duckdb.connect(str(store_path), read_only=True)
    try:
        return _get_watermark(con)
    finally:
        con.close()


def _session_files(projects_root: str | Path) -> list[Path]:
    """Every ``<slug>/<session>.jsonl`` main transcript (subagent logs excluded)."""
    return sorted(p for p in Path(projects_root).glob("*/*.jsonl") if "subagents" not in p.parts)


def _push_by_session(events_path: Path) -> dict[str, list[Span]]:
    """Push spans (WAL) with emissions linked, grouped by ``session_id``.

    Emission is linked over ALL push spans first (it is per-spoke-run and can cross
    sessions), then the spans are bucketed by session so each is ingested alongside the
    session that owns it. Push spans with no ``session_id`` (ad-hoc, sessionless) are
    dropped — the store holds only post-watermark spoke activity, keyed by session.
    """
    push_spans = _load_push_spans(events_path)
    _link_emissions(push_spans)
    by_session: dict[str, list[Span]] = {}
    for span in push_spans:
        if span.session_id is None:
            continue
        by_session.setdefault(span.session_id, []).append(span)
    return by_session


def _upsert_session(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    span_dicts: list[dict[str, object]],
    turn_rows: list[dict[str, object]],
) -> None:
    """Replace one session's spans + turns (delete-then-insert, idempotent by id)."""
    con.execute("DELETE FROM spans WHERE session_id = ?", [session_id])
    con.execute("DELETE FROM turns WHERE session_id = ?", [session_id])
    if span_dicts:
        placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
        con.executemany(
            f"INSERT INTO spans VALUES ({placeholders})",
            [_row(span) for span in span_dicts],
        )
    if turn_rows:
        placeholders = ", ".join("?" for _ in _TURN_FIELDS)
        con.executemany(
            f"INSERT INTO turns VALUES ({placeholders})",
            [tuple(turn[field] for field in _TURN_FIELDS) for turn in turn_rows],
        )


def _correlate_session(
    session_file: Path,
    push_spans: list[Span],
    ccusage_costs: dict[str, float],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Parse + correlate one session into ``(span_dicts, turn_rows)``.

    Per-session correlation is identical to the whole-dataset parse: spoke_run_id is
    backfilled from a same-session push peer, tokens/cost are attributed from this
    session's own usage + ccusage rate, and turns are the once-per-turn rows.
    """
    parsed = parse_session_file(session_file)
    spans = parsed.spans + push_spans
    backfill_spoke_run_ids(spans)
    attribute_spans(spans, parsed.usage_events, ccusage_costs, agent_links=parsed.agent_links)
    turns = per_turn_rows(parsed.usage_events, ccusage_costs, reasoning_refs=parsed.reasoning_refs)
    return [span.to_dict() for span in spans], turns


def _version(
    con: duckdb.DuckDBPyConnection, events_path: Path, ccusage_costs: dict[str, float]
) -> str:
    """A content token that changes iff the materialized store could have changed.

    Folds the watermark, the WAL mtime, the ccusage cost map, and the per-session
    cursor state — so a dashboard can cache the in-memory read model on it and rebuild
    only on a real delta.
    """
    cursor = con.execute(
        "SELECT session_file, mtime_ns, size FROM ingest_cursor ORDER BY session_file"
    ).fetchall()
    events_mtime = events_path.stat().st_mtime_ns if events_path.exists() else 0
    digest = hashlib.sha1()
    digest.update(repr(_get_watermark(con)).encode())
    digest.update(repr(events_mtime).encode())
    digest.update(repr(sorted(ccusage_costs.items())).encode())
    digest.update(repr(cursor).encode())
    return digest.hexdigest()


def ingest_store(
    store_path: str | Path,
    *,
    events_path: str | Path,
    projects_root: str | Path,
    ccusage_costs: dict[str, float] | None = None,
    watermark: float | None = None,
) -> str:
    """Bring the persisted store at ``store_path`` up to date; return a content version.

    Creates the store empty at a watermark on first call (``watermark`` overrides the
    default init time, for tests / rebuilds), then ingests only the delta: session
    transcripts modified at/after the watermark whose cursor fingerprint changed. The
    historical backlog is never parsed.

    Args:
        store_path: Path to the persisted ``store.duckdb`` (created if absent).
        events_path: Telemetry ``events.jsonl`` (push-span WAL).
        projects_root: Claude ``projects`` root holding session transcripts.
        ccusage_costs: Map of ``session_id`` to ccusage ``totalCost`` (refreshed each
            ingest); defaults to empty (tokens attributed, cost left null).
        watermark: Override the init watermark (epoch seconds). Ignored once the store
            already records one.

    Returns:
        A version token that changes only when the materialized store could have.
    """
    ccusage_costs = ccusage_costs or {}
    events_path = Path(events_path)
    store_path = Path(store_path)
    store_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(store_path))
    try:
        _ensure_schema(con)
        if _get_watermark(con) is None:
            wm = watermark if watermark is not None else time.time()
            con.execute("INSERT INTO store_meta VALUES (?, ?)", [_WATERMARK_KEY, repr(wm)])
        wm = _get_watermark(con)
        assert wm is not None  # just set if it was missing

        # ccusage costs change between opens; refresh them wholesale (cheap).
        con.execute("DELETE FROM session_costs")
        if ccusage_costs:
            con.executemany("INSERT INTO session_costs VALUES (?, ?)", list(ccusage_costs.items()))

        push_by_session = _push_by_session(events_path)
        cursor = {
            row[0]: (row[1], row[2])
            for row in con.execute(
                "SELECT session_file, mtime_ns, size FROM ingest_cursor"
            ).fetchall()
        }

        for session_file in _session_files(projects_root):
            stat = session_file.stat()
            if stat.st_mtime < wm:  # pre-watermark backlog: never parsed
                continue
            key = str(session_file)
            if cursor.get(key) == (stat.st_mtime_ns, stat.st_size):
                continue  # unchanged since last ingest
            session_id = session_file.stem
            span_dicts, turn_rows = _correlate_session(
                session_file, push_by_session.get(session_id, []), ccusage_costs
            )
            _upsert_session(con, session_id, span_dicts, turn_rows)
            con.execute("DELETE FROM ingest_cursor WHERE session_file = ?", [key])
            con.execute(
                "INSERT INTO ingest_cursor VALUES (?, ?, ?)",
                [key, stat.st_mtime_ns, stat.st_size],
            )

        _create_views(con)
        return _version(con, events_path, ccusage_costs)
    finally:
        con.close()
