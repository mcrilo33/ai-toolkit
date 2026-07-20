"""Regression guard for the post-land sweep tripwire breach (issue #179).

The conditional post-land background sweep (issue #124) runs the FULL suite after a
land under the repo-integrity tripwire (issue #31). After landing #169 the sweep
tripped: the run stamped ``refs/tags/blocked/168`` onto the REAL repo — a test had
escaped isolation.

Root cause: pytest is launched with its working directory set to the real repo root.
Tests that shell out to a git-touching script without passing an explicit ``cwd=``
(hub-afk's ``inflight_worktrees`` -> ``_escalate_blocked`` is the live case) inherit
that cwd, so ``git worktree list`` / ``git tag`` silently operate on the real
repository. With an in-flight worktree for issue #168 present, ``_escalate_blocked``
stamped ``blocked/168`` there.

The cure lives in ``tests/conftest.py``: it relocates the session's working directory
to a throwaway git-repo sandbox at import time, so a bare ``git`` invocation with no
explicit cwd resolves the SANDBOX — never the real repo — and any stray write lands
there instead. Tests that pass an explicit ``cwd=`` (their own throwaway repos) are
unaffected. These tests pin that contract; do not drop the conftest chdir/sandbox
without removing this file's reason to exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The git-cwd isolation guard (issues #124/#179/#328): these tests pin the sandbox that
# keeps bare-git off the real repo — the property the whole suite's xdist-safety rests on.
# Run them in the single-process serial phase alongside the rest of the tripwire family,
# never under xdist workers. See docs/test-gate.md.
pytestmark = pytest.mark.serial


def test_bare_git_does_not_resolve_the_real_repo() -> None:
    # A subprocess that shells `git` with NO explicit cwd inherits the pytest process
    # cwd. Whatever repo that git resolves must NOT be the real checkout, or a script
    # that bare-`git`s (hub-afk's inflight_worktrees -> _escalate_blocked) would mutate
    # the real repo's refs — exactly the escape that stamped refs/tags/blocked/168.
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    toplevel = Path(result.stdout.strip()).resolve() if result.stdout.strip() else None

    assert toplevel != REPO_ROOT, (
        f"bare git resolves the real repo ({toplevel}); a test that shells git without "
        "cwd= would mutate the real repo (issue #179)"
    )


def test_default_cwd_is_outside_the_repo() -> None:
    # Belt to the suspenders above: even if some ancestor of the sandbox were a repo,
    # the session cwd must sit OUTSIDE the real toolkit checkout so an inherited-cwd
    # git write can never land on this repo's refs.
    cwd = Path(os.getcwd()).resolve()

    assert REPO_ROOT not in (cwd, *cwd.parents), (
        f"pytest cwd {cwd} is inside the real repo {REPO_ROOT}; bare-git writes "
        "would escape isolation (issue #179)"
    )
