"""Unit tests for scripts/worktree-land.sh — the hub-side landing sequence.

Landing is hub-owned: verify the spoke pushed → merge into the default branch →
run the full suite → push main → tear down the worktree (worktree-done.sh) →
close the issue → kill the stranded tmux window. Every guard must abort with a
precise reason BEFORE the merge, and a suite failure must roll main back.

Hermetic like test_worktree_done.py: git runs against a local bare `origin`, and
`gh`, `tmux`, `code`, and the test suite are logging stubs on PATH — no network,
no real issue closes, no real tmux server, no recursive pytest.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WORKTREE_LAND = Path(__file__).resolve().parents[2] / "scripts" / "worktree-land.sh"

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


def _make_spoke(hub: Path, tmp_path: Path, branch: str, *, push: bool) -> Path:
    """Add a worktree on `branch` with one commit; optionally push it to origin."""
    wt = tmp_path / branch.replace("/", "-")
    _git(hub, "worktree", "add", "-q", "-b", branch, str(wt))
    fname = f"{branch.replace('/', '-')}.txt"
    (wt / fname).write_text(f"work on {branch}\n")
    _git(wt, "add", fname)
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #1")
    if push:
        _git(wt, "push", "-q", "-u", "origin", branch)
    return wt


def _run_land(
    hub: Path,
    tmp_path: Path,
    *args: str,
    suite_exit: int = 0,
    gh_exit: int = 0,
    tmux_windows: str = "",
) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    """Run worktree-land.sh from the hub with logging stubs on PATH.

    Stubs `gh`, `tmux`, and `code` (one log line per invocation each) plus a
    `suite` script logging its cwd and exiting `suite_exit`, always passed via
    --test-cmd so the real pytest never runs recursively. `tmux_windows` is the
    line(s) the tmux stub prints for `list-windows`. Returns the completed
    process and the stub logs by name."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    logs = {name: tmp_path / f"{name}-calls.log" for name in ("gh", "tmux", "code", "suite")}
    for name, exit_code in (("gh", gh_exit), ("code", 0)):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs[name]}"\nexit {exit_code}\n')
        stub.chmod(0o755)
    tmux = bindir / "tmux"
    tmux.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs["tmux"]}"\n'
        f'case "$1" in list-windows) printf "%s\\n" "{tmux_windows}" ;; esac\nexit 0\n'
    )
    tmux.chmod(0o755)
    suite = bindir / "suite"
    suite.write_text(f'#!/bin/sh\nprintf "%s\\n" "$PWD" >> "{logs["suite"]}"\nexit {suite_exit}\n')
    suite.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    proc = subprocess.run(
        ["bash", str(WORKTREE_LAND), *args, "--test-cmd", str(suite)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, logs


def _local_branches(hub: Path) -> list[str]:
    out = _git(hub, "branch", "--format=%(refname:short)")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_sha(hub: Path, branch: str) -> str:
    out = _git(hub, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out.strip() else ""


def _log_text(log: Path) -> str:
    return log.read_text() if log.exists() else ""


# --- happy path ----------------------------------------------------------------


def test_lands_pushed_branch_into_main(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-done.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_fast_forwards_when_possible(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ff", push=True)
    spoke_sha = _git(wt, "rev-parse", "HEAD").strip()

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _git(hub, "rev-parse", "HEAD").strip() == spoke_sha


def test_merge_commit_when_main_advanced(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-diverged", push=True)
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    parents = _git(hub, "rev-list", "--parents", "-n1", "HEAD").split()
    assert len(parents) == 3  # merge commit: self + two parents


def test_worktree_removed_and_branch_pruned(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-pruned", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()
    assert "feature/1-pruned" not in _local_branches(hub)
    assert _remote_sha(hub, "feature/1-pruned") == ""


def test_keep_branch_flag_keeps_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-kept", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", "--keep-branch")

    assert proc.returncode == 0, proc.stderr
    assert "feature/1-kept" in _local_branches(hub)


# --- guards (all must abort BEFORE the merge) -----------------------------------


def test_refuses_off_default_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-guard", push=True)
    _git(hub, "checkout", "-qb", "side")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "main" in proc.stderr
    assert not (hub / "feature-1-guard.txt").exists()


def test_refuses_dirty_hub(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-guard", push=True)
    (hub / "README.md").write_text("dirty\n")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert not (hub / "feature-1-guard.txt").exists()


def test_refuses_never_pushed_spoke(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-local", push=False)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "push" in proc.stderr.lower()
    assert not (hub / "feature-1-local.txt").exists()


def test_refuses_spoke_ahead_of_upstream(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ahead", push=True)
    (wt / "extra.txt").write_text("not pushed\n")
    _git(wt, "add", "extra.txt")
    _git(wt, "commit", "-qm", "feat: extra", "-m", "Refs #1")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "push" in proc.stderr.lower()


def test_refuses_dirty_spoke_worktree(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-dirty", push=True)
    (wt / "wip.txt").write_text("uncommitted\n")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert not (hub / "feature-1-dirty.txt").exists()


def test_refuses_unknown_target(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-real", push=True)

    proc, _ = _run_land(hub, tmp_path, "99")

    assert proc.returncode != 0
    assert "feature/1-real" in proc.stderr  # candidates are listed for recovery


# --- the suite gate --------------------------------------------------------------


def test_suite_runs_from_hub_root(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-suite", push=True)

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _log_text(logs["suite"]).strip() == str(hub.resolve())


def test_suite_failure_rolls_back_merge(hub: Path, tmp_path: Path) -> None:
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-broken", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", suite_exit=1)

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha
    assert _remote_sha(hub, "main") == pre_sha  # nothing was pushed
    assert wt.exists()  # teardown never ran
    assert "feature/1-broken" in _local_branches(hub)


def test_skip_tests_flag_skips_suite(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-untested", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", "--skip-tests", suite_exit=1)

    assert proc.returncode == 0, proc.stderr
    assert _log_text(logs["suite"]) == ""


# --- ship and teardown -----------------------------------------------------------


def test_push_failure_aborts_before_teardown(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-stuck", push=True)
    _git(hub, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert wt.exists()  # worktree survives a failed ship
    assert _log_text(logs["gh"]) == ""  # issue was not closed


def test_issue_closed_via_gh(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-shipped", push=True)

    proc, logs = _run_land(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    assert "issue close 7" in _log_text(logs["gh"])


def test_gh_failure_is_non_fatal(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-ghdown", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", gh_exit=1)

    assert proc.returncode == 0, proc.stderr


def test_adhoc_branch_skips_issue_close(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "chore/adhoc-task", push=True)

    proc, logs = _run_land(hub, tmp_path, "adhoc-task")

    assert proc.returncode == 0, proc.stderr
    assert "issue close" not in _log_text(logs["gh"])


# --- tmux window cleanup (session-0 convention) -----------------------------------


def test_stranded_tmux_window_is_killed(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-stranded", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows="@3 1-stranded /gone/path")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" in _log_text(logs["tmux"])


def test_live_tmux_window_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-alive", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows=f"@3 1-alive {tmp_path}")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" not in _log_text(logs["tmux"])


def test_unrelated_tmux_window_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-mine", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows="@4 2-other /gone/path")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" not in _log_text(logs["tmux"])
