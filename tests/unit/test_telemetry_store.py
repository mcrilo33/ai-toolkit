"""Persisted incremental DuckDB store — watermark + delta ingest (Issue #62, RED).

Phase A of the telemetry store roadmap: replace read-time parsing of all historical
session logs (the 146s startup) with a persisted ``store.duckdb`` that is

- **created empty at a watermark** (its init time) and NEVER backfills history — only
  session transcripts modified at/after the watermark are ever parsed;
- **incrementally ingested** — a per-session cursor (``session_file -> mtime/size``)
  skips unchanged files, and span ids are deterministic so re-ingesting a grown
  transcript upserts idempotently (no double-count);
- **rebuildable** by deleting the file — a rebuild re-inits empty at a NEW watermark,
  it is not a historical re-parse.

These tests drive ``telemetry.store.ingest_store`` against the #22 telemetry fixtures
copied into ``tmp_path`` so file mtimes are controllable.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import telemetry.store as store_mod
from telemetry.store import ingest_store, store_watermark

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
SESSION_ID = "11111111-1111-1111-1111-111111111111"
RUN = "feature/22-demo+1700000000"
CCUSAGE = {SESSION_ID: 2.80}

# A fixed "old" mtime for copied session files so the watermark math is deterministic.
_OLD_MTIME = 1_600_000_000.0  # 2020-09-13, far before any plausible watermark


def _projects(tmp_path: Path, *, mtime: float = _OLD_MTIME) -> Path:
    """Copy the fixture projects tree into ``tmp_path`` with a fixed session mtime."""
    root = tmp_path / "projects"
    shutil.copytree(PROJECTS, root)
    for session_file in root.glob("*/*.jsonl"):
        if "subagents" in session_file.parts:
            continue
        import os

        os.utime(session_file, (mtime, mtime))
    return root


def _span_count(store_path: Path) -> int:
    import duckdb

    con = duckdb.connect(str(store_path), read_only=True)
    try:
        row = con.execute("SELECT count(*) FROM spans").fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def _run_span_count(store_path: Path, run: str = RUN) -> int:
    import duckdb

    con = duckdb.connect(str(store_path), read_only=True)
    try:
        row = con.execute("SELECT count(*) FROM spans WHERE spoke_run_id = ?", [run]).fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def test_new_store_records_a_watermark(tmp_path: Path) -> None:
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)

    ingest_store(store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE)

    assert store.exists()
    assert store_watermark(store) is not None


def test_pre_watermark_sessions_are_never_parsed(tmp_path: Path, monkeypatch) -> None:
    # Watermark is set AFTER the (old) session files, so the historical backlog must
    # never be parsed — the whole point of "no backfill".
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path, mtime=_OLD_MTIME)
    parsed: list[Path] = []
    real = store_mod.parse_session_file
    monkeypatch.setattr(store_mod, "parse_session_file", lambda p: parsed.append(p) or real(p))

    ingest_store(
        store,
        events_path=EVENTS,
        projects_root=projects,
        ccusage_costs=CCUSAGE,
        watermark=_OLD_MTIME + 10_000,
    )

    assert parsed == [], "pre-watermark session logs must not be parsed"
    assert _span_count(store) == 0


def test_post_watermark_session_is_ingested(tmp_path: Path) -> None:
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)

    # Watermark before the files → every session is post-watermark and ingests.
    ingest_store(
        store,
        events_path=EVENTS,
        projects_root=projects,
        ccusage_costs=CCUSAGE,
        watermark=0.0,
    )

    assert _run_span_count(store) >= 8, "the post-watermark spoke run must be ingested"


def test_second_open_skips_unchanged_sessions(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )

    parsed: list[Path] = []
    real = store_mod.parse_session_file
    monkeypatch.setattr(store_mod, "parse_session_file", lambda p: parsed.append(p) or real(p))
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )

    assert parsed == [], "an unchanged session must not be re-parsed on the next open"


def test_version_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)
    v1 = ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )
    v2 = ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )

    assert v1 == v2


def test_changed_transcript_reingests_idempotently(tmp_path: Path, monkeypatch) -> None:
    import os

    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )
    before = _span_count(store)

    # Bump the session file's mtime → the cursor sees it as changed and re-parses it.
    session_file = next(
        p for p in projects.glob("*/*.jsonl") if "subagents" not in p.parts and p.stem == SESSION_ID
    )
    parsed: list[Path] = []
    real = store_mod.parse_session_file
    monkeypatch.setattr(store_mod, "parse_session_file", lambda p: parsed.append(p) or real(p))
    os.utime(session_file, (_OLD_MTIME + 5, _OLD_MTIME + 5))
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )

    assert session_file in parsed, "a changed transcript must be re-parsed"
    # Deterministic span ids → re-ingesting the same content never duplicates rows.
    assert _span_count(store) == before


def test_rebuild_reinits_empty_at_a_new_watermark(tmp_path: Path) -> None:
    store = tmp_path / "store.duckdb"
    projects = _projects(tmp_path)
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )
    assert _span_count(store) > 0

    # Rebuild = delete the file and re-init. With a watermark after the (old) files,
    # the rebuild is empty — never a historical re-parse.
    store.unlink()
    for extra in (store.with_suffix(".duckdb.wal"),):
        extra.unlink(missing_ok=True)
    ingest_store(
        store,
        events_path=EVENTS,
        projects_root=projects,
        ccusage_costs=CCUSAGE,
        watermark=_OLD_MTIME + 10_000,
    )

    assert _span_count(store) == 0
    assert store_watermark(store) == _OLD_MTIME + 10_000
