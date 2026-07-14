"""Unit tests for shared/skills/hub/scripts/gate-broker-markers.sh — the marker/state
plumbing split out of gate-broker.sh (issue #275): dispatch/progress/answer epochs, the
event spool (#176), network-outage state (#249), the re-answer ceiling (#203), terminal
gate markers (#237), sibling-script resolution, the in-flight survey, _consume_gate_tag,
and the durable local block record (#109).

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral — the same
harness every gate-broker test file uses.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_BROKER = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "gate-broker.sh"


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


def _call(
    fn_call: str, *, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Source gate-broker.sh directly and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{GATE_BROKER}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
        input=stdin,
    )


# ── event spool (issue #176) ──────────────────────────────────────────────────
# The event-driven wake path: a spoke drops one <epoch>-<issue>-<type> file in the spool
# and signals the supervisor. The reader (afk_event_dir / afk_drain_event_issues) lives in
# the markers module so both the hub loop and these tests exercise it directly.


def test_afk_event_dir_is_under_the_state_dir(tmp_path: Path) -> None:
    result = _call("afk_event_dir", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.stdout.strip() == str(tmp_path / "st" / "events")


def test_afk_drain_event_issues_prints_distinct_issues_and_deletes(tmp_path: Path) -> None:
    events = tmp_path / "st" / "events"
    events.mkdir(parents=True)
    for name in ("100-5-gate", "101-5-park", "102-7-ready"):
        (events / name).touch()

    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["5", "7"], "duplicate events collapse to one issue each"
    assert not any(events.iterdir()), "every spool file is drained (deleted)"


def test_afk_drain_event_issues_drops_malformed_names(tmp_path: Path) -> None:
    events = tmp_path / "st" / "events"
    events.mkdir(parents=True)
    (events / "103-notanumber-x").touch()
    (events / "104-9-ready").touch()

    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.stdout.split() == ["9"], "a non-numeric issue field is skipped"
    assert not any(events.iterdir()), "malformed files are deleted too, never left to pile up"


def test_afk_drain_event_issues_empty_when_no_spool(tmp_path: Path) -> None:
    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.returncode == 0
    assert result.stdout.strip() == ""
