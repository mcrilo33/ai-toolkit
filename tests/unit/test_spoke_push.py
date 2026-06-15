"""Unit tests for scripts/spoke-push.sh (issue #37).

The spoke's PUSH step is collapsed into ONE allowlistable process so Claude
Code's Bash matcher — which decomposes a compound command and requires every
segment to be individually allowed — never re-prompts on a decorated push or on
the ``ready/<id>`` marker. A bare exact-push allow rule never matches once the
spoke pipes the push (``... | tail``) or chains it (``git tag X && git push``),
so the whole sequence runs through this single script instead.

The script must:

* refuse to run on the default branch ``main`` (defense in depth — the
  ``push-scope-guard`` hook still backstops a refspec touching ``main``);
* push the current branch to ``origin`` via a real ``git push`` (so the pre-push
  gate hooks still fire — the script does NOT bypass them);
* with ``--ready N`` create the ``ready/N`` tag at the tip and push it.

Hermetic setup mirrors test_push_scope_guard.py: a local bare ``origin`` (no
network) and a feature-branch checkout one commit ahead. Git config is pinned to
nothing so a host's global/system config never reaches the fixture repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SPOKE_PUSH = Path(__file__).resolve().parents[2] / "scripts" / "spoke-push.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir) must not reach the fixture repo's commits or pushes.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

OWN = "fix/37-spoke-push"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SPOKE_PUSH), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """A bare ``origin`` to push into — no network needed."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    return remote


@pytest.fixture()
def main_checkout(tmp_path: Path, remote: Path) -> Path:
    """A checkout sitting on the default branch ``main``."""
    repo = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@pytest.fixture()
def spoke(main_checkout: Path) -> Path:
    """A feature-branch checkout one commit ahead, not yet pushed."""
    _git(main_checkout, "checkout", "-q", "-b", OWN)
    (main_checkout / "work.txt").write_text("spoke work\n")
    _git(main_checkout, "add", "work.txt")
    _git(main_checkout, "commit", "-qm", "feat: work", "-m", "Refs #37")
    return main_checkout


def _remote_has_ref(remote: Path, ref: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", str(remote), ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout
    return bool(out.strip())


# ── Refuses on the default branch ────────────────────────────────────────────


def test_refuses_on_main(main_checkout: Path) -> None:
    result = _run(main_checkout)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "main" in (result.stdout + result.stderr).lower()


def test_refuses_on_main_does_not_push(main_checkout: Path, remote: Path) -> None:
    # The default branch is published only from the hub — a refusal must not
    # touch the remote at all.
    before = subprocess.run(
        ["git", "ls-remote", str(remote)], capture_output=True, text=True, env=_GIT_ENV
    ).stdout

    _run(main_checkout)

    after = subprocess.run(
        ["git", "ls-remote", str(remote)], capture_output=True, text=True, env=_GIT_ENV
    ).stdout
    assert before == after


# ── Pushes the current branch ────────────────────────────────────────────────


def test_pushes_current_branch(spoke: Path, remote: Path) -> None:
    result = _run(spoke)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, f"refs/heads/{OWN}"), "branch not pushed to origin"


# ── --ready N emits the completion marker ────────────────────────────────────


def test_ready_creates_and_pushes_tag(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "--ready", "37")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", "ready/37").strip() == "ready/37", "local tag not created"
    assert _remote_has_ref(remote, "refs/tags/ready/37"), "ready/37 tag not pushed to origin"


def test_plain_push_emits_no_marker(spoke: Path, remote: Path) -> None:
    result = _run(spoke)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _git(spoke, "tag", "-l", "ready/37").strip(), "marker emitted without --ready"
    assert not _remote_has_ref(remote, "refs/tags/ready/37")


def test_ready_is_idempotent(spoke: Path, remote: Path) -> None:
    # The marker emits via spoke-ready.sh's force tag + force push (#45), so a
    # re-run of the final push must not error on an already-existing tag.
    first = _run(spoke, "--ready", "37")
    second = _run(spoke, "--ready", "37")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, "re-running the ready push must be idempotent"
    assert _remote_has_ref(remote, "refs/tags/ready/37")
