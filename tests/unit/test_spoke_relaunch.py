"""Unit tests for scripts/spoke-relaunch.sh (issue #233).

When a spoke's tmux pane dies the WORKTREE survives — its branch, its
``.ai-toolkit/spoke-run-id``, task contract and ledger skeleton are all still on
disk. spoke-relaunch.sh formalizes the hand-rolled ``tmux new-window`` recovery:
it resolves the existing worktree, REUSES its spoke_run_id (so the relaunched run
continues the same Langfuse session — not a fresh trace), rebuilds the launch
command from the shared native-OTel prefix + pinned model/effort + a
relaunch-aware seed prompt, opens a tmux window, and stamps a ``relaunch``
lifecycle span (which feeds #231's relaunch_count).

Hermetic setup: a real linked git worktree with the ``.ai-toolkit`` scratch dir,
a logging ``tmux`` stub on PATH, and git config pinned to nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RELAUNCH = Path(__file__).resolve().parents[2] / "scripts" / "spoke-relaunch.sh"

# Pin git config to nothing so a host's global/system config never reaches the fixture.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

SPOKE_RUN_ID = "feature/42-relaunch-me+1700000000"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def spoke_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A main repo plus a linked worktree for issue 42 with an intact ``.ai-toolkit`` dir.

    Returns ``(main_repo, worktree_dir)``. The worktree carries the spoke identity, a task
    contract and a ledger skeleton — the state that survives a pane crash.
    """
    main = tmp_path / "ai-toolkit"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "seed.txt").write_text("seed\n")
    _git(main, "add", "-A")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")

    wt = tmp_path / "ai-toolkit-42"
    _git(main, "worktree", "add", "-q", "-b", "feature/42-relaunch-me", str(wt))
    ait = wt / ".ai-toolkit"
    ait.mkdir()
    (ait / "spoke-run-id").write_text(SPOKE_RUN_ID + "\n")
    (ait / "task.md").write_text("# Issue #42\n\nGate: none\n")
    (ait / "ledger-skeleton.md").write_text("#42.main - RED - do it\n")
    return main, wt


def _run(
    cwd: Path, *args: str, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess, str]:
    """Run spoke-relaunch.sh with a logging ``tmux`` stub on PATH.

    Returns the completed process and the tmux call-log contents.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "tmux-calls.log"
    log.touch()
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "new-window" ]; then printf "@1\\n"; fi\n'
        "exit 0\n"
    )
    tmux.chmod(0o755)
    env = {
        **_GIT_ENV,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("TMUX", None)
    env.pop("WT_SPOKE", None)
    env.pop("WT_AGENT_MODEL", None)
    env.pop("WT_AGENT_EFFORT", None)
    for key in ("AI_TOOLKIT_OTEL", "AI_TOOLKIT_TELEMETRY", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(RELAUNCH), *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )
    return proc, log.read_text()


def test_reuses_the_existing_spoke_run_id(spoke_worktree, tmp_path: Path) -> None:
    main, _wt = spoke_worktree

    proc, _calls = _run(
        main, "42", "--no-terminal", tmp_path=tmp_path, extra_env={"AI_TOOLKIT_OTEL": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    # The reused identity rides the launch command's OTEL_RESOURCE_ATTRIBUTES — a NEW id would
    # split the Langfuse session, which is exactly what relaunch must avoid.
    assert f"OTEL_RESOURCE_ATTRIBUTES=spoke_run_id={SPOKE_RUN_ID}" in proc.stdout
    assert "(reused)" in proc.stdout


def test_opens_a_tmux_window_named_for_the_branch_leaf(spoke_worktree, tmp_path: Path) -> None:
    main, _wt = spoke_worktree

    proc, calls = _run(main, "42", tmp_path=tmp_path)

    assert proc.returncode == 0, proc.stderr
    new_window = next((ln for ln in calls.splitlines() if ln.startswith("new-window")), "")
    assert "-n 42-relaunch-me" in new_window  # the branch leaf (feature/ stripped)


def test_refuses_a_worktree_without_a_spoke_run_id(spoke_worktree, tmp_path: Path) -> None:
    main, wt = spoke_worktree
    (wt / ".ai-toolkit" / "spoke-run-id").unlink()

    proc, _calls = _run(main, "42", "--no-terminal", tmp_path=tmp_path)

    assert proc.returncode != 0
    assert "spoke-run-id" in proc.stderr


def test_refuses_an_unresolvable_target(spoke_worktree, tmp_path: Path) -> None:
    main, _wt = spoke_worktree

    proc, _calls = _run(main, "999", "--no-terminal", tmp_path=tmp_path)

    assert proc.returncode != 0
    assert "no single worktree matches" in proc.stderr


def test_refuses_when_run_from_inside_a_spoke(spoke_worktree, tmp_path: Path) -> None:
    main, _wt = spoke_worktree

    proc, _calls = _run(main, "42", "--no-terminal", tmp_path=tmp_path, extra_env={"WT_SPOKE": "7"})

    assert proc.returncode != 0
    assert "relaunches run on the hub" in proc.stderr


def test_reports_the_intact_ledger_skeleton(spoke_worktree, tmp_path: Path) -> None:
    main, _wt = spoke_worktree

    proc, _calls = _run(main, "42", "--no-terminal", tmp_path=tmp_path)

    assert "ledger skeleton" in proc.stdout
    assert "intact" in proc.stdout
