import re
import shutil

import pytest

from conftest import V2, git


def setup(run, repo, branch="wp0", wt=None, mode=()):
    wt = wt or repo.wt(branch)
    return wt, run(["bash", f"{V2}/scripts/setup.sh", *mode], cwd=wt, ORCA_ROOT_PATH=repo.root, ORCA_WORKTREE_PATH=wt, ORCA_LINK_TRIES=3, AI_TOOLKIT_POLL=0)


def link(stubs, n):   # what `orca worktree show` answers for the worktree's linked issue
    stubs.reply("orca.worktree_show", f'{{"result":{{"worktree":{{"linkedIssue":{n}}}}}}}')


def test_provisions_claude_dir_run_id_and_excludes_and_is_idempotent(run, repo, stubs):
    (repo.root / ".ai-toolkit/rules").mkdir(parents=True)
    (repo.root / ".ai-toolkit/rules/bug-triage.md").write_text("main rule\n")   # the seed names this path: a worker needs it too
    wt, r = setup(run, repo)
    rid = (wt / ".ai-toolkit/spoke-run-id").read_text()
    assert r.returncode == 0, r.stderr
    assert rid.strip() and (wt / ".claude/hooks/guard.sh").exists() and git(wt, "status", "--porcelain") == ""
    assert (wt / ".ai-toolkit/setup-done").exists() and (wt / ".ai-toolkit/rules/bug-triage.md").read_text() == "main rule\n"
    assert setup(run, repo, wt=wt)[1].returncode == 0 and (wt / ".ai-toolkit/spoke-run-id").read_text() == rid
    assert (repo.root / ".git/info/exclude").read_text().count(".ai-toolkit/") == 1


def test_fails_loud_when_gh_fails_or_the_root_has_no_claude_dir(run, repo, stubs):
    stubs.reply("gh", "boom", rc=1)
    wt, r = setup(run, repo, "12-x")   # first provisioning still stops on a failed fetch (only a refresh keeps the old task.md)
    assert r.returncode != 0 and "issue 12" in r.stderr and not (wt / ".ai-toolkit/setup-done").exists()
    wt, _ = setup(run, repo)
    shutil.rmtree(repo.root / ".claude")
    r = setup(run, repo, wt=wt)[1]
    assert r.returncode != 0 and ".claude" in r.stderr and not (wt / ".ai-toolkit/setup-done").exists()


@pytest.mark.parametrize("branch,linked,issue,mode", [
    ("12-add-hello", None, "12", ()), ("12-add-hello", 7, "7", ()), ("12-add-hello", '"x;y"', "12", ()), ("wp0", None, None, ()),
    ("12-add-hello", "boom", "12", ()),   # first provisioning: no worker has run yet, so the branch name the coordinator chose may stand in for the link
    ("12-add-hello", None, None, ("--refresh",)), ("12-add-hello", "boom", None, ("--refresh",)),
    ("12-add-hello", 7, "7", ("--refresh",)),   # a refresh (a worker has run, and may have renamed its branch) reads Orca's link only
])
def test_task_md_comes_from_the_linked_issue_and_only_first_provisioning_may_use_the_branch_number(run, repo, stubs, branch, linked, issue, mode):
    (repo.root / ".ai-toolkit").mkdir(exist_ok=True)
    (repo.root / ".ai-toolkit/sync-manifest").write_text(".claude/hooks/guard.sh\n")   # a refresh needs the synced set
    if linked == "boom":
        stubs.reply("orca.worktree_show", "boom", rc=1)
    else:
        link(stubs, "null" if linked is None else linked)   # null: Orca answers, the worktree has no linked issue yet
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it.\\nGate: plan"}')
    wt = repo.wt(branch)
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit/task.md").write_text("# #99 old\n")
    wt, r = setup(run, repo, wt=wt, mode=mode)
    assert r.returncode == 0, r.stderr
    if issue:
        assert stubs.calls("gh")[0][:3] == ["issue", "view", issue] and stubs.calls("orca")[0][-1] == "--json"
        assert (wt / ".ai-toolkit/task.md").read_text() == "# #12 Add hello\n\nDo it.\nGate: plan\n"   # the format the gates' refresh writes too (one definition in lib.sh)
    else:
        assert stubs.calls("gh") == [] and (wt / ".ai-toolkit/task.md").read_text() == "# #99 old\n"
        assert bool(mode) == ("keeping the existing task.md" in r.stderr)
    assert len(stubs.calls("orca")) == (3 if mode and linked == "boom" else 1)   # a refresh waits for Orca up to the bound; first provisioning asks once


@pytest.mark.parametrize("mode", [(), ("--refresh",)], ids=["setup", "refresh"])
def test_runs_the_hosts_setup_local_hook(run, repo, stubs, mode):
    (repo.root / ".ai-toolkit").mkdir()
    (repo.root / ".ai-toolkit/setup.local.sh").write_text("touch hook-ran\n")
    (repo.root / ".ai-toolkit/sync-manifest").write_text(".claude/hooks/guard.sh\n")
    assert (setup(run, repo, mode=mode)[0] / "hook-ran").exists()


def test_refresh_recopies_claude_and_task_md_but_keeps_run_id_and_a_failed_fetch_keeps_the_old_task_md(run, repo, stubs):
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it."}')
    link(stubs, 12)   # a refresh learns the issue from Orca, never from the branch
    (repo.root / ".ai-toolkit").mkdir()
    manifest = repo.root / ".ai-toolkit/sync-manifest"
    (repo.root / ".claude/hooks/old.sh").write_text("old\n")
    (repo.root / ".claude/settings.local.json").write_text("main local\n")
    (repo.root / ".ai-toolkit/rules").mkdir()
    (repo.root / ".ai-toolkit/rules/bug-triage.md").write_text("rule\n")
    (repo.root / ".ai-toolkit/rules/old-rule.md").write_text("old rule\n")
    manifest.write_text(".claude/hooks/guard.sh\n.claude/hooks/old.sh\n.ai-toolkit/rules/bug-triage.md\n.ai-toolkit/rules/old-rule.md\n")
    wt, r = setup(run, repo, "12-x")
    rid, task = (wt / ".ai-toolkit/spoke-run-id").read_text(), (wt / ".ai-toolkit/task.md").read_text()
    (repo.root / ".claude/hooks/guard.sh").write_text("#!/bin/sh\nnewer\n")   # main moved on while this worktree was kept
    (wt / ".ai-toolkit/task.md").write_text("edited by the worker")
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it differently."}')   # the issue was rewritten after the first dispatch
    (wt / ".ai-toolkit/setup-done").unlink()
    (wt / ".claude/settings.local.json").write_text("worker grants\n")   # outside the synced set: neither copied over nor deleted
    (wt / ".claude/worker-note").write_text("mine\n")
    (repo.root / ".claude/settings.local.json").write_text("main local moved\n")
    (repo.root / ".claude/hooks/old.sh").unlink()   # a land's sync dropped old.sh from main
    (repo.root / ".ai-toolkit/rules/bug-triage.md").write_text("rule, moved on\n")   # a land changed a rule; another was dropped from the synced set
    (repo.root / ".ai-toolkit/rules/old-rule.md").unlink()
    manifest.write_text(".claude/hooks/guard.sh\n.ai-toolkit/rules/bug-triage.md\n")
    calls = len(stubs.calls("gh"))
    r = setup(run, repo, wt=wt, mode=("--refresh",))[1]
    assert r.returncode == 0, r.stderr
    assert (wt / ".ai-toolkit/rules/bug-triage.md").read_text() == "rule, moved on\n" and not (wt / ".ai-toolkit/rules/old-rule.md").exists()
    assert not (wt / ".claude/hooks/old.sh").exists() and (wt / ".claude/worker-note").read_text() == "mine\n"
    assert (wt / ".claude/settings.local.json").read_text() == "worker grants\n"
    assert (wt / ".claude/hooks/guard.sh").read_text() == "#!/bin/sh\nnewer\n" and (wt / ".ai-toolkit/setup-done").exists()
    assert (wt / ".ai-toolkit/spoke-run-id").read_text() == rid and "Do it differently." in (wt / ".ai-toolkit/task.md").read_text() != task
    assert len(stubs.calls("gh")) == calls + 1   # the refresh reads the issue once
    assert (wt / ".ai-toolkit/task.md").read_text() == "# #12 Add hello\n\nDo it differently.\n"
    stubs.reply("gh", "boom", rc=1)   # an outage must not block a re-dispatch: the old text stays and the warning names the issue
    kept = (wt / ".ai-toolkit/task.md").read_text()
    r = setup(run, repo, wt=wt, mode=("--refresh",))[1]
    assert r.returncode == 0 and "issue 12" in r.stderr and (wt / ".ai-toolkit/task.md").read_text() == kept and (wt / ".ai-toolkit/setup-done").exists()
    stubs.reply("gh", '{"number":12,"title":"Add hello","body":"Do it differently."}')
    git(repo.root, "add", "-f", ".claude/hooks/guard.sh")   # the repo tracks part of .claude: a refresh must not dirty or clobber it
    git(repo.root, "commit", "-qm", "track guard")
    (wt / ".claude/hooks/guard.sh").unlink()
    git(wt, "merge", "-q", "main")   # the branch now tracks it too
    (repo.root / ".claude/hooks/guard.sh").write_text("main moved again\n")
    (repo.root / ".claude/hooks/extra.sh").write_text("untracked\n")
    manifest.write_text(".claude/hooks/guard.sh\n.claude/hooks/extra.sh\n.ai-toolkit/rules/bug-triage.md\n")
    (wt / ".claude/hooks/guard.sh").write_text("worker edit\n")
    assert setup(run, repo, wt=wt, mode=("--refresh",))[1].returncode == 0
    assert (wt / ".claude/hooks/extra.sh").read_text() == "untracked\n" and (wt / ".claude/hooks/guard.sh").read_text() == "worker edit\n"
    manifest.write_text(".claude/hooks/extra.sh\n.ai-toolkit/rules/bug-triage.md\n")   # the branch tracks guard.sh, so main dropping it from the synced set does not delete it
    assert setup(run, repo, wt=wt, mode=("--refresh",))[1].returncode == 0 and (wt / ".claude/hooks/guard.sh").read_text() == "worker edit\n"
    (repo.root / ".git/info/exclude").write_text("")   # a repo that tracks .claude does not ignore it (an ignored file would be merged over silently)
    (repo.root / ".claude/rules").mkdir()
    (repo.root / ".claude/rules/added.md").write_text("tracked on main only\n")
    git(repo.root, "add", "-f", ".claude/rules/added.md")
    git(repo.root, "commit", "-qm", "main starts tracking a rule")
    assert setup(run, repo, wt=wt, mode=("--refresh",))[1].returncode == 0
    git(wt, "merge", "-q", "--no-edit", "main")   # land's merge of main must not trip over an untracked copy of what main tracks
    (wt / "escape").write_text("outside .claude\n")
    with (wt / ".ai-toolkit/synced-claude").open("a") as f:
        f.write(".claude/../escape\n")   # a record entry that climbs out of .claude/ is never deleted
    assert setup(run, repo, wt=wt, mode=("--refresh",))[1].returncode == 0 and (wt / "escape").exists()
    for cut in (None, ""):   # a missing or empty manifest is a failed refresh, not a licence to delete every recorded copy
        manifest.unlink(missing_ok=True) if cut is None else manifest.write_text(cut)
        r = setup(run, repo, wt=wt, mode=("--refresh",))[1]
        assert r.returncode != 0 and "manifest" in r.stderr and (wt / ".claude/hooks/extra.sh").exists() and not (wt / ".ai-toolkit/setup-done").exists()
        assert (wt / ".ai-toolkit/rules/bug-triage.md").exists()   # a failed refresh deletes no rule either
    shutil.rmtree(repo.root / ".claude")
    r = setup(run, repo, wt=wt, mode=("--refresh",))[1]   # a failed refresh is loud and leaves no ready marker
    assert r.returncode != 0 and ".claude" in r.stderr and not (wt / ".ai-toolkit/setup-done").exists()


def test_archive_is_a_noop_and_orca_yaml_points_at_real_scripts(run, tmp_path):
    assert all(run(["bash", f"{V2}/scripts/archive.sh"], cwd=tmp_path).returncode == 0 for _ in range(2))
    text = (V2 / "orca.yaml").read_text()
    assert "setupAgentStartupPolicy: wait-for-setup" in text
    assert [(V2 / s).is_file() for s in re.findall(r"(?:setup|archive): \./(\S+)", text)] == [True, True]


def test_setup_reads_the_gh_override_from_the_local_env_file(run, repo, tmp_path, link_script):
    alt = link_script(tmp_path / "alt-gh", '#!/bin/sh\necho \'{"number":12,"title":"Via alt","body":""}\'\n')
    (repo.root / ".ai-toolkit").mkdir()
    (repo.root / ".ai-toolkit/ai-toolkit.local.env").write_text(f"AI_TOOLKIT_GH={alt}\n")
    wt, r = setup(run, repo, "12-x")
    assert r.returncode == 0, r.stderr
    assert "#12 Via alt" in (wt / ".ai-toolkit/task.md").read_text()
