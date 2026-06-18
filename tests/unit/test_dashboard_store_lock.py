"""The dashboard reader renders past a concurrent store write lock (Issue #75, Bug 2).

``app._materialize_store`` reads the persisted store via ``from_persisted_store``, which
ATTACHes the live ``store.duckdb`` read-only. While another instance holds DuckDB's
single-writer lock that attach itself fails, so a second dashboard instance crashed. The
fix routes the read through ``app._materialize_lock_safe``, which falls back to a snapshot
copy when the live file is locked.

This drives that helper directly: build a real store, hold its write lock from a
subprocess, and assert the helper still returns a ``SpanStore`` exposing the spoke.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import duckdb

from _dashboard_helpers import load_app, load_queries

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
EVENTS = FIXTURES / "events.jsonl"
PROJECTS = FIXTURES / "projects"
CCUSAGE = {"11111111-1111-1111-1111-111111111111": 2.80}

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from telemetry.store import ingest_store


def _build_store(tmp_path: Path) -> Path:
    projects = tmp_path / "projects"
    shutil.copytree(PROJECTS, projects)
    store = tmp_path / "store.duckdb"
    ingest_store(
        store, events_path=EVENTS, projects_root=projects, ccusage_costs=CCUSAGE, watermark=0.0
    )
    return store


class _WriteLockHolder:
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
            "print('held', flush=True); time.sleep(30)"
        )
        self._proc = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        assert self._proc.stdout is not None
        assert self._proc.stdout.readline().strip() == "held"
        time.sleep(0.3)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=5)


def _load_app(monkeypatch):
    monkeypatch.setitem(sys.modules, "streamlit", MagicMock())
    monkeypatch.setitem(sys.modules, "queries", load_queries())
    return load_app()


def test_materialize_lock_safe_renders_while_store_is_locked(tmp_path: Path, monkeypatch) -> None:
    store = _build_store(tmp_path)
    queries = load_queries()
    expected_ids = queries.SpanStore.from_persisted_store(store).spoke_run_ids()
    app = _load_app(monkeypatch)

    with _WriteLockHolder(store):
        result = app._materialize_lock_safe(str(store))

    assert result.spoke_run_ids() == expected_ids, "locked read must match the unlocked store"


def test_materialize_lock_safe_uses_live_store_when_unlocked(tmp_path: Path, monkeypatch) -> None:
    store = _build_store(tmp_path)
    app = _load_app(monkeypatch)

    # No lock held → the live file is read directly; still a valid store.
    result = app._materialize_lock_safe(str(store))

    assert result is not None
    # Sanity: the attach raised nothing and the store is queryable.
    assert isinstance(result.spoke_run_ids(), list)


def test_live_store_attach_fails_without_the_fallback(tmp_path: Path) -> None:
    # Guards the premise: a plain read-only attach of the locked live file DOES crash,
    # so the snapshot fallback is load-bearing, not redundant.
    store = _build_store(tmp_path)
    with _WriteLockHolder(store):
        con = duckdb.connect(":memory:")
        try:
            with_error = False
            try:
                con.execute(f"ATTACH '{store}' AS s (READ_ONLY)")
            except duckdb.IOException:
                with_error = True
        finally:
            con.close()
    assert with_error, "a locked live-store attach must raise — the fallback is required"
