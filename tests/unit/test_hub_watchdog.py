"""Unit tests for shared/skills/hub/scripts/hub-watchdog.sh.

hub-watchdog.sh is the tier-2 OS-level supervision daemon (issue #251): a self-looping,
pidfile-singleton, source-recycling process with its own heartbeat that cross-checks the
tier-1 /afk drain and (in later subtasks) turns each intervention into a defect signal. This
subtask is the daemon SKELETON — these tests pin the loop/pidfile/heartbeat/self-recycle
scaffold and the drain-state cross-check, modelled on test_hub_otel_watch.py's daemon suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# hub-watchdog.sh cross-checks the macOS afk hub (kill -0, BSD tooling) like its siblings.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="hub-watchdog.sh targets the macOS afk hub"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_WATCHDOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-watchdog.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-watchdog.sh and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_WATCHDOG}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke hub-watchdog.sh DIRECTLY (the CLI dispatch, BASH_SOURCE == $0)."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(HUB_WATCHDOG), *args], capture_output=True, text=True, env=full_env
    )


def _drain_pattern_stub(tmp_path: Path, pattern: str) -> str:
    """A _wd_drain_state stub scripted per tick: 'L'=live, 'S'=stale, anything else=off.

    Ticks beyond the pattern read as off, so every loop terminates. The tick count persists
    in a file the test can assert on.
    """
    ticks = tmp_path / "ticks"
    return (
        f'TICKS="{ticks}"; PATTERN="{pattern}"; '
        "_wd_drain_state() { "
        'n=$(( $(cat "$TICKS" 2>/dev/null || echo 0) + 1 )); printf "%s" "$n" > "$TICKS"; '
        'c="${PATTERN:$((n-1)):1}"; '
        'case "$c" in L) echo live ;; S) echo stale ;; *) echo off ;; esac; }'
    )


_LOOP_ENV = "; ".join(["export HUB_WATCHDOG_INTERVAL=0", "export HUB_WATCHDOG_IDLE_TICKS=3"])


# ── the drain-state cross-check ────────────────────────────────────────────────


def test_drain_state_off_when_no_state_file(tmp_path: Path) -> None:
    result = _call(
        "_wd_drain_state",
        env={"AFK_STATE": str(tmp_path / "absent"), "AFK_HEARTBEAT": str(tmp_path / "absent")},
    )

    assert result.stdout.strip() == "off", result.stderr


def test_drain_state_live_when_heartbeat_pid_alive(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")  # this test process is alive

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "live", result.stderr


def test_drain_state_stale_when_armed_but_pid_dead(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    # A pid that is (almost certainly) not a live process — a very high number.
    hb.write_text("999999 1783879781\n")

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "stale", result.stderr


# ── the daemon loop ────────────────────────────────────────────────────────────


def test_loop_ticks_and_writes_heartbeat_on_each_live_tick(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_HEARTBEAT="{hb}"',
        "_wd_loop",  # empty baseline ⇒ no self-recycle
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    # Two live ticks then off to exhaustion: _wd_tick logged the drain state twice.
    assert result.stdout.count("drain supervisor is live") == 2
    assert hb.read_text().split()[0].isdigit(), "heartbeat stamped <pid> <epoch>"


def test_loop_exits_after_idle_ticks_when_drain_off(tmp_path: Path) -> None:
    parts = [_drain_pattern_stub(tmp_path, ""), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3", "torn down after exactly the idle grace"
    assert "drain supervisor is" not in result.stdout, "no tick ran while the drain was off"


def test_loop_live_tick_resets_idle_counter(tmp_path: Path) -> None:
    # live, off, off, live, then off to exhaustion → the mid-pattern live tick resets the idle
    # counter, so the loop survives to tick 7 and ticked twice. Would exit at tick 4 otherwise.
    parts = [_drain_pattern_stub(tmp_path, "LOOL"), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "7"
    assert result.stdout.count("drain supervisor is live") == 2


def test_loop_reexecs_when_source_changed(tmp_path: Path) -> None:
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        "_wd_source_hash() { echo CHANGED; }",
        "_wd_reexec() { echo REEXEC; exit 0; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" in result.stdout


def test_loop_no_reexec_on_empty_hash(tmp_path: Path) -> None:
    # A transient hasher failure returns '' — must NOT be read as a change (that would
    # spuriously re-exec and re-stamp an empty baseline).
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        "_wd_source_hash() { echo ''; }",
        "_wd_reexec() { echo REEXEC; exit 1; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" not in result.stdout


# ── the daemon singleton ───────────────────────────────────────────────────────


def test_daemon_refuses_second_start_while_pid_alive(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'printf "%s" "$$" > "{pidfile}"',  # a LIVE pid (our own shell)
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr
    assert not (tmp_path / "ticks").exists(), "the loop never ticked"
    assert pidfile.exists(), "the other daemon's pidfile is left intact"


def test_daemon_reclaims_stale_pidfile_and_removes_own_on_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, ""),  # all-off → 3 ticks then teardown
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'sleep 0.01 & _dead=$!; wait "$_dead"; printf "%s" "$_dead" > "{pidfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3"
    assert not pidfile.exists(), "the daemon removes its own pidfile on exit"


def test_daemon_appends_loop_output_to_logfile(tmp_path: Path) -> None:
    logfile = tmp_path / "wd.log"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{tmp_path / "wd.pid"}"',
        f'export HUB_WATCHDOG_LOG="{logfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "drain supervisor is live" in logfile.read_text()
    assert "drain supervisor is live" not in result.stdout, (
        "loop output goes to the log, not stdout"
    )


# ── CLI dispatch ───────────────────────────────────────────────────────────────


def test_cli_status_reports_drain_and_watchdog_state(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")

    result = _run(
        "--status",
        env={
            "AFK_STATE": str(state),
            "AFK_HEARTBEAT": str(hb),
            "HUB_WATCHDOG_PIDFILE": str(tmp_path / "absent.pid"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "drain=live" in result.stdout
    assert "watchdog=off" in result.stdout


def test_cli_daemon_dispatch_reports_already_running(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    pidfile.write_text(str(os.getpid()))

    result = _run(
        "--daemon",
        env={"HUB_WATCHDOG_PIDFILE": str(pidfile), "HUB_WATCHDOG_LOG": str(tmp_path / "wd.log")},
    )

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr


def test_cli_once_stamps_heartbeat_and_ticks(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    result = _run(
        "--once",
        env={"AFK_STATE": str(tmp_path / "absent"), "HUB_WATCHDOG_HEARTBEAT": str(hb)},
    )

    assert result.returncode == 0, result.stderr
    assert "tick: drain supervisor is off" in result.stdout
    fields = hb.read_text().split()
    assert len(fields) == 2 and fields[0].isdigit(), f"heartbeat is '<pid> <epoch>': {fields}"
