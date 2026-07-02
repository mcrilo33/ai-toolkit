"""The worktree scripts must run unmodified from a synced target's location.

Issue #18 installs worktree-{new,land,done,lib}.sh into a target's
``.ai-toolkit/scripts/`` and has the skills invoke them there. That only works
if the scripts run correctly from a foreign checkout — i.e. they locate the main
worktree by git introspection (``wt_main_root`` via ``git worktree list``) and
source their siblings by ``$SCRIPT_DIR`` (``BASH_SOURCE``), never by a path
relative to the ai-toolkit repo. These tests copy the scripts into a fresh
repo's ``.ai-toolkit/scripts/`` and drive them from there.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
HOOKS_LIB_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib"
WORKTREE_SCRIPTS = ("worktree-new.sh", "worktree-land.sh", "worktree-done.sh", "worktree-lib.sh")
# Co-located next to the scripts by sync-to-repo.sh (like telemetry.sh) so
# worktree-lib.sh finds it as a sibling in a synced target (issue #117).
COLOCATED_LIBS = ("base-branch.sh",)

# Isolate from the host's git config (this repo ships installable git hooks).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def synced_repo(tmp_path: Path) -> Path:
    """A fresh repo with the worktree scripts copied into .ai-toolkit/scripts/,
    exactly as sync-to-repo.sh would install them — no ai-toolkit checkout in sight."""
    repo = tmp_path / "project"
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
    _git(repo, "commit", "-qm", "chore: seed")

    scripts_dst = repo / ".ai-toolkit" / "scripts"
    scripts_dst.mkdir(parents=True)
    for name in WORKTREE_SCRIPTS:
        dst = scripts_dst / name
        dst.write_bytes((SCRIPTS_DIR / name).read_bytes())
        dst.chmod(0o755)
    for name in COLOCATED_LIBS:
        dst = scripts_dst / name
        dst.write_bytes((HOOKS_LIB_DIR / name).read_bytes())
        dst.chmod(0o755)
    return repo


def _run(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Drop the host's spoke marker so worktree-{land,done}.sh aren't refused by
    # the issue #26 role guard when these tests happen to run inside a spoke.
    env = {**_GIT_ENV}
    env.pop("WT_SPOKE", None)
    return subprocess.run(
        ["bash", str(repo / ".ai-toolkit" / "scripts" / script), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


def test_worktree_new_runs_from_ai_toolkit_scripts(synced_repo: Path) -> None:
    """worktree-new.sh creates the worktree+branch when run from .ai-toolkit/scripts/."""
    proc = _run(synced_repo, "worktree-new.sh", "42", "demo", "--no-code", "--no-terminal")

    assert proc.returncode == 0, proc.stderr
    wt_dir = synced_repo.parent / f"{synced_repo.name}-42"
    assert wt_dir.is_dir(), f"worktree not created at {wt_dir}"
    branches = _git(synced_repo, "branch", "--list", "feature/42-demo")
    assert "feature/42-demo" in branches


def test_worktree_done_resolves_sibling_lib(synced_repo: Path) -> None:
    """worktree-done.sh sources worktree-lib.sh from its own dir and tears down."""
    _run(synced_repo, "worktree-new.sh", "42", "demo", "--no-code", "--no-terminal")

    proc = _run(synced_repo, "worktree-done.sh", "42", "--no-code", "--force")

    assert proc.returncode == 0, proc.stderr
    wt_dir = synced_repo.parent / f"{synced_repo.name}-42"
    assert not wt_dir.exists(), f"worktree still present at {wt_dir}"
