"""Unit tests for scripts/worktree-done.sh teardown completeness.

Teardown is `orca worktree rm` (Orca owns the checkout) plus pruning the branch -- local and
origin -- but only when it is fully merged into the hub, so an abandoned teardown never loses
unmerged work. The `orca` PATH stub (conftest + _orca_stub) does what Orca does: it lists the
real git worktrees and removes them with `git worktree remove`, so these tests assert on the
recorded argv and on the resulting git state, never on a real Orca.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _orca_stub import install_forbidden_stubs, orca_calls, orca_link, orca_scenario

WORKTREE_DONE = Path(__file__).resolve().parents[2] / "scripts" / "worktree-done.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)
# A host GIT_SSH_COMMAND must not prefix the keepalive assertion (#119).
_GIT_ENV.pop("GIT_SSH_COMMAND", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    return hub


def _make_spoke(hub: Path, tmp_path: Path, branch: str, *, push: bool, merge: bool) -> Path:
    """Add a worktree on `branch` with one commit; optionally push it to origin
    and/or merge it into the hub's `main`."""
    wt = tmp_path / branch.replace("/", "-")
    _git(hub, "worktree", "add", "-q", "-b", branch, str(wt))
    # Branch-unique content + filename: a merged spoke lands its file in the hub,
    # so a second spoke branched off the hub must not write an identical blob
    # (that would stage nothing and fail the commit).
    fname = f"{branch.replace('/', '-')}.txt"
    (wt / fname).write_text(f"work on {branch}\n")
    _git(wt, "add", fname)
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #1")
    if push:
        _git(wt, "push", "-q", "-u", "origin", branch)
    if merge:
        _git(hub, "merge", "-q", "--no-ff", branch, "-m", "merge")
    return wt


def _run_done(
    hub: Path,
    tmp_path: Path,
    *args: str,
    spoke_marker: str | None = None,
    forbidden: bool = False,
) -> subprocess.CompletedProcess:
    """Run worktree-done.sh from the hub against the PATH-stubbed `orca`.

    `spoke_marker` sets WT_SPOKE to model a spoke session (issue #26). `forbidden` puts
    recording `tmux` / `code` / `git worktree add|remove|prune` stubs first on PATH; the log they
    write is `tmp_path / "bin" / "forbidden.log"` and stays empty unless a retired path ran."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if forbidden:
        install_forbidden_stubs(bindir)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.update(ORCA_SETTLE_SLEEP="0", ORCA_SETTLE_TRIES="3")
    env.pop("TMUX", None)
    # The host's own spoke marker must never steer the guard; set it explicitly
    # only when a test means to model a spoke session (issue #26).
    env.pop("WT_SPOKE", None)
    if spoke_marker is not None:
        env["WT_SPOKE"] = spoke_marker
    return subprocess.run(
        ["bash", str(WORKTREE_DONE), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _rm_calls(orca_bin: Path) -> list[list[str]]:
    return [c for c in orca_calls(orca_bin) if c[:2] == ["worktree", "rm"]]


def _local_branches(hub: Path) -> list[str]:
    out = _git(hub, "branch", "--format=%(refname:short)")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_has(hub: Path, branch: str) -> bool:
    return bool(_git(hub, "ls-remote", "--heads", "origin", branch).strip())


def test_merged_branch_is_pruned_locally(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert "feature/1-merged" not in _local_branches(hub)


def test_merged_branch_is_pruned_on_remote(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert not _remote_has(hub, "feature/1-merged")


def test_remote_branch_delete_carries_keepalive(hub: Path, tmp_path: Path) -> None:
    # The remote branch-delete push must route through wt_git_push (issue #119)
    # so every push the worktree scripts perform carries the SSH keepalive
    # options. A `git` shim in the same PATH-front bindir _run_done uses records
    # the env each `git push` runs with, then delegates to the real git.
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "push-invocations.log"
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = push ]; then echo "GIT_SSH_COMMAND=[$GIT_SSH_COMMAND] $*" >> "{log}"; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    _make_spoke(hub, tmp_path, "feature/8-keepalive", push=True, merge=True)

    proc = _run_done(hub, tmp_path, "8")

    assert proc.returncode == 0, proc.stderr
    assert not _remote_has(hub, "feature/8-keepalive")
    recorded = log.read_text()
    keepalive = "-o ServerAliveInterval=15 -o ServerAliveCountMax=40"
    delete_lines = [ln for ln in recorded.splitlines() if "--delete" in ln]
    assert delete_lines, f"no branch-delete push recorded: {recorded!r}"
    assert f"GIT_SSH_COMMAND=[ssh {keepalive}]" in delete_lines[0]


def _fetch_fail_git_shim(tmp_path: Path) -> None:
    """PATH-front `git` shim: every `git fetch` dies (the network-down/stale-SSH
    shape, issue #195); everything else delegates to the real git. Written into
    the same bindir _run_done prepends to PATH."""
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = fetch ]; then echo "fatal: unable to access origin (stubbed)" >&2; exit 128; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)


def test_remote_delete_skipped_when_fetch_fails(hub: Path, tmp_path: Path) -> None:
    # Issue #195 defense-in-depth: the merged-ness proof above the remote delete
    # is about the LOCAL branch; the remote ref may hold commits this checkout
    # never fetched. When the freshness fetch fails, the delete must be skipped
    # loudly — never run against last-known remote state. Local prune (merged-
    # only, safe) still runs, and teardown still succeeds.
    _make_spoke(hub, tmp_path, "feature/10-fetchdead", push=True, merge=True)
    _fetch_fail_git_shim(tmp_path)

    proc = _run_done(hub, tmp_path, "10")

    assert proc.returncode == 0, proc.stderr
    assert "feature/10-fetchdead" not in _local_branches(hub)  # local prune still runs
    assert _remote_has(hub, "feature/10-fetchdead"), "remote ref must survive a dead fetch"
    assert "origin/feature/10-fetchdead" in proc.stderr, "the skipped delete is loud"


def test_remote_delete_skipped_when_remote_has_unfetched_commits(hub: Path, tmp_path: Path) -> None:
    # Issue #195 defense-in-depth: the spoke pushed one more commit after this
    # checkout's last fetch — the stale tracking ref sits at the merged sha while
    # the real remote is ahead. After a SUCCESSFUL freshness fetch the remote ref
    # is no ancestor of the base, so the delete must be skipped with a warning;
    # deleting on the local-branch proof alone would destroy the remote-only commit.
    wt = _make_spoke(hub, tmp_path, "feature/11-late", push=True, merge=True)
    merged_sha = _git(hub, "rev-parse", "feature/11-late").strip()
    (wt / "late.txt").write_text("pushed after this checkout's last fetch\n")
    _git(wt, "add", "late.txt")
    _git(wt, "commit", "-qm", "feat: late", "-m", "Refs #1")
    _git(wt, "push", "-q", "origin", "feature/11-late")
    _git(wt, "reset", "-q", "--hard", merged_sha)  # local branch back at the merged sha
    # This checkout never fetched the late push: rewind the shared tracking ref.
    _git(hub, "update-ref", "refs/remotes/origin/feature/11-late", merged_sha)

    proc = _run_done(hub, tmp_path, "11")

    assert proc.returncode == 0, proc.stderr
    assert "feature/11-late" not in _local_branches(hub)  # local prune still runs
    assert _remote_has(hub, "feature/11-late"), "the remote-only commit must survive"
    assert "origin/feature/11-late" in proc.stderr, "the kept remote is loud"


def test_remote_delete_noop_when_remote_branch_already_gone(hub: Path, tmp_path: Path) -> None:
    # The remote branch was already deleted on origin (e.g. by a prior resumed
    # land) but the stale tracking ref survives locally. The freshness fetch
    # runs with --prune, so the ghost collapses and teardown recognizes there is
    # nothing to delete — no ancestor check against the stale ref, no misleading
    # "couldn't delete" / "kept remote" warning.
    _make_spoke(hub, tmp_path, "feature/12-gone", push=True, merge=True)
    _git(tmp_path / "remote.git", "update-ref", "-d", "refs/heads/feature/12-gone")
    _git(hub, "rev-parse", "--verify", "refs/remotes/origin/feature/12-gone")  # ghost present

    proc = _run_done(hub, tmp_path, "12")

    assert proc.returncode == 0, proc.stderr
    assert "feature/12-gone" not in _local_branches(hub)  # local prune still runs
    assert "origin/feature/12-gone" not in proc.stderr, "no warning about a branch already gone"


def test_remote_delete_proceeds_when_merged_into_origin_base_only(
    hub: Path, tmp_path: Path
) -> None:
    # Concurrent-hub case: the spoke pushed a late commit and ANOTHER hub landed
    # it on origin/main; this checkout's local main is behind. The late commit is
    # durably integrated in origin/main, so the remote delete must still proceed
    # instead of keeping the branch with a misleading "commits not in main" warn.
    wt = _make_spoke(hub, tmp_path, "feature/13-conc", push=True, merge=True)
    merged_sha = _git(hub, "rev-parse", "feature/13-conc").strip()
    (wt / "late.txt").write_text("landed by another hub\n")
    _git(wt, "add", "late.txt")
    _git(wt, "commit", "-qm", "feat: late", "-m", "Refs #1")
    _git(wt, "push", "-q", "origin", "feature/13-conc")
    # Another hub integrates the late push into origin/main; local main stays behind.
    _git(hub, "checkout", "-q", "-b", "tmpint", "main")
    _git(hub, "merge", "-q", "--no-ff", "feature/13-conc", "-m", "merge elsewhere")
    _git(hub, "push", "-q", "origin", "tmpint:main")
    _git(hub, "checkout", "-q", "main")
    _git(hub, "branch", "-qD", "tmpint")
    _git(wt, "reset", "-q", "--hard", merged_sha)  # local branch back at the merged tip

    proc = _run_done(hub, tmp_path, "13")

    assert proc.returncode == 0, proc.stderr
    assert "feature/13-conc" not in _local_branches(hub)
    assert not _remote_has(hub, "feature/13-conc"), "merged-into-origin/main branch is deleted"


def test_unmerged_branch_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/2-unmerged", push=False, merge=False)
    proc = _run_done(hub, tmp_path, "2")
    assert proc.returncode == 0, proc.stderr
    assert "feature/2-unmerged" in _local_branches(hub)


def test_refuses_when_run_as_spoke_session(hub: Path, tmp_path: Path) -> None:
    # Teardown is hub-owned: a spoke must not tear down its own worktree. Even a
    # cleanly-merged worktree (the happy teardown case) must be refused when the
    # session carries WT_SPOKE (issue #26). No override flag.
    wt = _make_spoke(hub, tmp_path, "feature/1-spoke", push=True, merge=True)

    proc = _run_done(hub, tmp_path, "1", spoke_marker="1")

    assert proc.returncode != 0
    assert "spoke" in proc.stderr.lower()
    assert "hub" in proc.stderr.lower()
    assert wt.exists()  # worktree untouched
    assert "feature/1-spoke" in _local_branches(hub)  # branch not pruned


def test_tears_down_when_not_a_spoke_session(hub: Path, tmp_path: Path) -> None:
    # The mirror: with WT_SPOKE unset (the hub), teardown proceeds.
    wt = _make_spoke(hub, tmp_path, "feature/1-hub", push=True, merge=True)

    proc = _run_done(hub, tmp_path, "1", spoke_marker=None)

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()


def test_missing_target_dir_does_not_prune_another_branch(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    # Both worktrees are merged + pushed, then both directories are deleted from
    # disk. Tearing down #2 with --force must prune ONLY feature/2-bbb — a path
    # match by canonicalized (empty) path would wrongly capture feature/1-aaa.
    wt_a = _make_spoke(hub, tmp_path, "feature/1-aaa", push=True, merge=True)
    wt_b = _make_spoke(hub, tmp_path, "feature/2-bbb", push=True, merge=True)
    orca_link(orca_bin, wt_a, issue=1)
    orca_link(orca_bin, wt_b, issue=2)
    subprocess.run(["rm", "-rf", str(wt_a), str(wt_b)], check=True)
    proc = _run_done(hub, tmp_path, "2", "--force")
    assert proc.returncode == 0, proc.stderr
    assert "feature/1-aaa" in _local_branches(hub)
    assert _remote_has(hub, "feature/1-aaa")
    assert "feature/2-bbb" not in _local_branches(hub)


def test_teardown_clears_hub_guard_allow_marker(hub: Path, tmp_path: Path) -> None:
    # The /quick lane (issue #89) grants the hub-guard escape hatch by dropping
    # `hub-guard-allow` in the common git-dir; teardown is the cleanup that
    # revokes it, so the hub never keeps a stale bypass after the lane ends.
    _make_spoke(hub, tmp_path, "quick/fix-typo", push=False, merge=True)
    marker = Path(_git(hub, "rev-parse", "--absolute-git-dir").strip()) / "hub-guard-allow"
    marker.write_text("")

    proc = _run_done(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()


def test_marker_revoked_even_when_removal_aborts(hub: Path, tmp_path: Path) -> None:
    # The bypass marker disables hub-guard on `main` while present, so teardown
    # must revoke it BEFORE the worktree removal — otherwise a removal that aborts
    # (dirty tree, no --force) would strand the marker and silently leave the guard
    # open. An untracked file makes `git worktree remove` refuse without --force.
    wt = _make_spoke(hub, tmp_path, "quick/fix-typo", push=False, merge=True)
    (wt / "scratch.txt").write_text("uncommitted\n")
    marker = Path(_git(hub, "rev-parse", "--absolute-git-dir").strip()) / "hub-guard-allow"
    marker.write_text("")

    proc = _run_done(hub, tmp_path, "fix-typo")

    assert proc.returncode != 0, "removal should abort on a dirty worktree without --force"
    assert wt.exists()  # worktree untouched
    assert not marker.exists()  # ...but the bypass is revoked regardless


def test_teardown_without_marker_is_a_noop(hub: Path, tmp_path: Path) -> None:
    # A normal (non-/quick) teardown has no marker to clear — it must not fail.
    _make_spoke(hub, tmp_path, "feature/3-plain", push=False, merge=True)

    proc = _run_done(hub, tmp_path, "3")

    assert proc.returncode == 0, proc.stderr


# --- configurable base branch (issue #117) --------------------------------------


def test_done_prunes_branch_merged_into_configured_base(hub: Path, tmp_path: Path) -> None:
    # The merged-ness prune check measures against the RESOLVED base branch,
    # not whatever branch the hub's HEAD happens to be on: a branch merged into
    # the configured develop is pruned even while the hub sits on main.
    _git(hub, "checkout", "-q", "-b", "develop")
    _git(hub, "checkout", "-q", "main")
    wt = _make_spoke(hub, tmp_path, "feature/7-base", push=False, merge=False)
    assert wt.exists()
    _git(hub, "checkout", "-q", "develop")
    _git(hub, "merge", "-q", "--no-ff", "feature/7-base", "-m", "merge")
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")

    proc = _run_done(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    assert "feature/7-base" not in _local_branches(hub)


# --- teardown through Orca (issue #364) -----------------------------------------


def _forbidden_log(tmp_path: Path) -> str:
    return (tmp_path / "bin" / "forbidden.log").read_text()


def test_abandon_removes_through_orca_with_run_hooks(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/4-abandon", push=True, merge=True)

    proc = _run_done(hub, tmp_path, "4", forbidden=True)

    assert proc.returncode == 0, proc.stderr
    assert _rm_calls(orca_bin) == [
        ["worktree", "rm", "--worktree", f"path:{wt}", "--run-hooks", "--json"]
    ]
    assert not wt.exists()
    assert _forbidden_log(tmp_path) == ""


def test_force_passes_through_to_orca_rm(orca_bin: Path, hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/4-force", push=True, merge=True)
    (wt / "scratch.txt").write_text("uncommitted\n")

    proc = _run_done(hub, tmp_path, "4", "--force")

    assert proc.returncode == 0, proc.stderr
    assert "--force" in _rm_calls(orca_bin)[0]
    assert not wt.exists()


def test_no_hooks_omits_run_hooks_from_the_orca_call(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/4-nohooks", push=True, merge=True)

    proc = _run_done(hub, tmp_path, "4", "--no-hooks")

    assert proc.returncode == 0, proc.stderr
    assert "--run-hooks" not in _rm_calls(orca_bin)[0]


def test_orca_refusal_exits_nonzero_with_orcas_error_and_keeps_everything(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/5-refused", push=True, merge=True)
    orca_scenario(orca_bin, {"_rm": {"refuse": True}})

    proc = _run_done(hub, tmp_path, "5")

    assert proc.returncode != 0
    assert "refusing to remove" in proc.stderr
    assert wt.exists()
    assert "feature/5-refused" in _local_branches(hub)
    assert _remote_has(hub, "feature/5-refused")


def test_drop_after_the_worktree_disappeared_counts_as_success(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/6-drop", push=True, merge=True)
    orca_scenario(orca_bin, {"_rm": {"drop": "after"}})

    proc = _run_done(hub, tmp_path, "6")

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()
    assert len(_rm_calls(orca_bin)) == 1, "a settled drop must never re-issue the removal"
    assert "feature/6-drop" not in _local_branches(hub)


def test_drop_with_the_worktree_still_present_is_a_loud_failure(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/6-stuck", push=True, merge=True)
    orca_scenario(orca_bin, {"_rm": {"drop": "before"}})

    proc = _run_done(hub, tmp_path, "6")

    assert proc.returncode != 0
    assert "runtime_unavailable" in proc.stderr
    assert wt.exists()
    assert "feature/6-stuck" in _local_branches(hub)


def test_merged_branch_orca_kept_is_deleted_afterwards(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/9-kept", push=True, merge=True)
    orca_scenario(orca_bin, {"_rm": {"branch": "never"}})

    proc = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    assert "feature/9-kept" not in _local_branches(hub)
    assert not _remote_has(hub, "feature/9-kept")


def test_unmerged_branch_orca_deleted_is_recreated_at_its_old_tip(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/9-lost", push=True, merge=False)
    tip = _git(hub, "rev-parse", "feature/9-lost").strip()
    orca_scenario(orca_bin, {"_rm": {"branch": "always"}})

    proc = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    assert _git(hub, "rev-parse", "feature/9-lost").strip() == tip
    assert "not merged" in proc.stdout
    assert _remote_has(hub, "feature/9-lost")


def test_fails_closed_when_orca_cannot_list_worktrees(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/9-down", push=True, merge=True)
    orca_scenario(orca_bin, {"worktree list": [{"rc": 1, "stderr": "runtime down"}]})

    proc = _run_done(hub, tmp_path, "9")

    assert proc.returncode != 0
    assert "orca" in proc.stderr
    assert wt.exists()
    assert _rm_calls(orca_bin) == []


def test_resolves_a_bare_branch_worktree_by_its_orca_issue(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "361-identity-hub-side", push=False, merge=True)
    orca_link(orca_bin, wt, issue=361)

    proc = _run_done(hub, tmp_path, "361")

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()


def test_branch_that_moved_on_during_the_removal_is_kept_not_pruned(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/9-late", push=False, merge=True)
    orca_scenario(orca_bin, {"_rm": {"commit": True, "branch": "never"}})

    proc = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    assert "gained commits" in proc.stderr
    assert "feature/9-late" in _local_branches(hub)
