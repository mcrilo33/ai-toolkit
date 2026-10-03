import os
import shutil

import pytest

from conftest import V2, git


@pytest.fixture
def tool(tmp_path, stubs):
    """A copy of v2/ with a git hooks dir (WP3 ships the real one) so install.sh has something to wire."""
    root = tmp_path / "tool"
    shutil.copytree(V2, root)
    (root / "hooks" / "git").mkdir(parents=True)
    (root / "hooks" / "git" / "pre-commit").write_text("#!/bin/sh\n")
    stubs.reply("orca", "1.4.219")
    return root


def test_install_points_core_hooks_path_at_the_synced_git_hooks(run, tool, repo):
    r = run(["bash", str(tool / "scripts" / "install.sh")], cwd=repo.root)
    assert r.returncode == 0, r.stderr
    assert git(repo.root, "config", "core.hooksPath") == str(tool / "hooks" / "git")
    assert os.access(tool / "hooks" / "git" / "pre-commit", os.X_OK)


def test_install_outside_a_git_repo_warns_and_still_succeeds(run, tool, tmp_path):
    r = run(["bash", str(tool / "scripts" / "install.sh")], cwd=tmp_path)
    assert r.returncode == 0 and "native git hooks" in r.stderr


def test_install_explains_why_jq_is_mandatory(run, tool, tmp_path):
    bin_ = tmp_path / "nojq"
    bin_.mkdir()
    for name in ("git", "dirname"):
        (bin_ / name).symlink_to(shutil.which(name))
    for name in ("orca", "gh", "python3"):
        (bin_ / name).write_text("#!/bin/sh\n")
        (bin_ / name).chmod(0o755)
    r = run(["/bin/bash", str(tool / "scripts" / "install.sh")], PATH=str(bin_))
    assert r.returncode != 0 and "jq" in r.stderr and "fail closed" in r.stderr
