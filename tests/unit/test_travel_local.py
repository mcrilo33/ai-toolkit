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
import time
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
    # Default stub stays alive (sleep); STUB_CAFFEINATE=die models a daemon that launches
    # but exits at once, so the post-launch liveness re-check must catch it.
    _write_stub(
        bindir / "caffeinate",
        f'echo "caffeinate $*" >> "{log}"\n'
        'if [ "${STUB_CAFFEINATE:-alive}" = "die" ]; then exit 1; fi\n'
        "exec sleep 300\n",
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


def _wait_for_log(env, needle: str, timeout: float = 5.0) -> str:
    """Poll the call-log until ``needle`` appears or ``timeout`` elapses; return the text.

    A backgrounded stub (e.g. ``caffeinate``) echoes its call line before ``exec``-ing
    ``sleep``, so that write races the parent script's return. Polling — rather than a
    single read gated on a fixed settle — observes the detached write deterministically.
    """
    deadline = time.monotonic() + timeout
    while True:
        text = env.log.read_text()
        if needle in text or time.monotonic() >= deadline:
            return text
        time.sleep(0.02)


def _source_functions(tmp_path: Path) -> Path:
    """A copy of travel-local.sh with its ``main "$@"`` dispatch line dropped, so a test
    can ``source`` just the function definitions without invoking the CLI (which would
    ``exit`` the sourcing shell before the test gets to call anything). Filters by content
    rather than assuming the dispatch is strictly the last line, so an unrelated edit after
    it (a trailing blank line, a shellcheck directive) doesn't break this helper."""
    lines = SCRIPT.read_text().splitlines(keepends=True)
    kept = [line for line in lines if line.strip() != 'main "$@"']
    assert len(kept) == len(lines) - 1, 'expected exactly one main "$@" dispatch line'
    functions = tmp_path / "travel-local-functions.sh"
    functions.write_text("".join(kept))
    return functions


def _run_pid_not_zombie(functions: Path, pid: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{functions}"; _pid_not_zombie "{pid}"'],
        capture_output=True,
        text=True,
    )


def _fork_and_exit() -> int:
    """Fork a child that exits(1) immediately; return its pid to the parent."""
    child_pid = os.fork()
    if child_pid == 0:
        os._exit(1)
    return child_pid


def _wait_for_zombie(pid: int, timeout: float = 10.0) -> str:
    """Poll `ps` until <pid> shows a zombie state; return the final state string."""
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline and "Z" not in state:
        state = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
        if "Z" not in state:
            time.sleep(0.02)
    return state


# --- _pid_not_zombie (unit) -------------------------------------------------------


@pytest.fixture()
def zombie_pid() -> Iterator[int]:
    """A real zombie pid: fork a child that exits immediately, confirm via `ps` state `Z`
    that it has actually zombified, and leave it unreaped (nothing but this fixture's own
    teardown can reap it, so it stays a zombie indefinitely -- no race against a deadline).
    """
    child_pid = _fork_and_exit()
    try:
        state = _wait_for_zombie(child_pid)
        assert "Z" in state, f"expected a zombie, got ps state={state!r}"
        yield child_pid
    finally:
        os.waitpid(child_pid, 0)


@pytest.fixture()
def running_pid() -> Iterator[int]:
    """A genuinely running process's pid."""
    proc = subprocess.Popen(["sleep", "5"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture()
def gone_pid() -> int:
    """A pid that has already been fully reaped -- names no process at all.

    Theoretically the OS could reassign this exact pid to an unrelated new process in the
    gap before the test's check runs, reintroducing a pid-reuse ambiguity. Accepted here for
    the same reason #367 accepts it for production: the window alone, not pid reuse, is what
    made the real bug reproducible -- reuse of one specific freed pid within milliseconds
    needs its own, separately improbable coincidence on top of the window.
    """
    child_pid = _fork_and_exit()
    os.waitpid(child_pid, 0)
    return child_pid


def test_pid_not_zombie_treats_zombie_as_dead(zombie_pid: int, tmp_path: Path) -> None:
    """Regression for #367: a zombie (exited, not yet reaped) must read as dead.

    `kill -0` alone cannot tell a zombie from a genuinely running process -- both answer
    0 -- which is exactly the ambiguity that made the caffeinate-dies rollback test flaky
    under load (a reaped-pending zombie misread as "alive"). `_pid_not_zombie` must reject
    it specifically (rc 1, not just "nonzero") -- independent of any timing or host-load
    luck, since `zombie_pid` already confirmed the zombie state before yielding.
    """
    functions = _source_functions(tmp_path)
    proc = _run_pid_not_zombie(functions, zombie_pid)
    assert proc.returncode == 1, (
        f"a zombie pid must read as not-alive (rc 1), got rc={proc.returncode}: {proc.stderr}"
    )


def test_pid_not_zombie_accepts_a_genuinely_running_process(
    running_pid: int, tmp_path: Path
) -> None:
    functions = _source_functions(tmp_path)
    result = _run_pid_not_zombie(functions, running_pid)
    assert result.returncode == 0, result.stderr


def test_pid_not_zombie_rejects_an_already_gone_pid(gone_pid: int, tmp_path: Path) -> None:
    functions = _source_functions(tmp_path)
    proc = _run_pid_not_zombie(functions, gone_pid)
    assert proc.returncode == 1, f"a gone pid must read as not-alive (rc 1), got {proc.returncode}"


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
    # caffeinate launches detached, so start_caffeinate's post-launch settle proves only
    # liveness (via kill -0), NOT that the forked stub wrote its call-log line. Poll for the
    # line so the caffeinate -s assertion is deterministic and does not depend on the settle.
    calls = _wait_for_log(env, "caffeinate -s")
    assert "caffeinate -s" in calls
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


def test_on_rolls_back_disablesleep_when_caffeinate_dies_after_launch(env) -> None:
    # The daemon launches but exits immediately. start_caffeinate's post-settle verdict
    # goes through an injectable probe (default: the zombie-aware `_pid_not_zombie`,
    # #367) rather than a bare `kill -0`. Racing the real stub's exec-to-exit latency
    # against a fixed settle is exactly what made this flaky under `-n auto` load (#368):
    # this host's endpoint-security stack (two AV/EDR agents hooking macOS's Endpoint
    # Security framework, which synchronously authorizes every exec()) can push a trivial
    # stub's real exit past any settle window under concurrent load -- observed directly
    # against this gate's own `-n auto` contention as 20/20 failures even at moderate
    # host load (an idle, uncontended host does not reproduce the delay in isolation; the
    # contention itself is the cause). No fixed settle is both reliable under adversarial
    # load and fast for every real `on` invocation (see #367's own instruction not to
    # "fix" it by enlarging the settle).
    #
    # The zombie-vs-running distinction itself is already covered, independent of timing,
    # by the `_pid_not_zombie` unit tests above (real forked zombie/running/gone pids).
    # This test's job is the rollback plumbing in `start_caffeinate`/`cmd_on` given "the
    # probe says dead" -- so it injects that verdict via AFK_TRAVEL_LIVENESS_PROBE instead
    # of racing a real process for it. STUB_CAFFEINATE="die" is kept so the real forked
    # stub still actually exits on its own -- no orphaned background process left for the
    # `env` fixture's pidfile-based reaper to miss (the probe override, not the real
    # process's fate, decides this test's outcome). The probe logs its own invocation so
    # the test can pin that it was actually consulted (revert the production seam back to
    # a bare `_pid_not_zombie "$pid"` call and this fails even on an idle host, instead of
    # silently re-admitting the old race).
    dead_probe = env.bindir / "fake-dead-probe"
    _write_stub(dead_probe, f'echo "probe $*" >> "{env.log}"\nexit 1\n')

    proc = env.run(
        "on",
        STUB_CAFFEINATE="die",
        AFK_TRAVEL_SETTLE="0",
        AFK_TRAVEL_LIVENESS_PROBE=str(dead_probe),
    )

    assert proc.returncode != 0
    calls = _calls(env)
    assert "probe " in calls, "AFK_TRAVEL_LIVENESS_PROBE was not consulted"
    assert "pmset -a disablesleep 1" in calls
    assert "pmset -a disablesleep 0" in calls
    # A launch that died leaves no live pidfile behind.
    assert not env.pidfile.exists()


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
