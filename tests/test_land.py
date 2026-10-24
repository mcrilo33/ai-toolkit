import json
import shutil
import subprocess

import pytest
from conftest import V2, git

LAND = str(V2 / "scripts/land.sh")
BRANCH = "9-feat"
GREEN = json.dumps([{"status": "completed", "conclusion": "success", "url": "u"}])
HOOK = """#!/bin/sh
Z=0000000000000000000000000000000000000000
while read old new ref; do
  [ "$new" = "$Z" ] && k=delete || k=push
  printf 'git\\037%s\\037%s\\n' "$k" "${ref#refs/heads/}" >> "$STUB_DIR/calls.log"
done
"""
POST_LAND = """#!/bin/bash
printf 'post-land' >> "$STUB_DIR/calls.log"; for a in "$@"; do printf '\\037%s' "$a" >> "$STUB_DIR/calls.log"; done
printf '\\037BASE_BRANCH=%s\\n' "$BASE_BRANCH" >> "$STUB_DIR/calls.log"; exit "${PL_RC:-0}"
"""


def commit(cwd, name, text="x", push=None, msg=None):
    (cwd / name).parent.mkdir(exist_ok=True)
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", msg or f"add {name}")
    if push:
        git(cwd, "push", "-q", "origin", push)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def L(stubs, repo, run, tmp_path, link_script):
    origin = tmp_path / "origin.git"
    link_script(origin / "hooks/post-receive", HOOK)
    wt = repo.wt(BRANCH)
    tip = commit(wt, "feature.txt", push=BRANCH)
    git(repo.root, "fetch", "-q")
    log = tmp_path / "stubs/calls.log"
    log.write_text("")   # drop the setup pushes: only land's own calls count
    stubs.reply("orca.worktree_show", json.dumps({"result": {"worktree": {"path": str(wt), "branch": f"refs/heads/{BRANCH}"}}}))
    stubs.reply("orca.orchestration_worker_list",
                json.dumps({"result": {"workers": [{"dispatchId": "ctx_other", "resource": {"worktreeId": "r::/elsewhere"}},
                                                   {"dispatchId": "ctx_9", "resource": {"worktreeId": f"r::{wt}"}}]}}))
    stubs.reply("gh.run_list", GREEN)

    def go(*args, cwd=None, **env):
        return run(["bash", LAND, *args], cwd=cwd or repo.root, AI_TOOLKIT_POLL=0, CI_TRIES=3, **env)

    def trail():   # every orca/gh call and every ref update on origin, in order
        rows = [ln.split("\x1f") for ln in log.read_text().splitlines()]
        return [(r[0], *[a for a in r[1:] if a not in ("--json",)]) for r in rows]

    def other_push(name, text="y"):   # somebody else moves origin/main
        o = tmp_path / "other"
        if not o.exists():
            subprocess.run(["git", "clone", "-q", str(origin), str(o)], check=True, capture_output=True)
        sha = commit(o, name, text, push="main")
        log.write_text("")   # setup, not part of land's trail
        return sha

    return type("L", (), dict(go=staticmethod(go), trail=staticmethod(trail), other_push=staticmethod(other_push), wt=wt, tip=tip,
                              root=repo.root, origin=origin, stubs=stubs, tmp=tmp_path, git=staticmethod(git)))


def main_sha(L):
    return git(L.origin, "rev-parse", "main")


def no_side_effects(L, before):
    assert main_sha(L) == before
    assert not [t for t in L.trail() if t[:3] in (("gh", "issue", "close"), ("orca", "worktree", "rm"))]


def test_ci_land_runs_every_step_in_order(L):
    r = L.go("9")
    assert r.returncode == 0, r.stderr
    sha = main_sha(L)
    assert sha == L.tip and git(L.root, "rev-parse", "HEAD") == sha
    t = L.trail()
    assert t == [
        ("orca", "worktree", "show", "--worktree", "issue:9"),
        ("gh", "run", "list", "--commit", L.tip, "status,conclusion,url"),
        ("git", "push", "main"),
        ("gh", "issue", "close", "9", "-c", f"landed in {sha}"),
        ("gh", "issue", "edit", "9", "--remove-label", "status:in-progress"),
        ("git", "delete", BRANCH),
        ("orca", "orchestration", "worker-list"),
        ("orca", "orchestration", "worker-release", "--dispatch", "ctx_9", *t[7][5:]),
        ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks"),
    ]
    assert "--retry-request" in t[7]
    # a repo without the sync sources is no toolkit checkout: land installs nothing and touches no installed copy
    assert not (L.root / ".ai-toolkit").exists() and (L.root / ".claude/hooks/guard.sh").read_text() == "#!/bin/sh\n"


def test_explicit_dispatch_skips_the_worker_list_lookup(L):
    assert L.go("--dispatch", "ctx_x", "9").returncode == 0
    assert ("orca", "orchestration", "worker-list") not in [t[:3] for t in L.trail()]
    assert [t for t in L.trail() if t[2:3] == ("worker-release",)][0][3:5] == ("--dispatch", "ctx_x")


def test_a_red_ci_merges_nothing(L):
    L.stubs.reply("gh.run_list", json.dumps([{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "failure"}]))
    before = main_sha(L)
    r = L.go("9")
    assert r.returncode == 4 and "red" in r.stderr
    no_side_effects(L, before)
    assert not [t for t in L.trail() if t[0] == "git"]


def test_ci_that_has_not_started_or_is_pending_is_polled_then_times_out(L):
    L.stubs.reply("gh.run_list", "[]", n=1)
    L.stubs.reply("gh.run_list", json.dumps([{"status": "in_progress", "conclusion": ""}]), n=2)
    L.stubs.reply("gh.run_list", GREEN, n=3)
    assert L.go("9").returncode == 0
    assert len([t for t in L.trail() if t[:3] == ("gh", "run", "list")]) == 3


def test_ci_that_never_finishes_times_out_without_merging(L):
    L.stubs.reply("gh.run_list", "[]")
    before = main_sha(L)
    r = L.go("9")
    assert r.returncode == 4 and "timed out" in r.stderr
    no_side_effects(L, before)


def test_main_moved_merges_main_on_the_spoke_and_checks_the_merged_tip(L):
    moved = L.other_push("from_main.txt")
    r = L.go("9")
    assert r.returncode == 0, r.stderr
    sha = main_sha(L)
    assert sha != L.tip and git(L.root, "rev-parse", f"{sha}^2") == moved   # merge commit: main is its second parent
    assert git(L.root, "ls-tree", "--name-only", "-r", sha).split() == ["README", "feature.txt", "from_main.txt"]
    t = L.trail()
    assert [x for x in t if x[0] == "git"] == [("git", "push", BRANCH), ("git", "push", "main"), ("git", "delete", BRANCH)]
    assert [x for x in t if x[:3] == ("gh", "run", "list")][-1][4] == sha   # CI was asked about the merged tip


def test_a_merge_conflict_exits_5_and_leaves_the_spoke_clean(L):
    L.other_push("feature.txt", "conflicting")
    before = main_sha(L)
    r = L.go("9")
    assert r.returncode == 5 and "conflict" in r.stderr
    no_side_effects(L, before)
    assert git(L.wt, "status", "--porcelain") == ""
    assert subprocess.run(["git", "-C", str(L.wt), "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True).returncode != 0
    assert git(L.wt, "rev-parse", "HEAD") == L.tip


def test_local_gate_runs_check_cmd_on_the_merged_tip_instead_of_ci(L):
    L.other_push("from_main.txt")
    r = L.go("--local-gate", "9", CHECK_CMD="test -f feature.txt && test -f from_main.txt && pwd > ran-in")
    assert r.returncode == 0, r.stderr
    assert (L.wt / "ran-in").read_text().strip() == str(L.wt.resolve())
    assert not [t for t in L.trail() if t[:3] == ("gh", "run", "list")]


def test_local_gate_env_switches_the_gate_without_the_flag(L):   # the coordinator lands with no flags: the local env file decides
    r = L.go("9", LOCAL_GATE=1, CHECK_CMD="test -f feature.txt")
    assert r.returncode == 0, r.stderr
    assert not [t for t in L.trail() if t[:3] == ("gh", "run", "list")]


def test_a_red_local_gate_merges_nothing(L):
    before = main_sha(L)
    r = L.go("--local-gate", "9", CHECK_CMD="exit 3")
    assert r.returncode == 4 and "local gate" in r.stderr
    no_side_effects(L, before)


def test_main_moving_during_the_gate_triggers_another_round(L):
    L.other_push("seed.txt")   # creates the clone; the land below merges it in round 1
    cmd = (f"echo g >> {L.tmp}/gates; [ -f {L.tmp}/moved ] || {{ touch {L.tmp}/moved; cd {L.tmp}/other && echo z > z.txt "
           f"&& git add z.txt && git commit -qm z && git push -q origin main; }}")
    r = L.go("--local-gate", "9", CHECK_CMD=cmd)
    assert r.returncode == 0, r.stderr
    assert len((L.tmp / "gates").read_text().split()) == 2
    assert git(L.root, "ls-tree", "--name-only", "-r", main_sha(L)).split() == ["README", "feature.txt", "seed.txt", "z.txt"]


def test_a_rejected_main_push_restores_local_main_and_closes_nothing(L, link_script):
    link_script(L.origin / "hooks/pre-receive", '#!/bin/sh\nwhile read o n r; do [ "$r" = refs/heads/main ] && exit 1; done\nexit 0\n')
    before = git(L.root, "rev-parse", "HEAD")
    r = L.go("9")
    assert r.returncode == 1 and "push" in r.stderr
    assert git(L.root, "rev-parse", "HEAD") == before and main_sha(L) == before
    assert not [t for t in L.trail() if t[:3] in (("gh", "issue", "close"), ("orca", "worktree", "rm"))]


def test_cleanup_failures_are_reported_after_landing_and_do_not_stop_the_rest(L):
    L.stubs.reply("gh.issue_close", "boom", rc=1)
    r = L.go("9")
    assert r.returncode == 6 and "cleanup" in r.stderr
    assert main_sha(L) == L.tip
    assert ("git", "delete", BRANCH) in L.trail() and ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks") in L.trail()


def test_usage_errors_exit_2(L):
    assert L.go().returncode == 2 and L.go("--bogus", "9").returncode == 2 and L.trail() == []


def test_refuses_to_run_inside_a_worktree(L):
    r = L.go("9", cwd=L.wt)
    assert r.returncode == 2 and "not a worktree" in r.stderr and L.trail() == []


def test_refuses_off_the_base_branch_or_with_a_dirty_main(L):
    (L.root / "README").write_text("dirty")
    assert L.go("9").returncode == 2 and L.trail() == []
    git(L.root, "checkout", "-q", "--", "README")
    git(L.root, "checkout", "-q", "-b", "side")
    r = L.go("9")
    assert r.returncode == 2 and "main" in r.stderr and L.trail() == []


def test_refuses_an_unpushed_or_dirty_spoke(L):
    commit(L.wt, "late.txt")   # committed, not pushed
    r = L.go("9")
    assert r.returncode == 2 and "not pushed" in r.stderr and not [t for t in L.trail() if t[0] != "orca"]
    git(L.wt, "push", "-q", "origin", BRANCH)
    (L.wt / "feature.txt").write_text("edited")
    r = L.go("9")
    assert r.returncode == 2 and "dirty" in r.stderr


def review_script(tmp_path, link_script):
    return link_script(tmp_path / "review.sh", '#!/bin/sh\ngh review "$@"\nexit "${REVIEW_RC:-0}"\n')


def test_review_is_skipped_by_default(L, tmp_path, link_script):
    assert L.go("9", REVIEW_CMD=review_script(tmp_path, link_script)).returncode == 0
    assert ("gh", "review", "9") not in L.trail()


def test_review_runs_first_and_a_non_approval_exits_3(L, tmp_path, link_script):
    rev = review_script(tmp_path, link_script)
    before = main_sha(L)
    r = L.go("--review", "9", REVIEW_CMD=rev, REVIEW_RC=1)
    assert r.returncode == 3 and "review" in r.stderr
    no_side_effects(L, before)
    assert [t[:2] for t in L.trail()] == [("orca", "worktree"), ("gh", "review")]
    n = len(L.trail())
    assert L.go("--review", "9", REVIEW_CMD=rev).returncode == 0
    assert [t[:3] for t in L.trail()[n:n + 3]] == [("orca", "worktree", "show"), ("gh", "review", "9"), ("gh", "run", "list")]
    # the review judges the exact tip: the sha it gets is the sha gated and the sha fast-forwarded onto main
    reviewed = L.trail()[n + 1]
    assert reviewed == ("gh", "review", "9", L.tip) and L.trail()[n + 2][4] == L.tip and main_sha(L) == L.tip


@pytest.mark.parametrize("msg", ["feat: green #9", "Merge sneaky #9"])   # a single-parent commit titled Merge is no merge
def test_a_non_test_commit_that_changes_a_test_file_is_refused_before_the_review(L, tmp_path, link_script, msg):
    commit(L.wt, "tests/test_a.py", msg=msg, push=BRANCH)
    before = main_sha(L)
    r = L.go("--review", "9", REVIEW_CMD=review_script(tmp_path, link_script))
    assert r.returncode == 2 and msg in r.stderr and "tests/test_a.py" in r.stderr and "test(<scope>)" in r.stderr, r.stderr
    assert not [t for t in L.trail() if t[:2] in (("gh", "review"), ("gh", "run"))]
    no_side_effects(L, before)
    if msg[0] == "f":   # a host that sets TEST_PATHS nowhere is told the check is off
        (L.tmp / "bare.env").write_text("BASE_BRANCH=main\n")
        r = L.go("9", AI_TOOLKIT_ENV=str(L.tmp / "bare.env"))
        assert r.returncode == 0 and "TEST_PATHS is set nowhere" in r.stderr, r.stderr


def test_red_test_then_green_feat_lands_and_a_test_change_commit_lands_with_a_warning(L):
    commit(L.wt, "tests/test_a.py", msg="test(a): red #9")
    commit(L.wt, "src/a.py", msg="feat(a): green #9")
    commit(L.wt, "tests/test_b.py", msg="refactor: rename #9\n\nTest-Change: rename", push=BRANCH)
    r = L.go("9")
    assert r.returncode == 0 and "Test-Change commit touches test files: tests/test_b.py" in r.stderr, r.stderr
    assert {"src/a.py", "tests/test_a.py", "tests/test_b.py"} <= set(git(L.origin, "ls-tree", "--name-only", "-r", "main").split())


def test_the_merge_of_main_a_worker_makes_still_lands_when_main_carries_test_changes(L):
    L.other_push("tests/test_main.py")
    git(L.wt, "fetch", "-q")
    git(L.wt, "merge", "-q", "--no-edit", "origin/main")
    git(L.wt, "push", "-q", "origin", BRANCH)
    r = L.go("9")
    assert r.returncode == 0, r.stderr
    assert "tests/test_main.py" in git(L.origin, "ls-tree", "--name-only", "-r", "main").split()


@pytest.mark.parametrize("push", ["1", ""])   # the worker pushed a new commit, or only committed locally, while the review ran
def test_a_branch_moved_during_the_review_is_refused_so_only_the_reviewed_tip_can_land(L, tmp_path, link_script, push):
    rev = link_script(tmp_path / "mover.sh", f'#!/bin/sh\ngh review "$@"\ncd "$WT" && echo b > b.txt && git add b.txt && git commit -qm b && {{ [ -z "$PUSH" ] || git push -q origin HEAD:{BRANCH}; }}\n')
    before = main_sha(L)
    r = L.go("--review", "9", REVIEW_CMD=rev, WT=L.wt, PUSH=push)
    assert r.returncode == 2 and "moved during the review" in r.stderr and L.tip in r.stderr and git(L.wt, "rev-parse", "HEAD") in r.stderr, r.stderr
    no_side_effects(L, before)
    assert not [t for t in L.trail() if t[:3] == ("gh", "run", "list")]


def test_a_commit_made_in_the_spoke_while_the_gate_runs_is_not_merged_and_landed_unreviewed(L):
    L.other_push("seed.txt")   # creates the clone
    # the first gate runs while main moves again and the worker commits: round 2 would merge main into that unreviewed HEAD
    cmd = (f"[ -f {L.tmp}/moved ] || {{ touch {L.tmp}/moved; (cd {L.tmp}/other && echo z > z.txt && git add z.txt && git commit -qm z && git push -q origin main) "
           f"&& cd {L.wt} && echo e > evil.txt && git add evil.txt && git commit -qm evil; }}")
    r = L.go("--local-gate", "9", CHECK_CMD=cmd)
    assert r.returncode == 2 and "moved off the reviewed tip" in r.stderr, r.stderr
    assert "evil.txt" not in git(L.origin, "ls-tree", "--name-only", "-r", "main") and L.tip != main_sha(L)   # only the other clone's push moved main
    assert not [t for t in L.trail() if t[:3] in (("gh", "issue", "close"), ("orca", "worktree", "rm"))]



def test_local_main_ahead_of_origin_is_refused_so_nothing_ungated_ships(L):
    commit(L.root, "ungated.txt")   # a local main commit that never ran the gate
    before = main_sha(L)
    r = L.go("9")
    assert r.returncode == 2 and "local main is not at origin/main" in r.stderr
    no_side_effects(L, before)
    assert not [t for t in L.trail() if t[0] == "git"]


def test_a_branch_pushed_after_the_gate_is_not_deleted(L):
    late = f"echo late > late.txt && git add late.txt && git commit -qm late && git push -q origin HEAD:{BRANCH}"
    r = L.go("--local-gate", "9", CHECK_CMD=late)
    assert r.returncode == 6 and "cleanup" in r.stderr
    assert main_sha(L) == L.tip   # the gated tip landed; the late commit did not
    assert git(L.origin, "rev-parse", BRANCH) != L.tip   # and the worker's late push is still there


def test_refuses_a_spoke_on_the_base_branch(L):
    L.stubs.reply("orca.worktree_show", json.dumps({"result": {"worktree": {"path": str(L.wt), "branch": "refs/heads/main"}}}))
    r = L.go("9")
    assert r.returncode == 2 and "base branch" in r.stderr and not [t for t in L.trail() if t[0] != "orca"]


def test_a_worktree_rm_that_failed_but_took_effect_is_not_a_cleanup_failure(L):
    L.stubs.reply("orca.worktree_rm", '{"error":"cli dropped"}', rc=1)
    L.stubs.reply("orca.worktree_show", '{"error":"not found"}', rc=1, n=2)   # gone on the re-check
    assert L.go("9").returncode == 0


def test_a_worktree_rm_that_really_failed_is_reported(L):
    L.stubs.reply("orca.worktree_rm", '{"error":"archive hook failed"}', rc=1)
    r = L.go("9")
    assert r.returncode == 6 and "cleanup" in r.stderr


def test_cleanup_only_finishes_the_post_push_steps_after_exit_6(L):
    L.stubs.reply("gh.issue_close", "boom", rc=1)
    assert L.go("9").returncode == 6
    L.stubs.reply("gh.issue_close", "")
    L.stubs.reply("gh.issue_edit", "boom", rc=1)   # a label that cannot be removed is a warning, not a cleanup failure
    n = len(L.trail())
    r = L.go("--cleanup-only", "9")
    assert r.returncode == 0, r.stderr
    assert "status:in-progress" in r.stderr
    t = L.trail()[n:]
    assert [x[:3] for x in t if x[0] == "gh"] == [("gh", "issue", "close"), ("gh", "issue", "edit")] and t[1][3:] == ("9", "-c", f"landed in {L.tip}")
    assert ("gh", "issue", "edit", "9", "--remove-label", "status:in-progress") in t
    assert [x[:3] for x in t if x[0] == "orca"] == [("orca", "worktree", "show"), ("orca", "orchestration", "worker-list"),
                                                      ("orca", "orchestration", "worker-release"), ("orca", "worktree", "rm")]
    assert not [x for x in t if x[0] == "git"]   # branch already deleted: nothing to push, main untouched


def test_cleanup_only_refuses_a_branch_that_has_not_landed(L):
    before = main_sha(L)
    r = L.go("--cleanup-only", "9")
    assert r.returncode == 2 and "not landed" in r.stderr
    no_side_effects(L, before)


@pytest.fixture
def gone(L):
    """Exit 6 where only `gh issue close` failed and the worktree is already gone (the common case)."""
    L.stubs.reply("gh.issue_close", "boom", rc=1)
    assert L.go("9").returncode == 6
    L.stubs.reply("gh.issue_close", "")
    L.stubs.reply("orca.worktree_show", "no such worktree", rc=1)
    return len(L.trail())


def test_cleanup_only_works_when_the_worktree_is_gone_given_the_branch_and_tip(L, gone):
    r = L.go("--cleanup-only", "--branch", BRANCH, "--tip", L.tip, "9")
    assert r.returncode == 0, r.stderr
    t = L.trail()[gone:]
    assert [x[:3] for x in t if x[0] == "gh"] == [("gh", "issue", "close"), ("gh", "issue", "edit")] and t[-2][3:] == ("9", "-c", f"landed in {L.tip}")
    assert [x[:3] for x in t if x[0] == "orca"] == [("orca", "worktree", "show")]   # no worker-list, release or rm


def test_cleanup_only_derives_the_tip_from_a_surviving_remote_branch_and_deletes_it(L, gone):
    git(L.root, "push", "-q", "origin", f"{L.tip}:refs/heads/{BRANCH}")
    git(L.root, "fetch", "-q")
    r = L.go("--cleanup-only", "--branch", BRANCH, "9")
    assert r.returncode == 0, r.stderr
    assert ("git", "delete", BRANCH) in L.trail()[gone:]


def test_cleanup_only_with_a_gone_worktree_still_refuses_an_unlanded_tip(L, gone):
    git(L.root, "push", "-q", "origin", f"{L.tip}:refs/heads/{BRANCH}")
    unlanded = commit(L.wt, "later.txt", push=BRANCH)
    r = L.go("--cleanup-only", "--branch", BRANCH, "--tip", unlanded, "9")
    assert r.returncode == 2 and "not landed" in r.stderr
    assert not [t for t in L.trail()[gone:] if t[:3] == ("gh", "issue", "close")]


@pytest.mark.parametrize("args, why", [(["--cleanup-only", "9"], "no Orca worktree"), (["--branch", BRANCH, "9"], "no Orca worktree"),
                                        (["--cleanup-only", "--branch", BRANCH, "9"], "--tip"),
                                        (["--cleanup-only", "--branch", "main", "--tip", "HEAD", "9"], "base branch")])
def test_a_missing_worktree_is_refused_unless_cleanup_only_can_name_the_branch_and_tip(L, gone, args, why):
    r = L.go(*args)   # the last case: origin/<branch> is gone too, so there is nothing to derive the tip from
    assert r.returncode == 2 and why in r.stderr
    assert not [t for t in L.trail()[gone:] if t[:3] == ("gh", "issue", "close")]


def toolkit_sources(L):
    """The spoke adds the sync sources and edits one synced file per class (hook, script, policy): the landed tip is a toolkit."""
    (L.wt / "scripts").mkdir(exist_ok=True)
    for name in ("sync.sh", "lib.sh"):
        shutil.copy(V2 / "scripts" / name, L.wt / "scripts" / name)
    files = {"scripts/dispatch.sh": "#!/bin/sh\nlanded\n", "hooks/claude/guard.sh": "#!/bin/sh\nlanded\n", "shared/rules/guidelines.md": "# G\n",
             "shared/rules/security.md": "# landed\n", "settings/ai-toolkit.env": "X=1\n"}
    for rel, text in files.items():
        (L.wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (L.wt / rel).write_text(text)
    git(L.wt, "add", "-A")
    git(L.wt, "commit", "-qm", "toolkit sources")
    git(L.wt, "push", "-q", "origin", BRANCH)
    return {".ai-toolkit/scripts/dispatch.sh": files["scripts/dispatch.sh"], ".ai-toolkit/hooks/claude/guard.sh": files["hooks/claude/guard.sh"],
            ".claude/rules/ai-toolkit/security.md": files["shared/rules/security.md"], ".ai-toolkit/scripts/sync.sh": (L.wt / "scripts/sync.sh").read_text()}


@pytest.mark.parametrize("broken", [False, True], ids=["refreshed", "refresh-fails-then-cleanup-only"])
def test_a_land_in_the_toolkit_checkout_refreshes_the_installed_copies(L, broken):
    want = toolkit_sources(L)
    if broken:
        (L.root / ".ai-toolkit").write_text("not a dir")   # the sync cannot install; everything else about the land is fine
    r = L.go("9")
    sha = main_sha(L)
    assert sha == git(L.wt, "rev-parse", "HEAD") and git(L.root, "rev-parse", "HEAD") == sha   # main is landed and pushed either way
    assert ("gh", "issue", "close", "9", "-c", f"landed in {sha}") in L.trail()
    if broken:
        assert r.returncode == 6 and "installed copies" in r.stderr and "scripts/sync.sh" in r.stderr   # distinct, visible, never a red land
        (L.root / ".ai-toolkit").unlink()
        r = L.go("--cleanup-only", "9")   # the existing way to finish an exit-6 land also retries the refresh
    assert r.returncode == 0, r.stderr
    assert {rel: (L.root / rel).read_text() for rel in want} == want


@pytest.fixture
def tracked(L, link_script):   # POST_LAND_CMD for an issue whose footer says `Tracker: PM-17` (or `body`)
    def setup(body="Scope: a\nTracker: PM-17\n"):
        L.stubs.reply("gh.issue_view", json.dumps({"number": 9, "title": "t", "body": body}))
        return link_script(L.tmp / "post-land.sh", POST_LAND)
    return setup


def post_land_calls(L):
    return [t for t in L.trail() if t[0] == "post-land"]


def test_post_land_cmd_runs_once_after_the_base_push_with_the_issue_the_key_and_the_sha(L, tracked):
    r = L.go("9", POST_LAND_CMD=tracked())
    assert r.returncode == 0, r.stderr
    t = L.trail()
    assert post_land_calls(L) == [("post-land", "9", "PM-17", L.tip, "BASE_BRANCH=main")]
    assert t.index(("git", "push", "main")) < t.index(post_land_calls(L)[0]) < t.index(("gh", "issue", "close", "9", "-c", f"landed in {L.tip}"))


@pytest.mark.parametrize("cmd,body", [(False, "Scope: a\nTracker: PM-17\n"), (True, "Scope: a\n")])
def test_post_land_cmd_does_not_run_without_the_command_or_without_a_tracker_footer(L, tracked, cmd, body):
    script = tracked(body)
    r = L.go("9", **({"POST_LAND_CMD": script} if cmd else {}))
    assert r.returncode == 0, r.stderr
    assert post_land_calls(L) == [] and main_sha(L) == L.tip


def test_post_land_cmd_does_not_run_again_on_the_cleanup_only_retry(L, tracked):
    script = tracked()
    L.stubs.reply("gh.issue_close", "boom", rc=1)
    assert L.go("9", POST_LAND_CMD=script).returncode == 6
    L.stubs.reply("gh.issue_close", "")
    assert L.go("--cleanup-only", "9", POST_LAND_CMD=script).returncode == 0
    assert len(post_land_calls(L)) == 1


def test_a_failing_post_land_cmd_comments_on_the_issue_and_never_changes_the_exit_code(L, tracked):
    r = L.go("9", POST_LAND_CMD=tracked(), PL_RC=3)
    assert r.returncode == 0 and "warning" in r.stderr
    t = L.trail()
    assert ("gh", "issue", "comment", "9", "-b", f"tracker update failed, do it by hand: PM-17 {L.tip}") in t
    assert ("gh", "issue", "close", "9", "-c", f"landed in {L.tip}") in t and ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks") in t


@pytest.mark.parametrize("tail", ["\r", " "])
def test_a_tracker_footer_with_a_trailing_cr_or_space_still_runs_the_command_with_the_key(L, tracked, tail):
    assert L.go("9", POST_LAND_CMD=tracked(f"Scope: a\nTracker: PM-17{tail}\n")).returncode == 0
    assert [t[:3] for t in post_land_calls(L)] == [("post-land", "9", "PM-17")]


@pytest.mark.parametrize("key", ["PM 17", "PM-17;touch x", "PM-$(id)"])
def test_a_tracker_key_that_is_not_one_token_is_refused_with_a_warning_and_no_call(L, tracked, key):
    r = L.go("9", POST_LAND_CMD=tracked(f"Scope: a\nTracker: {key}\n"))
    assert r.returncode == 0 and "warning" in r.stderr and "Tracker" in r.stderr
    assert post_land_calls(L) == [] and main_sha(L) == L.tip


# --- #507: an issue is done when merged into the true main branch (MAIN_BRANCH), not when BASE_BRANCH takes it
AWAITING = "status:awaiting-main"
POST_LAND_MAIN = """#!/bin/bash
printf 'post-land' >> "$STUB_DIR/calls.log"; for a in "$@"; do printf '\\037%s' "$a" >> "$STUB_DIR/calls.log"; done
printf '\\037BASE_BRANCH=%s\\037MAIN_BRANCH=%s\\n' "$BASE_BRANCH" "$MAIN_BRANCH" >> "$STUB_DIR/calls.log"
"""
PR = {"number": 5, "url": "https://x/pull/5", "state": "MERGED", "reviewDecision": "APPROVED", "mergeCommit": {"oid": "abc123"}, "body": "Closes #9\nCloses #4\n"}
OPEN_PR = [{"url": "https://x/pull/5", "isDraft": False}]


def calls_of(L, *prefix):
    return [t for t in L.trail() if t[:len(prefix)] == prefix]


def test_integration_land_pushes_the_base_leaves_the_issue_open_awaiting_main_and_opens_the_rolling_pr(L, link_script):
    L.stubs.reply("gh.pr_list", "[]")
    L.stubs.reply("gh.pr_create", "https://x/pull/5")
    L.stubs.reply("gh.issue_list", json.dumps([{"number": 4}]))
    L.stubs.reply("gh.issue_view", json.dumps({"number": 9, "title": "t", "body": "Scope: a\nTracker: PM-17\n"}))
    r = L.go("9", MAIN_BRANCH="develop", POST_LAND_CMD=link_script(L.tmp / "pl.sh", POST_LAND_MAIN))
    assert r.returncode == 7, r.stderr
    t = L.trail()
    assert main_sha(L) == L.tip and ("git", "push", "main") in t
    assert not calls_of(L, "gh", "issue", "close") and not calls_of(L, "post-land")
    assert ("gh", "issue", "edit", "9", "--add-label", AWAITING) in t and ("gh", "issue", "edit", "9", "--remove-label", "status:in-progress") in t
    assert [x[-1] for x in calls_of(L, "gh", "issue", "comment", "9")] == [f"integrated in main at {L.tip}, open until merged into develop"]
    (create,) = calls_of(L, "gh", "pr", "create")
    assert create[create.index("--head") + 1] == "main" and create[create.index("--base") + 1] == "develop"
    body = create[create.index("--body") + 1]
    assert "Closes #9" in body and "Closes #4" in body   # this issue and the ones already waiting
    assert ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks") in t and ("git", "delete", BRANCH) in t   # the work is on the base: nothing needs the worktree
    assert "PR: https://x/pull/5" in r.stdout


def test_a_second_integration_land_adds_its_issue_to_the_open_rolling_pr_instead_of_opening_another(L):
    L.stubs.reply("gh.pr_list", json.dumps(OPEN_PR))
    L.stubs.reply("gh.issue_list", json.dumps([{"number": 3}]))
    r = L.go("9", MAIN_BRANCH="develop")
    assert r.returncode == 7, r.stderr
    assert not calls_of(L, "gh", "pr", "create")
    (edit,) = calls_of(L, "gh", "pr", "edit")
    assert edit[3] == "https://x/pull/5" and "Closes #3" in edit[-1] and "Closes #9" in edit[-1]
    assert "PR: https://x/pull/5" in r.stdout


def test_pr_mode_opens_a_draft_for_ci_pushes_nothing_then_readies_it_and_keeps_the_worktree(L):
    L.stubs.reply("gh.pr_list", "[]", n=1)
    L.stubs.reply("gh.pr_list", json.dumps([{"url": "https://x/pull/6", "isDraft": True}]), n=2)
    L.stubs.reply("gh.pr_create", "https://x/pull/6")
    before = main_sha(L)
    r = L.go("9", LAND_MODE="pr")
    assert r.returncode == 7, r.stderr
    t = L.trail()
    assert main_sha(L) == before and not [x for x in t if x[0] == "git"]   # the base is not pushed, nor the branch or anything else
    (create,) = calls_of(L, "gh", "pr", "create")
    assert "--draft" in create and create[create.index("--head") + 1] == BRANCH and create[create.index("--base") + 1] == "main"
    assert t.index(create) < t.index(calls_of(L, "gh", "run", "list")[0]) < t.index(("gh", "pr", "ready", "https://x/pull/6"))   # draft, CI on the tip, then ready
    assert "Closes #9" in calls_of(L, "gh", "pr", "edit")[0][-1] and L.tip in calls_of(L, "gh", "pr", "edit")[0][-1]
    assert ("gh", "issue", "edit", "9", "--add-label", AWAITING) in t and not calls_of(L, "gh", "issue", "close")
    assert [x[3:5] for x in calls_of(L, "orca", "orchestration", "worker-release")] == [("--dispatch", "ctx_9")]
    assert not calls_of(L, "orca", "worktree", "rm") and ("git", "delete", BRANCH) not in t
    assert "https://x/pull/6" in calls_of(L, "gh", "issue", "comment", "9")[0][-1] and "PR: https://x/pull/6" in r.stdout


def test_a_second_pr_mode_land_updates_the_open_pr_with_the_local_gate(L):
    L.stubs.reply("gh.pr_list", json.dumps(OPEN_PR))
    r = L.go("9", LAND_MODE="pr", LOCAL_GATE=1, CHECK_CMD="true")
    assert r.returncode == 7, r.stderr
    assert not calls_of(L, "gh", "pr", "create") and not calls_of(L, "gh", "pr", "ready") and not calls_of(L, "gh", "run")
    assert len(calls_of(L, "gh", "pr", "edit")) == 1 and "Closes #9" in calls_of(L, "gh", "pr", "edit")[0][-1]


@pytest.mark.parametrize("env", [{"LAND_MODE": "pr", "MAIN_BRANCH": "develop"}, {"LAND_MODE": "merge"}], ids=["pr-with-another-main", "unknown-mode"])
def test_an_incoherent_land_mode_is_a_usage_error(L, env):
    assert L.go("9", **env).returncode == 2 and not L.trail()


def squash_into_develop(L):   # the human merged the promotion PR: develop holds a squash commit that main does not
    o = L.tmp / "dev"
    subprocess.run(["git", "clone", "-q", str(L.origin), str(o)], check=True, capture_output=True)
    git(o, "switch", "-q", "-c", "develop")
    commit(o, "squashed.txt", push="develop")
    (L.tmp / "stubs/calls.log").write_text("")


def test_merged_closes_every_issue_of_the_squash_merged_rolling_pr_runs_post_land_with_the_merge_sha_and_merges_main_back(L, link_script):
    squash_into_develop(L)
    L.other_push("on_base.txt")   # the base moved on after the PR was opened: the merge-back must keep it
    L.stubs.reply("gh.pr_list", json.dumps([PR]))
    L.stubs.reply("gh.issue_view", json.dumps({"state": "OPEN", "labels": [{"name": AWAITING}], "body": "Scope: a\nTracker: PM-17\n"}))
    r = L.go("--merged", "9", MAIN_BRANCH="develop", POST_LAND_CMD=link_script(L.tmp / "pl.sh", POST_LAND_MAIN))
    assert r.returncode == 0, r.stderr
    t = L.trail()
    for m in ("9", "4"):
        assert ("gh", "issue", "close", m, "-c", "merged into develop in abc123") in t and ("gh", "issue", "edit", m, "--remove-label", AWAITING) in t
    assert [x[1:] for x in calls_of(L, "post-land")] == [("9", "PM-17", "abc123", "BASE_BRANCH=main", "MAIN_BRANCH=develop"), ("4", "PM-17", "abc123", "BASE_BRANCH=main", "MAIN_BRANCH=develop")]
    assert git(L.origin, "ls-tree", "--name-only", "-r", "main").split() == ["README", "on_base.txt", "squashed.txt"]   # merged back, so the next rolling PR shows only new work
    assert not calls_of(L, "orca", "worktree", "rm")


def test_merged_in_pr_mode_closes_the_issue_and_removes_the_branch_and_the_worktree(L, link_script):
    L.stubs.reply("gh.pr_list", json.dumps([{**PR, "body": "Closes #9\n"}]))
    L.stubs.reply("gh.issue_view", json.dumps({"state": "OPEN", "labels": [{"name": AWAITING}], "body": "Scope: a\nTracker: PM-17\n"}))
    r = L.go("--merged", "9", LAND_MODE="pr", POST_LAND_CMD=link_script(L.tmp / "pl.sh", POST_LAND_MAIN))
    assert r.returncode == 0, r.stderr
    t = L.trail()
    assert ("gh", "issue", "close", "9", "-c", "merged into main in abc123") in t and calls_of(L, "post-land")[0][1:4] == ("9", "PM-17", "abc123")
    assert ("git", "delete", BRANCH) in t and ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks") in t and ("git", "push", "main") not in t


@pytest.mark.parametrize("env, state", [({"MAIN_BRANCH": "develop"}, "OPEN"), ({"MAIN_BRANCH": "develop"}, "CLOSED"), ({}, "MERGED")], ids=["open", "closed", "nothing-waits"])
def test_merged_refuses_a_pr_that_is_not_merged_and_touches_nothing(L, env, state):
    L.stubs.reply("gh.pr_list", json.dumps([{**PR, "state": state}]))
    r = L.go("--merged", "9", **env)
    assert r.returncode == 2, r.stderr
    assert not [x for x in L.trail() if x[:3] in (("gh", "issue", "close"), ("gh", "issue", "edit"), ("orca", "worktree", "rm")) or x[0] in ("git", "post-land")]
