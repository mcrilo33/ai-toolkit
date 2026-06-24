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


# --- native-OTel collector preflight (auto-ensure) ----------------------------
# wt_otel_collector_preflight brings the otelcol collector (:4317, the port CC
# exports to and that forks to the bridge) up idempotently for an opted-in spoke.
# It mirrors the bridge preflight, so the decision (start / skip / warn) is unit-
# tested the same way: source the lib and override the two collaborators —
# wt_port_listening (the ":4317 already up?" probe) and wt_collector_launch (the
# actual `docker run` start) — so the branch logic runs with no live socket and
# no real container spawned. The collector must come up BEFORE the bridge (it
# forks to the bridge), so the preflight runs first at spawn.


def _collector_preflight(
    *, gate: bool, auth: bool, port_up: bool
) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_collector_preflight with the two collaborators stubbed.

    wt_port_listening is forced to the desired up/down verdict and
    wt_collector_launch is replaced with a marker echo, so a started collector
    prints ``LAUNCHED <repo>`` and nothing real is spawned. The gate
    (AI_TOOLKIT_OTEL) and LANGFUSE_BASIC_AUTH are set/unset explicitly so a host
    value can never steer the decision.
    """
    parts = [
        f"wt_port_listening() {{ return {0 if port_up else 1}; }}",
        'wt_collector_launch() { echo "LAUNCHED $1"; }',
        "export AI_TOOLKIT_OTEL=1" if gate else "unset AI_TOOLKIT_OTEL",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_collector_preflight /repo",
    ]
    return _call("; ".join(parts))


def test_collector_preflight_launches_when_down_and_authed() -> None:
    # Gate on, collector down, auth present → start exactly one collector.
    result = _collector_preflight(gate=True, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED /repo" in result.stdout


def test_collector_preflight_warns_when_auth_missing() -> None:
    # Gate on, collector down, auth absent → warn (telemetry won't reach Langfuse)
    # but never start a collector and never fail the spawn.
    result = _collector_preflight(gate=True, auth=False, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_collector_preflight_idempotent_when_already_up() -> None:
    # Gate on, auth present, but :4317 already listens → never a second collector.
    result = _collector_preflight(gate=True, auth=True, port_up=True)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_collector_preflight_noop_without_gate() -> None:
    # Gate off → a no-op regardless of auth/port (opt-in is strictly explicit; this
    # is the AI_TOOLKIT_OTEL=0 full opt-out path — no collector started).
    result = _collector_preflight(gate=False, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_collector_launch_publishes_four_ports_and_unquoted_auth(tmp_path: Path) -> None:
    # wt_collector_launch must `docker run` lf-collector with ALL FOUR ports
    # published (4317/4318/4418/8889), mount the repo's otelcol.yaml, and forward
    # LANGFUSE_BASIC_AUTH verbatim — a quoted Authorization 401s while metrics
    # still flow (looks like a pipeline bug but is auth). The non-secret endpoints
    # default to the local stack when the operator leaves them unset. A `docker`
    # stub on PATH records its argv + the auth env it was launched with.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dump = tmp_path / "docker-invocation.txt"
    stub = bindir / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        '{ echo "ARGV=$*"; '
        'echo "LANGFUSE_BASIC_AUTH=[$LANGFUSE_BASIC_AUTH]"; '
        'echo "LANGFUSE_OTLP_ENDPOINT=$LANGFUSE_OTLP_ENDPOINT"; '
        'echo "BRIDGE_OTLP_ENDPOINT=$BRIDGE_OTLP_ENDPOINT"; } > "$STUB_DOCKER_DUMP"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_DOCKER_DUMP": str(dump),
        "TZ": "UTC",
        # Operator value set unquoted; the launch must not add extra quotes.
        "LANGFUSE_BASIC_AUTH": "Basic dG9rOnNlYw==",
    }
    # Drop the endpoints so the launch's defaults are exercised.
    env.pop("LANGFUSE_OTLP_ENDPOINT", None)
    env.pop("BRIDGE_OTLP_ENDPOINT", None)
    subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_collector_launch /repo'],
        env=env,
        check=True,
    )

    recorded = dump.read_text()
    argv = next(line for line in recorded.splitlines() if line.startswith("ARGV="))
    for port in ("4317:4317", "4318:4318", "4418:4418", "8889:8889"):
        assert f"-p {port}" in argv, f"missing published port {port} in: {argv}"
    assert "--name lf-collector" in argv
    assert "/repo/dashboard/langfuse/otelcol.yaml:/etc/otelcol-contrib/config.yaml" in argv
    # Forwarded verbatim — no surrounding quotes baked into the value.
    assert "LANGFUSE_BASIC_AUTH=[Basic dG9rOnNlYw==]" in recorded
    # Non-secret endpoints defaulted to the local stack.
    assert "LANGFUSE_OTLP_ENDPOINT=http://host.docker.internal:3000/api/public/otel" in recorded
    assert "BRIDGE_OTLP_ENDPOINT=http://host.docker.internal:4319" in recorded
