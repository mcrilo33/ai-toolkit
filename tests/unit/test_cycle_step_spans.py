"""Unit tests for cycle-gate step spans (Issue #21, subtask 4 — RED).

The solo-cycle gates fire as hooks; subtask 4 has the canonical gate for each
cycle phase ALSO emit a ``kind: step`` span tagged with that phase, on top of
the automatic ``kind: hook`` span:

* ``red-proof-verify.sh``  → step/red    (fires on a Tested-RED commit)
* ``commit-gauntlet.sh``   → step/green  (fires on a NON-RED commit)
* ``review-window-open.sh``→ step/review (fires when the code-review subagent starts)
* ``git-push-review.sh``   → step/push   (fires on git push)

A RED commit must NOT also produce a step/green (that is red-proof-verify's gate).
The mechanism (``telemetry_mark_step`` + emit at hook exit) stays opt-in,
invisible, and metadata-only.
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


def _steps(telemetry_dir: Path, *, phase: str | None = None) -> list[dict]:
    out = [e for e in _events(telemetry_dir) if e.get("kind") == "step"]
    if phase is not None:
        out = [e for e in out if e.get("phase") == phase]
    return out


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


# ── mechanism ──────────────────────────────────────────────


class TestStepSpanMechanism:
    def test_mark_step_emits_step_span_at_exit(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        script = (
            f'source "{TELEMETRY_LIB}"; telemetry_arm_hook_span; '
            "telemetry_mark_step green; telemetry_set_status success"
        )
        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=_env(telemetry_dir),
            cwd=str(project_root),
        )

        steps = _steps(telemetry_dir, phase="green")
        assert len(steps) == 1
        assert steps[0]["kind"] == "step"
        assert steps[0]["status"] == "success"
        # The hook span is still emitted alongside the step span.
        assert any(e.get("kind") == "hook" for e in _events(telemetry_dir))

    def test_no_step_span_without_mark(self, project_root: Path, telemetry_dir: Path) -> None:
        script = f'source "{TELEMETRY_LIB}"; telemetry_arm_hook_span; telemetry_set_status success'
        subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=_env(telemetry_dir),
            cwd=str(project_root),
        )

        assert _steps(telemetry_dir) == []


# ── per-gate integration ───────────────────────────────────


class TestGateStepSpans:
    def test_push_gate_emits_step_push(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload("git push origin main", project_root)

        _run(GIT_PUSH_REVIEW, payload, _env(telemetry_dir), project_root)

        assert len(_steps(telemetry_dir, phase="push")) == 1

    def test_review_gate_emits_step_review(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = json.dumps(
            {"subagent_type": "code-review", "workspace_roots": [str(project_root)]}
        )

        _run(REVIEW_WINDOW_OPEN, payload, _env(telemetry_dir), project_root)

        assert len(_steps(telemetry_dir, phase="review")) == 1

    def test_green_gate_emits_step_green_on_plain_commit(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload("git commit -m 'feat: add thing'", project_root)

        _run(COMMIT_GAUNTLET, payload, _env(telemetry_dir), project_root)

        assert len(_steps(telemetry_dir, phase="green")) == 1

    def test_green_gate_skips_red_commit(self, project_root: Path, telemetry_dir: Path) -> None:
        # A RED commit (Tested-RED trailer) is the red gate's job, not green.
        payload = _shell_payload(
            "git commit -m 'test: red' -m 'Tested-RED: tests/x.py::test_y'", project_root
        )

        _run(COMMIT_GAUNTLET, payload, _env(telemetry_dir), project_root)

        assert _steps(telemetry_dir, phase="green") == []

    def test_red_gate_emits_step_red(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(
            "git commit -m 'test: red' -m 'Tested-RED: tests/x.py::test_y'", project_root
        )

        _run(RED_PROOF_VERIFY, payload, _env(telemetry_dir), project_root)

        # status may be deny/success depending on the node run; the span must exist.
        assert len(_steps(telemetry_dir, phase="red")) == 1


# ── discipline ─────────────────────────────────────────────


class TestStepSpanDiscipline:
    def test_step_span_noop_when_disabled(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload("git push origin main", project_root)

        _run(GIT_PUSH_REVIEW, payload, _env(telemetry_dir, enabled=False), project_root)

        assert not (telemetry_dir / "events.jsonl").exists()
