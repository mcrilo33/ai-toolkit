"""Unit tests for scripts/worktree-new.sh tmux window naming (issue #8).

The tmux window opened for a new worktree must carry the human-readable branch
leaf (e.g. `8-some-slug` for `feature/8-some-slug`), not the bare issue number,
and the name must be pinned (`automatic-rename off`, `allow-rename off`) so the
process running inside the window cannot clobber it. A logging `tmux` stub on
PATH keeps the test hermetic while a fake TMUX env var steers the script down
the tmux branch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

WORKTREE_NEW = Path(__file__).resolve().parents[2] / "scripts" / "worktree-new.sh"

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


def _run_new(hub: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-new.sh from the hub with a logging `tmux` stub on PATH.

    A fake TMUX in the environment steers the script down the tmux branch. The
    stub appends each invocation's argument string to a log (one line per call)
    and answers `new-window` with a fake window id `@1`, which the script
    captures via `-P -F '#{window_id}'`. Returns the completed process and the
    log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "tmux-calls.log"
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
        "TMUX": "/tmp/fake-tmux-socket,1234,0",
    }
    proc = subprocess.run(
        ["bash", str(WORKTREE_NEW), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _new_window_name(calls: str) -> str:
    """Extract the value passed to `-n` in the `new-window` invocation."""
    line = next(ln for ln in calls.splitlines() if ln.startswith("new-window"))
    tokens = line.split()
    return tokens[tokens.index("-n") + 1]


def _pins_option_off(calls: str, option: str) -> bool:
    """True if some `set-window-option` call targets @1 and sets `option` off."""
    return any(
        ln.startswith("set-window-option") and "-t @1" in ln and f"{option} off" in ln
        for ln in calls.splitlines()
    )


def test_tmux_window_named_with_branch_leaf(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert _new_window_name(calls) == "8-some-slug"


def test_tmux_window_name_pinned_against_rename(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert _pins_option_off(calls, "automatic-rename")
    assert _pins_option_off(calls, "allow-rename")
