"""Regression guard for issue #295 — hermetic git commit identity.

The conftest must supply GIT_AUTHOR_*/GIT_COMMITTER_* so a test that commits in a
throwaway repo works WITHOUT the dev's ~/.gitconfig — the divergence that made main
CI-red three times (green on macOS-with-global-config, red on CI's clean runner).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_conftest_sets_hermetic_commit_identity() -> None:
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        assert os.environ.get(var), f"conftest must pin {var} for hermetic commits (#295)"


def test_commit_works_when_git_cannot_auto_derive_identity(tmp_path: Path) -> None:
    # user.useConfigOnly=true disables git's OS-user-database auto-derivation — the
    # macOS behavior that MASKED this bug locally (a commit succeeded on the dev box
    # while CI's clean runner, which cannot auto-derive, died with exit 128). With
    # auto-derivation off on BOTH platforms, the commit succeeds ONLY because the
    # conftest pins GIT_COMMITTER_*/GIT_AUTHOR_* in the env. Deterministic red-proof:
    # this fails without the conftest fix on any platform, passes with it.
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, env=os.environ, check=True)
    result = subprocess.run(
        ["git", "-c", "user.useConfigOnly=true", "commit", "-q", "--allow-empty", "-m", "hermetic"],
        cwd=repo,
        env=os.environ,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
