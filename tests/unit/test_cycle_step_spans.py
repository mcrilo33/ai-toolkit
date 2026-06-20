"""Regression guard: cycle gates no longer emit ``kind: step`` spans (Issue #100).

Originally (Issue #21) each solo-cycle gate emitted a ``kind: step`` span tagged with
its phase, on top of the automatic ``kind: hook`` span. Issue #100 DROPS that emission:
cycle steps are now derived in the assembler from the todo ledger (``TaskCreate`` subject
+ ``TaskUpdate`` ``in_progress``/``completed`` windows), so the flat per-commit ``step:*``
markers are pure noise in the spoke-tree.

These tests pin the new contract: the gate hooks still emit their single ``kind: hook``
span, but never a ``kind: step`` span, and ``telemetry_mark_step`` no longer exists in the
telemetry lib.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = ROOT / "shared" / "hooks"
TELEMETRY_LIB = HOOKS_DIR / "lib" / "telemetry.sh"
GIT_PUSH_REVIEW = HOOKS_DIR / "git-push-review.sh"
REVIEW_WINDOW_OPEN = HOOKS_DIR / "review-window-open.sh"
COMMIT_GAUNTLET = HOOKS_DIR / "commit-gauntlet.sh"
RED_PROOF_VERIFY = HOOKS_DIR / "red-proof-verify.sh"


def _env(telemetry_dir: Path, *, enabled: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    for var in ("AI_TOOLKIT_TELEMETRY", "AI_TOOLKIT_TELEMETRY_DIR", "CURSOR_PROJECT_DIR"):
        env.pop(var, None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    env["AI_TOOLKIT_WORKFLOW_REV"] = "testrev0"
    return env


def _shell_payload(command: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": "",
            "workspace_roots": [str(root)],
        }
    )


def _run(script: Path, payload: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
    )


def _events(telemetry_dir: Path) -> list[dict]:
    f = telemetry_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _steps(telemetry_dir: Path) -> list[dict]:
    return [e for e in _events(telemetry_dir) if e.get("kind") == "step"]


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


class TestStepEmissionRemoved:
    def test_telemetry_lib_no_longer_defines_mark_step(self) -> None:
        text = TELEMETRY_LIB.read_text()
        assert "telemetry_mark_step" not in text
        assert "kind step" not in text  # the `--kind step` emission is gone

    def test_no_gate_hook_calls_mark_step(self) -> None:
        for hook in (GIT_PUSH_REVIEW, REVIEW_WINDOW_OPEN, COMMIT_GAUNTLET, RED_PROOF_VERIFY):
            assert "telemetry_mark_step" not in hook.read_text(), hook.name


class TestGatesEmitNoStepSpan:
    def test_push_gate_emits_hook_but_no_step(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload("git push origin main", project_root)

        _run(GIT_PUSH_REVIEW, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []
        assert any(e.get("kind") == "hook" for e in _events(telemetry_dir))

    def test_review_gate_emits_no_step(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = json.dumps(
            {"subagent_type": "code-review", "workspace_roots": [str(project_root)]}
        )

        _run(REVIEW_WINDOW_OPEN, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []

    def test_green_gate_emits_no_step_on_plain_commit(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload("git commit -m 'feat: add thing'", project_root)

        _run(COMMIT_GAUNTLET, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []

    def test_red_gate_emits_no_step(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(
            "git commit -m 'test: red' -m 'Tested-RED: tests/x.py::test_y'", project_root
        )

        _run(RED_PROOF_VERIFY, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir) == []
