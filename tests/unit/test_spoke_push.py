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
import shutil
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
    # Mirror the real repo: .review/ is gitignored so a review artifact never
    # reads as a dirty tree in the ready gate's clean-tree precondition (#172).
    (repo / ".gitignore").write_text(".review/\n")
    _git(repo, "add", "README.md", ".gitignore")
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
    # The --ready path inherits spoke-ready's #172 gate, which needs a review of
    # the tip; stamp one so the ready happy path is the fixture's default state.
    _stamp_review(main_checkout)
    return main_checkout


def _stamp_review(repo: Path, *, age_offset: int = 0) -> Path:
    """Write a ``.review/*.json`` artifact and set its mtime relative to the tip.

    spoke-ready's precondition 3 (issue #172) accepts a review only when the
    newest ``.review/*.json`` is at least as new as the tip commit. ``age_offset``
    shifts the mtime off the HEAD commit time: ``0`` sits it on the ``>=``
    boundary, a negative value makes it stale.
    """
    review_dir = repo / ".review"
    review_dir.mkdir(exist_ok=True)
    artifact = review_dir / "review.json"
    artifact.write_text('{"verdict": "APPROVE"}\n')
    tip = int(_git(repo, "log", "-1", "--format=%ct", "HEAD").strip())
    stamp = tip + age_offset
    os.utime(artifact, (stamp, stamp))
    return artifact


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


# ── The push carries the SSH keepalive options (issue #119) ──────────────────
# The spoke's per-subtask push runs the ~6-minute pre-push suite INSIDE
# `git push`; GitHub reaps the idle SSH connection mid-gate and the post-gate
# transfer dies (exit 141). The push must route through wt_git_push so
# GIT_SSH_COMMAND carries ServerAlive* keepalive options across the gate. A
# `git` shim on PATH records the env each `git push` runs with, then delegates
# to the real git so the hermetic push still lands on the bare origin.

KEEPALIVE_OPTS = "-o ServerAliveInterval=15 -o ServerAliveCountMax=40"


def _install_git_shim(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH-front `git` logging GIT_SSH_COMMAND + argv per push, then delegating."""
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    log = tmp_path / "push-invocations.log"
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = push ]; then echo "GIT_SSH_COMMAND=[$GIT_SSH_COMMAND] $*" >> "{log}"; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    return log, bindir


def _run_with_shim(
    repo: Path, bindir: Path, *, git_ssh_command: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("GIT_SSH_COMMAND", None)
    if git_ssh_command is not None:
        env["GIT_SSH_COMMAND"] = git_ssh_command
    return subprocess.run(
        ["bash", str(SPOKE_PUSH)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


def test_push_carries_keepalive_ssh_command(spoke: Path, remote: Path, tmp_path: Path) -> None:
    log, bindir = _install_git_shim(tmp_path)

    result = _run_with_shim(spoke, bindir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, f"refs/heads/{OWN}"), "branch not pushed to origin"
    recorded = log.read_text()
    assert f"GIT_SSH_COMMAND=[ssh {KEEPALIVE_OPTS}] push -u origin {OWN}" in recorded


def test_push_preserves_existing_git_ssh_command(spoke: Path, remote: Path, tmp_path: Path) -> None:
    # An operator's GIT_SSH_COMMAND (identity, options) must survive as the
    # prefix — appended-to, never clobbered.
    log, bindir = _install_git_shim(tmp_path)

    result = _run_with_shim(spoke, bindir, git_ssh_command="ssh -o ConnectTimeout=7")

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = log.read_text()
    assert f"GIT_SSH_COMMAND=[ssh -o ConnectTimeout=7 {KEEPALIVE_OPTS}] push -u origin {OWN}" in (
        recorded
    )


# ── --ready inherits spoke-ready's #172 precondition gate ────────────────────
# The --ready path shells out to spoke-ready.sh, which mechanically verifies the
# ready/N preconditions (clean tree, HEAD==@{upstream}, review artifact binds the
# tip). A refusal propagates (set -e) — but the BRANCH push, which runs first, has
# already happened, so an unmet precondition blocks only the marker, not the push.


def test_ready_refused_without_review_artifact(spoke: Path, remote: Path) -> None:
    shutil.rmtree(spoke / ".review")  # no review of the tip

    result = _run(spoke, "--ready", "37")

    assert result.returncode != 0, "a missing review artifact must block ready/37"
    assert _remote_has_ref(remote, f"refs/heads/{OWN}"), "the branch push still happens"
    assert not _remote_has_ref(remote, "refs/tags/ready/37")


def test_ready_refused_on_dirty_tree(spoke: Path, remote: Path) -> None:
    (spoke / "work.txt").write_text("uncommitted\n")

    result = _run(spoke, "--ready", "37")

    assert result.returncode != 0, "a dirty tree must block the ready marker"
    assert not _remote_has_ref(remote, "refs/tags/ready/37")


def test_ready_refusal_surfaces_pushed_but_unmarked(spoke: Path, remote: Path) -> None:
    # Two-phase recovery contract (#200): the branch push succeeds but the ready emission is
    # refused. spoke-push must surface "PUSHED-BUT-UNMARKED" loudly and exit the DISTINCT code
    # (4) — so the caller can tell "origin ahead, no completion signal" from a branch-push
    # failure and re-run just the marker.
    shutil.rmtree(spoke / ".review")  # ready is refused (no review artifact)

    result = _run(spoke, "--ready", "37")

    assert result.returncode == 4, (
        "a pushed-but-unmarked finish must exit the distinct sentinel (4): "
        + result.stdout
        + result.stderr
    )
    assert _remote_has_ref(remote, f"refs/heads/{OWN}"), "the branch push still reached origin"
    assert not _remote_has_ref(remote, "refs/tags/ready/37"), "no marker was emitted"
    assert "PUSHED-BUT-UNMARKED" in result.stderr, "the gap must be surfaced visibly"


def test_queued_subtasks_defer_the_marker_without_a_false_unmarked_alarm(
    spoke: Path, remote: Path, tmp_path: Path
) -> None:
    # #278: a packed spoke's terminal ready is REFUSED while it still owes queued subtasks —
    # by design, not by failure. That is not the two-phase gap above: the branch pushed fine
    # and the marker was deliberately withheld. Shouting PUSHED-BUT-UNMARKED would send the
    # spoke chasing a phantom emission bug instead of doing the work it still owes, so the
    # distinct code (5) must pass straight through with a truthful message.
    state = tmp_path / "afk-state"
    (state / "queued-37").mkdir(parents=True)
    (state / "queued-37" / "265").touch()

    result = subprocess.run(
        ["bash", str(SPOKE_PUSH), "--ready", "37"],
        cwd=str(spoke),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "AFK_STATE_DIR": str(state)},
    )

    assert result.returncode == 5, result.stdout + result.stderr
    assert _remote_has_ref(remote, f"refs/heads/{OWN}"), "the branch push still reached origin"
    assert not _remote_has_ref(remote, "refs/tags/ready/37"), "the terminal marker is withheld"
    assert "PUSHED-BUT-UNMARKED" not in result.stderr, "a deferred marker is not a failed one"
    assert "265" in result.stderr, "it must name what is still owed"


def test_ready_force_emits_despite_unmet_precondition(spoke: Path, remote: Path) -> None:
    shutil.rmtree(spoke / ".review")

    result = subprocess.run(
        ["bash", str(SPOKE_PUSH), "--ready", "37"],
        cwd=str(spoke),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "AI_TOOLKIT_READY_FORCE": "1"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/37")
