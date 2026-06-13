"""Unit tests for worktree lifecycle telemetry (Issue #21, subtask 3 — RED).

Subtask 3 instruments the worktree scripts so the hub/spoke lifecycle shows up
as spans:

* ``worktree-new.sh`` MINTS a ``spoke_run_id`` (``<branch>+<spawn-epoch>``),
  writes it to ``<worktree>/.ai-toolkit/spoke-run-id`` (so every hook/script
  emitting inside that worktree reads it), and emits a ``lifecycle/spawn`` span.
* ``worktree-land.sh`` emits a ``lifecycle/land`` span.
* ``worktree-done.sh`` emits a ``lifecycle/teardown`` span.

Minting the id is independent of the opt-in gate (the spoke's identity must
exist even if telemetry is turned on later mid-run); span EMISSION stays opt-in,
invisible, and metadata-only (no worktree path leaks into the event).

Hermetic like test_worktree_new.py / test_worktree_land.py: git runs against a
local bare ``origin``; no tmux/code/network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WORKTREE_NEW = SCRIPTS / "worktree-new.sh"
WORKTREE_DONE = SCRIPTS / "worktree-done.sh"
WORKTREE_LAND = SCRIPTS / "worktree-land.sh"

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    # .ai-toolkit/ holds synced runtime state (incl. the minted spoke-run-id) and
    # is gitignored in every real repo, so it never counts as a dirty worktree.
    (hub / ".gitignore").write_text(".ai-toolkit/\n")
    _git(hub, "add", "README.md", ".gitignore")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    return hub


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


def _tele_env(telemetry_dir: Path | None, *, enabled: bool = True) -> dict[str, str]:
    env = {**_GIT_ENV}
    for var in ("AI_TOOLKIT_TELEMETRY", "AI_TOOLKIT_TELEMETRY_DIR", "AI_TOOLKIT_WORKFLOW_REV"):
        env.pop(var, None)
    env.pop("TMUX", None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    if telemetry_dir is not None:
        env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    env["AI_TOOLKIT_WORKFLOW_REV"] = "testrev0"
    return env


def _run(script: Path, hub: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _events(telemetry_dir: Path) -> list[dict]:
    f = telemetry_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _spans(telemetry_dir: Path, *, name: str) -> list[dict]:
    return [e for e in _events(telemetry_dir) if e.get("name") == name]


def _make_spoke(hub: Path, tmp_path: Path, issue: str, slug: str) -> Path:
    """Create a spoke via worktree-new (telemetry OFF so its dir starts clean)."""
    res = _run(
        WORKTREE_NEW,
        hub,
        _tele_env(None, enabled=False),
        issue,
        slug,
        "--no-code",
        "--no-terminal",
    )
    assert res.returncode == 0, res.stderr
    return tmp_path / f"{hub.name}-{issue}"


# ── worktree-new: mint + spawn span ────────────────────────


class TestWorktreeNewSpoke:
    def test_mints_spoke_run_id_file(self, hub: Path, tmp_path: Path) -> None:
        _run(
            WORKTREE_NEW,
            hub,
            _tele_env(None, enabled=False),
            "42",
            "alpha",
            "--no-code",
            "--no-terminal",
        )

        srid = tmp_path / f"{hub.name}-42" / ".ai-toolkit" / "spoke-run-id"
        assert srid.is_file()
        content = srid.read_text().strip()
        assert re.fullmatch(r"feature/42-alpha\+\d+", content), content

    def test_mint_is_independent_of_opt_in(
        self, hub: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        # Telemetry OFF: the id is still minted, but no event log is written.
        _run(
            WORKTREE_NEW,
            hub,
            _tele_env(telemetry_dir, enabled=False),
            "42",
            "alpha",
            "--no-code",
            "--no-terminal",
        )

        srid = tmp_path / f"{hub.name}-42" / ".ai-toolkit" / "spoke-run-id"
        assert srid.is_file()
        assert not (telemetry_dir / "events.jsonl").exists()

    def test_emits_spawn_lifecycle_span(self, hub: Path, telemetry_dir: Path) -> None:
        _run(
            WORKTREE_NEW,
            hub,
            _tele_env(telemetry_dir),
            "42",
            "alpha",
            "--no-code",
            "--no-terminal",
        )

        spans = _spans(telemetry_dir, name="worktree-new")
        assert len(spans) == 1
        span = spans[0]
        assert span["kind"] == "lifecycle"
        assert span["phase"] == "spawn"
        assert span["status"] == "success"

    def test_spawn_span_carries_minted_spoke_run_id_and_branch(
        self, hub: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        _run(
            WORKTREE_NEW,
            hub,
            _tele_env(telemetry_dir),
            "42",
            "alpha",
            "--no-code",
            "--no-terminal",
        )

        srid = (tmp_path / f"{hub.name}-42" / ".ai-toolkit" / "spoke-run-id").read_text().strip()
        span = _spans(telemetry_dir, name="worktree-new")[0]
        assert span["spoke_run_id"] == srid
        assert span["branch"] == "feature/42-alpha"

    def test_spawn_span_leaks_no_worktree_path(
        self, hub: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        _run(
            WORKTREE_NEW,
            hub,
            _tele_env(telemetry_dir),
            "42",
            "alpha",
            "--no-code",
            "--no-terminal",
        )

        content = (telemetry_dir / "events.jsonl").read_text()
        wt_dir = str(tmp_path / f"{hub.name}-42")
        assert wt_dir not in content


# ── worktree-done: teardown span ───────────────────────────


class TestWorktreeDoneSpoke:
    def test_emits_teardown_lifecycle_span(
        self, hub: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        wt = _make_spoke(hub, tmp_path, "7", "beta")
        srid = (wt / ".ai-toolkit" / "spoke-run-id").read_text().strip()

        res = _run(WORKTREE_DONE, hub, _tele_env(telemetry_dir), "7", "--no-code", "--force")

        assert res.returncode == 0, res.stderr
        spans = _spans(telemetry_dir, name="worktree-done")
        assert len(spans) == 1
        assert spans[0]["kind"] == "lifecycle"
        assert spans[0]["phase"] == "teardown"
        assert spans[0]["spoke_run_id"] == srid

    def test_done_succeeds_when_repo_gitignore_omits_ai_toolkit(self, tmp_path: Path) -> None:
        # A repo whose COMMITTED .gitignore does not list .ai-toolkit/. The minted
        # spoke-run-id must still not count as an untracked change — worktree-new
        # adds .ai-toolkit/ to the repo's git exclude — so a plain (no --force)
        # teardown succeeds instead of failing on `git worktree remove` (exit 128).
        remote = tmp_path / "remote.git"
        hub = tmp_path / "hub"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)],
            check=True,
            capture_output=True,
            env=_GIT_ENV,
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(hub)],
            check=True,
            capture_output=True,
            env=_GIT_ENV,
        )
        for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            _git(hub, "config", k, v)
        (hub / "README.md").write_text("seed\n")
        (hub / ".gitignore").write_text("*.log\n")  # deliberately NOT .ai-toolkit/
        _git(hub, "add", "README.md", ".gitignore")
        _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
        _git(hub, "remote", "add", "origin", str(remote))
        _git(hub, "push", "-q", "-u", "origin", "main")

        new = _run(
            WORKTREE_NEW,
            hub,
            _tele_env(None, enabled=False),
            "5",
            "delta",
            "--no-code",
            "--no-terminal",
        )
        assert new.returncode == 0, new.stderr

        done = _run(WORKTREE_DONE, hub, _tele_env(None, enabled=False), "5", "--no-code")
        assert done.returncode == 0, done.stderr


# ── worktree-land: land span ───────────────────────────────


class TestWorktreeLandSpoke:
    def test_emits_land_lifecycle_span(
        self, hub: Path, tmp_path: Path, telemetry_dir: Path
    ) -> None:
        # A spoke with one pushed commit and the ready marker so land proceeds.
        wt = _make_spoke(hub, tmp_path, "9", "gamma")
        (wt / "f.txt").write_text("work\n")
        _git(wt, "add", "f.txt")
        _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #9")
        _git(wt, "push", "-q", "-u", "origin", "feature/9-gamma")
        _git(wt, "tag", "ready/9")
        _git(wt, "push", "-q", "origin", "ready/9")

        # Stub gh so the issue-close is a no-op; skip the suite.
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        for name in ("gh", "code"):
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        env = _tele_env(telemetry_dir)
        env["PATH"] = f"{bindir}:{os.environ['PATH']}"
        suite = bindir / "suite"
        suite.write_text("#!/bin/sh\nexit 0\n")
        suite.chmod(0o755)

        res = _run(WORKTREE_LAND, hub, env, "9", "--skip-tests")

        assert res.returncode == 0, res.stderr
        spans = _spans(telemetry_dir, name="worktree-land")
        assert len(spans) == 1
        assert spans[0]["kind"] == "lifecycle"
        assert spans[0]["phase"] == "land"
