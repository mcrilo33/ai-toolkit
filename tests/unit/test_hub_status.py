"""Unit tests for shared/skills/hub/scripts/hub-status.sh.

The dashboard's worktree state label drives the hub's merge proposals, so the
push/mergeable classification must be correct: push state is measured against
the branch's own upstream, mergeability against the default branch. A `gh` stub
keeps the issue-survey section hermetic (no network, no real GitHub remote).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HUB_STATUS = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-status.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub_with_spokes(tmp_path: Path) -> Path:
    """A hub (main checkout) with two spoke worktrees: one fully pushed and one
    with a local-only commit."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")

    # Pushed spoke: a commit on its branch, pushed to its upstream.
    pushed = tmp_path / "pushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/1-pushed", str(pushed))
    (pushed / "a.txt").write_text("a\n")
    _git(pushed, "add", "a.txt")
    _git(pushed, "commit", "-qm", "feat: a", "-m", "Refs #1")
    _git(pushed, "push", "-q", "-u", "origin", "feature/1-pushed")

    # Unpushed spoke: a local-only commit, no upstream.
    unpushed = tmp_path / "unpushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/2-unpushed", str(unpushed))
    (unpushed / "b.txt").write_text("b\n")
    _git(unpushed, "add", "b.txt")
    _git(unpushed, "commit", "-qm", "feat: b", "-m", "Refs #2")
    return hub


def _run_hub_status(hub: Path, tmp_path: Path) -> str:
    """Run hub-status.sh from the hub with a `gh` stub on PATH (exits 1, so the
    issue survey degrades to '(none open)' without touching the network)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)  # skip the tmux section
    return subprocess.run(
        ["bash", str(HUB_STATUS)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def test_pushed_spoke_is_mergeable(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "pushed → mergeable" in line


def test_unpushed_spoke_is_unpushed(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/2-unpushed" in ln)
    assert "unpushed" in line


def test_hub_branch_labelled_hub(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("main"))
    assert "(hub)" in line
