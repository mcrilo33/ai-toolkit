import json

import pytest
from conftest import V2


def lib(run, code, **env):
    return run(["bash", "-euo", "pipefail", "-c", f". {V2}/scripts/lib.sh; {code}"], **env)


def test_env_precedence_is_caller_then_local_then_defaults(run, tmp_path):
    (tmp_path / "d.env").write_text('A=default\nB=default\nC="two words"\nD=default\n# c\n')
    (tmp_path / "l.env").write_text("B=local\nD=\n")
    r = lib(run, 'load_env; echo "$A|$B|$C|$D"', AI_TOOLKIT_ENV=tmp_path / "d.env", AI_TOOLKIT_LOCAL_ENV=tmp_path / "l.env", A="caller")
    assert r.stdout.strip() == "caller|local|two words|"   # an empty local value opts out of a non-empty default
    shipped = lib(run, 'unset DISPATCH_LABEL; load_env; echo "$DISPATCH_LABEL"', AI_TOOLKIT_LOCAL_ENV=tmp_path / "none.env")
    assert shipped.stdout.strip() == "ai-toolkit"   # the dispatch label is on by default
    assert len(lib(run, 'load_env; echo "$SPOKE_MODEL $BASE_BRANCH $OTEL_ENDPOINT"').stdout.split()) == 3


def test_orca_mutate_replays_an_unsettled_call_with_the_same_request_id(run, stubs):
    stubs.reply("orca.orchestration_reply", '{"error":{"code":"runtime_unavailable"}}', rc=1, n=1)
    stubs.reply("orca.orchestration_reply", '{"ok":true}', n=2)
    r = lib(run, "orca_mutate orchestration reply --id m1", ORCA_RETRY_SLEEP=0)
    first, second = stubs.calls("orca")
    assert r.returncode == 0 and r.stdout.strip() == '{"ok":true}' and first == second and "--retry-request" in first


def test_orca_mutate_does_not_replay_a_definite_failure(run, stubs):
    stubs.reply("orca", '{"error":{"code":"worktree_dirty"}}', rc=1)
    assert lib(run, "orca_mutate worktree rm --worktree x", ORCA_RETRY_SLEEP=0).returncode == 1
    assert len(stubs.calls("orca")) == 1


def test_issue_footer_reads_the_last_matching_line(run):
    r = lib(run, 'b=$(printf "t\\nScope: a.py\\nScope: hello.py tests/\\n"); issue_footer "$b" Scope; issue_footer "$b" Model; echo end')
    assert r.stdout.splitlines() == ["hello.py tests/", "end"]


MAIN_ROW = {"path": "MAIN", "repoId": "this", "isMainWorktree": True}   # the main checkout's own row, as a real scoped list returns it


@pytest.mark.parametrize("rows, want", [
    ([MAIN_ROW, {"path": "/w5", "linkedIssue": 5, "repoId": "this"}, {"path": "/other", "linkedIssue": 5, "repoId": "other"}], "path:/w5"),
    ([MAIN_ROW, {"path": "/w1", "linkedIssue": 1, "repoId": "this"}], None),
    ([MAIN_ROW, {"path": "/w5", "linkedIssue": 5, "repoId": "this"}, {"path": "/w5b", "linkedIssue": 5, "repoId": "this"}], None),
    ([MAIN_ROW, {"path": "/other", "linkedIssue": 5, "repoId": "other"}], None),
    (None, None),
], ids=["one", "none", "two-in-this-repo", "another-repo-only", "orca-failed"])   # #531: a worktree of another Orca repo linked to the same number never resolves
def test_worktree_of_resolves_only_this_repos_worktree_for_an_issue(run, stubs, repo, rows, want):
    if rows is None:
        stubs.reply("orca.worktree_list", "boom", rc=1)
    else:
        rows = [{**r, "path": str(repo.root.resolve()) if r["path"] == "MAIN" else r["path"]} for r in rows]
        stubs.reply("orca.worktree_list", json.dumps({"result": {"worktrees": rows}}))
    r = run(["bash", "-euo", "pipefail", "-c", f". {V2}/scripts/lib.sh; worktree_of 5"], cwd=repo.root)
    assert (r.returncode, r.stdout) == ((0, want) if want else (1, ""))
    assert stubs.calls("orca")[0][:4] == ["worktree", "list", "--repo", f"path:{repo.root.resolve()}"]


def test_gh_binary_can_be_swapped_by_env_for_stubbed_runs(run, stubs, tmp_path, link_script):
    alt = link_script(tmp_path / "alt-gh", '#!/bin/sh\necho "alt $*"\n')
    assert lib(run, "gh issue view 4", AI_TOOLKIT_GH=alt).stdout.strip() == "alt issue view 4"
    lib(run, "gh issue view 4")
    assert stubs.calls("gh") == [["issue", "view", "4"]]
