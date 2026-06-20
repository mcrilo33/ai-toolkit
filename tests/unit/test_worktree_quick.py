"""Unit tests for scripts/worktree-quick.sh — the /quick express lane (issue #89).

worktree-quick.sh is a trimmed worktree-new.sh: it creates an isolated worktree
on a `quick/<slug>` (or `chore/<slug>`) branch, copies the gitignored `.claude/`
runtime config, mints the `spoke_run_id`, and sets the `.ai-toolkit/` git exclude
— exactly like worktree-new.sh — but DOES NOT create an issue, seed a kickoff
prompt, spawn a tmux window, or launch a separate `claude` agent. The current
hub session enters the printed worktree path itself.

To let that hub session drive commits into the worktree (the hub-guard otherwise
denies a commit run with the hub's cwd on the default branch), the script drops
the explicit `hub-guard-allow` escape-hatch marker in the common git-dir — the
same file hub-guard.sh honors.

A logging `tmux` stub on PATH keeps the test hermetic and lets us assert the
script never touches tmux (no window, no kickoff).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WORKTREE_QUICK = Path(__file__).resolve().parents[2] / "scripts" / "worktree-quick.sh"

# Pin git config to nothing so a host's global config never reaches the commits
# the tests drive (this repo itself ships installable git hooks).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote and a
    gitignored `.claude/` runtime dir to copy."""
    base = tmp_path
    remote = base / "hub-remote.git"
    hub = base / "hub"
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
    # A representative .claude/ runtime config (gitignored; copied verbatim).
    (hub / ".claude" / "skills").mkdir(parents=True)
    (hub / ".claude" / "settings.json").write_text("{}\n")
    return hub


def _run_quick(hub: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-quick.sh from the hub with a logging `tmux` stub on PATH.

    The stub records every invocation so a test can assert the script NEVER
    drives tmux. Returns the completed process and the tmux call-log path.
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
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    env.pop("WT_SPOKE", None)
    proc = subprocess.run(
        ["bash", str(WORKTREE_QUICK), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _branches(hub: Path) -> list[str]:
    return _git(hub, "branch", "--format=%(refname:short)").split()


def _common_git_dir(hub: Path) -> Path:
    return Path(_git(hub, "rev-parse", "--absolute-git-dir").strip())


def _worktree_dir(hub: Path, slug: str) -> Path:
    return hub.parent / f"{hub.name}-{slug}"


def test_creates_worktree_on_quick_branch(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert "quick/fix-typo" in _branches(hub)
    assert _worktree_dir(hub, "fix-typo").is_dir()


def test_chore_type_creates_chore_branch(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "bump-dep", "-t", "chore")

    assert proc.returncode == 0, proc.stderr
    assert "chore/bump-dep" in _branches(hub)


def test_mints_spoke_run_id(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    run_id = (_worktree_dir(hub, "fix-typo") / ".ai-toolkit" / "spoke-run-id").read_text().strip()
    assert run_id.startswith("quick/fix-typo+")


def test_sets_ai_toolkit_exclude(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    wt = _worktree_dir(hub, "fix-typo")
    exclude = Path(_git(wt, "rev-parse", "--git-path", "info/exclude").strip())
    assert ".ai-toolkit/" in exclude.read_text()


def test_copies_claude_runtime_config(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert (_worktree_dir(hub, "fix-typo") / ".claude" / "settings.json").is_file()


def test_drops_hub_guard_allow_marker(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert (_common_git_dir(hub) / "hub-guard-allow").exists()


def test_does_not_touch_tmux(hub: Path, tmp_path: Path) -> None:
    # No kickoff, no separate session: the script must never invoke tmux.
    proc, log = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert log.read_text() == ""


def test_does_not_launch_an_agent(hub: Path, tmp_path: Path) -> None:
    # The current session enters the worktree; no `claude` agent is launched.
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert "claude --model" not in proc.stdout


def test_prints_worktree_path(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert str(_worktree_dir(hub, "fix-typo")) in proc.stdout


def test_rejects_unknown_type(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo", "-t", "feature")

    assert proc.returncode != 0
    assert "type" in proc.stderr.lower()
