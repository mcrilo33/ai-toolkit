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
from datetime import datetime
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
from telemetry.session_parser import UsageEvent, parse_session_file
from telemetry.spans import Span
from telemetry.spoke_runs import backfill_spoke_run_ids

_COLUMN_NAMES: tuple[str, ...] = tuple(col.split(" ", 1)[0] for col in _COLUMNS)
_WATERMARK_KEY = "watermark"


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the store's tables + the unified ``spans`` view if absent (idempotent).

    Spans live in two partitions with distinct lifecycles:

    - ``pull_spans`` — parsed from session transcripts, ingested per session on a
      cursor (a transcript only grows, so a session's pull spans are stable between
      opens);
    - ``push_spans`` — read wholesale from the ``events.jsonl`` WAL on every ingest,
      because the WAL and the transcripts grow independently (a hook can append a push
      span — e.g. the push-gate ``step`` — long after the transcript settles), and
      ``lifecycle``/``script`` spans from standalone scripts carry no ``session_id`` at
      all, so they cannot be keyed to a transcript.

    The ``spans`` view is their union — every query reads it exactly as the old
    single-table dataset.
    """
    con.execute(f"CREATE TABLE IF NOT EXISTS pull_spans ({', '.join(_COLUMNS)})")
    con.execute(f"CREATE TABLE IF NOT EXISTS push_spans ({', '.join(_COLUMNS)})")
    con.execute(
        "CREATE OR REPLACE VIEW spans AS "
        "SELECT * FROM pull_spans UNION ALL SELECT * FROM push_spans"
    )
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


def _load_linked_push_spans(events_path: Path) -> list[Span]:
    """All WAL push spans with the script→marker emission link resolved.

    Emission is per-spoke-run and can cross sessions, so it is linked over the whole
    push set (read in full from the append-only WAL — cheap) before the spans are used.
    """
    push_spans = _load_push_spans(events_path)
    _link_emissions(push_spans)
    return push_spans


def _push_peers_by_session(push_spans: list[Span]) -> dict[str, list[Span]]:
    """Session-bearing push spans grouped by ``session_id`` — the backfill peers.

    A pull span recovers its null ``spoke_run_id`` from a push span sharing its
    ``session_id``; sessionless push spans cannot be a peer, so they are omitted here
    (they are still persisted by :func:`_refresh_push_spans`).
    """
    by_session: dict[str, list[Span]] = {}
    for span in push_spans:
        if span.session_id is not None:
            by_session.setdefault(span.session_id, []).append(span)
    return by_session


def _insert_spans(con: duckdb.DuckDBPyConnection, table: str, span_dicts: list[dict]) -> None:
    if not span_dicts:
        return
    placeholders = ", ".join("?" for _ in _COLUMN_NAMES)
    con.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})", [_row(span) for span in span_dicts]
    )


def _upsert_session(
    con: duckdb.DuckDBPyConnection,
    session_id: str,
    span_dicts: list[dict[str, object]],
    turn_rows: list[dict[str, object]],
) -> None:
    """Replace one session's PULL spans + turns (delete-then-insert, idempotent by id)."""
    con.execute("DELETE FROM pull_spans WHERE session_id = ?", [session_id])
    con.execute("DELETE FROM turns WHERE session_id = ?", [session_id])
    _insert_spans(con, "pull_spans", span_dicts)
    if turn_rows:
        placeholders = ", ".join("?" for _ in _TURN_FIELDS)
        con.executemany(
            f"INSERT INTO turns VALUES ({placeholders})",
            [tuple(turn[field] for field in _TURN_FIELDS) for turn in turn_rows],
        )


def _correlate_session(
    session_file: Path,
    push_peers: list[Span],
    ccusage_costs: dict[str, float],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Parse + correlate one session's PULL spans into ``(span_dicts, turn_rows)``.

    Per-session correlation is identical to the whole-dataset parse: spoke_run_id is
    backfilled from a same-session push peer, tokens/cost are attributed from this
    session's own usage + ccusage rate, and turns are the once-per-turn rows. The push
    peers are used only to backfill the pull spans' run id — they are persisted
    separately (wholesale), so they are not returned here.
    """
    parsed = parse_session_file(session_file)
    backfill_spoke_run_ids(parsed.spans + push_peers)
    attribute_spans(
        parsed.spans, parsed.usage_events, ccusage_costs, agent_links=parsed.agent_links
    )
    turns = per_turn_rows(parsed.usage_events, ccusage_costs, reasoning_refs=parsed.reasoning_refs)
    return [span.to_dict() for span in parsed.spans], turns


def _usage_events_from_turns(con: duckdb.DuckDBPyConnection) -> list[UsageEvent]:
    """Reconstruct per-turn usage from the persisted ``turns`` table.

    Push-span attribution brackets a session's main turns; the ``turns`` table already
    carries every turn's tokens + cache breakdown, so it is reused as the usage source
    rather than re-parsing the transcripts.
    """
    rows = con.execute(
        "SELECT session_id, ts, model, source, agent_id, tokens_in, tokens_out, "
        "cache_read, cache_creation FROM turns"
    ).fetchall()
    return [
        UsageEvent(
            session_id=r[0],
            ts=r[1],
            model=r[2],
            source=r[3],
            agent_id=r[4],
            input_tokens=r[5] or 0,
            output_tokens=r[6] or 0,
            cache_read=r[7] or 0,
            cache_creation=r[8] or 0,
        )
        for r in rows
    ]


def _epoch(ts_iso: str | None) -> float | None:
    """ISO-8601 timestamp → epoch seconds, or ``None`` when absent/malformed."""
    if not ts_iso:
        return None
    try:
        return datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _refresh_push_spans(
    con: duckdb.DuckDBPyConnection,
    push_spans: list[Span],
    ccusage_costs: dict[str, float],
    watermark: float,
) -> None:
    """Replace the whole push partition from the WAL (attributed) on every ingest.

    The WAL grows independently of the transcripts, so push spans are re-applied
    wholesale rather than gated on a session cursor — this keeps post-settle push spans
    (e.g. the push gate) and sessionless ``lifecycle``/``script`` spans present. Only
    spans emitted at/after the watermark are kept, by ``ts_start`` (sessionless spans
    have no transcript mtime to test), so a pre-watermark spoke never resurfaces as a
    push-only ghost. Tokens are bracketed from the persisted turns; a sessionless span
    brackets nothing and so stays at zero tokens, exactly as the old parse produced.
    """
    post = [s for s in push_spans if (e := _epoch(s.ts_start)) is not None and e >= watermark]
    attribute_spans(post, _usage_events_from_turns(con), ccusage_costs)
    con.execute("DELETE FROM push_spans")
    _insert_spans(con, "push_spans", [span.to_dict() for span in post])


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

        push_spans = _load_linked_push_spans(events_path)
        push_peers = _push_peers_by_session(push_spans)
        cursor = {
            row[0]: (row[1], row[2])
            for row in con.execute(
                "SELECT session_file, mtime_ns, size FROM ingest_cursor"
            ).fetchall()
        }

        # Pull spans: parse + correlate only post-watermark transcripts whose cursor moved.
        for session_file in _session_files(projects_root):
            stat = session_file.stat()
            if stat.st_mtime < wm:  # pre-watermark backlog: never parsed
                continue
            key = str(session_file)
            if cursor.get(key) == (stat.st_mtime_ns, stat.st_size):
                continue  # unchanged since last ingest
            session_id = session_file.stem
            span_dicts, turn_rows = _correlate_session(
                session_file, push_peers.get(session_id, []), ccusage_costs
            )
            _upsert_session(con, session_id, span_dicts, turn_rows)
            con.execute("DELETE FROM ingest_cursor WHERE session_file = ?", [key])
            con.execute(
                "INSERT INTO ingest_cursor VALUES (?, ?, ?)",
                [key, stat.st_mtime_ns, stat.st_size],
            )

        # Push spans: re-applied wholesale from the WAL (attributed off the persisted
        # turns) every open, independent of the transcript cursor.
        _refresh_push_spans(con, push_spans, ccusage_costs, wm)

        _create_views(con)
        return _version(con, events_path, ccusage_costs)
    finally:
        con.close()
