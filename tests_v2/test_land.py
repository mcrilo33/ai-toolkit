import json
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


def commit(cwd, name, text="x", push=None):
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-qm", f"add {name}")
    if push:
        git(cwd, "push", "-q", "origin", push)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def L(stubs, repo, run, tmp_path):
    origin = tmp_path / "origin.git"
    (origin / "hooks/post-receive").write_text(HOOK)
    (origin / "hooks/post-receive").chmod(0o755)
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
        ("git", "delete", BRANCH),
        ("orca", "orchestration", "worker-list"),
        ("orca", "orchestration", "worker-release", "--dispatch", "ctx_9", *t[6][5:]),
        ("orca", "worktree", "rm", "--worktree", "issue:9", "--run-hooks"),
    ]
    assert "--retry-request" in t[6]


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


def test_a_rejected_main_push_restores_local_main_and_closes_nothing(L):
    (L.origin / "hooks/pre-receive").write_text('#!/bin/sh\nwhile read o n r; do [ "$r" = refs/heads/main ] && exit 1; done\nexit 0\n')
    (L.origin / "hooks/pre-receive").chmod(0o755)
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


def review_script(tmp_path):
    rev = tmp_path / "review.sh"
    rev.write_text('#!/bin/sh\ngh review "$@"\nexit "${REVIEW_RC:-0}"\n')
    rev.chmod(0o755)
    return rev


def test_review_is_skipped_by_default(L, tmp_path):
    assert L.go("9", REVIEW_CMD=review_script(tmp_path)).returncode == 0
    assert ("gh", "review", "9") not in L.trail()


def test_review_runs_first_and_a_non_approval_exits_3(L, tmp_path):
    rev = review_script(tmp_path)
    before = main_sha(L)
    r = L.go("--review", "9", REVIEW_CMD=rev, REVIEW_RC=1)
    assert r.returncode == 3 and "review" in r.stderr
    no_side_effects(L, before)
    assert [t[:2] for t in L.trail()] == [("orca", "worktree"), ("gh", "review")]
    n = len(L.trail())
    assert L.go("--review", "9", REVIEW_CMD=rev).returncode == 0
    assert [t[:3] for t in L.trail()[n:n + 3]] == [("orca", "worktree", "show"), ("gh", "review", "9"), ("gh", "run", "list")]


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
    n = len(L.trail())
    r = L.go("--cleanup-only", "9")
    assert r.returncode == 0, r.stderr
    t = L.trail()[n:]
    assert [x[:3] for x in t if x[0] == "gh"] == [("gh", "issue", "close")] and t[1][3:] == ("9", "-c", f"landed in {L.tip}")
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
    assert [x[:3] for x in t if x[0] == "gh"] == [("gh", "issue", "close")] and t[-1][3:] == ("9", "-c", f"landed in {L.tip}")
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
