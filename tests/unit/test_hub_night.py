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
import subprocess
from pathlib import Path

import pytest

HUB_NIGHT = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-night.sh"
)


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
