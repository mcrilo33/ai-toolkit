"""Unit tests for shared/skills/hub/scripts/gate-broker-detect.sh — the park/gate
detection stage split out of gate-broker.sh (issue #275): transcript idle helpers,
extract_pending_question, slot_state, _gate_parked, _gate_answer_landed, and the
still-parked / moved-on predicates.

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral. The
behavioral park/slot-state assertions migrate here from test_gate_broker.py in the #275
test-partition pass; this file also pins that the module's public surface loads (proof the
fail-closed source loop wired it in).
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


DETECT_SURFACE = (
    "_transcript_idle_seconds",
    "_task_output_mtime",
    "_spoke_idle_seconds",
    "extract_pending_question",
    "_is_seed_replay",
    "slot_state",
    "spoke_over_ceiling",
    "_gate_parked",
    "_gate_answer_landed",
    "_gate_artifact_path",
    "_read_gate_artifact",
    "_spoke_still_parked",
    "_spoke_moved_on",
)


def test_detect_module_surface_loads() -> None:
    # The detect module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-detect.sh in (a missing module would
    # leave these undefined and the drain could not detect a parked gate).
    fns = " ".join(DETECT_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"
