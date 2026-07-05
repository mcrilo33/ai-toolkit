"""Unit tests for scripts/telemetry-ingest-spoke.sh — post-run Langfuse ingestion.

worktree-land.sh calls this helper once, best-effort, after the push lands but
before the worktree/tmux teardown, so #87 (loaded-context itemization) populates
automatically for an OTel spoke. The view builder is the ONLY telemetry step —
the transcript backfill (#92) was retired in #140. The helper is factored out
precisely so it can be tested in isolation — never against a real land, never
touching main.

Hermetic, like test_test_select.py: a temp worktree carrying the `.ai-toolkit/`
artifacts worktree-new.sh mints (the `spoke-run-id` file and the `raw-bodies`
dir that exists only under AI_TOOLKIT_OTEL=1) plus a `python3.12` stub on PATH
that logs `RUN <args>` and exits a chosen code. No real python, network, or git.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INGEST = Path(__file__).resolve().parents[2] / "scripts" / "telemetry-ingest-spoke.sh"
SPOKE_RUN_ID = "feature/otel-teardown-ingest+1700000000"
AUTH = "Basic cGstbGYteDpzay1sZi15"  # base64("pk-lf-x:sk-lf-y")


def _make_python_stub(bindir: Path, runlog: Path, *, exit_code: int = 0) -> None:
    """Install a `python3.12` stub on PATH that logs its argv to `runlog`.

    Every invocation appends `RUN PYTHONPATH=<env> <args>` and exits
    `exit_code`, so a test can assert which ingester ran with which
    spoke_run_id / body dir / import path, and that a failing step does not
    propagate out of the best-effort helper.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "python3.12"
    stub.write_text(
        f'#!/bin/sh\nprintf "RUN PYTHONPATH=%s %s\\n" "${{PYTHONPATH:-}}" "$*" >> "{runlog}"\n'
        f"exit {exit_code}\n"
    )
    stub.chmod(0o755)


def _make_repo(tmp_path: Path, *, script_dir: str, git: bool = True) -> Path:
    """A checkout carrying the ingest script at `script_dir` plus the
    telemetry python package at scripts/telemetry/ — the ONLY place it exists.

    Issue #136: `sync_workflow_scripts` ships the .sh files to
    `.ai-toolkit/scripts/` but never the python package, so the script must
    resolve the ingesters relative to the repo checkout, not to itself. With
    `git=False` the layout is a bare non-git install, exercising the
    SCRIPT_DIR-sibling fallback.
    """
    repo = tmp_path / "repo"
    sdir = repo / script_dir
    sdir.mkdir(parents=True)
    shutil.copy(INGEST, sdir / "telemetry-ingest-spoke.sh")
    pkg = repo / "scripts" / "telemetry"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "langfuse_spoke_tree.py").touch()
    if git:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


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
    worktree: Path,
    bindir: Path,
    *,
    auth: str | None = AUTH,
    script: Path = INGEST,
    conf: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
        # Pin the auth-resolver conf: a developer's real ~/.afk-telemetry must
        # never leak credentials into the auth-unset tests.
        "AFK_TELEMETRY_CONF": str(conf) if conf else "/nonexistent/afk-telemetry",
    }
    # A caller PYTHONPATH would suffix the exported one — drop it so the layout
    # tests can assert the resolved import path exactly.
    env.pop("PYTHONPATH", None)
    env.pop("LANGFUSE_BASIC_AUTH", None)
    if auth is not None:
        env["LANGFUSE_BASIC_AUTH"] = auth
    return subprocess.run(
        ["bash", str(script), str(worktree)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_runs_view_builder_only_when_auth_set(worktree: Path, tmp_path: Path) -> None:
    # Arrange
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir)

    # Assert: the view builder is the whole telemetry step — no backfill (#140)
    assert result.returncode == 0, result.stderr
    runs = runlog.read_text().splitlines()
    assert len(runs) == 1, runs
    (tree,) = runs
    assert "langfuse_spoke_tree.py" in tree
    assert SPOKE_RUN_ID in tree
    assert str(worktree / ".ai-toolkit" / "raw-bodies") in tree
    assert "--request-bodies" in tree
    # --root pins the spoke checkout so the disk fallback never measures the hub.
    assert f"--root {worktree}" in tree
    assert "langfuse_backfill" not in tree


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


def test_resolves_auth_from_conf_when_env_unset(worktree: Path, tmp_path: Path) -> None:
    # Arrange: no env credential — the shared ~/.afk-telemetry conf carries it,
    # the way a manual re-run (or a hub session) encounters the script (#136)
    conf = tmp_path / "afk-telemetry"
    conf.write_text(f'LANGFUSE_BASIC_AUTH="{AUTH}"\n')
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir, auth=None, conf=conf)

    # Assert: the script resolves auth itself and runs the view builder
    assert result.returncode == 0, result.stderr
    (tree,) = runlog.read_text().splitlines()
    assert "langfuse_spoke_tree.py" in tree
    assert SPOKE_RUN_ID in tree


def test_best_effort_when_the_step_fails(worktree: Path, tmp_path: Path) -> None:
    # Arrange: the view builder errors out
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog, exit_code=1)

    # Act
    result = _run(worktree, bindir)

    # Assert: a failing step never fails the land
    assert result.returncode == 0, result.stderr
    assert len(runlog.read_text().splitlines()) == 1


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


def test_hub_layout_resolves_package_at_repo_scripts(worktree: Path, tmp_path: Path) -> None:
    # Arrange: the script runs from <repo>/scripts/, the package beside it
    repo = _make_repo(tmp_path, script_dir="scripts")
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir, script=repo / "scripts" / "telemetry-ingest-spoke.sh")

    # Assert: the view builder and import path both resolve inside the checkout
    assert result.returncode == 0, result.stderr
    (tree,) = runlog.read_text().splitlines()
    assert str(repo / "scripts" / "telemetry" / "langfuse_spoke_tree.py") in tree
    assert f"PYTHONPATH={repo / 'scripts'} " in tree


def test_synced_layout_resolves_package_at_repo_scripts(worktree: Path, tmp_path: Path) -> None:
    # Arrange: the synced copy runs from <repo>/.ai-toolkit/scripts/, which has
    # NO telemetry/ subpackage — the package lives at <repo>/scripts/telemetry
    repo = _make_repo(tmp_path, script_dir=".ai-toolkit/scripts")
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(
        worktree, bindir, script=repo / ".ai-toolkit" / "scripts" / "telemetry-ingest-spoke.sh"
    )

    # Assert: resolution follows the repo checkout, never the synced copy (#136)
    assert result.returncode == 0, result.stderr
    (tree,) = runlog.read_text().splitlines()
    assert str(repo / "scripts" / "telemetry" / "langfuse_spoke_tree.py") in tree
    assert ".ai-toolkit/scripts/telemetry/" not in tree
    assert f"PYTHONPATH={repo / 'scripts'} " in tree


def test_non_git_install_falls_back_to_script_sibling(worktree: Path, tmp_path: Path) -> None:
    # Arrange: no git checkout anywhere — the package co-located beside the script
    repo = _make_repo(tmp_path, script_dir="scripts", git=False)
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(worktree, bindir, script=repo / "scripts" / "telemetry-ingest-spoke.sh")

    # Assert: the SCRIPT_DIR-sibling candidate resolves when git introspection can't
    assert result.returncode == 0, result.stderr
    (tree,) = runlog.read_text().splitlines()
    assert str(repo / "scripts" / "telemetry" / "langfuse_spoke_tree.py") in tree
    assert f"PYTHONPATH={repo / 'scripts'} " in tree


def test_skips_when_telemetry_package_missing(worktree: Path, tmp_path: Path) -> None:
    # Arrange: a foreign synced target — no toolkit checkout, no package anywhere
    repo = _make_repo(tmp_path, script_dir=".ai-toolkit/scripts")
    shutil.rmtree(repo / "scripts" / "telemetry")
    bindir, runlog = tmp_path / "bin", tmp_path / "runlog"
    _make_python_stub(bindir, runlog)

    # Act
    result = _run(
        worktree, bindir, script=repo / ".ai-toolkit" / "scripts" / "telemetry-ingest-spoke.sh"
    )

    # Assert: warn-and-skip — never a python call on a path that can't exist
    assert result.returncode == 0, result.stderr
    assert not runlog.exists()
    assert "skipping" in result.stderr


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
