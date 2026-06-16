"""Unit tests for control-script ``kind=script`` spans (Issue #54 track E).

The marker emitter (``spoke-ready.sh``) and the push wrapper (``spoke-push.sh``)
are control-plane scripts but emit no telemetry today. Track E makes them
first-class trace nodes: each emits one ``kind=script`` span when it succeeds,
gated on ``AI_TOOLKIT_TELEMETRY=1``. ``spoke-ready`` tags the span with the
marker namespace it emitted (``phase`` = ready|gate|accept|blocked) so the trace
distinguishes a completion marker from a PLAN-gate park. The emission link
(``emits``) stays null on push — the parser fills it later.

Hermetic like test_spoke_ready.py / test_spoke_push.py: a local bare ``origin``
(no network) and a feature-branch checkout one commit ahead.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SPOKE_READY = SCRIPTS / "spoke-ready.sh"
SPOKE_PUSH = SCRIPTS / "spoke-push.sh"

# Pin git config to nothing so a host's global/system config never reaches the
# fixture repo's commits or pushes.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

OWN = "feature/54-track-e"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _tele_env(telemetry_dir: Path, *, enabled: bool = True) -> dict[str, str]:
    env = {**_GIT_ENV}
    for var in ("AI_TOOLKIT_TELEMETRY", "AI_TOOLKIT_TELEMETRY_DIR", "AI_TOOLKIT_WORKFLOW_REV"):
        env.pop(var, None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    env["AI_TOOLKIT_WORKFLOW_REV"] = "testrev0"
    return env


def _run(script: Path, repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


def _events(telemetry_dir: Path) -> list[dict]:
    f = telemetry_dir / "events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()]


def _scripts(telemetry_dir: Path, *, name: str) -> list[dict]:
    return [
        e for e in _events(telemetry_dir) if e.get("kind") == "script" and e.get("name") == name
    ]


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    return remote


@pytest.fixture()
def spoke(tmp_path: Path, remote: Path) -> Path:
    """A feature-branch checkout one commit ahead, branch pushed to origin."""
    repo = tmp_path / "spoke"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "checkout", "-q", "-b", OWN)
    (repo / "work.txt").write_text("spoke work\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-qm", "feat: work", "-m", "Refs #54")
    _git(repo, "push", "-q", "-u", "origin", OWN)
    return repo


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


# ── spoke-ready: marker emitter as a run-node ──────────────


class TestSpokeReadyScriptSpan:
    def test_ready_emits_script_span(self, spoke: Path, telemetry_dir: Path) -> None:
        res = _run(SPOKE_READY, spoke, _tele_env(telemetry_dir), "54")

        assert res.returncode == 0, res.stdout + res.stderr
        spans = _scripts(telemetry_dir, name="spoke-ready")
        assert len(spans) == 1
        assert spans[0]["phase"] == "ready"
        assert spans[0]["status"] == "success"
        assert spans[0]["emits"] is None

    def test_gate_span_tagged_with_gate_phase(self, spoke: Path, telemetry_dir: Path) -> None:
        res = _run(SPOKE_READY, spoke, _tele_env(telemetry_dir), "--gate", "54")

        assert res.returncode == 0, res.stdout + res.stderr
        spans = _scripts(telemetry_dir, name="spoke-ready")
        assert len(spans) == 1
        assert spans[0]["phase"] == "gate"

    def test_noop_when_disabled(self, spoke: Path, telemetry_dir: Path) -> None:
        res = _run(SPOKE_READY, spoke, _tele_env(telemetry_dir, enabled=False), "54")

        assert res.returncode == 0, res.stdout + res.stderr
        assert not (telemetry_dir / "events.jsonl").exists()


# ── spoke-push: push wrapper as a run-node ─────────────────


class TestSpokePushScriptSpan:
    def test_push_emits_script_span(self, spoke: Path, telemetry_dir: Path) -> None:
        res = _run(SPOKE_PUSH, spoke, _tele_env(telemetry_dir))

        assert res.returncode == 0, res.stdout + res.stderr
        spans = _scripts(telemetry_dir, name="spoke-push")
        assert len(spans) == 1
        assert spans[0]["status"] == "success"
        assert spans[0]["emits"] is None

    def test_noop_when_disabled(self, spoke: Path, telemetry_dir: Path) -> None:
        res = _run(SPOKE_PUSH, spoke, _tele_env(telemetry_dir, enabled=False))

        assert res.returncode == 0, res.stdout + res.stderr
        assert not (telemetry_dir / "events.jsonl").exists()
