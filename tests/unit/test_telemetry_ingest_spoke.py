"""Unit tests for scripts/telemetry-ingest-spoke.sh — post-run Langfuse ingestion.

worktree-land.sh calls this helper once, best-effort, after the push lands but
before the worktree/tmux teardown, so #87 (loaded-context itemization) and #92
(transcript backfill) populate automatically for an OTel spoke. The helper is
factored out precisely so it can be tested in isolation — never against a real
land, never touching main.

Hermetic, like test_test_select.py: a temp worktree carrying the `.ai-toolkit/`
artifacts worktree-new.sh mints (the `spoke-run-id` file and the `raw-bodies`
dir that exists only under AI_TOOLKIT_OTEL=1) plus a `python3.12` stub on PATH
that logs `RUN <args>` and exits a chosen code. No real python, network, or git.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INGEST = Path(__file__).resolve().parents[2] / "scripts" / "telemetry-ingest-spoke.sh"
SPOKE_RUN_ID = "feature/otel-teardown-ingest+1700000000"
AUTH = "Basic cGstbGYteDpzay1sZi15"  # base64("pk-lf-x:sk-lf-y")


def _make_python_stub(bindir: Path, runlog: Path, *, exit_code: int = 0) -> None:
    """Install a `python3.12` stub on PATH that logs its argv to `runlog`.

    Every invocation appends `RUN <args>` and exits `exit_code`, so a test can
    assert which ingester ran with which spoke_run_id / body dir, and that a
    failing step does not propagate out of the best-effort helper.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "python3.12"
    stub.write_text(f'#!/bin/sh\nprintf "RUN %s\\n" "$*" >> "{runlog}"\nexit {exit_code}\n')
    stub.chmod(0o755)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """An OTel spoke worktree: `.ai-toolkit/spoke-run-id` + a `raw-bodies` dir."""
    wt = tmp_path / "wt"
    ait = wt / ".ai-toolkit"
    ait.mkdir(parents=True)
    (ait / "spoke-run-id").write_text(SPOKE_RUN_ID + "\n")
    (ait / "raw-bodies").mkdir()
    return wt


def _run(
    worktree: Path, bindir: Path, *, auth: str | None = AUTH
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
    }
    env.pop("LANGFUSE_BASIC_AUTH", None)
    if auth is not None:
        env["LANGFUSE_BASIC_AUTH"] = auth
    return subprocess.run(
        ["bash", str(INGEST), str(worktree)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_runs_both_ingesters_when_auth_set(worktree: Path, tmp_path: Path) -> None:
    # Arrange
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir)

    # Assert
    assert result.returncode == 0, result.stderr
    runs = runlog.read_text().splitlines()
    assert len(runs) == 2, runs
    tree, backfill = runs
    assert "langfuse_spoke_tree.py" in tree
    assert SPOKE_RUN_ID in tree
    assert str(worktree / ".ai-toolkit" / "raw-bodies") in tree
    assert "--request-bodies" in tree
    # --root pins the spoke checkout so the disk fallback never measures the hub.
    assert f"--root {worktree}" in tree
    assert "langfuse_backfill.py" in backfill
    assert SPOKE_RUN_ID in backfill
    assert "--thinking" in backfill


def test_skips_when_auth_unset(worktree: Path, tmp_path: Path) -> None:
    # Arrange
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir, auth=None)

    # Assert: no python call, a one-line skip notice, land not failed
    assert result.returncode == 0, result.stderr
    assert not runlog.exists()
    assert "LANGFUSE_BASIC_AUTH" in (result.stdout + result.stderr)


def test_best_effort_when_a_step_fails(worktree: Path, tmp_path: Path) -> None:
    # Arrange: the ingesters error out
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog, exit_code=1)

    # Act
    result = _run(worktree, bindir)

    # Assert: a failing step never fails the land; both are still attempted
    assert result.returncode == 0, result.stderr
    assert len(runlog.read_text().splitlines()) == 2


def test_skips_non_otel_spoke(worktree: Path, tmp_path: Path) -> None:
    # Arrange: no raw-bodies dir → not an AI_TOOLKIT_OTEL spoke
    (worktree / ".ai-toolkit" / "raw-bodies").rmdir()
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir)

    # Assert
    assert result.returncode == 0, result.stderr
    assert not runlog.exists()


def test_skips_when_spoke_run_id_missing(worktree: Path, tmp_path: Path) -> None:
    # Arrange: the id file the ingesters key on is gone
    (worktree / ".ai-toolkit" / "spoke-run-id").unlink()
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir)

    # Assert
    assert result.returncode == 0, result.stderr
    assert not runlog.exists()
