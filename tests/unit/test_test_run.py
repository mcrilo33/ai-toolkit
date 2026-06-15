"""Unit tests for scripts/test-run.sh — the one-shape test runner wrapper.

A spoke that has just written a new shell script must make it executable before
the suite can exec it. Doing that as a standalone ``chmod +x new.sh`` trips the
``ask`` permission rule, and folding it into ``chmod +x new.sh && pytest`` defeats
the scope-guard auto-allow the moment any redirection or newline is involved (see
the ``scope-guard-hooks-bail-on-redirection-and-newlines`` lesson). This wrapper
collapses "make repo scripts runnable, then run pytest" into ONE allowlistable
command — ``scripts/test-run.sh`` — so the chmod never prompts and pytest args
forward straight through.

The chmod step must be DIFF-SAFE: it only restores the executable bit on files
git already tracks as ``100755`` (a fresh checkout that dropped the bit) and adds
it to brand-new untracked ``*.sh``. It must never flip a tracked sourced library
(``100644``) executable, which would dirty the worktree and pollute the spoke's
review diff.

Hermetic, like test_spoke_push.py: a throwaway git repo under ``tmp_path`` with a
trivial pytest test, run through real pytest (one fast trivial node, never the
real suite — args are always scoped to the fixture's own ``tests/``).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

TEST_RUN = Path(__file__).resolve().parents[2] / "scripts" / "test-run.sh"

# Pin git config to nothing so a host's global config can't reach the fixture repo.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(TEST_RUN), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _is_exec(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A git repo with a tracked exec script (bit dropped), a tracked sourced lib,
    an untracked new script, and a trivial passing test."""
    repo = tmp_path / "proj"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)

    # A tracked executable script: committed 100755, then the working-tree bit is
    # dropped to simulate a fresh checkout that lost it.
    exec_sh = repo / "scripts" / "exec.sh"
    exec_sh.parent.mkdir(parents=True)
    exec_sh.write_text("#!/usr/bin/env bash\necho hi\n")
    exec_sh.chmod(0o755)
    _git(repo, "add", "scripts/exec.sh")

    # A tracked sourced library: committed 100644, must stay non-executable.
    lib_sh = repo / "scripts" / "lib.sh"
    lib_sh.write_text("# sourced, not executed\n")
    _git(repo, "add", "scripts/lib.sh")

    # A tracked-100755 script whose path has a space — its restore loop is the one
    # that word-split and aborted the wrapper before the NUL-delimited read fix.
    spaced_sh = repo / "scripts" / "has space.sh"
    spaced_sh.write_text("#!/usr/bin/env bash\necho spaced\n")
    spaced_sh.chmod(0o755)
    _git(repo, "add", "scripts/has space.sh")

    # A trivial passing test so real pytest exits 0 fast.
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    _git(repo, "add", "tests/test_ok.py")
    _git(repo, "commit", "-qm", "chore: seed")

    # Drop the exec bit on the tracked-100755 scripts (fresh-checkout simulation).
    exec_sh.chmod(0o644)
    spaced_sh.chmod(0o644)

    # A brand-new untracked script the spoke just wrote, not yet executable.
    new_sh = repo / "scripts" / "new.sh"
    new_sh.write_text("#!/usr/bin/env bash\necho new\n")
    new_sh.chmod(0o644)

    # An untracked script whose name has a space and a non-ASCII char — the chmod
    # step must not split on the space nor choke on git's octal path-quoting.
    awkward_sh = repo / "scripts" / "my scrïpt.sh"
    awkward_sh.write_text("#!/usr/bin/env bash\necho awkward\n")
    awkward_sh.chmod(0o644)

    return repo


# ── Forwards args to pytest and returns its exit code ────────────────────────


def test_runs_pytest_and_passes(project: Path) -> None:
    result = _run(project, "tests/")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in (result.stdout + result.stderr)


def test_returns_pytest_failure_exit_code(project: Path) -> None:
    (project / "tests" / "test_bad.py").write_text("def test_bad():\n    assert False\n")

    result = _run(project, "tests/test_bad.py")

    assert result.returncode != 0, "a failing pytest run must propagate a non-zero exit code"


# ── Diff-safe chmod ──────────────────────────────────────────────────────────


def test_chmods_untracked_script(project: Path) -> None:
    _run(project, "tests/")

    assert _is_exec(project / "scripts" / "new.sh"), "untracked new script not made executable"


def test_chmods_untracked_script_with_awkward_name(project: Path) -> None:
    # A space- and unicode-laden path must be read verbatim (NUL-delimited), not
    # word-split or octal-quoted, or the chmod targets the wrong path and the
    # whole wrapper aborts under `set -e` before pytest ever runs.
    result = _run(project, "tests/")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _is_exec(project / "scripts" / "my scrïpt.sh"), "awkwardly-named script not made exec"


def test_restores_bit_on_tracked_executable(project: Path) -> None:
    _run(project, "tests/")

    assert _is_exec(project / "scripts" / "exec.sh"), "tracked-100755 script's bit not restored"


def test_restores_bit_on_tracked_executable_with_space(project: Path) -> None:
    result = _run(project, "tests/")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _is_exec(project / "scripts" / "has space.sh"), "spaced tracked-100755 bit not restored"


def test_leaves_tracked_lib_non_executable(project: Path) -> None:
    _run(project, "tests/")

    lib = project / "scripts" / "lib.sh"
    assert not _is_exec(lib), "a tracked sourced lib (100644) must not be made executable"
    # Diff-safety: the wrapper must not dirty the worktree by changing lib's mode.
    porcelain = _git(project, "status", "--porcelain", "--", "scripts/lib.sh")
    assert porcelain.strip() == "", f"wrapper dirtied a tracked lib: {porcelain!r}"


# ── Degrades outside a git repo ──────────────────────────────────────────────


def test_runs_outside_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "nogit"
    (plain / "tests").mkdir(parents=True)
    (plain / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    result = _run(plain, "tests/")

    assert result.returncode == 0, result.stdout + result.stderr
