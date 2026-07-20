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


def _dead_pid() -> int:
    """A pid that named a real process which has since exited — a crashed
    supervisor's heartbeat pid. Spawn a trivial process and reap it so the pid is
    gone (reuse within a test is vanishingly unlikely)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


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

    # A logging `gh` stub on PATH so the issue #236 status-label mirror is hermetic
    # and its `gh issue edit` / `gh label create` calls are asserted from $GH_LOG.
    # $GH_MIRROR_RC forces a nonzero exit to model an offline gh.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh-calls.log"
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        '{ printf "%s" "$*" | tr "\\n" " "; printf "\\n"; } >> "$GH_LOG"\n'
        'case "$*" in "issue edit"*|"issue comment"*|"label create"*) exit "${GH_MIRROR_RC:-0}";; esac\n'
    )
    gh.chmod(0o755)

    env = {
        **os.environ,
        "HUB_NOTIFY_SEEN_FILE": str(seen_file if seen_file is not None else tmp_path / "seen"),
        "HUB_NOTIFY_CMD": str(notifier),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_LOG": str(gh_log),
        "HUB_LABEL_SEEN_FILE": str(tmp_path / "label-seen"),
    }
    env.pop("AI_TOOLKIT_BASE_BRANCH", None)
    env.pop("AI_TOOLKIT_GH_LIFECYCLE_LABELS", None)
    env.pop("GH_MIRROR_RC", None)
    # Control afk-mode deterministically: pop any inherited state/heartbeat, then
    # arm a LIVE drain window only when the test asks for it. hub-notify gates
    # suppression on supervisor liveness (#215) — an armed .afk-state whose
    # heartbeat pid is a running process — so `afk=True` writes BOTH the armed
    # state and a heartbeat naming this test process's own (live) pid. A stale
    # window (armed state, dead/absent heartbeat) is modelled per-test via
    # env_extra, and must NOT suppress.
    env.pop("AFK_STATE", None)
    env.pop("AFK_HEARTBEAT", None)
    if afk:
        afk_state = tmp_path / "afk-state"
        afk_state.write_text("drain\n")
        env["AFK_STATE"] = str(afk_state)
        afk_hb = tmp_path / "afk-heartbeat"
        afk_hb.write_text(f"{os.getpid()} 0\n")
        env["AFK_HEARTBEAT"] = str(afk_hb)
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


def test_gate_marker_finds_qcm_surface_via_afk_state_dir(hub: Path, tmp_path: Path) -> None:
    # hub-notify must find a surface written by the broker under AFK_STATE_DIR (the same
    # override _afk_state_dir/_broker_qcm_dir honor) — the two paths must coincide, or the
    # ping silently degrades. This locks the default-path contract via the shared knob.
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")
    statedir = tmp_path / "sd"
    (statedir / "gate-broker").mkdir(parents=True)
    (statedir / "gate-broker" / "qcm-1.md").write_text("# Gate 1 QCM\n")

    _proc, messages = _run(hub, tmp_path, env_extra={"AFK_STATE_DIR": str(statedir)})

    assert len(messages) == 1
    assert "qcm" in messages[0].lower(), messages[0]


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


# Supervisor-liveness gating (issue #215): the afk-suppression of gate/ready is
# gated on the drain supervisor actually running — an armed .afk-state whose
# heartbeat pid is a live process — NOT on .afk-state being merely non-empty. A
# crashed drain (armed state, dead/absent heartbeat) is STALE and must not
# suppress, or the operator loses visibility into parked/ready spokes forever.
def test_stale_drain_absent_heartbeat_does_not_suppress_gate(hub: Path, tmp_path: Path) -> None:
    # Armed window, no heartbeat file at all → no supervisor behind the state →
    # the gate ping still fires.
    state = tmp_path / "stale-afk-state"
    state.write_text("drain\n")
    missing_hb = tmp_path / "no-heartbeat"  # never created
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(
        hub, tmp_path, env_extra={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(missing_hb)}
    )

    assert len(messages) == 1
    assert "parked" in messages[0].lower()


def test_stale_drain_dead_heartbeat_pid_does_not_suppress_ready(hub: Path, tmp_path: Path) -> None:
    # Heartbeat present but its pid is gone (supervisor crashed after stamping) →
    # stale → the ready ping still fires.
    state = tmp_path / "stale-afk-state"
    state.write_text("drain\n")
    hb = tmp_path / "dead-heartbeat"
    hb.write_text(f"{_dead_pid()} 0\n")
    _git(hub, "tag", "ready/1", "feature/1-work")

    _proc, messages = _run(
        hub, tmp_path, env_extra={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)}
    )

    assert len(messages) == 1
    assert "/land 1" in messages[0]


def test_garbled_heartbeat_pid_does_not_suppress_gate(hub: Path, tmp_path: Path) -> None:
    # A partially-written heartbeat whose pid field is non-numeric ("12ab") reads as
    # no live supervisor (mirrors hub-afk's _afk_pid_alive guard) → stale → still fires.
    state = tmp_path / "stale-afk-state"
    state.write_text("drain\n")
    hb = tmp_path / "garbled-heartbeat"
    hb.write_text("12ab 0\n")
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc, messages = _run(
        hub, tmp_path, env_extra={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)}
    )

    assert len(messages) == 1
    assert "parked" in messages[0].lower()


def test_afk_suppressed_gate_pings_after_drain_ends(hub: Path, tmp_path: Path) -> None:
    # A gate suppressed under a LIVE drain is NOT recorded as seen (#215): the ping
    # is deferred, not lost. Once the drain ends and attended resumes, the still-
    # present marker fires so the operator regains visibility into the parked spoke.
    seen = tmp_path / "seen"
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc1, first = _run(hub, tmp_path, seen_file=seen, afk=True)
    _proc2, second = _run(hub, tmp_path, seen_file=seen, afk=False)

    assert first == []  # suppressed while the drain is live
    assert len(second) == 1  # deferred, then delivered once attended resumes
    assert "parked" in second[0].lower()


def test_afk_suppressed_gate_pings_when_supervisor_dies(hub: Path, tmp_path: Path) -> None:
    # The exact issue scenario: a park is suppressed under a live drain, then the
    # supervisor crashes (state stays armed, heartbeat pid gone). Because the
    # suppressed marker was never recorded seen, the now-stale window lets it fire —
    # the lost-forever ping is instead delivered on the next poll.
    seen = tmp_path / "seen"
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    _proc1, first = _run(hub, tmp_path, seen_file=seen, afk=True)
    armed_state = tmp_path / "afk-state"  # written by the afk=True run, still armed
    dead_hb = tmp_path / "crashed-heartbeat"
    dead_hb.write_text(f"{_dead_pid()} 0\n")
    _proc2, second = _run(
        hub,
        tmp_path,
        seen_file=seen,
        env_extra={"AFK_STATE": str(armed_state), "AFK_HEARTBEAT": str(dead_hb)},
    )

    assert first == []
    assert len(second) == 1
    assert "parked" in second[0].lower()


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


# --- status-label mirror (issue #236) -----------------------------------------
# The hub-notify watch loop is the single writer that mirrors spoke-emitted
# gate/ready/blocked marker transitions onto the GitHub issue's status:* label.
# It flips the label with its OWN dedup seen-set (independent of the ping seen-set)
# so a label moves exactly once per new marker sha, and — unlike the ping — the flip
# is DECOUPLED from afk suppression: under a live drain the ping is withheld but the
# label must still reflect state (the whole point of remote visibility). Best-effort.


def _gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _status_edits(tmp_path: Path) -> list[str]:
    return [c for c in _gh_calls(tmp_path) if c.startswith("issue edit")]


def test_gate_marker_flips_status_gate_label(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    proc, _ = _run(hub, tmp_path)

    assert proc.returncode == 0, proc.stderr
    edits = _status_edits(tmp_path)
    assert len(edits) == 1, edits
    assert edits[0].startswith("issue edit 1 ")
    assert "--add-label status:gate" in edits[0]
    assert "--remove-label status:in-progress" in edits[0]


def test_ready_marker_flips_status_ready_label(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-work")

    proc, _ = _run(hub, tmp_path)

    assert proc.returncode == 0, proc.stderr
    edits = _status_edits(tmp_path)
    assert len(edits) == 1 and "--add-label status:ready" in edits[0]


def test_blocked_marker_flips_status_blocked_label(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "blocked", "-m", "stuck", "blocked/1", "feature/1-work")

    proc, _ = _run(hub, tmp_path)

    assert proc.returncode == 0, proc.stderr
    edits = _status_edits(tmp_path)
    assert len(edits) == 1 and "--add-label status:blocked" in edits[0]


def test_status_label_flip_is_deduped(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")
    seen = tmp_path / "seen"
    label_seen = tmp_path / "label-seen"

    _run(hub, tmp_path, seen_file=seen, env_extra={"HUB_LABEL_SEEN_FILE": str(label_seen)})
    # A second poll with no marker movement must not re-flip.
    (tmp_path / "gh-calls.log").unlink(missing_ok=True)
    proc, _ = _run(
        hub, tmp_path, seen_file=seen, env_extra={"HUB_LABEL_SEEN_FILE": str(label_seen)}
    )

    assert proc.returncode == 0, proc.stderr
    assert _status_edits(tmp_path) == [], "a steady marker must not re-flip the label"


def test_status_label_flips_even_when_ping_afk_suppressed(hub: Path, tmp_path: Path) -> None:
    # Under a live drain the gate PING is withheld, but the label must still flip —
    # the remote issue view must reflect state during a drain.
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    proc, messages = _run(hub, tmp_path, afk=True)

    assert proc.returncode == 0, proc.stderr
    assert messages == [], "the gate ping must be afk-suppressed"
    edits = _status_edits(tmp_path)
    assert len(edits) == 1 and "--add-label status:gate" in edits[0]


def test_status_label_reflips_on_force_moved_marker(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")
    seen = tmp_path / "seen"
    label_seen = tmp_path / "label-seen"
    _run(hub, tmp_path, seen_file=seen, env_extra={"HUB_LABEL_SEEN_FILE": str(label_seen)})

    # Advance the branch and force-move the gate marker → a fresh sha → re-flip.
    (hub / "b.txt").write_text("b\n")
    _git(hub, "checkout", "-q", "feature/1-work")
    _git(hub, "add", "b.txt")
    _git(hub, "commit", "-qm", "feat: b", "-m", "Refs #1")
    _git(hub, "tag", "-f", "-a", "-m", "plan", "gate/1", "feature/1-work")
    _git(hub, "checkout", "-q", "main")
    (tmp_path / "gh-calls.log").unlink(missing_ok=True)

    proc, _ = _run(
        hub, tmp_path, seen_file=seen, env_extra={"HUB_LABEL_SEEN_FILE": str(label_seen)}
    )

    assert proc.returncode == 0, proc.stderr
    assert len(_status_edits(tmp_path)) == 1, "a force-moved marker must re-flip the label"


def test_status_label_mirror_opt_out_makes_no_calls(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "-a", "-m", "plan", "gate/1", "feature/1-work")

    proc, _ = _run(hub, tmp_path, env_extra={"AI_TOOLKIT_GH_LIFECYCLE_LABELS": "0"})

    assert proc.returncode == 0, proc.stderr
    assert _status_edits(tmp_path) == []


def test_status_label_mirror_survives_offline_gh(hub: Path, tmp_path: Path) -> None:
    _git(hub, "tag", "ready/1", "feature/1-work")

    proc, messages = _run(hub, tmp_path, env_extra={"GH_MIRROR_RC": "1"})

    # A failing gh never breaks the watcher — the ping still fires.
    assert proc.returncode == 0, proc.stderr
    assert any("/land 1" in m for m in messages)


# ── issue #241: warned-record pings (the /afk answerer warns instead of blocking) ──
# A converted stop site writes a durable warned-<issue>.txt under the state dir instead of
# parking the spoke blocked. hub-notify surfaces these — and, unlike the once-deduped blocked
# ping, RE-FIRES on an interval so a standing warning stays loud until the human post-adjusts.


def test_warned_record_fires_notification(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "warned-5.txt").write_text("1700000000\ttook the reversible alternative\n")

    _proc, messages = _run(
        hub,
        tmp_path,
        env_extra={
            "AFK_STATE_DIR": str(statedir),
            "HUB_NOTIFY_WARN_SEEN_FILE": str(tmp_path / "warn-seen"),
            "HUB_NOTIFY_NOW": "1700000000",
        },
    )

    assert any("#5 WARNING" in m and "reversible alternative" in m for m in messages), messages


def test_warned_record_refires_after_interval(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "warned-5.txt").write_text("1000\tstanding warning\n")
    warn_seen = tmp_path / "warn-seen"

    def run_at(now: str):
        return _run(
            hub,
            tmp_path,
            env_extra={
                "AFK_STATE_DIR": str(statedir),
                "HUB_NOTIFY_WARN_SEEN_FILE": str(warn_seen),
                "HUB_NOTIFY_WARN_REPEAT": "600",
                "HUB_NOTIFY_NOW": now,
            },
        )

    # The captured notifier log accumulates across runs (shared tmp_path), so a re-fire shows
    # as one MORE "#5 WARNING" line; a suppressed poll adds none.
    _p1, m1 = run_at("1000")
    n1 = sum("#5 WARNING" in m for m in m1)
    _p2, m2 = run_at("1100")  # only 100s later, < 600s repeat window → no re-fire
    n2 = sum("#5 WARNING" in m for m in m2)
    _p3, m3 = run_at("1700")  # 700s after the first fire → re-fires
    n3 = sum("#5 WARNING" in m for m in m3)

    assert n1 == 1, m1
    assert n2 == 1, "within the repeat window a warning must not re-fire"
    assert n3 == 2, "after the repeat window a standing warning re-fires"


# ── issue #336: test-budget breach pings (the duration-budget watcher surfaces a breach) ──
# The watcher (scripts/test-budget-watch.sh) writes one test-budget-breach-<slug>.txt per
# current breach under the AFK state dir; hub-notify fires exactly ONE OS notification per
# NEW breach (own seen-set), mode-aware — suppressed under a live drain (the scoper filing
# is the durable unattended record), fired when attended. Quiet when nothing breaches.


def _budget_env(statedir: Path, seen: Path, **extra: str) -> dict[str, str]:
    return {"AFK_STATE_DIR": str(statedir), "HUB_NOTIFY_BUDGET_SEEN_FILE": str(seen), **extra}


def test_budget_breach_record_fires_notification(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "test-budget-breach-suite.txt").write_text(
        "#336 test-budget breach — suite total 500.00s > 480s budget\n"
    )

    _proc, messages = _run(hub, tmp_path, env_extra=_budget_env(statedir, tmp_path / "bseen"))

    assert any("test-budget breach" in m and "500.00s" in m for m in messages), messages


def test_budget_breach_deduped_on_second_run(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "test-budget-breach-suite.txt").write_text(
        "#336 test-budget breach — suite total 500.00s > 480s budget\n"
    )
    seen = tmp_path / "bseen"

    _p1, m1 = _run(hub, tmp_path, env_extra=_budget_env(statedir, seen))
    _p2, m2 = _run(hub, tmp_path, env_extra=_budget_env(statedir, seen))

    assert sum("test-budget breach" in m for m in m1) == 1
    assert sum("test-budget breach" in m for m in m2) == 1, (
        "a persistent breach record must not re-fire the ping"
    )


def test_no_budget_breach_record_fires_nothing(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()

    _proc, messages = _run(hub, tmp_path, env_extra=_budget_env(statedir, tmp_path / "bseen"))

    assert not any("test-budget breach" in m for m in messages), messages


def test_budget_breach_suppressed_under_live_drain(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "test-budget-breach-suite.txt").write_text(
        "#336 test-budget breach — suite total 500.00s > 480s budget\n"
    )

    _proc, messages = _run(
        hub, tmp_path, afk=True, env_extra=_budget_env(statedir, tmp_path / "bseen")
    )

    assert not any("test-budget breach" in m for m in messages), (
        "a budget breach ping is suppressed under a live drain (attended-only)"
    )


def test_budget_breach_fires_when_attended(hub: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "test-budget-breach-suite.txt").write_text(
        "#336 test-budget breach — suite total 500.00s > 480s budget\n"
    )

    _proc, messages = _run(
        hub, tmp_path, afk=False, env_extra=_budget_env(statedir, tmp_path / "bseen")
    )

    assert any("test-budget breach" in m for m in messages), "attended: the breach must ping"
