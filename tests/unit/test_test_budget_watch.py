"""Unit tests for scripts/test-budget-watch.sh — the duration-budget watcher.

Issue #336: the pre-push gate's wall-clock rots silently when a slow test creeps
in (#328's one-off profile found a 122.6s test — ~6.6% of the run — by luck). The
watcher reads the per-test durations gate-sweep's `-n auto` full run already
produces off the critical path, compares them against two configured budgets
(`test_budget.slow_test_seconds` + `test_budget.suite_seconds` in
settings/ai-toolkit.yml), and on a breach:

  * dispatches `followup-scoper` (the `TEST_BUDGET_SCOPER_CMD` seam here) to file a
    deduped enhancement carrying the durations as evidence, and
  * writes a breach record under the AFK state dir for hub-notify to surface.

Debounce is single-writer (AFK principle #5): a last-seen set under the
git-common-dir so a persistent slow test files ONCE per regression, not every
sweep. Best-effort throughout (AFK #2/#6): a filer miss never fails the caller and
is logged LOUDLY, never dropped silently.

Hermetic like test_gate_sweep.py: a throwaway git repo per test, a synthetic
`--durations=0` capture file, and a logging scoper stub via TEST_BUDGET_SCOPER_CMD.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WATCH = Path(__file__).resolve().parents[2] / "scripts" / "test-budget-watch.sh"

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _read(path: Path) -> str:
    """Read a file the watcher may or may not have written (empty when absent)."""
    return path.read_text() if path.exists() else ""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit (the git-common-dir home for state)."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(
            ["git", "config", k, v], cwd=str(r), check=True, capture_output=True, env=_GIT_ENV
        )
    (r / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(r), check=True, capture_output=True, env=_GIT_ENV)
    subprocess.run(
        ["git", "commit", "-qm", "seed"], cwd=str(r), check=True, capture_output=True, env=_GIT_ENV
    )
    return r


def _capture(*, slow_test: float | None = None, fast: float = 1.2, suite: float = 130.0) -> str:
    """A synthetic pytest `--durations=0` + summary capture.

    `slow_test` (when given) is the call-phase seconds of tests/unit/test_slow.py::test_x;
    `fast` a well-under-budget test; `suite` the summary wall-clock ("in <suite>s").
    """
    lines = [
        "============================= slowest durations =============================",
    ]
    if slow_test is not None:
        lines.append(f"{slow_test:.2f}s call     tests/unit/test_slow.py::test_x")
        lines.append("0.30s setup    tests/unit/test_slow.py::test_x")
    lines.append(f"{fast:.2f}s call     tests/unit/test_fast.py::test_y")
    lines.append(f"===== 1 passed in {suite:.2f}s =====")
    return "\n".join(lines) + "\n"


def _run(
    repo: Path,
    tmp_path: Path,
    capture: str,
    *,
    slow: int = 30,
    suite: int = 480,
    scoper_log: Path | None = None,
    scoper_exit: int = 0,
    state_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the watcher over `capture`; return (proc, scoper_log).

    The scoper dispatch is stubbed via TEST_BUDGET_SCOPER_CMD (logs its argv); its
    exit is `scoper_exit`. Budgets come from the direct env overrides so no config
    file is needed. `state_dir` overrides AFK_STATE_DIR (breach-record home).
    """
    cap = tmp_path / "cap.txt"
    cap.write_text(capture)
    if scoper_log is None:
        scoper_log = tmp_path / "scoper.log"
    scoper = tmp_path / "scoper.sh"
    scoper.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{scoper_log}"\nexit {scoper_exit}\n'
    )
    scoper.chmod(0o755)
    if state_dir is None:
        state_dir = tmp_path / "state"
    env = {
        **_GIT_ENV,
        "TEST_BUDGET_SLOW_SECONDS": str(slow),
        "TEST_BUDGET_SUITE_SECONDS": str(suite),
        # The seam is invoked `bash -c "$CMD" test-budget-watch <kind> <node> <secs>`
        # (mirrors HUB_WATCHDOG_SCOPER_CMD), so the CMD must forward the positional
        # args with "$@" to reach the stub.
        "TEST_BUDGET_SCOPER_CMD": f'bash "{scoper}" "$@"',
        "AFK_STATE_DIR": str(state_dir),
    }
    proc = subprocess.run(
        ["bash", str(WATCH), str(cap), "--branch", "feature/336", "--issue", "336"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return proc, scoper_log


def _breach_records(state_dir: Path) -> list[Path]:
    return sorted(state_dir.glob("test-budget-breach-*.txt")) if state_dir.exists() else []


# --- a single-test breach trips the watcher --------------------------------------


def test_over_budget_test_dispatches_scoper(repo: Path, tmp_path: Path) -> None:
    proc, scoper_log = _run(repo, tmp_path, _capture(slow_test=122.60))

    assert proc.returncode == 0, proc.stderr
    log = _read(scoper_log)
    assert "test_slow.py::test_x" in log, "the over-budget node must reach the scoper"


def test_over_budget_test_writes_breach_record(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    _run(repo, tmp_path, _capture(slow_test=122.60), state_dir=state)

    records = _breach_records(state)
    assert len(records) == 1, "one breach record for hub-notify to surface"
    assert "122" in records[0].read_text()


# --- dedup: an immediate second run does NOT re-file (single-writer debounce) -----


def test_repeat_breach_is_debounced(repo: Path, tmp_path: Path) -> None:
    scoper_log = tmp_path / "scoper.log"
    cap = _capture(slow_test=122.60)

    _run(repo, tmp_path, cap, scoper_log=scoper_log)
    first = _read(scoper_log)
    _run(repo, tmp_path, cap, scoper_log=scoper_log)
    second = _read(scoper_log)

    assert first.count("test_slow.py::test_x") == 1
    assert second == first, "a repeat breach must not re-dispatch the scoper"


# --- a whole-suite breach is detected independently ------------------------------


def test_over_budget_suite_dispatches_scoper(repo: Path, tmp_path: Path) -> None:
    # No single test over budget, but the suite total (500s) exceeds the 480s budget.
    proc, scoper_log = _run(repo, tmp_path, _capture(slow_test=None, suite=500.0))

    assert proc.returncode == 0, proc.stderr
    assert "suite" in _read(scoper_log), "a suite-total breach must dispatch the scoper"


# --- under-budget stays completely silent ----------------------------------------


def test_under_budget_is_silent(repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    proc, scoper_log = _run(repo, tmp_path, _capture(slow_test=12.0, suite=130.0), state_dir=state)

    assert proc.returncode == 0, proc.stderr
    assert _read(scoper_log) == "", "no breach → no scoper dispatch"
    assert _breach_records(state) == [], "no breach → no record"


# --- best-effort: a failing filer never fails the caller, and logs loudly ---------


def test_scoper_failure_does_not_fail_and_logs_loudly(repo: Path, tmp_path: Path) -> None:
    proc, _ = _run(repo, tmp_path, _capture(slow_test=122.60), scoper_exit=3)

    assert proc.returncode == 0, "a filer miss must never fail the sweep (AFK #6)"
    log = _read(repo / ".git" / ".test-budget-watch" / "watch.log")
    assert "ERROR" in log, "a filer miss must be logged LOUDLY, never dropped (AFK #2)"


# --- a cleared breach drops from the seen-set and re-files on a later regression --


def test_cleared_breach_refiles_on_reoccurrence(repo: Path, tmp_path: Path) -> None:
    scoper_log = tmp_path / "scoper.log"

    _run(repo, tmp_path, _capture(slow_test=122.60), scoper_log=scoper_log)
    _run(repo, tmp_path, _capture(slow_test=12.0), scoper_log=scoper_log)  # under budget now
    _run(repo, tmp_path, _capture(slow_test=122.60), scoper_log=scoper_log)  # regressed again

    assert _read(scoper_log).count("test_slow.py::test_x") == 2, (
        "a breach that cleared then re-occurred is a new regression → re-file"
    )


# --- budgets are read from the config file when no env override is given ----------


def test_budgets_read_from_config_file(repo: Path, tmp_path: Path) -> None:
    config = tmp_path / "ai-toolkit.yml"
    config.write_text(
        "enabled: true\n\ntest_budget:\n  slow_test_seconds: 5\n  suite_seconds: 999\n\nmodel:\n  spoke:\n    model: x\n"
    )
    cap = tmp_path / "cap.txt"
    cap.write_text(_capture(slow_test=8.0, suite=130.0))  # 8s > 5s config budget
    scoper_log = tmp_path / "scoper.log"
    scoper = tmp_path / "scoper.sh"
    scoper.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{scoper_log}"\n')
    scoper.chmod(0o755)
    env = {
        **_GIT_ENV,
        "TEST_BUDGET_CONFIG": str(config),
        "TEST_BUDGET_SCOPER_CMD": f'bash "{scoper}" "$@"',
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    proc = subprocess.run(
        ["bash", str(WATCH), str(cap)], cwd=str(repo), capture_output=True, text=True, env=env
    )

    assert proc.returncode == 0, proc.stderr
    assert "test_slow.py::test_x" in _read(scoper_log), "the 5s config budget must flag the 8s test"
