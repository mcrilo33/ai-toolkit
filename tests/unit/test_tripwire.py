"""Unit tests for the pre-push repo-integrity tripwire (issue #31).

The #29/#30 GIT_DIR leak corrupted the real repo SILENTLY: a test fixture's
``git`` call moved ``main`` and flipped ``core.bare`` during the pre-push gate,
and nothing noticed. #21 + #30 fixed the known vector (env stripping); this
tripwire is the safety net for the whole CLASS of isolation breaches.

Before the gate runs pytest it snapshots the real repo's integrity markers —
``HEAD`` + every local ref tip, ``core.bare``, ``core.worktree`` — and re-reads
them after. Any change means a test escaped isolation and mutated THIS repo: the
push is aborted (non-zero), the snapshot restored, and the changed marker named.
A hermetic test that creates/deletes its OWN tmpdir repo must NOT trip it.

Two layers are covered:

* the ``tripwire_*`` library in ``lib/utils.sh`` (capture / check / restore),
  exercised directly against a throwaway repo, and
* the ``test-select.sh`` pre-push gate, exercised with a ``pytest`` stub that
  mutates (or does not mutate) the real repo, asserting trip+restore+abort vs a
  clean pass.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "shared" / "hooks"
UTILS = HOOKS / "lib" / "utils.sh"
TEST_SELECT = HOOKS / "test-select.sh"
ZERO_SHA = "0" * 40
BREACH_RC = 97

# Pin git config to nothing so a host's global config can't reach these commits.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _commit(repo: Path, files: dict[str, str], msg: str = "change") -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _rev(repo)


def _stdin(local_sha: str, remote_sha: str, ref: str = "refs/heads/main") -> str:
    return f"{ref} {local_sha} {ref} {remote_sha}\n"


# --- library-level: tripwire_capture / tripwire_check / tripwire_restore ---------


def _lib(repo: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib/utils.sh sourced and cwd at `repo`."""
    script = f'set -euo pipefail\nsource "{UTILS}"\ncd "{repo}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=_GIT_ENV)


def test_check_clean_returns_zero(repo: Path) -> None:
    proc = _lib(repo, 'b="$(tripwire_capture)"; tripwire_check "$b" && echo CLEAN')

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_detects_ref_move(repo: Path) -> None:
    # Snapshot, then move main onto a fresh empty commit; check must report it.
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        "git commit --allow-empty -q -m sneak\n"
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/main" in proc.stdout


def test_restore_resets_moved_ref(repo: Path) -> None:
    before = _rev(repo, "main")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit commit --allow-empty -q -m sneak\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "main") == before  # the moved ref is back


def test_check_detects_bare_flip(repo: Path) -> None:
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        "git config core.bare true\n"
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "core.bare" in proc.stdout


def test_restore_resets_bare_flip(repo: Path) -> None:
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit config core.bare true\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"


# --- integration: the test-select.sh pre-push gate -------------------------------


def _make_pytest_stub(bindir: Path, body: str) -> None:
    """Install a `pytest` stub: answers `--help`, else runs `body` then exits 0."""
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "pytest").write_text(
        f'#!/bin/sh\ncase "$1" in --help|-h) echo "usage: pytest"; exit 0 ;; esac\n{body}\nexit 0\n'
    )
    (bindir / "pytest").chmod(0o755)


def _run_select(
    repo: Path, stdin: str, bindir: Path, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_breach_ref_move_aborts_and_restores(repo: Path, tmp_path: Path) -> None:
    # A FULL-tier diff (.yml) so the stub runs as the suite; the stub mutates the
    # real repo (moves main) the way an escaped test would.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    before = _rev(repo, "main")
    _make_pytest_stub(tmp_path / "bin", "git commit --allow-empty -q -m sneak")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == BREACH_RC, proc.stderr  # push aborted, not 0
    assert "refs/heads/main" in proc.stderr  # names the changed marker
    assert _rev(repo, "main") == before  # repo restored


def test_breach_bare_flip_aborts_and_restores(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", "git config core.bare true")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == BREACH_RC, proc.stderr
    assert "core.bare" in proc.stderr
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"  # restored


def test_clean_run_passes(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", ":")  # touches nothing

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # no trip on a clean run


def test_hermetic_tmpdir_does_not_trip(repo: Path, tmp_path: Path) -> None:
    # A well-behaved hermetic test creates and deletes its OWN tmpdir repo; that
    # must NOT count as mutating THIS repo (no false positive).
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(
        tmp_path / "bin",
        'd="$(mktemp -d)"; git init -q "$d"; '
        'git -C "$d" -c user.email=a@b.c -c user.name=x commit --allow-empty -q -m own; '
        'rm -rf "$d"',
    )

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr


def test_known_gitdir_scenario_passes_through(repo: Path, tmp_path: Path) -> None:
    # The already-fixed GIT_DIR vector: git exports GIT_DIR into the hook. The
    # pytest child runs with it stripped (issue #30), so a hermetic test reaches
    # only its own tmpdir — the tripwire sees an intact repo and lets the push by.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(
        tmp_path / "bin",
        'd="$(mktemp -d)"; git init -q "$d"; rm -rf "$d"',
    )

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"GIT_DIR": str(repo / ".git")}
    )

    assert proc.returncode == 0, proc.stderr
