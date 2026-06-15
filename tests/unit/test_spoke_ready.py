"""Unit tests for scripts/spoke-ready.sh (issue #45).

Marker emission is a workflow control signal. Today the spoke emits ``ready/N``
and ``gate/N`` by having the LLM hand-run a ``git tag … && git push …`` chain —
unreliable (narrated-not-run), re-prompting (the compound command never matches a
bare allow rule), and wasteful (pushing the tag fires the full pre-push suite for
a tag that carries no code). ``spoke-ready.sh`` collapses marker emission into ONE
allowlistable command so the model launches a single canonical process:

    spoke-ready.sh <N>          # emit ready/N at HEAD (annotated, force) and push it
    spoke-ready.sh --gate <N>   # emit gate/N (the PLAN-gate park marker)

The script must:

* create an ANNOTATED tag at HEAD and push it to ``origin`` in one invocation;
* be IDEMPOTENT — a re-run force-moves the tag and re-pushes without error;
* default the ``ready/N`` message and stamp ``gate/N`` with the park state ``plan``;
* refuse when no issue number is given.

Hermetic setup mirrors test_spoke_push.py: a local bare ``origin`` (no network)
and a feature-branch checkout one commit ahead. Git config is pinned to nothing so
a host's global/system config never reaches the fixture repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SPOKE_READY = Path(__file__).resolve().parents[2] / "scripts" / "spoke-ready.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir) must not reach the fixture repo's commits or pushes.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

OWN = "fix/45-spoke-ready"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SPOKE_READY), *args],
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
    """A feature-branch checkout one commit ahead, branch pushed to origin."""
    _git(main_checkout, "checkout", "-q", "-b", OWN)
    (main_checkout / "work.txt").write_text("spoke work\n")
    _git(main_checkout, "add", "work.txt")
    _git(main_checkout, "commit", "-qm", "feat: work", "-m", "Refs #45")
    _git(main_checkout, "push", "-q", "-u", "origin", OWN)
    return main_checkout


def _remote_has_ref(remote: Path, ref: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", str(remote), ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout
    return bool(out.strip())


def _tag_type(repo: Path, tag: str) -> str:
    return _git(repo, "cat-file", "-t", tag).strip()


# ── ready/N: create + push the completion marker ─────────────────────────────


def test_ready_creates_tag(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", "ready/45").strip() == "ready/45", "local tag not created"


def test_ready_tag_is_annotated(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tag_type(spoke, "ready/45") == "tag", "marker must be an annotated tag"


def test_ready_pushes_tag(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45"), "ready/45 not pushed to origin"


def test_ready_points_at_head(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    tagged = _git(spoke, "rev-list", "-n", "1", "ready/45").strip()
    head = _git(spoke, "rev-parse", "HEAD").strip()
    assert tagged == head, "marker must point at HEAD"


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_ready_idempotent_rerun(spoke: Path, remote: Path) -> None:
    first = _run(spoke, "45")
    second = _run(spoke, "45")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, "re-run must not error — emission is idempotent"
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_retags_at_new_head(spoke: Path) -> None:
    _run(spoke, "45")
    (spoke / "more.txt").write_text("more work\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")

    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    tagged = _git(spoke, "rev-list", "-n", "1", "ready/45").strip()
    head = _git(spoke, "rev-parse", "HEAD").strip()
    assert tagged == head, "re-run must force-move the marker to the new tip"


# ── --gate N: the PLAN-gate park marker ──────────────────────────────────────


def test_gate_creates_and_pushes_tag(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", "gate/45").strip() == "gate/45", "local gate tag not created"
    assert _remote_has_ref(remote, "refs/tags/gate/45"), "gate/45 not pushed to origin"


def test_gate_tag_message_is_plan(spoke: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    message = _git(spoke, "tag", "-l", "--format=%(contents)", "gate/45")
    assert "plan" in message, "gate marker must carry the 'plan' park state"


def test_gate_does_not_emit_ready(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _git(spoke, "tag", "-l", "ready/45").strip(), "--gate must not emit ready/N"
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


# ── Misuse ───────────────────────────────────────────────────────────────────


def test_missing_issue_number_errors(spoke: Path) -> None:
    result = _run(spoke)

    assert result.returncode != 0, "an issue number is required"
