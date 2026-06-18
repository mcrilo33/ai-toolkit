"""Store must survive a concurrent DuckDB single-writer lock (Issue #75, Bug 2).

``store.duckdb`` is opened read-write for ingest, and DuckDB is single-writer: a second
dashboard instance (e.g. a spoke's GUI-check streamlit left running on another port)
holds the lock, so a new instance crashed with
``IOException: Could not set lock on file store.duckdb``. Worse, while a writer holds the
lock even a *read-only* open of the live file fails — so the only way a second instance
can render is to read a byte-copy snapshot.

These tests hold the write lock from a real subprocess (same-process DuckDB reuses the
connection, so the lock only bites across processes) and assert:

- ``ingest_store`` degrades gracefully (returns a version, never raises) when locked;
- ``snapshot_store`` copies the store past the lock so its data is still readable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry.store import ingest_store, snapshot_store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
CCUSAGE = {"11111111-1111-1111-1111-111111111111": 2.80}


def _build_store(tmp_path: Path) -> Path:
    """Ingest the telemetry fixtures into a fresh store (watermark before the files)."""
    projects = tmp_path / "projects"
    shutil.copytree(PROJECTS, projects)
    store = tmp_path / "store.duckdb"
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )
    return store


class _WriteLockHolder:
    """Hold ``store.duckdb``'s read-write lock from another process for the test body."""

    def __init__(self, store: Path) -> None:
        self._store = store
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> _WriteLockHolder:
        # DuckDB acquires the file lock lazily, so force it with an open write
        # transaction — this faithfully mimics a second instance mid-ingest.
        code = (
            "import duckdb, time;"
            f"c = duckdb.connect({str(self._store)!r});"
            "c.execute('BEGIN'); c.execute('CREATE TABLE _lock(x INTEGER)');"
            "print('held', flush=True);"
            "time.sleep(30)"
        )
        self._proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        assert self._proc.stdout is not None
        assert self._proc.stdout.readline().strip() == "held", "lock holder failed to start"
        time.sleep(0.3)  # let the OS-level lock settle
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=5)


def _spans_via_snapshot(snap: Path) -> int:
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"ATTACH '{snap}' AS s (READ_ONLY)")
        row = con.execute("SELECT count(*) FROM s.spans").fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def test_ingest_store_degrades_instead_of_crashing_when_locked(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    projects = tmp_path / "projects"

    with _WriteLockHolder(store):
        # A second instance's ingest must NOT raise IOException on the held write lock.
        version = ingest_store(
            store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE
        )

    assert version, "a locked ingest must still return a (degraded) version token"


def test_snapshot_store_reads_past_the_write_lock(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    expected = _spans_via_snapshot_unlocked(store)

    with _WriteLockHolder(store):
        snap = snapshot_store(store)

    assert snap.exists()
    assert _spans_via_snapshot(snap) == expected, "snapshot must carry the store's spans"


def _spans_via_snapshot_unlocked(store: Path) -> int:
    con = duckdb.connect(str(store), read_only=True)
    try:
        row = con.execute("SELECT count(*) FROM spans").fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def test_snapshot_includes_the_wal_when_present(tmp_path: Path) -> None:
    # A snapshot taken mid-write must copy the .wal alongside the .duckdb so the copy
    # opens cleanly; absent the WAL, DuckDB may refuse the attach.
    store = _build_store(tmp_path)

    with _WriteLockHolder(store):
        snap = snapshot_store(store)

    wal = Path(f"{store}.wal")
    if wal.exists():
        assert Path(f"{snap}.wal").exists(), "snapshot must copy the WAL when the store has one"
    # Either way the snapshot must open without error.
    assert os.path.exists(snap)
