"""Unit tests for shared/skills/hub/scripts/gate-broker-answerer.sh — the REASON stage split
out of gate-broker.sh (issue #275): the read-only worktree reasoner (#155), the
automatable-decisions log + codification, the decision journal + warn-and-continue (#241),
build_answerer_prompt, and the bounded run_answerer / parse_decision path (#171).

The module is a pure function-definition file sourced by the entry lib; these tests source
``gate-broker.sh`` (which sources the module) so the split stays behavior-neutral. The
behavioral reasoner/journal assertions migrate here from test_gate_broker.py in the #275
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


ANSWERER_SURFACE = (
    "reasoner_allowed_tools",
    "assert_readonly_tools",
    "read_decisions_digest",
    "log_decision",
    "codify_decisions",
    "broker_journal_decision",
    "broker_warn_continue",
    "build_answerer_prompt",
    "run_answerer",
    "parse_decision",
    "parse_decision_field",
    "is_auth_failure",
)


def test_answerer_module_surface_loads() -> None:
    # The answerer module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-answerer.sh in (a missing module
    # would leave run_answerer / parse_decision undefined and the drain could not reason).
    fns = " ".join(ANSWERER_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_parse_decision_extracts_answer() -> None:
    # A representative parse lands identically through the split module.
    result = _call("parse_decision 'reasoning here\nANSWER: use Redis'")

    assert result.returncode == 0, result.stderr
    kind, _, text = result.stdout.strip().partition("\t")
    assert kind == "ANSWER"
    assert text == "use Redis"
