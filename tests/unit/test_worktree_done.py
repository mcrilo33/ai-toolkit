"""Unit tests for scripts/worktree-done.sh teardown completeness.

Teardown must be a clean mirror of creation: remove the worktree, fold the folder
out of the VS Code review window (`code --remove`, the inverse of worktree-new's
`code --add`), and prune the branch — but only when it is fully merged into the
hub, so an abandoned teardown never loses unmerged work. A `code` stub on PATH
keeps the VS Code calls hermetic (the host really has `code` installed).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

WORKTREE_DONE = Path(__file__).resolve().parents[2] / "scripts" / "worktree-done.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


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
    hub: Path, tmp_path: Path, *args: str, code_exit: int = 0, spoke_marker: str | None = None
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-done.sh from the hub with a logging `code` stub on PATH.

    The stub logs one line per invocation — `<present|absent> <args>`, where the
    first token records whether the path passed to `code --remove` ($2) still
    exists on disk at call time — and exits `code_exit` (pass a nonzero value to
    simulate a VS Code failure). The existence token lets a test assert ordering:
    `code --remove` must run while the worktree is still on disk (issue #43).
    `spoke_marker` sets WT_SPOKE to model a spoke session (issue #26).
    Returns the completed process and the log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "code-calls.log"
    code = bindir / "code"
    code.write_text(
        "#!/bin/sh\n"
        'if [ -e "$2" ]; then exists=present; else exists=absent; fi\n'
        f'printf "%s %s\\n" "$exists" "$*" >> "{log}"\n'
        f"exit {code_exit}\n"
    )
    code.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    # Point the VS Code Open Recent cleanup (issue #103) at a per-test Code dir so
    # it never touches the host's real state store. Tests that exercise the prune
    # pre-populate <vscode>/User/globalStorage/storage.json under this same dir.
    vscode_dir = tmp_path / "vscode"
    vscode_dir.mkdir(exist_ok=True)
    env["AI_TOOLKIT_VSCODE_DIR"] = str(vscode_dir)
    env.pop("TMUX", None)
    # The host's own spoke marker must never steer the guard; set it explicitly
    # only when a test means to model a spoke session (issue #26).
    env.pop("WT_SPOKE", None)
    if spoke_marker is not None:
        env["WT_SPOKE"] = spoke_marker
    proc = subprocess.run(
        ["bash", str(WORKTREE_DONE), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _local_branches(hub: Path) -> list[str]:
    out = _git(hub, "branch", "--format=%(refname:short)")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_has(hub: Path, branch: str) -> bool:
    return bool(_git(hub, "ls-remote", "--heads", "origin", branch).strip())


# --- VS Code "Open Recent" cleanup helpers (issue #103) ----------------------
def _write_recent(
    tmp_path: Path, paths: list[str], *, history: bool = False, running_pid: int | None = None
) -> Path:
    """Seed a fake VS Code state store under the per-test Code dir.

    Mirrors the real layout: the recent folders live in `storage.json` under
    `lastKnownMenubarData` (File → Open Recent submenu). When `history=True`, also
    seed the older `history.recentlyOpenedPathsList` key. When `running_pid` is
    given, drop a `code.lock` holding that PID so the teardown's running-check
    sees VS Code as live (and must skip the scrub). Returns the storage.json path.
    """
    base = tmp_path / "vscode"
    gs = base / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    submenu = [
        {"id": "openRecentFolder", "label": p, "uri": {"$mid": 1, "path": p, "scheme": "file"}}
        for p in paths
    ]
    data: dict = {
        "lastKnownMenubarData": {
            "menus": {
                "File": {
                    "items": [
                        {
                            "id": "submenuitem.MenubarRecentMenu",
                            "label": "Open &&Recent",
                            "submenu": {"items": submenu},
                        }
                    ]
                }
            }
        }
    }
    if history:
        data["history.recentlyOpenedPathsList"] = {
            "entries": [{"folderUri": f"file://{p}"} for p in paths]
        }
    storage = gs / "storage.json"
    storage.write_text(json.dumps(data))
    if running_pid is not None:
        (base / "code.lock").write_text(str(running_pid))
    return storage


def _recent_paths(storage: Path) -> list[str]:
    d = json.loads(storage.read_text())
    items = d["lastKnownMenubarData"]["menus"]["File"]["items"][0]["submenu"]["items"]
    return [it["uri"]["path"] for it in items]


def _history_uris(storage: Path) -> list[str]:
    d = json.loads(storage.read_text())
    return [e.get("folderUri") for e in d["history.recentlyOpenedPathsList"]["entries"]]


def test_merged_branch_is_pruned_locally(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc, _ = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert "feature/1-merged" not in _local_branches(hub)


def test_merged_branch_is_pruned_on_remote(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-merged", push=True, merge=True)
    proc, _ = _run_done(hub, tmp_path, "1")
    assert proc.returncode == 0, proc.stderr
    assert not _remote_has(hub, "feature/1-merged")


def test_unmerged_branch_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/2-unmerged", push=False, merge=False)
    proc, _ = _run_done(hub, tmp_path, "2")
    assert proc.returncode == 0, proc.stderr
    assert "feature/2-unmerged" in _local_branches(hub)


def test_keep_branch_flag_keeps_merged_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/3-merged", push=False, merge=True)
    proc, _ = _run_done(hub, tmp_path, "3", "--keep-branch")
    assert proc.returncode == 0, proc.stderr
    assert "feature/3-merged" in _local_branches(hub)


def test_code_remove_called_with_worktree_path(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/4-wt", push=False, merge=True)
    _, log = _run_done(hub, tmp_path, "4")
    calls = log.read_text() if log.exists() else ""
    assert "--remove" in calls
    assert str(wt) in calls


def test_code_remove_runs_before_worktree_deletion(hub: Path, tmp_path: Path) -> None:
    # Issue #43: `code --remove` must fire BEFORE `git worktree remove` deletes
    # the directory, so VS Code resolves the path and folds the folder out
    # cleanly (no ghost pane). The stub stamps each call with whether the path
    # still exists on disk — `present` proves the on-disk delete had not yet run.
    _make_spoke(hub, tmp_path, "feature/7-order", push=False, merge=True)
    proc, log = _run_done(hub, tmp_path, "7")
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text() if log.exists() else ""
    assert "--remove" in calls  # sanity: code --remove ran
    assert "present" in calls and "absent" not in calls, (
        f"code --remove ran after the worktree was deleted (ghost pane): {calls!r}"
    )


def test_no_code_flag_skips_code_remove(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/5-wt", push=False, merge=True)
    _, log = _run_done(hub, tmp_path, "5", "--no-code")
    assert not log.exists() or log.read_text().strip() == ""


def test_code_and_remote_failures_are_non_fatal(hub: Path, tmp_path: Path) -> None:
    # Push the branch (so its remote-tracking ref exists), merge it, then break
    # `origin` so the remote delete must fail. With a `code` stub that also
    # fails, both steps warn but teardown still succeeds.
    _make_spoke(hub, tmp_path, "feature/6-merged", push=True, merge=True)
    _git(hub, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    proc, _ = _run_done(hub, tmp_path, "6", code_exit=1)
    assert proc.returncode == 0, proc.stderr
    assert "feature/6-merged" not in _local_branches(hub)  # local delete still ran


def test_refuses_when_run_as_spoke_session(hub: Path, tmp_path: Path) -> None:
    # Teardown is hub-owned: a spoke must not tear down its own worktree. Even a
    # cleanly-merged worktree (the happy teardown case) must be refused when the
    # session carries WT_SPOKE (issue #26). No override flag.
    wt = _make_spoke(hub, tmp_path, "feature/1-spoke", push=True, merge=True)

    proc, _ = _run_done(hub, tmp_path, "1", spoke_marker="1")

    assert proc.returncode != 0
    assert "spoke" in proc.stderr.lower()
    assert "hub" in proc.stderr.lower()
    assert wt.exists()  # worktree untouched
    assert "feature/1-spoke" in _local_branches(hub)  # branch not pruned


def test_tears_down_when_not_a_spoke_session(hub: Path, tmp_path: Path) -> None:
    # The mirror: with WT_SPOKE unset (the hub), teardown proceeds.
    wt = _make_spoke(hub, tmp_path, "feature/1-hub", push=True, merge=True)

    proc, _ = _run_done(hub, tmp_path, "1", spoke_marker=None)

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()


def test_missing_target_dir_does_not_prune_another_branch(hub: Path, tmp_path: Path) -> None:
    # Both worktrees are merged + pushed, then both directories are deleted from
    # disk. Tearing down #2 with --force must prune ONLY feature/2-bbb — a path
    # match by canonicalized (empty) path would wrongly capture feature/1-aaa.
    wt_a = _make_spoke(hub, tmp_path, "feature/1-aaa", push=True, merge=True)
    wt_b = _make_spoke(hub, tmp_path, "feature/2-bbb", push=True, merge=True)
    subprocess.run(["rm", "-rf", str(wt_a), str(wt_b)], check=True)
    proc, _ = _run_done(hub, tmp_path, "2", "--force")
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

    proc, _ = _run_done(hub, tmp_path, "fix-typo")

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

    proc, _ = _run_done(hub, tmp_path, "fix-typo")

    assert proc.returncode != 0, "removal should abort on a dirty worktree without --force"
    assert wt.exists()  # worktree untouched
    assert not marker.exists()  # ...but the bypass is revoked regardless


def test_teardown_without_marker_is_a_noop(hub: Path, tmp_path: Path) -> None:
    # A normal (non-/quick) teardown has no marker to clear — it must not fail.
    _make_spoke(hub, tmp_path, "feature/3-plain", push=False, merge=True)

    proc, _ = _run_done(hub, tmp_path, "3")

    assert proc.returncode == 0, proc.stderr


def test_recent_entry_pruned_when_vscode_not_running(hub: Path, tmp_path: Path) -> None:
    # Issue #103: teardown must drop the just-removed worktree from VS Code's Open
    # Recent list. With no `code.lock` (VS Code closed), the scrub runs and removes
    # only the matching path — sibling recent entries are left untouched.
    wt = _make_spoke(hub, tmp_path, "feature/4-recent", push=False, merge=True)
    sibling = str(tmp_path / "ai-toolkit-other")
    storage = _write_recent(tmp_path, [str(wt), sibling])

    proc, _ = _run_done(hub, tmp_path, "4")

    assert proc.returncode == 0, proc.stderr
    remaining = _recent_paths(storage)
    assert str(wt) not in remaining
    assert sibling in remaining


def test_recent_cleanup_skipped_when_vscode_running(hub: Path, tmp_path: Path) -> None:
    # A live VS Code instance overwrites storage.json on flush, so editing it would
    # be lost (or race). When `code.lock` holds a live PID, the scrub is a no-op and
    # the entry stays — the documented safe behavior.
    wt = _make_spoke(hub, tmp_path, "feature/4-running", push=False, merge=True)
    storage = _write_recent(tmp_path, [str(wt)], running_pid=os.getpid())

    proc, _ = _run_done(hub, tmp_path, "4")

    assert proc.returncode == 0, proc.stderr
    assert str(wt) in _recent_paths(storage)


def test_no_code_flag_skips_recent_cleanup(hub: Path, tmp_path: Path) -> None:
    # `--no-code` opts out of every VS Code touch, including the recent-list scrub.
    wt = _make_spoke(hub, tmp_path, "feature/5-nocode", push=False, merge=True)
    storage = _write_recent(tmp_path, [str(wt)])

    proc, _ = _run_done(hub, tmp_path, "5", "--no-code")

    assert proc.returncode == 0, proc.stderr
    assert str(wt) in _recent_paths(storage)


def test_recent_cleanup_noop_when_storage_absent(hub: Path, tmp_path: Path) -> None:
    # No state store on disk (CLI-only host) — the scrub is a silent no-op and
    # teardown still succeeds.
    _make_spoke(hub, tmp_path, "feature/4-nostore", push=False, merge=True)

    proc, _ = _run_done(hub, tmp_path, "4")

    assert proc.returncode == 0, proc.stderr


def test_recent_cleanup_removes_history_list_entry(hub: Path, tmp_path: Path) -> None:
    # Older VS Code versions keep the recent paths in `history.recentlyOpenedPathsList`;
    # the scrub removes the matching entry there too (forward/backward compat).
    wt = _make_spoke(hub, tmp_path, "feature/4-history", push=False, merge=True)
    storage = _write_recent(tmp_path, [str(wt)], history=True)

    proc, _ = _run_done(hub, tmp_path, "4")

    assert proc.returncode == 0, proc.stderr
    assert f"file://{wt}" not in _history_uris(storage)


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

    proc, _ = _run_done(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    assert "feature/7-base" not in _local_branches(hub)
