"""Unit tests for scripts/afk-travel.sh — the travel-mode clamshell/Wi-Fi toggle.

The script shells out to `pmset`, `networksetup`, and `security`, and prefixes the
`pmset` write with `sudo`. Every one of those is overridable by env, so these tests
stub them with recording shims (no real hardware, no sudo, no network) and assert on
what the script *asked* them to do.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "afk-travel.sh"

PMSET_STUB = """#!/usr/bin/env bash
echo "$@" >> "$PMSET_LOG"
case "$*" in
  *ps*)  echo "Now drawing from '${FAKE_POWER:-AC Power}'" ;;
  *-g*)  echo "  SleepDisabled          ${FAKE_SLEEPDISABLED:-0}" ;;
esac
"""

NETWORKSETUP_STUB = """#!/usr/bin/env bash
echo "$@" >> "$NETWORKSETUP_LOG"
case "$1" in
  -getairportnetwork) echo "Current Wi-Fi Network: ${FAKE_SSID:-HomeWifi}" ;;
esac
"""

SECURITY_STUB = """#!/usr/bin/env bash
key=""; prev=""
for a in "$@"; do [ "$prev" = "-s" ] && key="$a"; prev="$a"; done
case "$key" in
  AFK_HOTSPOT_SSID)     [ -n "${FAKE_HOTSPOT_SSID:-}" ]     && echo "$FAKE_HOTSPOT_SSID"     || exit 1 ;;
  AFK_HOTSPOT_PASSWORD) [ -n "${FAKE_HOTSPOT_PASSWORD:-}" ] && echo "$FAKE_HOTSPOT_PASSWORD" || exit 1 ;;
  *) exit 1 ;;
esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A clean environment pointing the script at recording stubs (no sudo/hw)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("pmset", PMSET_STUB),
        ("networksetup", NETWORKSETUP_STUB),
        ("security", SECURITY_STUB),
    ):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)

    e = dict(os.environ)
    e.update(
        {
            "AFK_PMSET_BIN": str(bindir / "pmset"),
            "AFK_NETWORKSETUP_BIN": str(bindir / "networksetup"),
            "AFK_SECURITY_BIN": str(bindir / "security"),
            "AFK_SUDO": "",  # drop the sudo prefix under test
            "PMSET_LOG": str(tmp_path / "pmset.log"),
            "NETWORKSETUP_LOG": str(tmp_path / "networksetup.log"),
            "USER": e.get("USER", "tester"),
        }
    )
    return e


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _log(env: dict[str, str], key: str) -> str:
    p = Path(env[key])
    return p.read_text() if p.exists() else ""


def test_on_disables_clamshell_sleep(env: dict[str, str]) -> None:
    env["FAKE_POWER"] = "AC Power"

    result = _run(env, "on")

    assert result.returncode == 0
    assert "-a disablesleep 1" in _log(env, "PMSET_LOG")
    assert "travel mode ON" in result.stdout


def test_off_restores_sleep(env: dict[str, str]) -> None:
    result = _run(env, "off")

    assert result.returncode == 0
    assert "-a disablesleep 0" in _log(env, "PMSET_LOG")
    assert "travel mode OFF" in result.stdout


def test_on_joins_configured_hotspot(env: dict[str, str]) -> None:
    env["FAKE_POWER"] = "AC Power"
    env["FAKE_HOTSPOT_SSID"] = "MyPhone"
    env["FAKE_HOTSPOT_PASSWORD"] = "s3cret"
    env["FAKE_SSID"] = "HomeWifi"  # currently on a different net → must switch

    result = _run(env, "on")

    joined = _log(env, "NETWORKSETUP_LOG")
    assert "-setairportnetwork en0 MyPhone s3cret" in joined
    assert "Wi-Fi -> 'MyPhone'" in result.stdout


def test_on_without_hotspot_creds_warns_and_skips_switch(env: dict[str, str]) -> None:
    env["FAKE_POWER"] = "AC Power"
    # No FAKE_HOTSPOT_SSID → the security stub reports no secret.

    result = _run(env, "on")

    assert result.returncode == 0
    assert "-a disablesleep 1" in _log(env, "PMSET_LOG")
    assert "-setairportnetwork" not in _log(env, "NETWORKSETUP_LOG")
    assert "switch Wi-Fi to your phone hotspot by hand" in result.stderr


def test_on_battery_warns_but_still_toggles(env: dict[str, str]) -> None:
    env["FAKE_POWER"] = "Battery Power"

    result = _run(env, "on")

    assert result.returncode == 0
    assert "ON BATTERY" in result.stderr
    assert "-a disablesleep 1" in _log(env, "PMSET_LOG")


def test_status_reports_state(env: dict[str, str]) -> None:
    env["FAKE_POWER"] = "AC Power"
    env["FAKE_SLEEPDISABLED"] = "1"

    result = _run(env, "status")

    assert result.returncode == 0
    assert "SleepDisabled: 1" in result.stdout
    assert "Power:         AC" in result.stdout


def test_unknown_subcommand_exits_2(env: dict[str, str]) -> None:
    result = _run(env, "bogus")

    assert result.returncode == 2
    assert "usage:" in result.stderr
