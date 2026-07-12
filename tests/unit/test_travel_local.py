"""Unit tests for scripts/travel-local.sh (issue #248).

``travel-local on|off|status`` keeps an /afk drain alive on THIS Mac while it is
carried lid-closed in a bag on the iPhone hotspot — no second machine. The OS-level
pieces it drives (Wi-Fi join, ``pmset -a disablesleep``, ``caffeinate -s``) are all
stubbed on PATH so no real system state is touched:

* ``uname`` — flips the macOS guard (``STUB_UNAME``).
* ``networksetup`` — hotspot join / SSID read / Wi-Fi device (``STUB_JOIN_RC``,
  ``STUB_SSID``).
* ``pmset`` (via a ``sudo`` shim that execs its args) — the disablesleep switch and
  the ``-g`` state read (``STUB_DISABLESLEEP``).
* ``caffeinate`` — the belt-and-braces awake-holder; the default stub stays alive
  (``sleep``), ``STUB_CAFFEINATE=die`` exits at once to model a failed launch.
* ``curl`` — the api.anthropic.com reachability probe (``STUB_CURL_RC``).

The ``off`` epoch refresh is checked against a fake gate-broker (``AFK_GATE_BROKER``)
that exposes the real function names — travel-local must stamp BOTH the progress and
the answer-attempt epoch per in-flight issue, mirroring hub-afk's ``resume_spoke``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class TravelEnv:
    """Hermetic harness handed to each test: stub paths + a ``run`` helper."""

    bindir: Path
    log: Path
    conf: Path
    pidfile: Path
    state: Path
    base_env: dict[str, str]
    run: Callable[..., subprocess.CompletedProcess[str]]


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "travel-local.sh"


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[TravelEnv]:
    """A hermetic PATH of logging stubs + a fake gate-broker and hub-afk.

    Returns a :class:`TravelEnv`: ``bindir``, ``log`` (the call-log path), ``conf``
    (the ~/.afk-travel path), ``pidfile``, ``state`` (AFK_STATE_DIR), ``base_env``
    and a ``run(*args, **overrides)`` helper. Any caffeinate the run leaves alive is
    reaped in teardown via the pidfile.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()

    _write_stub(bindir / "uname", 'echo "${STUB_UNAME:-Darwin}"\n')
    _write_stub(
        bindir / "networksetup",
        f'echo "networksetup $*" >> "{log}"\n'
        'case "$1" in\n'
        '  -listallhardwareports) printf "Hardware Port: Wi-Fi\\nDevice: en0\\n" ;;\n'
        '  -getairportnetwork) echo "Current Wi-Fi Network: ${STUB_SSID:-TestNet}" ;;\n'
        '  -setairportnetwork) exit "${STUB_JOIN_RC:-0}" ;;\n'
        "esac\n"
        "exit 0\n",
    )
    _write_stub(
        bindir / "pmset",
        f'echo "pmset $*" >> "{log}"\n'
        'if [ "$1" = "-g" ]; then\n'
        '  printf " SleepDisabled\\t\\t${STUB_DISABLESLEEP:-0}\\n"\n'
        "fi\n"
        "exit 0\n",
    )
    # sudo shim: log the sudo call, then exec the (stubbed) command it wraps.
    _write_stub(
        bindir / "sudo",
        f'echo "sudo $*" >> "{log}"\n'
        'while [ "$1" = "-n" ] || [ "$1" = "-A" ]; do shift; done\n'
        'exec "$@"\n',
    )
    _write_stub(
        bindir / "caffeinate",
        f'echo "caffeinate $*" >> "{log}"\nexec sleep 300\n',
    )
    _write_stub(bindir / "curl", f'echo "curl $*" >> "{log}"\nexit "${{STUB_CURL_RC:-0}}"\n')

    # A fake gate-broker exposing the real function names travel-local sources.
    state = tmp_path / "afkstate"
    broker = tmp_path / "gate-broker.sh"
    broker.write_text(
        "inflight_issues() { printf '%s\\n' ${STUB_INFLIGHT:-}; }\n"
        "afk_now() { echo 1700000000; }\n"
        'stamp_progress_epoch() { mkdir -p "$AFK_STATE_DIR"; '
        'echo 1700000000 > "$AFK_STATE_DIR/progress-$1.epoch"; }\n'
        'stamp_answer_attempt() { mkdir -p "$AFK_STATE_DIR"; '
        'echo 1700000000 > "$AFK_STATE_DIR/answer-attempt-$1.epoch"; }\n'
    )

    hubafk = tmp_path / "hub-afk.sh"
    _write_stub(hubafk, 'echo "AFK STATUS: draining-idle"\n')

    conf = tmp_path / "afk-travel.conf"
    conf.write_text("TRAVEL_HOTSPOT_SSID='Mathieu iPhone'\n")

    pidfile = tmp_path / "caffeinate.pid"

    base_env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AFK_TRAVEL_CONF": str(conf),
        "AFK_TRAVEL_WIFI_DEV": "en0",
        "AFK_TRAVEL_PIDFILE": str(pidfile),
        "AFK_TRAVEL_JOIN_DELAY": "0",
        "AFK_TRAVEL_JOIN_RETRIES": "3",
        "AFK_GATE_BROKER": str(broker),
        "AFK_HUB_AFK": str(hubafk),
        "AFK_STATE_DIR": str(state),
    }

    def run(*args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        run_env = {**base_env, **overrides}
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=run_env
        )

    yield TravelEnv(
        bindir=bindir,
        log=log,
        conf=conf,
        pidfile=pidfile,
        state=state,
        base_env=base_env,
        run=run,
    )

    # Reap any caffeinate the run left alive.
    if pidfile.exists():
        with contextlib.suppress(ValueError, ProcessLookupError, PermissionError):
            os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)


def _calls(env) -> str:
    return env.log.read_text()


# --- guards -------------------------------------------------------------------


def test_non_macos_fails_fast(env) -> None:
    proc = env.run("status", STUB_UNAME="Linux")

    assert proc.returncode != 0
    assert "macOS" in (proc.stderr + proc.stdout)


def test_on_refuses_without_config(env) -> None:
    missing = env.conf.parent / "does-not-exist"

    proc = env.run("on", AFK_TRAVEL_CONF=str(missing))

    assert proc.returncode != 0
    assert ".afk-travel" in proc.stderr
    assert "TRAVEL_HOTSPOT_SSID" in proc.stderr
    # A refusal must not touch power state.
    assert "pmset -a disablesleep" not in _calls(env)


def test_unknown_verb_is_usage_error(env) -> None:
    proc = env.run("frobnicate")

    assert proc.returncode != 0
    assert "usage" in (proc.stderr + proc.stdout).lower()


# --- on -----------------------------------------------------------------------


def test_on_happy_path_joins_verifies_disablesleep_and_caffeinates(env) -> None:
    proc = env.run("on")

    assert proc.returncode == 0, proc.stderr
    calls = _calls(env)
    assert "networksetup -setairportnetwork en0 Mathieu iPhone" in calls
    assert "curl" in calls
    assert "pmset -a disablesleep 1" in calls
    # The pidfile (written synchronously by the launcher) is the deterministic proof the
    # caffeinate daemon was started — the daemon's own call-log line is async.
    assert env.pidfile.read_text().strip(), "caffeinate pid not recorded"
    assert "lid-close safe" in proc.stdout.lower()


def test_on_fails_when_hotspot_never_appears(env) -> None:
    proc = env.run("on", STUB_JOIN_RC="1")

    assert proc.returncode != 0
    assert "Personal Hotspot" in proc.stderr
    # Join is step 1 — a join failure never reaches the power switch.
    assert "pmset -a disablesleep 1" not in _calls(env)


def test_on_rolls_back_disablesleep_when_caffeinate_unavailable(env) -> None:
    proc = env.run("on", AFK_TRAVEL_CAFFEINATE="caffeinate-absent")

    assert proc.returncode != 0
    calls = _calls(env)
    # It flipped disablesleep on, then rolled it back before exiting.
    assert "pmset -a disablesleep 1" in calls
    assert "pmset -a disablesleep 0" in calls


def test_on_is_idempotent_reusing_a_live_caffeinate(env) -> None:
    first = env.run("on")
    assert first.returncode == 0, first.stderr
    pid_after_first = env.pidfile.read_text()

    second = env.run("on")

    assert second.returncode == 0, second.stderr
    # The second run saw the live pidfile and reused it — the pid is unchanged, proving no
    # second caffeinate was launched.
    assert env.pidfile.read_text() == pid_after_first


# --- off ----------------------------------------------------------------------


def test_off_restores_disablesleep_and_kills_caffeinate(env) -> None:
    env.run("on")
    assert env.pidfile.exists()

    proc = env.run("off")

    assert proc.returncode == 0, proc.stderr
    assert "pmset -a disablesleep 0" in _calls(env)
    assert not env.pidfile.exists()


def test_off_restores_home_ssid_when_configured(env) -> None:
    env.conf.write_text("TRAVEL_HOTSPOT_SSID='Mathieu iPhone'\nTRAVEL_HOME_SSID='HomeNet'\n")
    env.run("on")

    env.run("off")

    assert "networksetup -setairportnetwork en0 HomeNet" in _calls(env)


def test_off_stamps_both_progress_and_answer_attempt_epochs(env) -> None:
    env.run("on")

    proc = env.run("off", STUB_INFLIGHT="41 42")

    assert proc.returncode == 0, proc.stderr
    for issue in ("41", "42"):
        assert (env.state / f"progress-{issue}.epoch").exists(), f"progress-{issue} not stamped"
        assert (env.state / f"answer-attempt-{issue}.epoch").exists(), (
            f"answer-attempt-{issue} not stamped"
        )


# --- status -------------------------------------------------------------------


def test_status_reports_four_surfaces_plus_afk(env) -> None:
    proc = env.run("status", STUB_DISABLESLEEP="1", STUB_SSID="Mathieu iPhone")

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.lower()
    assert "disablesleep" in out
    assert "caffeinate" in out
    assert "ssid" in out
    assert "connectivity" in out
    assert "draining-idle" in proc.stdout  # the /afk status surface
