"""Unit tests for scripts/worktree-done.sh teardown completeness.

Teardown must be a clean mirror of creation: remove the worktree, fold the folder
out of the VS Code review window (`code --remove`, the inverse of worktree-new's
`code --add`), and prune the branch — but only when it is fully merged into the
hub, so an abandoned teardown never loses unmerged work. A `code` stub on PATH
keeps the VS Code calls hermetic (the host really has `code` installed).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WORKTREE_DONE = Path(__file__).resolve().parents[2] / "scripts" / "worktree-done.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
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
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    return hub


def _make_spoke(hub: Path, tmp_path: Path, branch: str, *, push: bool, merge: bool) -> Path:
    """Add a worktree on `branch` with one commit; optionally push it to origin
    and/or merge it into the hub's `main`."""
    wt = tmp_path / branch.replace("/", "-")
    _git(hub, "worktree", "add", "-q", "-b", branch, str(wt))
    # Branch-unique content + filename: a merged spoke lands its file in the hub,
    # so a second spoke branched off the hub must not write an identical blob
    # (that would stage nothing and fail the commit).
    fname = f"{branch.replace('/', '-')}.txt"
    (wt / fname).write_text(f"work on {branch}\n")
    _git(wt, "add", fname)
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #1")
    if push:
        _git(wt, "push", "-q", "-u", "origin", branch)
    if merge:
        _git(hub, "merge", "-q", "--no-ff", branch, "-m", "merge")
    return wt


def _run_done(
    hub: Path, tmp_path: Path, *args: str, code_exit: int = 0
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-done.sh from the hub with a logging `code` stub on PATH.

    The stub appends its argument string to a log (one line per invocation) and
    exits `code_exit` — pass a nonzero value to simulate a VS Code failure.
    Returns the completed process and the log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "code-calls.log"
    code = bindir / "code"
    code.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit {code_exit}\n')
    code.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    proc = subprocess.run(
        ["bash", str(WORKTREE_DONE), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _local_branches(hub: Path) -> list[str]:
    out = _git(hub, "branch", "--format=%(refname:short)")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_has(hub: Path, branch: str) -> bool:
    return bool(_git(hub, "ls-remote", "--heads", "origin", branch).strip())


def test_merged_branch_is_pruned_locally(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc, _ = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert "feature/1-merged" not in _local_branches(hub)


def test_merged_branch_is_pruned_on_remote(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc, _ = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert not _remote_has(hub, "feature/1-merged")


def test_unmerged_branch_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/2-unmerged", push=False, merge=False)
    proc, _ = _run_done(hub, tmp_path, "2")
    assert proc.returncode == 0, proc.stderr
    assert "feature/2-unmerged" in _local_branches(hub)


def test_keep_branch_flag_keeps_merged_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/3-merged", push=False, merge=True)
    proc, _ = _run_done(hub, tmp_path, "3", "--keep-branch")
    assert proc.returncode == 0, proc.stderr
    assert "feature/3-merged" in _local_branches(hub)


def test_code_remove_called_with_worktree_path(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/4-wt", push=False, merge=True)
    _, log = _run_done(hub, tmp_path, "4")
    calls = log.read_text() if log.exists() else ""
    assert "--remove" in calls
    assert str(wt) in calls


def test_no_code_flag_skips_code_remove(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/5-wt", push=False, merge=True)
    _, log = _run_done(hub, tmp_path, "5", "--no-code")
    assert not log.exists() or log.read_text().strip() == ""


def test_code_and_remote_failures_are_non_fatal(hub: Path, tmp_path: Path) -> None:
    # Push the branch (so its remote-tracking ref exists), merge it, then break
    # `origin` so the remote delete must fail. With a `code` stub that also
    # fails, both steps warn but teardown still succeeds.
    _make_spoke(hub, tmp_path, "feature/6-merged", push=True, merge=True)
    _git(hub, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    proc, _ = _run_done(hub, tmp_path, "6", code_exit=1)
    assert proc.returncode == 0, proc.stderr
    assert "feature/6-merged" not in _local_branches(hub)  # local delete still ran


def test_missing_target_dir_does_not_prune_another_branch(hub: Path, tmp_path: Path) -> None:
    # Both worktrees are merged + pushed, then both directories are deleted from
    # disk. Tearing down #2 with --force must prune ONLY feature/2-bbb — a path
    # match by canonicalized (empty) path would wrongly capture feature/1-aaa.
    wt_a = _make_spoke(hub, tmp_path, "feature/1-aaa", push=True, merge=True)
    wt_b = _make_spoke(hub, tmp_path, "feature/2-bbb", push=True, merge=True)
    subprocess.run(["rm", "-rf", str(wt_a), str(wt_b)], check=True)
    proc, _ = _run_done(hub, tmp_path, "2", "--force")
    assert proc.returncode == 0, proc.stderr
    assert "feature/1-aaa" in _local_branches(hub)
    assert _remote_has(hub, "feature/1-aaa")
    assert "feature/2-bbb" not in _local_branches(hub)
