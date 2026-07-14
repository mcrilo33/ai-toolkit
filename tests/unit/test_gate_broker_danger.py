"""Unit tests for shared/skills/hub/scripts/gate-broker-danger.sh — the Tier-2/Tier-3
CLASSIFY-deny stage split out of gate-broker.sh (issue #275): the static danger classifier
(classify_danger + the _danger_* segment checks, #261), the headless LLM judge
(judge_permission + _judge_*), and the drain-level judge-unavailable halt state (#268).

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral. The
behavioral Tier-2/Tier-3 assertions migrate here from test_gate_broker.py in the #275
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


DANGER_SURFACE = (
    "classify_danger",
    "_danger_network_seg",
    "_danger_credential_seg",
    "_danger_write_seg",
    "_danger_privilege_seg",
    "_danger_publish_seg",
    "_danger_eval_seg",
    "_danger_gh_seg",
    "judge_permission",
    "broker_judge_halt_pending",
    "broker_reset_judge_halt",
)


def test_danger_module_surface_loads() -> None:
    # The danger module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-danger.sh in (a missing module would
    # leave classify_danger / judge_permission undefined and the deny-wall would lose Tier 2+3).
    fns = " ".join(DANGER_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_classify_danger_denies_credential_read() -> None:
    # A representative Tier-2 DENY that needs no worktree context (a secret-file read) lands
    # identically through the split module. Worktree-relative write denials, which need the
    # spoke_repo fixture, migrate with that fixture in the #275 partition pass.
    result = _call('classify_danger "cat ~/.ssh/id_rsa" | cut -f1')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "DENY"
