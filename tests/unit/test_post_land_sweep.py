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
to a neutral, non-git sandbox at import time, so a bare ``git`` invocation with no
explicit cwd finds NO repository and cannot touch the real one. Tests that pass an
explicit ``cwd=`` (their own throwaway repos) are unaffected. These tests pin that
contract; do not drop the conftest chdir without removing this file's reason to exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_cwd_is_not_a_git_worktree() -> None:
    # A subprocess that shells `git` with NO explicit cwd inherits the pytest
    # process cwd. That cwd must not be a git work tree, or a script that bare-`git`s
    # would mutate whatever repo the suite happens to sit in — the real one.
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, (
        f"pytest cwd is a git work tree ({os.getcwd()}); a test that shells git "
        "without cwd= would mutate the real repo (issue #179)"
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
