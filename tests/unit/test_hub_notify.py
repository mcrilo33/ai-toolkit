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
    afk: bool = False,
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
    # Control afk-mode deterministically: pop any inherited state, then arm a
    # drain window (a non-empty .afk-state, matching hub-afk's convention) only
    # when the test asks for it.
    env.pop("AFK_STATE", None)
    if afk:
        afk_state = tmp_path / "afk-state"
        afk_state.write_text("drain\n")
        env["AFK_STATE"] = str(afk_state)
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
    # Bind the subject read: the gate word ("plan") comes from %(contents:subject),
    # not the "gate" fallback — asserting it keeps the derivation honest.
    assert "plan" in messages[0]


def test_gate_marker_points_to_qcm_surface_when_present(hub: Path, tmp_path: Path) -> None:
    # When the gate-broker attended adapter (#155) has written a QCM surface for the
    # parked gate, the hub-notify ping points the human at it — the announce channel is
    # hub-notify (NOT hub-status.sh, kept disjoint from #154).
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")
    qcm_dir = tmp_path / "qcm"
    qcm_dir.mkdir()
    (qcm_dir / "qcm-1.md").write_text("# Gate 1 QCM\nsummary\n")

    _proc, messages = _run(hub, tmp_path, env_extra={"GATE_BROKER_QCM_DIR": str(qcm_dir)})

    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "qcm" in messages[0].lower(), f"the ping must point at the QCM surface: {messages[0]}"


def test_gate_marker_without_qcm_surface_says_approve(hub: Path, tmp_path: Path) -> None:
    # No QCM surface yet ⇒ the plain "reply to approve" ping (attended default / PLAN park).
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, env_extra={"GATE_BROKER_QCM_DIR": str(tmp_path / "none")})

    assert len(messages) == 1
    assert "approve" in messages[0].lower()
    assert "qcm" not in messages[0].lower()


def test_blocked_marker_surfaces_reason_from_body(hub: Path, tmp_path: Path) -> None:
    # Real spoke-ready.sh format: subject "blocked", reason in the tag BODY.
    _git(
        hub,
        "tag",
        "-a",
        "-m",
        "blocked",
        "-m",
        "merge conflict in foo.py",
        "blocked/1",
        "feature/1-work",
    )

    _proc, messages = _run(hub, tmp_path)

    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "BLOCKED" in messages[0]
    assert "merge conflict in foo.py" in messages[0]


def test_blocked_marker_without_reason_says_needs_human(hub: Path, tmp_path: Path) -> None:
    # Subject "blocked", empty body → the "needs a human" fallback (and the
    # subject-is-"blocked" suppression that stops it leaking into the message).
    _git(hub, "tag", "-a", "-m", "blocked", "blocked/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path)

    assert len(messages) == 1
    assert messages[0] == "#1 BLOCKED — needs a human"


def test_accept_marker_is_not_watched(hub: Path, tmp_path: Path) -> None:
    # accept/<N> is a real sibling marker (human sign-off) spoke-ready.sh emits,
    # deliberately NOT one of the three notified classes.
    _git(hub, "tag", "-a", "-m", "accept", "accept/1", "feature/1-work")

    proc, messages = _run(hub, tmp_path)

    assert proc.returncode == 0
    assert messages == []


def test_malformed_tag_is_skipped(hub: Path, tmp_path: Path) -> None:
    # A non-numeric issue in a watched namespace is ignored by the numeric guard.
    _git(hub, "tag", "ready/abc", "feature/1-work")

    proc, messages = _run(hub, tmp_path)

    assert proc.returncode == 0
    assert messages == []


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


# Mode-aware gating (issue #146): under a live /afk drain the answerer services
# gate parks and the drain auto-lands ready spokes, so only blocked/<N> (the
# escalation a human must act on) pings. Attended, every class pings.
def test_afk_suppresses_gate_ping(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, afk=True)

    assert messages == []


def test_afk_suppresses_ready_ping(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, afk=True)

    assert messages == []


def test_afk_still_fires_blocked(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "blocked", "-m", "stuck", "blocked/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, afk=True)

    assert len(messages) == 1
    assert "#1" in messages[0]
    assert "BLOCKED" in messages[0]


def test_attended_fires_gate_when_no_afk_window(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, afk=False)

    assert len(messages) == 1
    assert "parked" in messages[0].lower()


def test_afk_suppressed_gate_is_recorded_as_seen(hub: Path, tmp_path: Path) -> None:
    # A marker suppressed under a drain is still persisted into the seen-set, so
    # it must NOT belatedly ping when the window ends and attended resumes.
    seen = tmp_path / "seen"
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc1, first = _run(hub, tmp_path, seen_file=seen, afk=True)
    _proc2, second = _run(hub, tmp_path, seen_file=seen, afk=False)

    assert first == []
    assert second == []


# /afk drain-complete (issue #150): hub-afk writes <git-common-dir>/.afk-drain-complete
# with the landed count when a drain finishes. hub-notify fires ONE "/afk drain
# complete — <k> landed" ping and consumes (removes) the file, so a completed drain
# notifies exactly once and the steady post-drain state never repeats it. This is
# independent of the marker seen-set and of afk-mode (the drain already cleared
# .afk-state before this runs).
def test_drain_complete_fires_once_naming_count(hub: Path, tmp_path: Path) -> None:
    done = tmp_path / "drain-complete"
    done.write_text("3\n")

    proc, messages = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert proc.returncode == 0
    assert messages == ["/afk drain complete — 3 landed"]
    assert not done.exists(), "the completion file is consumed (removed) after firing"


def test_drain_complete_consumed_not_refired(hub: Path, tmp_path: Path) -> None:
    done = tmp_path / "drain-complete"
    done.write_text("2\n")

    _proc1, first = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})
    proc2, second = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert first == ["/afk drain complete — 2 landed"]
    assert proc2.returncode == 0
    assert second == first, "consumed → the steady post-drain state fires nothing more"


def test_drain_complete_zero_landed_still_fires(hub: Path, tmp_path: Path) -> None:
    # A drain that landed nothing still fires exactly one "0 landed" signal.
    done = tmp_path / "drain-complete"
    done.write_text("0\n")

    _proc, messages = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert messages == ["/afk drain complete — 0 landed"]


def test_drain_complete_malformed_count_defaults_zero(hub: Path, tmp_path: Path) -> None:
    # A partially-written / corrupt count reads as 0 rather than leaking garbage.
    done = tmp_path / "drain-complete"
    done.write_text("garbage\n")

    _proc, messages = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert messages == ["/afk drain complete — 0 landed"]
    assert not done.exists()


def test_drain_complete_fires_regardless_of_afk_mode(hub: Path, tmp_path: Path) -> None:
    # The drain-complete ping is not gated by afk-mode: even if a stale .afk-state
    # is still armed, a finished drain's completion must still notify.
    done = tmp_path / "drain-complete"
    done.write_text("1\n")

    _proc, messages = _run(hub, tmp_path, afk=True, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert messages == ["/afk drain complete — 1 landed"]


def test_no_drain_complete_file_fires_nothing(hub: Path, tmp_path: Path) -> None:
    # Absent completion file (the common steady state) ⇒ no drain-complete ping.
    done = tmp_path / "drain-complete"  # never created

    _proc, messages = _run(hub, tmp_path, env_extra={"AFK_DRAIN_COMPLETE": str(done)})

    assert messages == []


def test_whitespace_only_afk_state_does_not_suppress(hub: Path, tmp_path: Path) -> None:
    # Parity with afk_read_state: a whitespace-only .afk-state is NOT armed, so
    # a gate marker still fires (guards against dropping the non-empty trim).
    state = tmp_path / "blank-afk-state"
    state.write_text("   \n")
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(hub, tmp_path, env_extra={"AFK_STATE": str(state)})

    assert len(messages) == 1
    assert "parked" in messages[0].lower()
