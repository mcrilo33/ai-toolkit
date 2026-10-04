import re
import shutil

import pytest

from conftest import V2, git


def setup(run, repo, branch="wp0", wt=None, mode=()):
    wt = wt or repo.wt(branch)
    return wt, run(["bash", f"{V2}/scripts/setup.sh", *mode], cwd=wt, ORCA_ROOT_PATH=repo.root, ORCA_WORKTREE_PATH=wt)


def test_provisions_claude_dir_run_id_and_excludes_and_is_idempotent(run, repo, stubs):
    wt, r = setup(run, repo)
    rid = (wt / ".ai-toolkit/spoke-run-id").read_text()
    assert r.returncode == 0, r.stderr
    assert rid.strip() and (wt / ".claude/hooks/guard.sh").exists() and git(wt, "status", "--porcelain") == ""
    assert (wt / ".ai-toolkit/setup-done").exists()
    assert setup(run, repo, wt=wt)[1].returncode == 0 and (wt / ".ai-toolkit/spoke-run-id").read_text() == rid
    assert (repo.root / ".git/info/exclude").read_text().count(".ai-toolkit/") == 1


def test_fails_loud_when_gh_fails_or_the_root_has_no_claude_dir(run, repo, stubs):
    stubs.reply("gh", "boom", rc=1)
    assert "issue 12" in setup(run, repo, "12-x")[1].stderr
    wt, _ = setup(run, repo)
    shutil.rmtree(repo.root / ".claude")
    r = setup(run, repo, wt=wt)[1]
    assert r.returncode != 0 and ".claude" in r.stderr and not (wt / ".ai-toolkit/setup-done").exists()


@pytest.mark.parametrize("branch,linked,issue", [("12-add-hello", None, "12"), ("12-add-hello", 7, "7"), ("wp0", None, None)])
def test_task_md_comes_from_the_linked_issue_or_the_branch_number(run, repo, stubs, branch, linked, issue):
    if linked:
        stubs.reply("orca.worktree_show", f'{{"result":{{"worktree":{{"linkedIssue":{linked}}}}}}}')
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it.\\nGate: plan"}')
    wt, r = setup(run, repo, branch)
    assert r.returncode == 0, r.stderr
    if issue:
        assert stubs.calls("gh")[0][:3] == ["issue", "view", issue] and stubs.calls("orca")[0][-1] == "--json"
        assert "#12 Add hello" in (wt / ".ai-toolkit/task.md").read_text()
    else:
        assert stubs.calls("gh") == [] and not (wt / ".ai-toolkit/task.md").exists()


@pytest.mark.parametrize("mode", [(), ("--refresh",)], ids=["setup", "refresh"])
def test_runs_the_hosts_setup_local_hook(run, repo, stubs, mode):
    (repo.root / ".ai-toolkit").mkdir()
    (repo.root / ".ai-toolkit/setup.local.sh").write_text("touch hook-ran\n")
    assert (setup(run, repo, mode=mode)[0] / "hook-ran").exists()


def test_refresh_recopies_claude_but_keeps_run_id_and_task_md_and_asks_nobody(run, repo, stubs):
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it."}')
    wt, r = setup(run, repo, "12-x")
    rid, task = (wt / ".ai-toolkit/spoke-run-id").read_text(), (wt / ".ai-toolkit/task.md").read_text()
    (repo.root / ".claude/hooks/guard.sh").write_text("#!/bin/sh\nnewer\n")   # main moved on while this worktree was kept
    (wt / ".ai-toolkit/task.md").write_text("edited by the worker")
    (wt / ".ai-toolkit/setup-done").unlink()
    calls = len(stubs.calls("gh")) + len(stubs.calls("orca"))
    r = setup(run, repo, wt=wt, mode=("--refresh",))[1]
    assert r.returncode == 0, r.stderr
    assert (wt / ".claude/hooks/guard.sh").read_text() == "#!/bin/sh\nnewer\n" and (wt / ".ai-toolkit/setup-done").exists()
    assert (wt / ".ai-toolkit/spoke-run-id").read_text() == rid and (wt / ".ai-toolkit/task.md").read_text() == "edited by the worker" != task
    assert len(stubs.calls("gh")) + len(stubs.calls("orca")) == calls   # no issue fetch, no orca lookup
    git(repo.root, "add", "-f", ".claude/hooks/guard.sh")   # the repo tracks part of .claude: a refresh must not dirty or clobber it
    git(repo.root, "commit", "-qm", "track guard")
    (wt / ".claude/hooks/guard.sh").unlink()
    git(wt, "merge", "-q", "main")   # the branch now tracks it too
    (repo.root / ".claude/hooks/guard.sh").write_text("main moved again\n")
    (repo.root / ".claude/hooks/extra.sh").write_text("untracked\n")
    (wt / ".claude/hooks/guard.sh").write_text("worker edit\n")
    assert setup(run, repo, wt=wt, mode=("--refresh",))[1].returncode == 0
    assert (wt / ".claude/hooks/extra.sh").read_text() == "untracked\n" and (wt / ".claude/hooks/guard.sh").read_text() == "worker edit\n"
    shutil.rmtree(repo.root / ".claude")
    r = setup(run, repo, wt=wt, mode=("--refresh",))[1]   # a failed refresh is loud and leaves no ready marker
    assert r.returncode != 0 and ".claude" in r.stderr and not (wt / ".ai-toolkit/setup-done").exists()


def test_archive_is_a_noop_and_orca_yaml_points_at_real_scripts(run, tmp_path):
    assert all(run(["bash", f"{V2}/scripts/archive.sh"], cwd=tmp_path).returncode == 0 for _ in range(2))
    text = (V2 / "orca.yaml").read_text()
    assert "setupAgentStartupPolicy: wait-for-setup" in text
    assert [(V2 / s).is_file() for s in re.findall(r"(?:setup|archive): \./(\S+)", text)] == [True, True]


def test_setup_reads_the_gh_override_from_the_local_env_file(run, repo, tmp_path):
    alt = tmp_path / "alt-gh"
    alt.write_text('#!/bin/sh\necho \'{"number":12,"title":"Via alt","body":""}\'\n')
    alt.chmod(0o755)
    (repo.root / ".ai-toolkit").mkdir()
    (repo.root / ".ai-toolkit/ai-toolkit.local.env").write_text(f"AI_TOOLKIT_GH={alt}\n")
    wt, r = setup(run, repo, "12-x")
    assert r.returncode == 0, r.stderr
    assert "#12 Via alt" in (wt / ".ai-toolkit/task.md").read_text()
