"""Unit tests for dashboard/run.sh — the dashboard launcher's port selection.

hex admin runs its own Streamlit on the default 8501, so this dashboard must
*not* land there: a collision makes the browser open hex admin instead of the
dashboard (the ``dashboard-port-vs-hex-admin-8501`` lesson). ``run.sh`` therefore
picks the first FREE port in a dedicated range (default 8600-8699), honouring an
explicit ``STREAMLIT_SERVER_PORT`` override and falling back to a random ephemeral
port (with a note) only when the whole range is busy.

Hermetic: ``run.sh`` is invoked with a fake ``streamlit`` on ``PATH`` that just
echoes its argv, so the real Streamlit never launches. The chosen port is read
back out of ``--server.port``. Ports are held busy by binding real sockets in the
test for the duration of the subprocess.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

RUN_SH = Path(__file__).resolve().parents[2] / "dashboard" / "run.sh"


def _free_port() -> int:
    """Return a port that was free a moment ago (small, acceptable TOCTOU race)."""
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _hold(port: int) -> socket.socket:
    """Bind (no SO_REUSEADDR) and listen so ``run.sh`` sees the port as busy."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", port))
    sock.listen(1)
    return sock


def _run(tmp_path: Path, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    streamlit = fake_bin / "streamlit"
    streamlit.write_text('#!/usr/bin/env bash\necho "STREAMLIT_ARGS: $*"\n')
    streamlit.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    # Never inherit a real STREAMLIT_SERVER_PORT from the host.
    env.pop("STREAMLIT_SERVER_PORT", None)
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        ["bash", str(RUN_SH)],
        capture_output=True,
        text=True,
        env=env,
    )


def _chosen_port(result: subprocess.CompletedProcess) -> int:
    match = re.search(r"--server\.port\s+(\d+)", result.stdout)
    assert match, (
        f"no --server.port in streamlit args:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    return int(match.group(1))


def test_picks_a_port_in_the_default_range(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert 8600 <= _chosen_port(result) <= 8699


def test_honours_explicit_streamlit_server_port(tmp_path: Path) -> None:
    # An explicit port wins even when it is outside the dedicated range.
    result = _run(tmp_path, {"STREAMLIT_SERVER_PORT": "8501"})

    assert result.returncode == 0, result.stderr
    assert _chosen_port(result) == 8501


def test_skips_a_busy_port_and_picks_the_next_free_one(tmp_path: Path) -> None:
    base = _free_port()
    held = _hold(base)
    try:
        result = _run(
            tmp_path,
            {
                "AI_TOOLKIT_DASHBOARD_PORT_MIN": str(base),
                "AI_TOOLKIT_DASHBOARD_PORT_MAX": str(base + 50),
            },
        )
    finally:
        held.close()

    assert result.returncode == 0, result.stderr
    chosen = _chosen_port(result)
    assert chosen != base, "must skip the busy first port in the range"
    assert base < chosen <= base + 50, "must still pick within the dedicated range"


def test_respects_a_custom_range(tmp_path: Path) -> None:
    base = _free_port()
    result = _run(
        tmp_path,
        {
            "AI_TOOLKIT_DASHBOARD_PORT_MIN": str(base),
            "AI_TOOLKIT_DASHBOARD_PORT_MAX": str(base + 10),
        },
    )

    assert result.returncode == 0, result.stderr
    assert base <= _chosen_port(result) <= base + 10


def test_falls_back_to_ephemeral_when_range_is_busy(tmp_path: Path) -> None:
    # A one-port range that is fully occupied forces the ephemeral fallback.
    busy = _free_port()
    held = _hold(busy)
    try:
        result = _run(
            tmp_path,
            {
                "AI_TOOLKIT_DASHBOARD_PORT_MIN": str(busy),
                "AI_TOOLKIT_DASHBOARD_PORT_MAX": str(busy),
            },
        )
    finally:
        held.close()

    assert result.returncode == 0, result.stderr
    chosen = _chosen_port(result)
    assert chosen != busy, "fully-busy range must fall back to a different port"
    assert chosen > 0
    assert "range" in result.stderr.lower(), "a note about the busy range should be printed"


def test_prints_the_chosen_url(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    port = _chosen_port(result)
    assert f"http://localhost:{port}" in result.stderr


def test_rejects_non_numeric_range(tmp_path: Path) -> None:
    # A malformed range must fail fast, never exec streamlit with an empty port.
    result = _run(
        tmp_path,
        {"AI_TOOLKIT_DASHBOARD_PORT_MIN": "abc", "AI_TOOLKIT_DASHBOARD_PORT_MAX": "xyz"},
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "--server.port" not in result.stdout, "must not launch streamlit on a bad range"


def test_keeps_localhost_only_and_stats_off(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert "--server.address localhost" in result.stdout
    assert "--browser.gatherUsageStats false" in result.stdout
