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
    testmon_cmd: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run gate-sweep.sh in `repo` with a stubbed runner + gh; return (proc, gh log).

    `testmon_cmd` (issue #276) stubs the baseline-refresh command via
    GATE_SWEEP_TESTMON_CMD — it runs with TESTMON_DATAFILE pointed at the baseline.
    """
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
        # Default the #276 baseline refresh to a no-op so a green GATE_SWEEP_CMD sweep
        # never falls through to a REAL `pytest --testmon` run (the real pytest on PATH
        # carries testmon). The baseline-refresh tests pass their own stub.
        "GATE_SWEEP_TESTMON_CMD": testmon_cmd if testmon_cmd is not None else ":",
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


# --- --run: refresh the pre-warmed .testmondata baseline on a green sweep (#276) ----


def _baseline(repo: Path) -> Path:
    return repo / ".git" / ".testmondata-baseline"


def test_run_green_sweep_refreshes_the_testmon_baseline(repo: Path, tmp_path: Path) -> None:
    # After a green full sweep, refresh the maintained baseline .testmondata (built via
    # `pytest --testmon` at TESTMON_DATAFILE) so future spokes copy it and run a
    # first-push incremental instead of the full-suite seed (issue #276).
    _mint(repo, "testmon")
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(
        repo,
        tmp_path,
        "--run",
        _head(repo),
        cmd=_runner_cmd(runner_log),
        testmon_cmd='printf "DB" > "$TESTMON_DATAFILE"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _wait_for(_baseline(repo)), "a green sweep must refresh the baseline .testmondata"
    assert _baseline(repo).read_text() == "DB"


def test_run_red_sweep_does_not_refresh_the_baseline(repo: Path, tmp_path: Path) -> None:
    # A red sweep proved the opposite of full-green — it must not mint a baseline off a
    # tree whose suite is failing.
    _mint(repo, "testmon")
    runner_log = tmp_path / "runner.log"

    _run_sweep(
        repo,
        tmp_path,
        "--run",
        _head(repo),
        "--branch",
        "feature/9-x",
        "--issue",
        "9",
        cmd=_runner_cmd(runner_log, exit_code=1),
        testmon_cmd='printf "DB" > "$TESTMON_DATAFILE"',
    )

    time.sleep(0.8)
    assert not _baseline(repo).exists(), "a red sweep must not refresh the baseline"


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
    assert "#9" in text  # the source issue, as a real reference


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


# --- --run: observe-only tripwire on the live hub surface (issue #267) -------------
# The sweep re-runs the full suite on the hub/main checkout while a concurrent /afk
# drain legitimately FF-advances main/origin/*/sibling refs, stamps needs-human-land/*
# tags, and moves HEAD. Those moves must NOT read as a repo-integrity breach and file a
# spurious "Post-land sweep red": the verdict is the suite's OWN pytest exit. These
# drive run_suite's REAL-runner path (a PATH `pytest` stub, no GATE_SWEEP_CMD), which
# resolves via detect_pytest and runs under the observe-only tripwire — the path that
# historically returned TRIPWIRE_BREACH_RC (97) on the drain's ref movement.


def _run_sweep_real_runner(
    repo: Path,
    tmp_path: Path,
    *args: str,
    suite_exit: int = 0,
    drain: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run gate-sweep.sh via the REAL-runner path with a PATH `pytest` stub.

    No GATE_SWEEP_CMD is set, so run_suite resolves the stub through detect_pytest and
    wraps it in the sweep's tripwire. The stub answers --help/--version, optionally
    simulates a concurrent drain (FF-advancing main/HEAD, creating origin/* refs, a
    sibling branch, and a needs-human-land/* tag), then exits `suite_exit`. The drain
    advances via an empty commit so HEAD^{tree} is preserved (the stamp keys on it).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh-calls.log"
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{gh_log}"\nexit 0\n')
    gh.chmod(0o755)
    drain_body = (
        (
            "git commit --allow-empty -q -m 'drain lands another spoke'\n"
            "git update-ref refs/remotes/origin/main HEAD\n"
            "git update-ref refs/remotes/origin/HEAD HEAD\n"
            "git branch feature/251-sibling HEAD 2>/dev/null || true\n"
            "git update-ref refs/remotes/origin/feature/251-sibling HEAD\n"
            "git tag needs-human-land/261 HEAD 2>/dev/null || true\n"
        )
        if drain
        else ""
    )
    pytest_stub = bindir / "pytest"
    pytest_stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in --help|-h) echo "usage: pytest"; exit 0 ;; '
        '--version|-V) echo "pytest 9.9"; exit 0 ;; esac\n'
        f"{drain_body}"
        f"exit {suite_exit}\n"
    )
    pytest_stub.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", str(GATE_SWEEP), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc, gh_log


def test_run_green_under_concurrent_drain_movement(repo: Path, tmp_path: Path) -> None:
    # A passing suite must exit green, mint the `full` stamp, and file NO issue even
    # though main/origin/*/sibling/needs-human-land tag/HEAD all moved during the run.
    # Pre-#267 the whole-repo tripwire read those FF moves as a breach (97) and filed a
    # spurious red.
    _mint(repo, "testmon")

    proc, gh_log = _run_sweep_real_runner(repo, tmp_path, "--run", _head(repo), suite_exit=0)

    assert proc.returncode == 0, proc.stderr
    assert "tier=full\n" in _stamp_text(repo)  # green upgrade fired under the drain
    assert not gh_log.exists() or gh_log.read_text() == ""  # no spurious "sweep red" issue


def test_run_red_under_concurrent_drain_still_files_issue(repo: Path, tmp_path: Path) -> None:
    # A genuinely failing suite still files a red issue amid the same drain movement:
    # the verdict is the suite's own exit code, not the ref churn.
    _mint(repo, "testmon")

    proc, gh_log = _run_sweep_real_runner(
        repo,
        tmp_path,
        "--run",
        _head(repo),
        "--branch",
        "feature/9-x",
        "--issue",
        "9",
        suite_exit=1,
    )

    assert proc.returncode == 0, proc.stderr
    assert gh_log.exists() and "issue create" in gh_log.read_text()  # real red still files
    assert "tier=testmon\n" in _stamp_text(repo)  # red keeps the pruned stamp


def test_run_suite_parallelizes_the_full_sweep(repo: Path, tmp_path: Path) -> None:
    # Part 1 (issue #276): the post-land full sweep is embarrassingly parallel, so
    # run_suite threads `-n auto` onto the real-runner invocation. Assert the suite
    # actually saw it (an argv-logging pytest stub on the real-runner path).
    _mint(repo, "testmon")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.log"
    pytest_stub = bindir / "pytest"
    pytest_stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in --help|-h) echo "usage: pytest"; echo "  -n numprocesses"; exit 0 ;; '
        '--version|-V) echo "pytest 9.9"; exit 0 ;; esac\n'
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        "exit 0\n"
    )
    pytest_stub.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}

    subprocess.run(
        ["bash", str(GATE_SWEEP), "--run", _head(repo)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert argv_log.exists(), "the real-runner sweep never invoked pytest"
    assert "-n auto" in argv_log.read_text()  # the full sweep, parallelized


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


def _run_sweep_with_rm_shim(
    repo: Path,
    tmp_path: Path,
    *args: str,
    cmd: str,
    inject_queue: Path,
    inject_line: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the worker with an `rm` shim that re-queues once in the claim window.

    The shim, on the first removal of a `queue*` file, atomically (re)creates a
    fresh `queue` with `inject_line` — a newer request arriving exactly as the
    worker consumes the queue — then performs the real removal. The read-then-rm
    drain deletes that newer request unprocessed (the bug); the mv-to-private-copy
    claim leaves it as a fresh `queue` that survives the next iteration.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh-calls.log"
    gh = bindir / "gh"
    gh.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{gh_log}"\nexit 0\n')
    gh.chmod(0o755)
    sentinel = tmp_path / "rm-injected"
    rm = bindir / "rm"
    rm.write_text(
        "#!/bin/sh\n"
        'if [ -n "$RM_INJECT_SENTINEL" ] && [ ! -e "$RM_INJECT_SENTINEL" ]; then\n'
        '  for arg in "$@"; do\n'
        '    case "${arg##*/}" in\n'
        "      queue*)\n"
        '        : > "$RM_INJECT_SENTINEL"\n'
        '        printf "%s" "$RM_INJECT_LINE" > "$RM_INJECT_QUEUE"\n'
        "        break ;;\n"
        "    esac\n"
        "  done\n"
        "fi\n"
        'exec /bin/rm "$@"\n'
    )
    rm.chmod(0o755)
    env = {
        **_GIT_ENV,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GATE_SWEEP_CMD": cmd,
        "RM_INJECT_SENTINEL": str(sentinel),
        "RM_INJECT_QUEUE": str(inject_queue),
        "RM_INJECT_LINE": inject_line,
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


def test_run_requeue_in_claim_window_is_not_dropped(repo: Path, tmp_path: Path) -> None:
    # A newer request mv'd over `queue` between the worker's read and its rm
    # must not be silently deleted (the #124 safety net owes it a run). The
    # atomic claim (mv to a private copy) leaves a late arrival as a fresh
    # queue; a read-then-rm drain removes it unprocessed.
    _mint(repo, "testmon")
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    # A request already queued (drained after the initial HEAD sweep).
    (_sweep_dir(repo) / "queue").write_text("shaB\tfeature/199-B\t199\n")
    red = 'printf "FAILED tests/unit/test_x.py::t - E\\n"; exit 1'

    proc, gh_log = _run_sweep_with_rm_shim(
        repo,
        tmp_path,
        "--run",
        _head(repo),
        "--branch",
        "feature/199-A",
        "--issue",
        "199",
        cmd=red,
        inject_queue=_sweep_dir(repo) / "queue",
        inject_line="shaC\tfeature/199-C\t199\n",
    )

    assert proc.returncode == 0, proc.stderr
    text = gh_log.read_text()
    assert "feature/199-A" in text  # the landed request swept
    assert "feature/199-B" in text  # the already-queued follow-up swept
    assert "feature/199-C" in text  # the request re-queued in the claim window: not dropped


def test_run_signal_while_holding_lock_releases_the_lock(repo: Path, tmp_path: Path) -> None:
    # The release trap is installed before acquiring the lock and covers signals,
    # so a worker killed while holding the lock releases it — no stale lock left
    # to wedge the safety net until the kill-0 self-heal notices.
    _mint(repo, "testmon")

    # The suite signals its own worker mid-sweep (SIGTERM), then returns.
    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd="kill -TERM $PPID")

    assert proc.returncode == 0, proc.stderr  # trapped signal exits clean (best-effort)
    assert not (_sweep_dir(repo) / "lock.pid").exists()  # trap released the lock


def test_run_queue_path_preserves_the_live_holders_lock(repo: Path, tmp_path: Path) -> None:
    # Installing the release trap before acquire means the queue-blocked path
    # runs it on return; its LOCK_OWNED guard must keep it a no-op there so a
    # worker that only queued never deletes the live holder's pidfile.
    _mint(repo, "testmon")
    _sweep_dir(repo).mkdir(parents=True, exist_ok=True)
    live = f"{os.getpid()}\n"
    (_sweep_dir(repo) / "lock.pid").write_text(live)
    runner_log = tmp_path / "runner.log"

    proc, _ = _run_sweep(repo, tmp_path, "--run", _head(repo), cmd=_runner_cmd(runner_log))

    assert proc.returncode == 0, proc.stderr
    assert not runner_log.exists()  # it queued instead of sweeping
    assert (_sweep_dir(repo) / "lock.pid").read_text() == live  # live lock untouched


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
