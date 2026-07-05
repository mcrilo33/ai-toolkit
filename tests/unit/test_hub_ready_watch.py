"""Unit tests for shared/skills/hub/scripts/hub-ready-watch.sh.

The watcher turns the hub's on-demand `pushed → mergeable` check into a
proactive poll: each run fetches tags, diffs the `ready/*` completion markers
(issue #16) against a persisted last-seen set, and surfaces a "#N → run /land N"
line for each NEWLY-ready spoke — without the user running /hub (issue #25).

Hard rules under test: it surfaces a proposal only (never merges), only a
`ready/*` tag at its branch tip triggers (no false-fire on mid-task pushes or
stale markers), and a failed fetch (offline) still detects local tags.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

WATCH = (
    Path(__file__).resolve().parents[2]
    / "shared"
    / "skills"
    / "hub"
    / "scripts"
    / "hub-ready-watch.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A hub (main checkout) with one pushed spoke worktree (feature/1-pushed,
    one commit ahead of main, pushed to its upstream). No ready tag yet — the
    tests add markers as needed."""
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

    spoke = tmp_path / "spoke1"
    _git(hub, "worktree", "add", "-q", "-b", "feature/1-pushed", str(spoke))
    (spoke / "a.txt").write_text("a\n")
    _git(spoke, "add", "a.txt")
    _git(spoke, "commit", "-qm", "feat: a", "-m", "Refs #1")
    _git(spoke, "push", "-q", "-u", "origin", "feature/1-pushed")
    return hub


def _run(
    hub: Path,
    tmp_path: Path,
    *,
    seen_file: Path | None = None,
    break_remote: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run hub-ready-watch.sh from the hub.

    Args:
        hub: Main checkout to run from.
        tmp_path: Test tmpdir; the default seen-file lives under it.
        seen_file: Path persisting the last-seen marker set. Pass the same
            path across runs to test the not-re-surfaced behavior; the default
            is a fresh nonexistent path so a lone run sees every tag as new.
        break_remote: Point origin at a nonexistent path so `git fetch` fails
            — exercises offline degradation.

    Returns:
        The CompletedProcess with captured stdout/stderr.
    """
    if break_remote:
        _git(hub, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    env_seen = str(seen_file if seen_file is not None else tmp_path / "seen-default")
    import os

    env = {**os.environ, "HUB_READY_SEEN_FILE": env_seen}
    # The host's base-branch override (#117) must never steer the script under test.
    env.pop("AI_TOOLKIT_BASE_BRANCH", None)
    # Isolate the merged-in hub-notify (#146): capture its notifications to a log
    # via HUB_NOTIFY_CMD (never fire real osascript during the suite), keep its
    # seen-set under tmp_path, and strip any inherited drain state so mode-gating
    # is deterministic. Notifications are readable at _notify_log(tmp_path).
    notifier = tmp_path / "notifier.sh"
    notifier.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{_notify_log(tmp_path)}"\n')
    notifier.chmod(0o755)
    env["HUB_NOTIFY_CMD"] = str(notifier)
    env["HUB_NOTIFY_SEEN_FILE"] = str(tmp_path / "hub-notify-seen")
    env.pop("AFK_STATE", None)
    return subprocess.run(
        ["bash", str(WATCH)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _notify_log(tmp_path: Path) -> Path:
    """Path the injected hub-notify notifier appends each fired message to."""
    return tmp_path / "hub-notify.log"


def _notifications(tmp_path: Path) -> list[str]:
    """The messages hub-notify fired during _run (empty when none)."""
    log = _notify_log(tmp_path)
    return log.read_text().splitlines() if log.exists() else []


def test_new_ready_tag_is_surfaced_with_land_command(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-pushed")

    result = _run(hub, tmp_path)

    assert result.returncode == 0
    line = next(ln for ln in result.stdout.splitlines() if "#1" in ln)
    assert "/land 1" in line
    assert "feature/1-pushed" in line


def test_surfaced_line_includes_ahead_behind_state(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-pushed")

    out = _run(hub, tmp_path).stdout

    line = next(ln for ln in out.splitlines() if "#1" in ln)
    assert "↑1" in line
    assert "↓0" in line


def test_already_seen_tag_is_not_resurfaced(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-pushed")
    seen = tmp_path / "seen"

    first = _run(hub, tmp_path, seen_file=seen)
    second = _run(hub, tmp_path, seen_file=seen)

    assert "#1" in first.stdout
    assert second.returncode == 0
    assert "#1" not in second.stdout


def test_no_ready_tag_surfaces_nothing(hub: Path, tmp_path: Path) -> None:
    # A pushed branch with NO ready tag is a mid-task push — it must not fire.
    result = _run(hub, tmp_path)

    assert result.returncode == 0
    assert "#1" not in result.stdout
    assert "/land" not in result.stdout


def test_annotated_ready_tag_is_surfaced(hub: Path, tmp_path: Path) -> None:
    # Annotated tags carry their own object sha; the tip gate must peel to the
    # commit (^{commit}) or it silently never fires. Regression for that.
    _git(hub, "tag", "-a", "-m", "ready", "ready/1", "feature/1-pushed")

    out = _run(hub, tmp_path).stdout

    line = next(ln for ln in out.splitlines() if "#1" in ln)
    assert "/land 1" in line


def test_stale_marker_off_tip_not_surfaced(hub: Path, tmp_path: Path) -> None:
    # Marker points at main's seed sha, not the branch tip — not a completion
    # claim for the branch, so do not propose land.
    seed = _git(hub, "rev-parse", "main").strip()
    _git(hub, "tag", "ready/1", seed)

    out = _run(hub, tmp_path).stdout

    assert "#1" not in out
    assert "/land" not in out


def test_mid_task_push_after_tagging_not_surfaced(hub: Path, tmp_path: Path) -> None:
    # The core false-fire guard: spoke tags at C1, then pushes C2 (another
    # subtask) without re-tagging. The marker is now behind the tip — it must
    # not propose land until the spoke re-tags at the final tip.
    spoke = tmp_path / "spoke1"
    _git(hub, "tag", "ready/1", "feature/1-pushed")
    (spoke / "c.txt").write_text("c\n")
    _git(spoke, "add", "c.txt")
    _git(spoke, "commit", "-qm", "feat: another subtask", "-m", "Refs #1")
    _git(spoke, "push", "-q", "origin", "feature/1-pushed")

    out = _run(hub, tmp_path).stdout

    assert "#1" not in out
    assert "/land" not in out


def test_missing_worktree_does_not_fire(hub: Path, tmp_path: Path) -> None:
    # Degraded case: the tag exists but its worktree was torn down, so the tip
    # can't be verified. Stay silent rather than propose an unverifiable land.
    _git(hub, "tag", "ready/1", "feature/1-pushed")
    _git(hub, "worktree", "remove", str(tmp_path / "spoke1"))

    result = _run(hub, tmp_path)

    assert result.returncode == 0
    assert "#1" not in result.stdout


def test_retagged_marker_refires(hub: Path, tmp_path: Path) -> None:
    # Surface once at the tip; the spoke then pushes more and re-tags at the new
    # tip (git tag -f). The moved marker is a fresh completion claim → re-fire.
    spoke = tmp_path / "spoke1"
    _git(hub, "tag", "ready/1", "feature/1-pushed")
    seen = tmp_path / "seen"

    first = _run(hub, tmp_path, seen_file=seen).stdout

    (spoke / "b.txt").write_text("b\n")
    _git(spoke, "add", "b.txt")
    _git(spoke, "commit", "-qm", "feat: b", "-m", "Refs #1")
    _git(spoke, "push", "-q", "origin", "feature/1-pushed")
    _git(hub, "tag", "-f", "ready/1", "feature/1-pushed")

    second = _run(hub, tmp_path, seen_file=seen).stdout

    assert "#1" in first
    assert "#1" in second


def test_offline_fetch_failure_still_detects_local_tag(hub: Path, tmp_path: Path) -> None:
    # A worktree-created ready tag is locally visible (shared ref store) even
    # when origin is unreachable; the failed fetch must not abort the watcher.
    _git(hub, "tag", "ready/1", "feature/1-pushed")

    result = _run(hub, tmp_path, break_remote=True)

    assert result.returncode == 0
    assert "#1" in result.stdout
    assert "/land 1" in result.stdout


def test_run_never_merges_to_main(hub: Path, tmp_path: Path) -> None:
    # The whole point: surface a proposal, never merge. main must be untouched
    # and the spoke branch must not become an ancestor of main.
    _git(hub, "tag", "ready/1", "feature/1-pushed")
    before = _git(hub, "rev-parse", "main").strip()

    _run(hub, tmp_path)

    after = _git(hub, "rev-parse", "main").strip()
    assert before == after
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "feature/1-pushed", "main"],
        cwd=str(hub),
        capture_output=True,
    )
    assert merged.returncode != 0  # not merged


# --- configurable base branch (issue #117) --------------------------------------


def test_counts_measured_against_configured_base(hub: Path, tmp_path: Path) -> None:
    # The surfaced ↑/↓ counts must be measured against the RESOLVED base
    # (config ai-toolkit.base-branch), not literal main: with develop = main+1
    # configured, the 1-ahead-of-main spoke reads ↑1 ↓1.
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")
    _git(hub, "tag", "ready/1", "feature/1-pushed")

    result = _run(hub, tmp_path)

    assert result.returncode == 0
    line = next(ln for ln in result.stdout.splitlines() if "#1" in ln)
    assert "↑1" in line
    assert "↓1" in line


def test_ready_watch_loop_also_fires_hub_notify(hub: Path, tmp_path: Path) -> None:
    # #146: the single hub loop drives BOTH surfaces — hub-ready-watch invokes
    # the co-located hub-notify each poll, so a new blocked/<N> marker (which the
    # ready watch itself never surfaces) still produces one OS notification.
    _git(hub, "tag", "-a", "-m", "blocked", "-m", "merge conflict", "blocked/1", "feature/1-pushed")

    result = _run(hub, tmp_path)

    assert result.returncode == 0
    notes = _notifications(tmp_path)
    assert len(notes) == 1
    assert "#1" in notes[0]
    assert "BLOCKED" in notes[0]
