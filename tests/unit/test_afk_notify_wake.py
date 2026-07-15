"""Unit tests for shared/hooks/afk-notify-wake.sh (issue #176).

The Notification hook is the event-driven wake's announcer for the UN-scripted parks — a
permission dialog or an AskUserQuestion that Claude Code surfaces as a Notification. On
such an event it resolves the issue from the spoke's branch slug, drops a
``<epoch>-<issue>-park`` file in the event spool, and SIGUSR1s the heartbeat pid — but ONLY
when a live supervisor is running, so an attended session leaves no artifact. Best-effort
throughout: it always exits 0 (a Notification hook can never block a session).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "afk-notify-wake.sh"

# Pin git config to nothing so a host's global/system config never reaches the fixtures.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=_GIT_ENV)


def _spoke(tmp_path: Path, branch: str = "feature/176-afk-event-driven-wake") -> Path:
    repo = tmp_path / "spoke"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", branch)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "seed")
    return repo


def _run(
    repo: Path, env: dict[str, str], payload: str | None = None
) -> subprocess.CompletedProcess:
    if payload is None:
        payload = f'{{"cwd":"{repo}","hook_event_name":"Notification","message":"perm"}}'
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(repo),
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def _wake_env(heartbeat: Path, state_dir: Path) -> dict[str, str]:
    return {**_GIT_ENV, "AFK_HEARTBEAT": str(heartbeat), "AFK_STATE_DIR": str(state_dir)}


def _write_heartbeat(hb: Path, pid: int, *, token: str | None = "wake1") -> None:
    """Stamp a "<pid> <epoch> [wake1]" heartbeat. token=None ⇒ a pre-#176 supervisor with
    no USR1 trap advertises no wake-capability (bare two-field line)."""
    line = f"{pid} 1000000" + (f" {token}" if token else "")
    hb.write_text(line + "\n")


def test_spools_a_park_event_when_supervisor_live(tmp_path: Path) -> None:
    repo = _spoke(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        result = _run(repo, _wake_env(hb, state))
    finally:
        proc.terminate()

    assert result.returncode == 0, result.stdout + result.stderr
    events = list((state / "events").glob("*-176-park"))
    assert len(events) == 1, f"a live supervisor must get one park event, saw {events}"


def test_signals_a_wake_capable_heartbeat_pid(tmp_path: Path) -> None:
    repo = _spoke(tmp_path)
    flag_file = tmp_path / "signalled"
    # `sleep & wait` so USR1 interrupts the wait and the trap fires at once (see the real
    # supervisor's afk_interruptible_sleep).
    proc = subprocess.Popen(
        ["bash", "-c", f'trap "touch {flag_file}; exit 0" USR1; sleep 30 & wait']
    )
    hb = tmp_path / "hb"
    _write_heartbeat(hb, proc.pid)  # three-field: advertises the `wake1` token
    try:
        _run(repo, _wake_env(hb, tmp_path / "afk-state"))
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()

    assert flag_file.exists(), "a wake-capable heartbeat pid must receive SIGUSR1"


def test_no_signal_to_a_wake_incapable_supervisor(tmp_path: Path) -> None:
    # The #207 fix: a two-field heartbeat is a pre-#176 supervisor with no USR1 trap, so the
    # default SIGUSR1 action would TERMINATE it. The hook must still spool the park (the tick
    # backstop services it) but send NO signal. Proven with a trap-LESS process: had a signal
    # arrived, the default action would kill it and `wait` would return before the timeout.
    repo = _spoke(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])  # no USR1 trap: a bare signal kills it
    hb = tmp_path / "hb"
    _write_heartbeat(hb, proc.pid, token=None)  # two-field: no wake-capability token
    state = tmp_path / "afk-state"
    try:
        result = _run(repo, _wake_env(hb, state))
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1)  # still alive ⇒ no signal was delivered
    finally:
        if proc.poll() is None:
            proc.terminate()

    assert result.returncode == 0, result.stdout + result.stderr
    events = list((state / "events").glob("*-176-park"))
    assert len(events) == 1, f"a token-less heartbeat still gets the spool write, saw {events}"


def test_no_op_on_a_hub_checkout(tmp_path: Path) -> None:
    # A hub sits on the default branch (slug `main` -> no issue number): nothing to announce.
    repo = _spoke(tmp_path, branch="main")
    proc = subprocess.Popen(["sleep", "30"])
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        result = _run(repo, _wake_env(hb, state))
    finally:
        proc.terminate()

    assert result.returncode == 0
    assert not (state / "events").exists(), "a non-issue slug must announce nothing"


def test_no_op_without_a_heartbeat(tmp_path: Path) -> None:
    repo = _spoke(tmp_path)
    state = tmp_path / "afk-state"

    result = _run(repo, _wake_env(tmp_path / "hb", state))  # heartbeat never written

    assert result.returncode == 0
    assert not (state / "events").exists(), "no live supervisor => no spool artifact"


def test_no_op_when_heartbeat_pid_is_dead(tmp_path: Path) -> None:
    repo = _spoke(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])
    dead_pid = proc.pid
    proc.terminate()
    proc.wait()
    hb = tmp_path / "hb"
    hb.write_text(f"{dead_pid} 1000000\n")
    state = tmp_path / "afk-state"

    result = _run(repo, _wake_env(hb, state))

    assert result.returncode == 0
    assert not (state / "events").exists(), "a dead heartbeat pid => nothing to wake"


def test_always_exits_zero_outside_a_git_repo(tmp_path: Path) -> None:
    # A Notification hook can never block a session; a non-repo cwd degrades to a silent no-op.
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    result = subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(non_repo),
        input=f'{{"cwd":"{non_repo}"}}',
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# Guard: the elapsed-time assumption in the signal test must not silently regress into a
# multi-second hang if the trap logic breaks.
def test_signal_delivery_is_prompt(tmp_path: Path) -> None:
    repo = _spoke(tmp_path)
    flag_file = tmp_path / "signalled"
    proc = subprocess.Popen(
        ["bash", "-c", f'trap "touch {flag_file}; exit 0" USR1; sleep 30 & wait']
    )
    hb = tmp_path / "hb"
    _write_heartbeat(hb, proc.pid)  # three-field: advertises the `wake1` token
    start = time.monotonic()
    try:
        _run(repo, _wake_env(hb, tmp_path / "afk-state"))
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"the wake signal must arrive promptly, took {elapsed:.1f}s"


def test_shadow_writes_a_park_transition_to_the_lifecycle_log(tmp_path: Path) -> None:
    # #300 migration step 1: the hook records a `parked` transition alongside the
    # wake spool. Purely additive — no detector reads it yet. Proves the spoke-side
    # deploy path (the hook sources the synced transition-log.sh across layouts).
    repo = _spoke(tmp_path)
    proc = subprocess.Popen(["sleep", "30"])
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        result = _run(repo, _wake_env(hb, state))
    finally:
        proc.terminate()

    assert result.returncode == 0, result.stdout + result.stderr
    log = state / "transitions" / "176.jsonl"
    assert log.is_file(), "a park must shadow-write a transition record"
    line = log.read_text().splitlines()[-1]
    assert '"kind":"transition"' in line
    assert '"to":"parked"' in line
    assert '"actor":"afk-notify-wake"' in line


def test_no_transition_written_on_a_hub_checkout(tmp_path: Path) -> None:
    # The hook no-ops before the shadow write on a non-spoke (slug has no issue),
    # so no stray transition file appears.
    repo = _spoke(tmp_path, branch="main")
    state = tmp_path / "afk-state"
    hb = tmp_path / "hb"
    hb.write_text("1 1000000\n")

    _run(repo, _wake_env(hb, state))

    assert not (state / "transitions").exists()
