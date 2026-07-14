"""Unit tests for shared/skills/hub/scripts/gate-broker-permission.sh — permission-dialog
handling + the programmatic PreToolUse hook entry points split out of gate-broker.sh
(issue #275): the pane-path detection (#149, extract_pending_command / _permission_pending /
_reason_permission / _decide_permission), the allow-only afk_permission_hook_decide (#253),
and the deny-wall afk_danger_guard_decide (#261) with its emit/supervisor/spoke-mode helpers.

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral. The
behavioral hook/deny-wall assertions migrate here from test_gate_broker.py in the #275
test-partition pass; this file also pins that the module's public surface loads — the two
hook decide functions the shims (afk-danger-guard.sh / afk-permission-hook.sh) resolve.
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


PERMISSION_SURFACE = (
    "extract_pending_command",
    "_permission_pending",
    "_reason_permission",
    "_decide_permission",
    "_afk_supervisor_live",
    "_afk_hook_emit_allow",
    "_afk_hook_emit_deny",
    "afk_permission_hook_decide",
    "_afk_spoke_mode",
    "afk_danger_guard_decide",
)


def test_permission_module_surface_loads() -> None:
    # The permission module's public surface must resolve after the entry lib sources it. The
    # two hook decide functions the shims (afk-danger-guard.sh / afk-permission-hook.sh) resolve
    # via `command -v` MUST be present — proof the fail-closed source loop wired the module in.
    fns = " ".join(PERMISSION_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"
