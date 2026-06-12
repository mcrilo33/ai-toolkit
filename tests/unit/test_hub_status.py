"""Unit tests for shared/skills/hub/scripts/hub-status.sh.

The dashboard's worktree state label drives the hub's merge proposals, so the
push/mergeable classification must be correct: push state is measured against
the branch's own upstream, mergeability against the default branch. A `gh` stub
keeps the issue-survey section hermetic (no network, no real GitHub remote).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HUB_STATUS = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-status.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub_with_spokes(tmp_path: Path) -> Path:
    """A hub (main checkout) with two spoke worktrees: one fully pushed and one
    with a local-only commit."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")

    # Pushed spoke: a commit on its branch, pushed to its upstream.
    pushed = tmp_path / "pushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/1-pushed", str(pushed))
    (pushed / "a.txt").write_text("a\n")
    _git(pushed, "add", "a.txt")
    _git(pushed, "commit", "-qm", "feat: a", "-m", "Refs #1")
    _git(pushed, "push", "-q", "-u", "origin", "feature/1-pushed")

    # Unpushed spoke: a local-only commit, no upstream.
    unpushed = tmp_path / "unpushed"
    _git(hub, "worktree", "add", "-q", "-b", "feature/2-unpushed", str(unpushed))
    (unpushed / "b.txt").write_text("b\n")
    _git(unpushed, "add", "b.txt")
    _git(unpushed, "commit", "-qm", "feat: b", "-m", "Refs #2")

    # Pushed-but-not-ahead spoke: branched from main, pushed, no new commits —
    # nothing left to merge, so "pushed" (not "pushed → mergeable").
    pushed_even = tmp_path / "even"
    _git(hub, "worktree", "add", "-q", "-b", "feature/3-even", str(pushed_even))
    _git(pushed_even, "push", "-q", "-u", "origin", "feature/3-even")
    return hub


def _run_hub_status_proc(
    hub: Path,
    tmp_path: Path,
    *,
    panes: str = "",
    inside_tmux: bool = False,
    current_session: str = "hub-sess",
    issue_state: str = "",
    tmux_fail: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run hub-status.sh from the hub with `gh` and `tmux` stubs on PATH.

    Args:
        hub: Main checkout to run the script from.
        tmp_path: Test tmpdir; the stub bin dir is created under it.
        panes: Multi-line ``session:window<TAB>path`` text the tmux stub
            prints for ``list-panes`` (empty → no panes).
        inside_tmux: When True, set a fake TMUX env var; otherwise TMUX is
            popped so the run is hermetic.
        current_session: What the tmux stub prints for ``display-message``.
        issue_state: When non-empty, the gh stub answers ``issue view`` with
            this on stdout (exit 0); ``issue list`` keeps failing. When empty,
            every gh call exits 1 (degrades to "(none open)").
        tmux_fail: When True, every tmux invocation exits 1 (no server).

    Returns:
        The CompletedProcess with captured stdout/stderr.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    if issue_state:
        gh.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then\n'
            "  printf '%s\\n' \"$HUB_STATUS_TEST_ISSUE_STATE\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    else:
        gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        '[ "${HUB_STATUS_TEST_TMUX_FAIL:-}" = "1" ] && exit 1\n'
        'case "$1" in\n'
        "  list-panes)\n"
        '    [ -n "${HUB_STATUS_TEST_PANES:-}" ] && printf \'%s\\n\' "$HUB_STATUS_TEST_PANES"\n'
        "    ;;\n"
        "  display-message)\n"
        "    printf '%s\\n' \"$HUB_STATUS_TEST_SESSION\"\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)  # hermetic by default; faked only when inside_tmux
    if inside_tmux:
        env["TMUX"] = "/tmp/tmux-test/default,1234,0"
    env["HUB_STATUS_TEST_PANES"] = panes
    env["HUB_STATUS_TEST_SESSION"] = current_session
    env["HUB_STATUS_TEST_ISSUE_STATE"] = issue_state
    env["HUB_STATUS_TEST_TMUX_FAIL"] = "1" if tmux_fail else ""
    return subprocess.run(
        ["bash", str(HUB_STATUS)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_hub_status(
    hub: Path,
    tmp_path: Path,
    *,
    panes: str = "",
    inside_tmux: bool = False,
    current_session: str = "hub-sess",
    issue_state: str = "",
    tmux_fail: bool = False,
) -> str:
    """Run hub-status.sh and return its stdout (see _run_hub_status_proc)."""
    return _run_hub_status_proc(
        hub,
        tmp_path,
        panes=panes,
        inside_tmux=inside_tmux,
        current_session=current_session,
        issue_state=issue_state,
        tmux_fail=tmux_fail,
    ).stdout


def test_pushed_spoke_is_mergeable(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "pushed → mergeable" in line


def test_unpushed_spoke_is_unpushed(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/2-unpushed" in ln)
    assert "unpushed" in line


def test_pushed_even_spoke_is_pushed_not_mergeable(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if "feature/3-even" in ln)
    assert "pushed" in line and "mergeable" not in line


def test_hub_branch_labelled_hub(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("main"))
    assert "(hub)" in line


# --- Cross-session correlation (issue #8) -----------------------------------


def test_spoke_pane_listed_across_sessions(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "tmux 0:3" in line


def test_worktree_row_shows_issue_number_and_state(hub_with_spokes: Path, tmp_path: Path) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path, issue_state="OPEN")

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "#1 OPEN" in line


def test_issue_state_degrades_to_question_mark_without_gh(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    out = _run_hub_status(hub_with_spokes, tmp_path)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "#1 ?" in line


def test_jump_select_window_when_pane_in_current_session(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(
        hub_with_spokes, tmp_path, panes=panes, inside_tmux=True, current_session="0"
    )

    assert "tmux select-window -t '0:3'" in out


def test_jump_switch_client_when_pane_in_other_session(
    hub_with_spokes: Path, tmp_path: Path
) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(
        hub_with_spokes, tmp_path, panes=panes, inside_tmux=True, current_session="hub-sess"
    )

    assert "tmux switch-client -t '0:3'" in out


def test_jump_attach_when_outside_tmux(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"0:3\t{tmp_path / 'pushed'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    assert "tmux attach -t 0 \\; select-window -t '0:3'" in out


def test_worktree_without_pane_shows_no_pane(hub_with_spokes: Path, tmp_path: Path) -> None:
    panes = f"1:1\t{tmp_path / 'somewhere-unrelated'}"

    out = _run_hub_status(hub_with_spokes, tmp_path, panes=panes)

    line = next(ln for ln in out.splitlines() if "feature/1-pushed" in ln)
    assert "no pane" in line
    assert "select-window" not in out
    assert "switch-client" not in out


def test_degrades_when_tmux_unavailable(hub_with_spokes: Path, tmp_path: Path) -> None:
    # TMUX is set but every tmux call fails (stale env, dead server): the
    # script must still exit 0 and print the branch-state rows.
    result = _run_hub_status_proc(hub_with_spokes, tmp_path, inside_tmux=True, tmux_fail=True)

    line = next(ln for ln in result.stdout.splitlines() if "feature/1-pushed" in ln)
    assert result.returncode == 0
    assert "pushed → mergeable" in line
