"""Native commit-msg backstop for red-proof-verify (issue #210).

`red-proof-verify.sh` proves a RED commit's `Tested-RED:` node actually FAILS
before its implementation exists. It was wired ONLY as a Claude Code PreToolUse
hook (`if: "Bash(git commit *)"`), which leaves three fail-open paths with no
backstop: a chained/prefixed/env-assigned commit the `if` filter never matches,
a CC crash (any exit != 2 lets the commit through), and a malformed payload
(silent exit 0).

The fix installs red-proof-verify as a BLOCKING gate in the NATIVE commit-msg
hook alongside commit-quality + commit-gauntlet. Native git invokes the hook on
every real `git commit` regardless of how it was spelled, so all three paths are
covered at once. These tests drive real commits through the installed hook.

Hermetic, like test_install_git_hooks.py: a throwaway repo plus a bare origin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "scripts" / "install-git-hooks.sh"

# Mirror test_install_git_hooks: strip any ambient arming signal and pin the git
# config to /dev/null so the hermetic repo cannot inherit the host's config.
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if k != "UNATTENDED"},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

# The node-execution tests need a real pytest runner: the backstop RUNS the
# Tested-RED node. Skip them where pytest is not resolvable (matches the
# pytest_runner guard in test_commit_hooks.py).
pytest_runner = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v pytest"], capture_output=True).returncode != 0,
    reason="pytest not on PATH",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo on an issue-anchored feature branch, tracking a bare origin.

    The feature branch (`feature/1-x`) supplies the issue anchor commit-quality
    demands, so a message-only `Refs #N` is not needed — the native commit-msg
    hook `@json`-encodes the message, rendering its newlines as literal `\\n`, so
    commit-quality's line-anchored message regex only sees the branch anchor
    (the same reason real spokes work on `feature/<id>-…` branches).
    """
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
    _git(r, "checkout", "-q", "-b", "feature/1-x")
    return r


def _install(repo: Path) -> Path:
    """Install the native hooks; return the ai-toolkit-scripts directory."""
    proc = subprocess.run(
        ["bash", str(INSTALL), str(repo)], capture_output=True, text=True, env=_GIT_ENV
    )
    assert proc.returncode == 0, proc.stderr
    return repo / ".git" / "hooks" / "ai-toolkit-scripts"


def _stage(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(repo, "add", rel)


def _commit(repo: Path, *msg_args: str) -> subprocess.CompletedProcess[str]:
    """Run a real `git commit` (drives the native commit-msg hook chain)."""
    return subprocess.run(
        ["git", "commit", *msg_args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


# The RED commit's message: conventional subject + issue anchor so commit-quality
# and commit-gauntlet (which run first in the chain) pass and the commit reaches
# red-proof-verify. The Tested-RED node is appended per-test.
_PASS_NODE_MSG = ("-m", "test: add trivial test", "-m", "Refs #1")
_PASSING_TEST = "def test_trivial():\n    assert True\n"


# --- the script is copied into the hooks dir ------------------------------------


def test_red_proof_verify_copied_into_hooks(repo: Path) -> None:
    # The native commit-msg hook cannot run red-proof-verify unless the installer
    # copies it alongside the other cage scripts.
    scripts = _install(repo)

    verify = scripts / "red-proof-verify.sh"
    assert verify.is_file()
    assert os.access(verify, os.X_OK)


# --- the blocking contract: a passing Tested-RED node aborts the commit ---------


@pytest_runner
def test_commit_msg_blocks_passing_tested_red_node(repo: Path) -> None:
    # A "RED" test that actually PASSES asserts already-existing behavior — it
    # drives no new code, so the native backstop must abort the commit. Under the
    # fail-open wiring the native chain had no red-proof gate and the commit went
    # through.
    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()
    _stage(repo, "tests/test_trivial.py", _PASSING_TEST)

    commit = _commit(
        repo,
        *_PASS_NODE_MSG,
        "-m",
        "Tested-RED: tests/test_trivial.py::test_trivial",
    )

    assert commit.returncode != 0, "native red-proof backstop must block a passing Tested-RED node"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed  # nothing committed


@pytest_runner
def test_chained_commit_blocked_by_native_backstop(repo: Path) -> None:
    # Issue #210 path 1: a chained/prefixed commit that the CC `if:` filter never
    # matches (`true; git commit …`). Native git invokes commit-msg regardless of
    # the surrounding shell, so the backstop still fires and blocks.
    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()
    _stage(repo, "tests/test_trivial.py", _PASSING_TEST)

    proc = subprocess.run(
        [
            "bash",
            "-c",
            "true; git commit -m 'test: add trivial test' -m 'Refs #1' "
            "-m 'Tested-RED: tests/test_trivial.py::test_trivial'",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )

    assert proc.returncode != 0, "chained commit must still hit the native red-proof backstop"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed


# --- no over-block: a genuine RED node (fails) commits fine ---------------------


@pytest_runner
def test_commit_msg_allows_genuine_red_node(repo: Path) -> None:
    # A genuine red-before-green test FAILS before its implementation exists →
    # the backstop must allow the commit (it only blocks passing/unrunnable
    # nodes). The node fails by assertion so the file stays lint/type clean for
    # commit-gauntlet, isolating red-proof-verify as the gate under test.
    _install(repo)
    _stage(
        repo,
        "tests/test_feature.py",
        "def test_compute():\n    # RED: the real computation is not implemented yet\n"
        "    assert 0 == 42\n",
    )

    commit = _commit(
        repo,
        "-m",
        "test: add failing test",
        "-m",
        "Refs #1",
        "-m",
        "Tested-RED: tests/test_feature.py::test_compute",
    )

    assert commit.returncode == 0, commit.stderr
    body = _git(repo, "show", "-s", "--format=%B", "HEAD")
    assert "Tested-RED: tests/test_feature.py::test_compute" in body
