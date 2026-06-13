"""Unit tests for scripts/install-git-hooks.sh — the native pre-push test gate.

Issue #19 makes the native pre-push hook the single owner of test execution. The
installer must (1) copy test-select.sh into the hooks dir alongside the other
cage scripts, and (2) wire it into the emitted pre-push hook as a BLOCKING gate —
a non-zero exit aborts the push — fed git's pre-push stdin, while the advisory
red-proof / reviewer-sep warns keep running non-blocking.

Hermetic, like test_worktree_land.py: a throwaway repo plus a bare origin. After
install, the COPIED ai-toolkit-scripts/test-select.sh (and the advisory scripts)
are replaced with logging stubs so each push's outcome is driven deterministically
— the selector's own logic is covered by test_test_select.py. The commit to push
is made BEFORE install so the commit-msg hook never enters the picture.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "scripts" / "install-git-hooks.sh"
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo on `main` tracking a bare origin, with one pushed seed commit."""
    remote = tmp_path / "remote.git"
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(r, "remote", "add", "origin", str(remote))
    _git(r, "push", "-q", "-u", "origin", "main")
    return r


def _install(repo: Path) -> Path:
    """Install the native hooks; return the hooks directory."""
    proc = subprocess.run(
        ["bash", str(INSTALL), str(repo)], capture_output=True, text=True, env=_GIT_ENV
    )
    assert proc.returncode == 0, proc.stderr
    return repo / ".git" / "hooks"


def _scripts_dir(hooks: Path) -> Path:
    return hooks / "ai-toolkit-scripts"


def _stub_selector(hooks: Path, *, exit_code: int, stdin_log: Path | None = None) -> None:
    """Overwrite the copied test-select.sh with a stub of a known exit code."""
    sel = _scripts_dir(hooks) / "test-select.sh"
    body = "#!/bin/sh\n"
    if stdin_log is not None:
        body += f'cat >> "{stdin_log}"\n'
    body += f"exit {exit_code}\n"
    sel.write_text(body)
    sel.chmod(0o755)


def _unpushed_commit(repo: Path, fname: str = "change.txt") -> str:
    """Make a commit to push BEFORE hooks exist (so commit-msg never fires)."""
    (repo / fname).write_text("work\n")
    _git(repo, "add", fname)
    _git(repo, "commit", "-qm", "feat: work", "-m", "Refs #1")
    return _git(repo, "rev-parse", "HEAD").strip()


def _push(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _remote_sha(repo: Path, branch: str = "main") -> str:
    out = _git(repo, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out.strip() else ""


# --- the script is copied into the hooks dir ------------------------------------


def test_test_select_copied_into_hooks(repo: Path) -> None:
    hooks = _install(repo)

    sel = _scripts_dir(hooks) / "test-select.sh"
    assert sel.is_file()
    assert os.access(sel, os.X_OK)


# --- the blocking contract: a failing selector aborts the push ------------------


def test_pre_push_blocks_when_selector_fails(repo: Path) -> None:
    seed = _remote_sha(repo)
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=1)

    push = _push(repo)

    assert push.returncode != 0  # the gate aborted the push
    assert _remote_sha(repo) == seed  # nothing shipped


def test_pre_push_allows_when_selector_passes(repo: Path) -> None:
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()


# --- the selector is fed git's pre-push stdin -----------------------------------


def test_selector_receives_prepush_stdin(repo: Path, tmp_path: Path) -> None:
    local = _unpushed_commit(repo)
    hooks = _install(repo)
    log = tmp_path / "stdin.log"
    _stub_selector(hooks, exit_code=0, stdin_log=log)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert log.is_file(), "selector was not invoked"
    assert local in log.read_text()  # the pushed local sha reached the selector


# --- advisory warns stay non-blocking -------------------------------------------


def test_advisory_warns_do_not_block(repo: Path) -> None:
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)
    # Make the advisory scripts "fail": the hook must swallow it and still push.
    for name in ("red-proof-warn.sh", "reviewer-sep-warn.sh"):
        adv = _scripts_dir(hooks) / name
        adv.write_text("#!/bin/sh\nexit 1\n")
        adv.chmod(0o755)

    push = _push(repo)

    assert push.returncode == 0, push.stderr  # advisory exit codes never block
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()
