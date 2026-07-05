"""Unit tests for shared/skills/hub/scripts/hub-notify.sh.

The hub is the single notifier (issue #146): spokes silence their own idle
notifications, and the hub fires ONE OS notification per NEW lifecycle
transition — a `gate/<N>` (parked), `ready/<N>` (done), or `blocked/<N>`
(needs a human) marker tag. Dedupe mirrors hub-ready-watch: a persisted
last-seen set keyed on "<tag> <sha>", so a brand-new tag OR a force-moved one
(git tag -f after another push) counts as a fresh transition, and a steady
state fires nothing.

Unlike hub-ready-watch, hub-notify does NOT gate on a live worktree at the
branch tip: `blocked/<N>` is emitted exactly when a spoke is reaped / torn
down, so requiring a resolvable worktree would drop the most important pings.
The marker's appearance IS the transition.

The OS notifier is injected via HUB_NOTIFY_CMD (an executable receiving the
message as $1) so tests capture content without firing real notifications.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

NOTIFY = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-notify.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A hub (main checkout) with one feature branch, feature/1-work, one commit
    ahead of main. No marker tags yet — each test adds the markers it needs."""
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "checkout", "-q", "-b", "feature/1-work")
    (hub / "a.txt").write_text("a\n")
    _git(hub, "add", "a.txt")
    _git(hub, "commit", "-qm", "feat: a", "-m", "Refs #1")
    _git(hub, "checkout", "-q", "main")
    return hub


def _run(
    hub: Path,
    tmp_path: Path,
    *,
    seen_file: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run hub-notify.sh, capturing each fired notification's message.

    Returns (completed_process, messages) where messages is the ordered list of
    strings passed to the injected notifier.
    """
    notify_log = tmp_path / "notifications.log"
    notifier = tmp_path / "notifier.sh"
    notifier.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{notify_log}"\n')
    notifier.chmod(0o755)

    env = {
        **os.environ,
        "HUB_NOTIFY_SEEN_FILE": str(seen_file if seen_file is not None else tmp_path / "seen"),
        "HUB_NOTIFY_CMD": str(notifier),
    }
    env.pop("AI_TOOLKIT_BASE_BRANCH", None)
    if env_extra:
        env.update(env_extra)

    proc = subprocess.run(
        ["bash", str(NOTIFY)], cwd=str(hub), capture_output=True, text=True, env=env
    )
    messages = notify_log.read_text().splitlines() if notify_log.exists() else []
    return proc, messages


def test_new_ready_marker_fires_once_with_land_action(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-work")

    proc, messages = _run(hub, tmp_path)

    assert proc.returncode == 0
    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "/land 1" in messages[0]


def test_new_gate_marker_fires_with_approve_action(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path)

    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "parked" in messages[0].lower()
    assert "approve" in messages[0].lower()


def test_new_blocked_marker_fires_needs_human(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "merge conflict", "blocked/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path)

    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "BLOCKED" in messages[0]


def test_seen_marker_is_not_refired(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-work")
    seen = tmp_path / "seen"

    _first_proc, first = _run(hub, tmp_path, seen_file=seen)
    second_proc, second = _run(hub, tmp_path, seen_file=seen)

    assert len(first) == 1
    assert second_proc.returncode == 0
    assert second == first  # steady state: no NEW notification appended


def test_retagged_marker_refires(hub: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen"
    _git(hub, "tag", "ready/1", "feature/1-work")
    _proc, before = _run(hub, tmp_path, seen_file=seen)

    # Spoke pushes more and re-tags at the new tip (git tag -f) → fresh sha.
    (hub / "b.txt").write_text("b\n")
    _git(hub, "checkout", "-q", "feature/1-work")
    _git(hub, "add", "b.txt")
    _git(hub, "commit", "-qm", "feat: b", "-m", "Refs #1")
    _git(hub, "tag", "-f", "ready/1", "feature/1-work")
    _git(hub, "checkout", "-q", "main")

    # The captured log accumulates across runs (shared tmp_path notifier), so the
    # moved marker being a fresh transition means exactly one NEW notification.
    _proc2, after = _run(hub, tmp_path, seen_file=seen)

    assert len(after) == len(before) + 1
    assert "#1" in after[-1]


def test_each_marker_class_fires_once(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")
    _git(hub, "tag", "ready/2", "feature/1-work")
    _git(hub, "tag", "-a", "-m", "stuck", "blocked/3", "feature/1-work")

    _proc, messages = _run(hub, tmp_path)

    assert len(messages) == 3
    joined = "\n".join(messages)
    assert "#1" in joined
    assert "#2" in joined
    assert "#3" in joined


def test_no_markers_fires_nothing(hub: Path, tmp_path: Path) -> None:
    proc, messages = _run(hub, tmp_path)

    assert proc.returncode == 0
    assert messages == []
