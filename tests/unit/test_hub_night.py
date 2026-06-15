"""Unit tests for shared/skills/hub/scripts/hub-night.sh.

The adaptive night dispatcher (issue #41, Phase 1 of epic #40) drains a queue of
`night`-labelled issues overnight with a self-tuning concurrency cap. This file
covers the pure decision layer — the concurrency formula, the time-left clock,
and the strict launch cutoff — by sourcing the script (a source-guard keeps the
supervisor loop from running on import) and calling its functions directly.

Worked cases for the clamp formula `clamp(ceil(tasks_left * T_task / time_left),
1, MAX)` with the documented defaults (T_task=90, MAX=3):

    5 tasks  / 480 min -> 1   (queue fits the night -> sequential)
    20 tasks / 480 min -> 3   (ceil 3.75 = 4, clamped to the cap)
    5 tasks  / 150 min -> 3   (night burning down -> ramps up to the cap)
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path

import pytest

HUB_NIGHT = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-night.sh"
)
# The hub reaps a hung/idle/overrun spoke by emitting blocked/<issue> on its
# behalf via the canonical marker emitter (issue #40 ST2). Tests point the
# supervisor at the real script so a reap actually emits the marker.
SPOKE_READY = Path(__file__).resolve().parents[2] / "scripts" / "spoke-ready.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-night.sh and invoke a shell expression against its functions.

    The script's source-guard means sourcing only defines functions; the
    supervisor loop never runs. ``fn_call`` is a shell snippet such as
    ``night_target 20 480``.

    Args:
        fn_call: Shell expression to run after sourcing the script.
        env: Extra environment (knob overrides). ``TZ=UTC`` is forced so the
            wake-time clock is deterministic regardless of the host timezone.

    Returns:
        The CompletedProcess with captured stdout/stderr.
    """
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_NIGHT}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    """UTC wall-clock to epoch seconds (paired with TZ=UTC in _call)."""
    return int(datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC).timestamp())


# --- night_target: the clamp formula -----------------------------------------


@pytest.mark.parametrize(
    "tasks_left,time_left,expected",
    [
        (5, 480, 1),  # queue fits a full night -> sequential
        (1, 480, 1),  # single task -> sequential
        (3, 90, 3),  # ceil(270/90) = 3, exactly at the cap
        (5, 150, 3),  # night burning down -> ramps to the cap
        (20, 480, 3),  # ceil(3.75) = 4, clamped to MAX=3
        (0, 480, 1),  # empty -> clamped up to the floor of 1
    ],
)
def test_night_target_worked_cases(tasks_left: int, time_left: int, expected: int) -> None:
    result = _call(f"night_target {tasks_left} {time_left}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_night_target_honors_task_minutes_knob() -> None:
    # T_task=60: ceil(10*60/120) = 5, clamped to the default MAX of 3.
    result = _call("night_target 10 120", env={"NIGHT_TASK_MINUTES": "60"})

    assert result.stdout.strip() == "3"


def test_night_target_honors_max_concurrency_knob() -> None:
    # Lift the cap to 5: ceil(20*90/480) = 4 is now under the cap.
    result = _call("night_target 20 480", env={"NIGHT_MAX_CONCURRENCY": "5"})

    assert result.stdout.strip() == "4"


def test_night_target_guards_nonpositive_time_left() -> None:
    # time_left <= 0 (the night is over) must not divide-by-zero; it pins to MAX.
    result = _call("night_target 5 0")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3"


# --- minutes_until: the wake-time clock --------------------------------------


def test_minutes_until_before_wake_time() -> None:
    # 23:00 -> next 07:00 is +8h = 480 minutes.
    now = _epoch(2026, 6, 15, 23, 0)

    result = _call(f"minutes_until 07:00 {now}")

    assert result.stdout.strip() == "480"


def test_minutes_until_wraps_to_tomorrow_when_past_wake_time() -> None:
    # 08:00 is past today's 07:00, so the next 07:00 is tomorrow: +23h = 1380.
    now = _epoch(2026, 6, 15, 8, 0)

    result = _call(f"minutes_until 07:00 {now}")

    assert result.stdout.strip() == "1380"


def test_epoch_at_lands_exactly_on_the_minute() -> None:
    # Regression: BSD `date -j -f` fills a missing %S from the current wall
    # clock, so a wake time on the minute would carry leaked seconds and shift
    # minutes_until by a minute depending on when it ran. The wake epoch must be
    # an exact :00 minute boundary — i.e. equal to the known 07:00:00 epoch.
    result = _call("_epoch_at 2026-06-15 07:00")

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == _epoch(2026, 6, 15, 7, 0)


# --- launch_cutoff_reached: the strict launch cutoff -------------------------


def test_launch_cutoff_reached_when_time_left_below_t_task() -> None:
    # Default T_task=90; 89 minutes left -> cutoff reached (no new spokes).
    result = _call("launch_cutoff_reached 89")

    assert result.returncode == 0


def test_launch_cutoff_not_reached_at_or_above_t_task() -> None:
    result = _call("launch_cutoff_reached 90")

    assert result.returncode != 0


# --- Dispatch tick (--once): queue, idempotent skip, cutoff ------------------
# These run the script end-to-end for one tick from a hub checkout, with `gh`
# (the night queue) and worktree-new.sh (WT_NEW, the dispatcher) stubbed so no
# real worktree, tmux window, or network is touched. The WT_NEW stub logs one
# line per call so a test can count dispatches and inspect the args.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def night_hub(tmp_path: Path) -> Path:
    """A hub (main checkout) with a bare origin and no in-flight worktrees.

    Tests add in-flight worktrees with ``_add_inflight`` and drive the queue
    via the ``gh`` stub's ``NIGHT_QUEUE`` env var.
    """
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
    return hub


def _add_inflight(hub: Path, tmp_path: Path, issue: int, slug: str = "wip") -> Path:
    """Create an in-flight worktree+branch for ``issue`` (feature/<issue>-<slug>)."""
    wt = tmp_path / f"wt-{issue}"
    _git(hub, "worktree", "add", "-q", "-b", f"feature/{issue}-{slug}", str(wt))
    return wt


def _run_once(
    hub: Path,
    tmp_path: Path,
    *,
    queue: str,
    now: int,
    once: bool = True,
    projects_dir: Path | None = None,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run ``hub-night.sh`` from the hub with gh + WT_NEW stubbed.

    Args:
        hub: Hub checkout to run from.
        tmp_path: Test tmpdir; the stub bin and dispatch log live under it.
        queue: Space-separated issue numbers the gh stub returns as the queue.
        now: Injected current time (epoch seconds) -> NIGHT_NOW.
        once: Pass ``--once`` (single tick). Set False to exercise the loop;
            ``timeout`` then guards against a hang.
        projects_dir: Claude projects root exported as CLAUDE_PROJECTS_DIR (for
            transcript-idle slot detection). When None, a nonexistent dir is
            used so the host's real ~/.claude/projects can never leak in.
        timeout: Wall-clock cap (seconds) on the subprocess.
        env: Extra knob overrides.

    Returns:
        (completed process, dispatch-log lines). Each log line is
        ``DISPATCH issue=<n> type=<yes|no> prompt=<yes|no>``.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then\n'
        '  for n in $NIGHT_QUEUE; do echo "$n"; done\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    wt_new = bindir / "wt-new-stub"
    log = tmp_path / "dispatch.log"
    wt_new.write_text(
        "#!/bin/sh\n"
        'issue="$1"; has_type=no; has_prompt=no\n'
        'for a in "$@"; do\n'
        '  [ "$a" = "--type" ] && has_type=yes\n'
        '  [ "$a" = "--prompt" ] && has_prompt=yes\n'
        "done\n"
        'printf "DISPATCH issue=%s type=%s prompt=%s\\n" "$issue" "$has_type" "$has_prompt" >> "$WT_NEW_LOG"\n'
    )
    wt_new.chmod(0o755)
    # A logging tmux stub keeps the reap's best-effort `tmux kill-window` hermetic
    # (no real windows touched); list-windows returns nothing so no kill is matched.
    tmux = bindir / "tmux"
    tmux_log = tmp_path / "tmux-calls.log"
    tmux_log.touch()
    tmux.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{tmux_log}"\nexit 0\n')
    tmux.chmod(0o755)
    fallback_projects = tmp_path / "no-claude-projects"
    full_env = {
        **os.environ,
        "TZ": "UTC",
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "NIGHT_QUEUE": queue,
        "NIGHT_NOW": str(now),
        "WT_NEW": str(wt_new),
        "WT_NEW_LOG": str(log),
        "SPOKE_READY": str(SPOKE_READY),
        "NIGHT_STATE_DIR": str(tmp_path / "night-state"),
        "CLAUDE_PROJECTS_DIR": str(projects_dir if projects_dir is not None else fallback_projects),
    }
    if env:
        full_env.update(env)
    argv = ["bash", str(HUB_NIGHT)]
    if once:
        argv.append("--once")
    proc = subprocess.run(
        argv,
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=timeout,
    )
    lines = log.read_text().splitlines() if log.exists() else []
    return proc, lines


def test_dispatch_up_to_target_from_queue(night_hub: Path, tmp_path: Path) -> None:
    # 5 queued, 150 min left -> target ceil(5*90/150)=3; no in-flight -> 3 spawn.
    now = _epoch(2026, 6, 15, 4, 30)  # 07:00 - 150m

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102 103 104 105", now=now)

    assert proc.returncode == 0, proc.stderr
    assert len(lines) == 3
    assert [ln.split()[1] for ln in lines] == ["issue=101", "issue=102", "issue=103"]


def test_inflight_issue_is_not_redispatched(night_hub: Path, tmp_path: Path) -> None:
    # Idempotent restart: issue 101 already has a worktree, so the only queued
    # task is skipped — no dispatch, no error (safe to re-run after a crash).
    _add_inflight(night_hub, tmp_path, 101)
    now = _epoch(2026, 6, 15, 4, 30)

    proc, lines = _run_once(night_hub, tmp_path, queue="101", now=now)

    assert proc.returncode == 0, proc.stderr
    assert lines == []


def test_launch_cutoff_blocks_all_dispatch(night_hub: Path, tmp_path: Path) -> None:
    # 60 min left (< T_task=90) -> strict cutoff: nothing is started.
    now = _epoch(2026, 6, 15, 6, 0)  # 07:00 - 60m

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now)

    assert proc.returncode == 0, proc.stderr
    assert lines == []


def test_dispatch_passes_type_feature_and_prompt(night_hub: Path, tmp_path: Path) -> None:
    now = _epoch(2026, 6, 15, 4, 30)

    _, lines = _run_once(night_hub, tmp_path, queue="101 102 103 104 105", now=now)

    assert lines, "expected at least one dispatch"
    assert all("type=yes" in ln and "prompt=yes" in ln for ln in lines)


# --- The night kickoff: agent-review + park-don't-guess (issue #40 ST4) --------
# The behavioral inversion of the daytime kickoff. Daytime parks at the PLAN gate
# for a human (gate/N); overnight the judgment gates route to an independent
# adversarial reviewer and uncertainty escalates to PARK (blocked/N), never to
# "ask" (which hangs the slot) or "guess". Markers emit via the scripted path.


def _kickoff_flat(issue: int = 7) -> tuple[subprocess.CompletedProcess[str], str]:
    proc = _call(f"kickoff_for {issue}")
    return proc, re.sub(r"\s+", " ", proc.stdout).lower()


def test_night_kickoff_is_park_dont_guess() -> None:
    proc, flat = _kickoff_flat()

    assert proc.returncode == 0, proc.stderr
    assert "unattended" in flat, "the night kickoff must state it runs unattended"
    assert "park" in flat
    assert "never" in flat and ("ask" in flat or "guess" in flat), (
        "uncertainty routes to park, never to ask (hangs) or guess"
    )


def test_night_kickoff_routes_judgment_gates_to_adversarial_review() -> None:
    proc, flat = _kickoff_flat()

    assert proc.returncode == 0, proc.stderr
    assert "adversarial" in flat or "refute" in flat
    assert "code-review" in flat, "the reviewer is the independent code-review agent"
    assert "two round" in flat or "2-round" in flat or "two-round" in flat, (
        "the revise loop is bounded to two rounds, then park"
    )


def test_night_kickoff_emits_terminal_markers_via_script() -> None:
    proc, flat = _kickoff_flat()

    assert proc.returncode == 0, proc.stderr
    for marker in ("ready/", "accept/", "blocked/"):
        assert marker in flat, f"the night kickoff must name the {marker} terminal marker"
    assert "spoke-ready.sh" in flat and "--accept" in flat and "--blocked" in flat, (
        "terminal markers emit via the scripted spoke-ready.sh path"
    )
    assert "git tag -f -a" not in flat, "no hand-written git tag/push chain"


def test_kickoff_is_not_plan_mode() -> None:
    """The kickoff prints the plan as a visible message — never harness plan mode.

    Regression lock (issue #40): the placeholder kickoff said 'present a concrete
    implementation plan in plan mode', which violates the #34 invariant the
    test_gated_spokes suite enforces for the docs — but kickoff_for lives in a .sh
    those wording tests don't scan, so the bug was latent and shipped to every
    night spoke. Lock it here so it can't be reintroduced.
    """
    proc = _call("kickoff_for 7")
    flat = re.sub(r"\s+", " ", proc.stdout).lower()

    assert proc.returncode == 0, proc.stderr
    assert "plan mode" not in flat, "the kickoff must not invoke plan mode"
    assert "exitplanmode" not in flat, "the kickoff must not invoke ExitPlanMode"
    assert "visible message" in flat, "the plan is presented as a normal visible message"
    assert "before green" in flat or "before writing code" in flat, (
        "the PLAN gate must pause before any implementation"
    )


# --- Slot-free detection + backfill (ST3) ------------------------------------
# An in-flight spoke frees its slot when it is done (a ready/<issue> tag at its
# branch tip) or idle (its newest transcript is older than NIGHT_IDLE_MINUTES);
# a busy spoke keeps occupying one. The supervisor backfills freed slots from
# the still-pending queue. With a full night the target is 1, so a backfill can
# only happen if the done/idle spoke stops counting against the cap — which is
# exactly what distinguishes ST3 from the ST2 "every in-flight occupies" rule.


def _seed_ready_tag(hub: Path, issue: int, slug: str = "wip") -> None:
    """Tag the in-flight branch tip ready/<issue> -> slot_state 'done'."""
    _git(hub, "tag", f"ready/{issue}", f"feature/{issue}-{slug}")


def _seed_transcript(projects_dir: Path, wt_path: Path, *, mtime: int) -> Path:
    """Write a newest-wins transcript for a worktree with a given mtime.

    The project dir is slugged from the worktree's realpath (git reports
    realpaths, e.g. /private/var on macOS), matching hub-night's lookup.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path.resolve()))
    project_dir = projects_dir / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / "sess.jsonl"
    transcript.write_text('{"type":"assistant"}\n')
    os.utime(transcript, (mtime, mtime))
    return transcript


def _seed_terminal_tag(hub: Path, kind: str, issue: int, slug: str = "wip") -> None:
    """Tag the in-flight branch tip <kind>/<issue> -> slot_state 'done' (issue #40).

    kind is one of the terminal namespaces ready/accept/blocked; the supervisor
    treats all three as freeing a slot, unlike the non-terminal gate/<issue> park.
    """
    _git(hub, "tag", f"{kind}/{issue}", f"feature/{issue}-{slug}")


@pytest.mark.parametrize("kind", ["accept", "blocked"])
def test_terminal_marker_frees_slot_for_backfill(
    night_hub: Path, tmp_path: Path, kind: str
) -> None:
    # 101 in flight + <kind>/101 at its tip -> terminal -> doesn't occupy a slot,
    # so the pending 102 backfills even though the full-night target is 1. This is
    # the accept/blocked counterpart of test_done_spoke_frees_slot_for_backfill.
    _add_inflight(night_hub, tmp_path, 101)
    _seed_terminal_tag(night_hub, kind, 101)
    now = _epoch(2026, 6, 15, 1, 0)  # ~6h to 07:00 -> target 1

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now)

    assert proc.returncode == 0, proc.stderr
    assert [ln.split()[1] for ln in lines] == ["issue=102"]


def test_gate_marker_does_not_free_slot(night_hub: Path, tmp_path: Path) -> None:
    # gate/101 is the NON-terminal PLAN park: a gate-parked spoke is awaiting
    # review and still owns its slot, so under a full-night target of 1 the
    # pending 102 is NOT backfilled. (Contrast the accept/blocked case above.)
    _add_inflight(night_hub, tmp_path, 101)
    _git(night_hub, "tag", "gate/101", "feature/101-wip")
    now = _epoch(2026, 6, 15, 1, 0)

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now)

    assert proc.returncode == 0, proc.stderr
    assert lines == [], "a gate-parked spoke must keep occupying its slot"


def test_done_spoke_frees_slot_for_backfill(night_hub: Path, tmp_path: Path) -> None:
    # 101 in flight + ready/101 at its tip -> done -> doesn't occupy a slot, so
    # the pending 102 is backfilled even though the full-night target is 1.
    _add_inflight(night_hub, tmp_path, 101)
    _seed_ready_tag(night_hub, 101)
    now = _epoch(2026, 6, 15, 1, 0)  # ~6h to 07:00 -> target 1

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now)

    assert proc.returncode == 0, proc.stderr
    assert [ln.split()[1] for ln in lines] == ["issue=102"]


def test_idle_spoke_is_reaped_then_frees_slot(night_hub: Path, tmp_path: Path) -> None:
    # 101 in flight but its transcript is 20 min stale (> NIGHT_IDLE_MINUTES=15)
    # with NO terminal/gate marker -> hung -> the supervisor REAPS it: emits
    # blocked/101 on its behalf (so the morning report shows a THINK row instead
    # of a silent disappearance) and only then frees the slot, so pending 102
    # backfills under a target of 1. (Issue #40 ST2: idle is a teardown, not a
    # bare accounting flip — a still-alive idle process must not be backfilled over.)
    wt = _add_inflight(night_hub, tmp_path, 101)
    projects = tmp_path / "projects"
    now = _epoch(2026, 6, 15, 1, 0)
    _seed_transcript(projects, wt, mtime=now - 20 * 60)

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now, projects_dir=projects)

    assert proc.returncode == 0, proc.stderr
    assert _git(night_hub, "tag", "-l", "blocked/101").strip() == "blocked/101", (
        "a hung idle spoke must be reaped with a blocked/N marker"
    )
    assert [ln.split()[1] for ln in lines] == ["issue=102"]


def test_busy_spoke_occupies_slot_and_blocks_backfill(night_hub: Path, tmp_path: Path) -> None:
    # 101 in flight, transcript active 1 min ago -> busy -> occupies the only
    # slot (full-night target 1), so pending 102 is NOT backfilled this tick.
    wt = _add_inflight(night_hub, tmp_path, 101)
    projects = tmp_path / "projects"
    now = _epoch(2026, 6, 15, 1, 0)
    _seed_transcript(projects, wt, mtime=now - 60)

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now, projects_dir=projects)

    assert proc.returncode == 0, proc.stderr
    assert lines == []


# --- Supervisor runtime ceiling + reap (issue #40 ST2) -----------------------
# hub-night.sh had NO per-spoke runtime kill: launch_cutoff only gates NEW
# launches and idle only changed slot ACCOUNTING, so a doom-looping or hung spoke
# ran unbounded on Opus until 07:00 and an idle-but-alive spoke got backfilled
# beside (breaching the cap). ST2 adds a real ceiling: a persisted per-spoke
# dispatch epoch + a wall-clock kill, and turns idle into an actual reap (kill the
# tmux window + emit blocked/N from the hub). gate-parked spokes are EXEMPT — they
# stopped on purpose awaiting review and are not hung.


def _epoch_file(tmp_path: Path, issue: int) -> Path:
    """The persisted dispatch-epoch file the supervisor stamps (NIGHT_STATE_DIR)."""
    return tmp_path / "night-state" / f"dispatch-{issue}.epoch"


def _seed_dispatch_epoch(tmp_path: Path, issue: int, epoch: int) -> None:
    """Pre-seed a spoke's dispatch epoch so a test can age it past the ceiling."""
    f = _epoch_file(tmp_path, issue)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{epoch}\n")


@pytest.mark.parametrize(
    "age_min,over",
    [(179, False), (180, False), (181, True), (0, False)],
)
def test_spoke_over_ceiling(age_min: int, over: bool) -> None:
    # spoke_over_ceiling <dispatch_epoch> <now> -> true when the spoke has run
    # longer than NIGHT_SPOKE_MAX_MINUTES (default 180 = 2*T_task). Strict ">".
    now = _epoch(2026, 6, 15, 4, 0)
    epoch = now - age_min * 60

    result = _call(f"spoke_over_ceiling {epoch} {now}")

    assert (result.returncode == 0) == over, result.stderr


def test_dispatch_stamps_a_persisted_epoch(night_hub: Path, tmp_path: Path) -> None:
    # A dispatched spoke gets its start time persisted (survives a supervisor
    # restart) so the wall-clock ceiling is measured from the real launch.
    now = _epoch(2026, 6, 15, 4, 30)

    proc, lines = _run_once(night_hub, tmp_path, queue="101", now=now)

    assert proc.returncode == 0, proc.stderr
    assert lines == ["DISPATCH issue=101 type=yes prompt=yes"]
    epoch_file = _epoch_file(tmp_path, 101)
    assert epoch_file.is_file(), "dispatch must stamp a persisted epoch"
    assert epoch_file.read_text().strip() == str(now), "the stamped epoch is the dispatch time"


def test_over_ceiling_spoke_is_reaped(night_hub: Path, tmp_path: Path) -> None:
    # 101 in flight, active transcript (not idle) but dispatched 200 min ago
    # (> ceiling 180) -> the supervisor reaps it: blocked/101 emitted on its
    # behalf, then the slot frees so pending 102 backfills under a target of 1.
    wt = _add_inflight(night_hub, tmp_path, 101)
    projects = tmp_path / "projects"
    now = _epoch(2026, 6, 15, 1, 0)
    _seed_transcript(projects, wt, mtime=now - 60)  # active, NOT idle
    _seed_dispatch_epoch(tmp_path, 101, now - 200 * 60)  # ran 200 min -> over ceiling

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now, projects_dir=projects)

    assert proc.returncode == 0, proc.stderr
    assert _git(night_hub, "tag", "-l", "blocked/101").strip() == "blocked/101", (
        "an over-ceiling spoke must be reaped with a blocked/N marker"
    )
    assert [ln.split()[1] for ln in lines] == ["issue=102"]


def test_gate_parked_idle_spoke_is_not_reaped(night_hub: Path, tmp_path: Path) -> None:
    # A PLAN-gate-parked spoke (gate/101 at tip) goes transcript-idle while it
    # waits for the human reply — but it stopped ON PURPOSE, it is not hung. The
    # supervisor must NOT reap it (no blocked/101) and it keeps its slot, so 102
    # is not backfilled. This is the parked-vs-hung distinction ST2 introduces.
    wt = _add_inflight(night_hub, tmp_path, 101)
    _git(night_hub, "tag", "gate/101", "feature/101-wip")
    projects = tmp_path / "projects"
    now = _epoch(2026, 6, 15, 1, 0)
    _seed_transcript(projects, wt, mtime=now - 20 * 60)  # idle, but gate-parked

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now, projects_dir=projects)

    assert proc.returncode == 0, proc.stderr
    assert not _git(night_hub, "tag", "-l", "blocked/101").strip(), (
        "a gate-parked spoke must not be reaped"
    )
    assert lines == [], "a gate-parked spoke keeps its slot"


# --- Supervisor loop termination (ST3) ---------------------------------------
# Without --once the script loops, but it must terminate (not hang) when the
# night is over or there is nothing left to supervise. timeout in _run_once is
# the safety net that turns a hang into a test failure.


def test_loop_exits_when_queue_empty_and_nothing_in_flight(night_hub: Path, tmp_path: Path) -> None:
    now = _epoch(2026, 6, 15, 1, 0)

    proc, lines = _run_once(night_hub, tmp_path, queue="", now=now, once=False)

    assert proc.returncode == 0, proc.stderr
    assert lines == []


def test_loop_exits_at_launch_cutoff(night_hub: Path, tmp_path: Path) -> None:
    # Past the cutoff there is nothing left to launch -> the loop ends rather
    # than spinning forever waiting for a window that will not open tonight.
    now = _epoch(2026, 6, 15, 6, 30)  # 30 min to 07:00 (< T_task)

    proc, lines = _run_once(night_hub, tmp_path, queue="101 102", now=now, once=False)

    assert proc.returncode == 0, proc.stderr
    assert lines == []
