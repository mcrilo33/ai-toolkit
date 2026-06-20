"""Unit tests for the portable date/time helpers in scripts/worktree-lib.sh.

``wt_date_ymd`` / ``wt_epoch_at`` live here so the unattended supervisor
(``hub-afk.sh``) and any future caller share one copy of the portable date/time
helpers. These tests source the lib and call the helpers directly, pinning
``TZ=UTC`` so the conversion is deterministic regardless of the host timezone.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import time
from pathlib import Path

WT_LIB = Path(__file__).resolve().parents[2] / "scripts" / "worktree-lib.sh"


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh under TZ=UTC and invoke a shell expression."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC"},
    )


def _epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """UTC wall-clock to epoch seconds (paired with TZ=UTC in _call)."""
    return int(datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC).timestamp())


def test_wt_date_ymd_formats_epoch_as_local_date() -> None:
    # 2026-06-17 13:45 UTC -> the date component only.
    result = _call(f"wt_date_ymd {_epoch(2026, 6, 17, 13, 45)}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026-06-17"


def test_wt_epoch_at_pins_seconds_to_zero() -> None:
    # The helper must return the :00 second of the given HH:MM, never leak the
    # invocation second (the BSD `date -j -f` footgun the comment warns about).
    expected = _epoch(2026, 6, 17, 7, 0)

    result = _call("wt_epoch_at 2026-06-17 07:00")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_wt_epoch_at_round_trips_with_wt_date_ymd() -> None:
    # Composing the two (the minutes_until idiom) must land on the same midnight.
    now = _epoch(2026, 6, 17, 23, 30)
    expected_midnight = _epoch(2026, 6, 17, 0, 0)

    result = _call(f'wt_epoch_at "$(wt_date_ymd {now})" 00:00')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected_midnight)


# --- native-OTel message-bridge preflight (auto-populate) ---------------------
# wt_otel_bridge_preflight brings the Langfuse message bridge up idempotently for
# an opted-in spoke. The decision (start / skip / warn) is unit-tested here by
# sourcing the lib and overriding the two collaborators — wt_port_listening (the
# ":4319 already up?" probe) and wt_bridge_launch (the actual nohup-python start) —
# so the branch logic is exercised with no live socket and no real server spawned.


def _preflight(*, gate: bool, auth: bool, port_up: bool) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_bridge_preflight with the two collaborators stubbed.

    wt_port_listening is forced to the desired up/down verdict and wt_bridge_launch
    is replaced with a marker echo, so a started bridge prints ``LAUNCHED <repo>``
    and nothing real is spawned. The gate (AI_TOOLKIT_OTEL) and LANGFUSE_BASIC_AUTH
    are set/unset explicitly so a host value can never steer the decision.
    """
    parts = [
        f"wt_port_listening() {{ return {0 if port_up else 1}; }}",
        'wt_bridge_launch() { echo "LAUNCHED $1"; }',
        "export AI_TOOLKIT_OTEL=1" if gate else "unset AI_TOOLKIT_OTEL",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_bridge_preflight /repo",
    ]
    return _call("; ".join(parts))


def test_bridge_preflight_launches_when_down_and_authed() -> None:
    # Gate on, bridge down, auth present → start exactly one bridge.
    result = _preflight(gate=True, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED /repo" in result.stdout


def test_bridge_preflight_warns_when_auth_missing() -> None:
    # Gate on, bridge down, auth absent → warn (audit #93 + LLM I/O won't land) but
    # never start a bridge and never fail the spawn.
    result = _preflight(gate=True, auth=False, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_bridge_preflight_idempotent_when_already_up() -> None:
    # Gate on, auth present, but the bridge is already listening → never a second one.
    result = _preflight(gate=True, auth=True, port_up=True)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_bridge_preflight_noop_without_gate() -> None:
    # Gate off → a no-op regardless of auth/port (opt-in is strictly explicit).
    result = _preflight(gate=False, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_bridge_launch_forwards_required_env_to_child(tmp_path: Path) -> None:
    # wt_bridge_launch must forward the child's REQUIRED config — LANGFUSE_BASIC_AUTH
    # (the bridge KeyErrors without it) and BRIDGE_PORT — even when the operator set
    # them as shell-internal (non-exported) values. A python3 stub on PATH records
    # the env it was launched with; the assignments in the expression are deliberately
    # NOT exported, so a pass proves the re-export inside wt_bridge_launch.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dump = tmp_path / "child-env.txt"
    stub = bindir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        '{ echo "LANGFUSE_BASIC_AUTH=$LANGFUSE_BASIC_AUTH"; '
        'echo "BRIDGE_PORT=$BRIDGE_PORT"; } > "$STUB_ENV_DUMP"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_ENV_DUMP": str(dump),
    }
    # Shell-internal (un-exported) operator values, then launch.
    expr = "LANGFUSE_BASIC_AUTH=tok-xyz; BRIDGE_PORT=4321; wt_bridge_launch /repo"
    subprocess.run(["bash", "-c", f'source "{WT_LIB}"; {expr}'], env=env, check=True)

    # The child is backgrounded via nohup; poll briefly for its env dump.
    deadline = time.monotonic() + 5.0
    while not dump.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert dump.exists(), "bridge child never launched / wrote its env"
    recorded = dump.read_text()
    assert "LANGFUSE_BASIC_AUTH=tok-xyz" in recorded
    assert "BRIDGE_PORT=4321" in recorded
