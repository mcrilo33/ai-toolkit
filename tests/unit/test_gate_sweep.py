"""Unit tests for scripts/gate-sweep.sh — the conditional post-land background sweep.

Issue #124: once the pre-push gate runs pruned sets (testmon/selected), a
selection miss could rot main silently. gate-sweep.sh is the safety net:

  * `--spawn <merged-sha> [--branch B] [--issue N]` — synchronous decision run
    by worktree-land.sh's tail: reads the green-tree stamp for
    `<merged-sha>^{tree}` and detaches a `--run` worker ONLY when the stamped
    tier is pruned (`testmon`/`selected`). A `full` stamp or no stamp at all
    (docs-only skip, --skip-tests) launches nothing. Always exits 0 — the
    sweep is best-effort and must never fail a land.
  * `--run <merged-sha> [--branch B] [--issue N]` — the worker: takes a
    pidfile lock under `<git-common-dir>/.gate-sweep/` (a held lock queues at
    most ONE newest-wins follow-up in `queue`), re-checks dedupe on the hub's
    current clean tree (an existing `full` stamp is never swept twice), runs
    the full suite (`GATE_SWEEP_CMD` override, else detected pytest). Green
    upgrades the tree's stamp to `full`; red files a GitHub issue carrying the
    failing test ids + the landed commit/branch, and a gh failure is written
    to `sweep.log`, never swallowed. Worker notes land in `sweep.log` so the
    detached run stays observable.

Hermetic like test_gate_stamp.py: a throwaway git repo per test, a stubbed
runner via GATE_SWEEP_CMD, and a logging `gh` stub on PATH.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

GATE_SWEEP = Path(__file__).resolve().parents[2] / "scripts" / "gate-sweep.sh"

# Pin git config to nothing so a host's global config can't reach these repos.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}").strip()


def _commit_change(repo: Path, rel: str, body: str) -> None:
    (repo / rel).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


def _stamps_dir(repo: Path) -> Path:
    return repo / ".git" / ".gate-stamps"


def _sweep_dir(repo: Path) -> Path:
    return repo / ".git" / ".gate-sweep"


def _sweep_log(repo: Path) -> str:
    log = _sweep_dir(repo) / "sweep.log"
    return log.read_text() if log.exists() else ""


def _mint(repo: Path, tier: str, env_fp: str = "py3.12/pytest-9") -> None:
    """Write a green-tree stamp for HEAD's tree directly (the #122 lib format)."""
    _stamps_dir(repo).mkdir(parents=True, exist_ok=True)
    (_stamps_dir(repo) / _tree(repo)).write_text(f"tier={tier}\nenv={env_fp}\n")


def _stamp_text(repo: Path) -> str:
    stamp = _stamps_dir(repo) / _tree(repo)
    return stamp.read_text() if stamp.exists() else ""


def _run_sweep(
    repo: Path,
    tmp_path: Path,
    *args: str,
    cmd: str,
    gh_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run gate-sweep.sh in `repo` with a stubbed runner + gh; return (proc, gh log)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh-calls.log"
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{gh_log}"\nexit {gh_exit}\n')
    gh.chmod(0o755)
    env = {
        **_GIT_ENV,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GATE_SWEEP_CMD": cmd,
    }
    proc = subprocess.run(
        ["bash", str(GATE_SWEEP), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc, gh_log


def _wait_for(path: Path, timeout: float = 10.0) -> bool:
    """Poll for a file the detached worker writes; True when it appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _runner_cmd(log: Path, *, exit_code: int = 0, extra: str = "") -> str:
    """A stub suite: log one RUN line, optionally print output, exit as told."""
    body = f'echo RUN >> "{log}"'
    if extra:
        body += f"; {extra}"
    return f"{body}; exit {exit_code}"


# --- --spawn: the launch decision, keyed on the landed tree's stamp tier ----------


@pytest.mark.parametrize("tier", ["testmon", "selected"])
def test_spawn_launches_sweep_for_pruned_tier(repo: Path, tmp_path: Path, tier: str) -> None:
    _mint(repo, tier)
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--spawn", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert _wait_for(runner_log), "pruned-tier land must launch the background sweep"
    assert runner_log.read_text().count("RUN") == 1  # exactly one sweep


def test_spawn_skips_full_stamped_tree(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "full")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--spawn", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    time.sleep(0.8)  # grace: a wrongly-spawned worker would have started by now
    assert not runner_log.exists()  # a full pass never re-sweeps


def test_spawn_skips_unstamped_tree(repo: Path, tmp_path: Path) -> None:
    # No stamp at all: docs-only skip or --skip-tests — the gate certified
    # nothing, so there is no pruned tier to backstop.
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--spawn", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    time.sleep(0.8)
    assert not runner_log.exists()


def test_spawn_returns_without_waiting_for_the_sweep(repo: Path, tmp_path: Path) -> None:
    # The land's duration must be unaffected: --spawn detaches and returns
    # while the (slow) suite is still running.
    _mint(repo, "testmon")

    t0 = time.monotonic()
    proc, _ = _run_sweep(repo, tmp_path, "--spawn", _head(repo), cmd="sleep 10")
    elapsed = time.monotonic() - t0

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 8, f"--spawn blocked on the sweep ({elapsed:.1f}s)"


# --- --run: dedupe + stamp upgrade -------------------------------------------------


def test_run_green_sweep_upgrades_stamp_to_full(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "testmon")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert runner_log.exists()
    assert "tier=full\n" in _stamp_text(repo)  # back-to-back lands now dedupe


def test_run_never_sweeps_a_full_stamped_tree(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "full")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert not runner_log.exists()  # dedupe holds at run time too, not just spawn


def test_run_dirty_tree_skips_and_logs(repo: Path, tmp_path: Path) -> None:
    # The sweep keys its proof on HEAD^{tree}; a dirty checkout would prove the
    # wrong tree, so the worker logs the skip instead of running.
    _mint(repo, "testmon")
    (repo / "stray.py").write_text("x = 1\n")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert not runner_log.exists()
    assert "dirty" in _sweep_log(repo)


# --- --run: red files a GitHub issue ----------------------------------------------


_RED_OUTPUT = (
    'printf "FAILED tests/unit/test_alpha.py::test_beta - AssertionError\\n'
    '=== 1 failed in 0.1s ===\\n"'
)


def test_run_red_files_issue_with_failing_ids_and_commit(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "testmon")
    head = _head(repo)
    runner_log = tmp_path / "runner.log"

    proc, gh_log = _run_sweep(
        repo,
        tmp_path,
        "--run",
        head,
        "--branch",
        "feature/9-widget",
        "--issue",
        "9",
        cmd=_runner_cmd(runner_log, exit_code=1, extra=_RED_OUTPUT),
    )

    assert proc.returncode == 0, proc.stderr  # best-effort: red never crashes the worker
    text = gh_log.read_text()
    assert "issue create" in text
    assert "tests/unit/test_alpha.py::test_beta" in text  # the failing ids
    assert head[:7] in text  # the landing commit
    assert "feature/9-widget" in text  # the branch that landed
    assert "9" in text  # the source issue


def test_run_red_keeps_the_pruned_stamp(repo: Path, tmp_path: Path) -> None:
    # A red sweep proved the OPPOSITE of full-green: the stamp must not upgrade.
    _mint(repo, "testmon")
    runner_log = tmp_path / "runner.log"

    _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log, exit_code=1))

    assert runner_log.exists()  # the suite ran (guards against a vacuous pass)
    assert "tier=testmon\n" in _stamp_text(repo)


def test_run_red_gh_failure_is_logged(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "testmon")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(
        repo,
        tmp_path,
        "--run",
        _head(repo),
        cmd=_runner_cmd(runner_log, exit_code=1, extra=_RED_OUTPUT),
        gh_exit=1,
    )

    assert proc.returncode == 0, proc.stderr
    log = _sweep_log(repo)
    assert "ERROR" in log and "issue" in log  # logged, never swallowed silently


# --- --run: one sweep at a time per checkout (lock + newest-wins queue) ------------


def test_run_lock_held_by_live_pid_queues_instead_of_running(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "testmon")
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    (_sweep_dir(repo) / "lock.pid").write_text(f"{os.getpid()}\n")  # a live sweep
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert not runner_log.exists()  # never two suites at once per checkout
    queue = _sweep_dir(repo) / "queue"
    assert queue.exists()
    assert _head(repo) in queue.read_text()


def test_run_blocked_requests_keep_only_the_newest(repo: Path, tmp_path: Path) -> None:
    _mint(repo, "testmon")
    old_head = _head(repo)
    _commit_change(repo, "README.md", "seed\nmore\n")
    new_head = _head(repo)
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    (_sweep_dir(repo) / "lock.pid").write_text(f"{os.getpid()}\n")
    runner_log = tmp_path / "runner.log"

    _run_sweep(repo, tmp_path, "--run", old_head, cmd=_runner_cmd(runner_log))
    _run_sweep(repo, tmp_path, "--run", new_head, cmd=_runner_cmd(runner_log))

    queued = (_sweep_dir(repo) / "queue").read_text()
    assert new_head in queued  # newest wins
    assert old_head not in queued  # at most ONE queued follow-up


def test_run_stale_lock_is_taken_over(repo: Path, tmp_path: Path) -> None:
    # A crashed sweep must not wedge the safety net forever: a pidfile whose
    # process is gone is stale, and the next worker takes the lock.
    _mint(repo, "testmon")
    dead = subprocess.Popen(["true"])
    dead.wait()
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    (_sweep_dir(repo) / "lock.pid").write_text(f"{dead.pid}\n")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert runner_log.exists()


def test_run_drains_queue_and_dedupes_same_tree(repo: Path, tmp_path: Path) -> None:
    # Back-to-back lands of the SAME content sweep once: the drained follow-up
    # hits the full stamp the first (green) pass just minted, and stops.
    _mint(repo, "testmon")
    head = _head(repo)
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    (_sweep_dir(repo) / "queue").write_text(f"{head}\tfeature/9-widget\t9\n")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", head, cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert not (_sweep_dir(repo) / "queue").exists()  # the follow-up was consumed
    assert runner_log.read_text().count("RUN") == 1  # …but the same tree ran once
    assert "tier=full\n" in _stamp_text(repo)
