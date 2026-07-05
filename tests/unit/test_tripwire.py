"""Unit tests for the pre-push repo-integrity tripwire (issue #31).

The #29/#30 GIT_DIR leak corrupted the real repo SILENTLY: a test fixture's
``git`` call moved ``main`` and flipped ``core.bare`` during the pre-push gate,
and nothing noticed. #21 + #30 fixed the known vector (env stripping); this
tripwire is the safety net for the whole CLASS of isolation breaches.

Before the gate runs pytest it snapshots the real repo's integrity markers —
``HEAD`` + every local ref tip, ``core.bare``, ``core.worktree`` — and re-reads
them after. A genuine change means a test escaped isolation and mutated THIS
repo: the push is aborted (non-zero), the snapshot restored, and the changed
marker named. A hermetic test that creates/deletes its OWN tmpdir repo must NOT
trip it.

Two #135 refinements: worktrees share one ref store, so a fast-forward advance
(or creation) of a branch checked out in a live sibling worktree is legitimate
concurrent spoke work, not an escape — it neither trips the check nor gets
rewound. And restore never orphans commits: a ref is never rewound to a strict
ancestor of its current tip (warn + abort instead), and an appeared ref checked
out in a registered worktree is never deleted.

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


def test_restore_refuses_rewind_to_strict_ancestor(repo: Path) -> None:
    # A ref that only GAINED commits (snapshot tip is a strict ancestor of the
    # current tip) must not be rewound — that orphans commits (issue #135's
    # data loss). Restore warns and leaves the ref alone; the abort, not the
    # rewind, is the protection.
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit commit --allow-empty -q -m sneak\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    tip = _rev(repo, "main")
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"  # not rewound
    assert "NOT rewinding" in proc.stderr
    assert "refs/heads/main" in proc.stderr
    assert tip == _rev(repo, "main")


def test_restore_still_recovers_rewound_ref(repo: Path) -> None:
    # The inverse move — the ref LOST commits during the run (snapshot tip is
    # ahead of the current tip) — is genuine corruption; restore must still
    # bring the ref forward to the snapshot.
    tip = _commit(repo, {"src/a.py": "x = 1\n"})
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit reset -q --hard HEAD~1\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "main") == tip  # the lost commit is back


def test_restore_skips_deleting_ref_checked_out_in_worktree(repo: Path, tmp_path: Path) -> None:
    # A ref that appeared during the run but is checked out in a registered
    # worktree is a live spoke's branch — deleting it destroys the spoke's
    # anchor. Restore must leave it in place.
    wt = tmp_path / "spawned"
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git worktree add -q -b feature/spawned "{wt}"\n'
        'tripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "refs/heads/feature/spawned")  # still exists
    assert "NOT deleting" in proc.stderr  # and the skip is named


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


def test_restore_preserves_worktree_path_with_spaces(repo: Path, tmp_path: Path) -> None:
    # core.worktree can be a path containing spaces; restore must round-trip it
    # verbatim, not truncate at the first space. Use a real dir (git validates it).
    spaced_dir = tmp_path / "work dir"
    spaced_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _git(repo, "config", "core.worktree", str(spaced_dir))
    proc = _lib(
        repo,
        f'b="$(tripwire_capture)"\ngit config core.worktree "{other_dir}"\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _git(repo, "config", "--get", "core.worktree").strip() == str(spaced_dir)


# --- live sibling worktrees (issue #135) ------------------------------------------
# Worktrees share one ref store: a live sibling spoke committing during a long
# gate advances its own branch by fast-forward. That is legitimate concurrent
# work, not a test escape — the tripwire must not trip on it (and must not
# rewind it). Genuine escapes still trip: any move of the CURRENT branch, a
# non-FF move/rewind of a sibling ref, and moves of refs no worktree has
# checked out.


@pytest.fixture()
def spoke(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of `repo` on its own branch — a live sibling spoke."""
    wt = tmp_path / "spoke"
    _git(repo, "worktree", "add", "-q", "-b", "feature/spoke", str(wt))
    return wt


def test_check_ignores_sibling_worktree_ff_advance(repo: Path, spoke: Path) -> None:
    # A live spoke committing mid-gate fast-forwards its checked-out branch;
    # the check must stay CLEAN (issue #135's false breach).
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git -C "{spoke}" commit --allow-empty -q -m spoke-work\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_ignores_branch_created_with_new_worktree(repo: Path, tmp_path: Path) -> None:
    # The hub spawning a new spoke mid-gate creates a branch + worktree; the
    # new ref belongs to a registered worktree and must not trip the check.
    wt = tmp_path / "spawned"
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git worktree add -q -b feature/spawned "{wt}"\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_trips_on_sibling_worktree_rewind(repo: Path, spoke: Path) -> None:
    # A sibling ref moving BACKWARD is not spoke work — something destroyed
    # commits in the shared ref store. Must still trip.
    _git(spoke, "commit", "--allow-empty", "-qm", "spoke-work")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git -C "{spoke}" reset -q --hard HEAD~1\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/feature/spoke" in proc.stdout


def test_check_trips_on_ff_advance_of_unregistered_branch(repo: Path) -> None:
    # An FF advance of a branch NO worktree has checked out has no live-spoke
    # explanation — that is exactly what an escaped test looks like. Must trip.
    _git(repo, "branch", "side")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        'sha="$(git commit-tree -m sneak -p refs/heads/side "$(git rev-parse side^{tree})")"\n'
        'git update-ref refs/heads/side "$sha"\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/side" in proc.stdout


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
    # The sneak commit FF-advanced main, so restore refuses the rewind (data
    # loss) and warns — the abort above is the protection (issue #135).
    assert "NOT rewinding" in proc.stderr
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"
    assert before in _git(repo, "log", "--format=%H", "main")  # ancestor intact


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


def test_live_spoke_commit_mid_gate_passes_and_survives(
    repo: Path, spoke: Path, tmp_path: Path
) -> None:
    # THE #135 regression, end to end: a stub "spoke" advances its own branch
    # while the gate runs. The push must NOT abort and the spoke's commit must
    # NOT be rewound.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", f'git -C "{spoke}" commit --allow-empty -q -m spoke-work')

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # push not aborted
    assert (
        _git(repo, "log", "-1", "--format=%s", "refs/heads/feature/spoke").strip() == "spoke-work"
    )  # the spoke's commit survives


def test_sibling_rewind_mid_gate_still_aborts_and_recovers(
    repo: Path, spoke: Path, tmp_path: Path
) -> None:
    # Counter-case: a sibling ref moving BACKWARD mid-gate is genuine
    # corruption — abort, and restore brings the lost commit back (forward
    # moves are not the strict-ancestor rewind restore refuses).
    _git(spoke, "commit", "--allow-empty", "-qm", "spoke-work")
    spoke_tip = _rev(repo, "refs/heads/feature/spoke")
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", f'git -C "{spoke}" reset -q --hard HEAD~1')

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == BREACH_RC, proc.stderr  # still a breach
    assert "refs/heads/feature/spoke" in proc.stderr  # named
    assert _rev(repo, "refs/heads/feature/spoke") == spoke_tip  # recovered


# --- the backstop: run_pytest_node under the tripwire (issue #31) -----------------
# The red-proof hooks run individual Tested-RED nodes through run_pytest_node,
# which shells out to pytest just like the gate. The tripwire wraps that run too:
# a node that mutates THIS repo yields the BREACH verdict and the snapshot is
# restored, so the caller can block instead of shipping a corrupted repo.


def _run_node(
    repo: Path, bindir: Path, node: str = "tests/test_x.py::test_x"
) -> subprocess.CompletedProcess[str]:
    """Source utils.sh and call run_pytest_node with `bindir` (the stub) on PATH."""
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    script = (
        f'set -uo pipefail\nsource "{UTILS}"\n'
        f'v="$(run_pytest_node "{repo}" "{node}")"\necho "VERDICT=$v"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_backstop_clean_node_passes_through(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", ":")  # exits 0, mutates nothing

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=PASS" in proc.stdout, proc.stderr  # normal verdict still flows


def test_backstop_node_bare_flip_breaches_and_restores(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", "git config core.bare true")

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # breach beats the PASS verdict
    assert "core.bare" in proc.stderr  # names the marker
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"  # restored


def test_backstop_node_ref_move_breaches_and_refuses_rewind(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", "git commit --allow-empty -q -m sneak")

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # the block still fires
    # FF advance → restore refuses the rewind and warns (issue #135).
    assert "NOT rewinding" in proc.stderr
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"
