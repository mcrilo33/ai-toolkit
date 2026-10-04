import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import V2


@pytest.fixture
def tool(tmp_path, stubs):
    root = tmp_path / "tool"
    shutil.copytree(V2, root)
    stubs.reply("orca", "1.4.219")
    return root


def synced(repo):
    """The repo as sync.sh leaves it: native git hooks under .ai-toolkit/hooks/git."""
    hook = repo.root / ".ai-toolkit" / "hooks" / "git" / "pre-commit"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n")
    return hook


def local_hooks_path(repo):
    return subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "core.hooksPath"],
                          capture_output=True, text=True).stdout.strip()


def test_install_sets_core_hooks_path_on_the_target_repo_only(run, tool, repo, tmp_path):
    hook = synced(repo)
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    r = run(["bash", str(tool / "scripts" / "install.sh")], cwd=repo.root)
    assert r.returncode == 0, r.stderr
    assert local_hooks_path(repo.root) == str(repo.root / ".ai-toolkit" / "hooks" / "git")
    assert os.access(hook, os.X_OK)
    assert local_hooks_path(other) == ""  # no other repo is bound
    assert "hooksPath" not in (tmp_path / "gitconfig").read_text()  # and never the global config


def test_install_takes_the_target_repo_as_an_argument(run, tool, repo, tmp_path):
    synced(repo)
    r = run(["bash", str(tool / "scripts" / "install.sh"), str(repo.root)], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert local_hooks_path(repo.root) == str(repo.root / ".ai-toolkit" / "hooks" / "git")


def test_install_in_a_repo_that_was_not_synced_wires_nothing(run, tool, repo):
    r = run(["bash", str(tool / "scripts" / "install.sh")], cwd=repo.root)
    assert r.returncode == 0 and "sync.sh" in r.stderr and local_hooks_path(repo.root) == ""


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


def test_a_test_over_the_time_limit_fails_naming_its_duration(pytester):
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile("import time\n\n\ndef test_slow():\n    time.sleep(0.6)\n\n\ndef test_fast():\n    pass\n")

    r = pytester.runpytest_inprocess("-o", "test_time_limit=0.3")

    r.assert_outcomes(passed=2, errors=1)
    r.stdout.re_match_lines([r".*test_slow.* took 0\.[6-9]\d* s \(.*\), limit 0\.3 s"])
    assert "test_fast" not in r.stdout.str().split("ERRORS")[-1]
