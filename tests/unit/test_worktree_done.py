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
import shutil
import subprocess
from pathlib import Path

import pytest

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
    # HOME is sandboxed so the workspace-file default ($HOME/.claude/….code-workspace,
    # issue #134) can never resolve to — let alone rewrite — the host's real file.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
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

    proc, _ = _run_done(hub, tmp_path, "8")

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

    proc, _ = _run_done(hub, tmp_path, "10")

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

    proc, _ = _run_done(hub, tmp_path, "11")

    assert proc.returncode == 0, proc.stderr
    assert "feature/11-late" not in _local_branches(hub)  # local prune still runs
    assert _remote_has(hub, "feature/11-late"), "the remote-only commit must survive"
    assert "origin/feature/11-late" in proc.stderr, "the kept remote is loud"


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


# --- review workspace file: direct remove + ghost sweep (issue #134) ----------
# Teardown edits the review workspace file's `folders` array directly (the
# mirror of worktree-new's direct append): drop the target's entry, sweep any
# entry whose path is gone from disk (self-healing for past `code --remove`
# misses), and never also call `code --remove`. The CLI call survives strictly
# as the missing-file fallback. `git config ai-toolkit.workspace-file` is
# pinned per-test so the host's real review workspace is never touched.


def _write_workspace(ws: Path, folders: list[dict]) -> str:
    """Write a VS Code-shaped workspace file (tab indent) and return its text."""
    ws.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"folders": folders, "settings": {}}, indent="\t") + "\n"
    ws.write_text(text)
    return text


def test_done_removes_entry_and_sweeps_ghosts_not_code_remove(hub: Path, tmp_path: Path) -> None:
    # Target entry dropped, dead-path ghost swept in the same pass, live sibling
    # and main-checkout entries preserved verbatim — and no `code` call at all.
    wt = _make_spoke(hub, tmp_path, "feature/9-ws", push=True, merge=True)
    live = _make_spoke(hub, tmp_path, "feature/7-live", push=False, merge=False)
    ws = tmp_path / "claude" / "review.code-workspace"
    main_entry = {"name": "hub", "path": os.path.relpath(hub, ws.parent)}
    live_entry = {"path": os.path.relpath(live, ws.parent)}
    _write_workspace(
        ws,
        [
            main_entry,
            {"name": wt.name, "path": os.path.relpath(wt, ws.parent)},
            {"path": "../gone-99"},
            live_entry,
        ],
    )
    _git(hub, "config", "ai-toolkit.workspace-file", str(ws))

    proc, log = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    doc = json.loads(ws.read_text())
    assert doc["folders"] == [main_entry, live_entry]
    calls = log.read_text() if log.exists() else ""
    assert calls == "", f"direct file edit and the `code` CLI must never both fire: {calls!r}"


def test_done_falls_back_to_code_remove_when_workspace_file_missing(
    hub: Path, tmp_path: Path
) -> None:
    # No workspace file at the configured location → the legacy `code --remove`
    # fallback fires exactly as before (still while the path exists on disk).
    wt = _make_spoke(hub, tmp_path, "feature/9-fb", push=True, merge=True)
    _git(
        hub,
        "config",
        "ai-toolkit.workspace-file",
        str(tmp_path / "claude" / "absent.code-workspace"),
    )

    proc, log = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text() if log.exists() else ""
    assert f"--remove {wt}" in calls
    assert "present" in calls and "absent" not in calls


def test_done_no_code_leaves_workspace_file_untouched(hub: Path, tmp_path: Path) -> None:
    # --no-code opts out of the whole VS Code fold: no file edit, no CLI call.
    wt = _make_spoke(hub, tmp_path, "feature/9-nc", push=True, merge=True)
    ws = tmp_path / "claude" / "review.code-workspace"
    before = _write_workspace(ws, [{"name": wt.name, "path": os.path.relpath(wt, ws.parent)}])
    _git(hub, "config", "ai-toolkit.workspace-file", str(ws))

    proc, log = _run_done(hub, tmp_path, "9", "--no-code")

    assert proc.returncode == 0, proc.stderr
    assert ws.read_text() == before
    assert not log.exists() or log.read_text().strip() == ""


# --- leftover-dir sweep after `git worktree remove` (issue #134) ---------------
# A lingering shell cwd or gitignored runtime files can leave the directory on
# disk even when git deregistered the worktree (#122 left ai-toolkit-122
# behind). After a successful `git worktree remove`, teardown must retry with an
# rm -rf of the leftover — and warn LOUDLY, still exiting 0, if even that fails.
# Simulated with a `git` shim whose `worktree remove` delegates to the real git
# and then recreates the directory holding an untracked file.


def _leftover_git_shim(tmp_path: Path, leftover_dir: Path) -> None:
    """PATH `git` shim: real `worktree remove`, then resurrect the directory."""
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = worktree ] && [ "$2" = remove ]; then\n'
        f'  "{real_git}" "$@"; rc=$?\n'
        f'  mkdir -p "{leftover_dir}"\n'
        f'  echo runtime-junk > "{leftover_dir}/leftover.txt"\n'
        '  exit "$rc"\n'
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)


def test_done_sweeps_leftover_dir_after_worktree_remove(hub: Path, tmp_path: Path) -> None:
    # git deregistered the worktree but the directory survived (untracked file)
    # → teardown rm -rf's the leftover and still succeeds.
    wt = _make_spoke(hub, tmp_path, "feature/9-leftover", push=True, merge=True)
    _leftover_git_shim(tmp_path, wt)

    proc, _ = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists(), "the resurrected leftover directory must be swept"


def test_done_warns_loudly_when_leftover_dir_survives_rm(hub: Path, tmp_path: Path) -> None:
    # Even the rm -rf retry fails (stubbed rm exits 1 without removing) → the
    # teardown must warn loudly, naming the path and the manual command, and
    # still exit 0 — a stuck directory must never abort branch pruning.
    wt = _make_spoke(hub, tmp_path, "feature/9-stuck", push=True, merge=True)
    _leftover_git_shim(tmp_path, wt)
    # The stub fails ONLY for the sweep's own target and delegates every other
    # rm to the real binary, so it can never alter unrelated rm uses in the
    # script (e.g. the hub-guard-allow revoke).
    real_rm = shutil.which("rm")
    assert real_rm is not None
    bindir = tmp_path / "bin"
    rm_stub = bindir / "rm"
    rm_stub.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  [ "$a" = "{wt}" ] && exit 1\n'
        "done\n"
        f'exec "{real_rm}" "$@"\n'
    )
    rm_stub.chmod(0o755)

    proc, _ = _run_done(hub, tmp_path, "9")

    assert proc.returncode == 0, proc.stderr
    assert wt.exists()
    assert str(wt) in proc.stderr
    assert "rm -rf" in proc.stderr
    assert "feature/9-stuck" not in _local_branches(hub), (
        "branch pruning must still run after a failed leftover sweep"
    )
