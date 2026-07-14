"""Unit tests for shared/skills/hub/scripts/gate-broker-classify.sh — the Tier-1 CLASSIFY
stage split out of gate-broker.sh (issue #275): the permission classifier
(classify_permission), the benign in-worktree mutation lane (#203), the read-only Read tool
lane (#181), and the redirect scanning that guards them.

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral. The
behavioral classification assertions migrate here from test_gate_broker.py in the #275
test-partition pass; this file also pins that the module's public surface loads.
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


CLASSIFY_SURFACE = (
    "classify_permission",
    "_pytest_seg_scoped",
    "_permission_seg_safe",
    "_permission_seg_mutation_ok",
    "_permission_seg_exec_ok",
    "_permission_seg_marker_ok",
    "_classify_read_tool",
    "_permission_redirect_scan",
    "_permission_redirects_ok",
)


def test_classify_module_surface_loads() -> None:
    # The classify module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-classify.sh in (a missing module
    # would leave classify_permission undefined and the deny-wall's Tier-1 lane would vanish).
    fns = " ".join(CLASSIFY_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_classify_permission_approves_scoped_git_add() -> None:
    # A representative Tier-1 verdict lands identically through the split module.
    result = _call('classify_permission "git add tests/x.py" | cut -f1')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APPROVE"
