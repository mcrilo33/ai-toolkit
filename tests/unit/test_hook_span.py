"""Unit tests for hook span-wrapping (Issue #21, subtask 2 — RED).

Subtask 2 extends the hook lib so that **every hook invocation** emits exactly
one span (``kind: hook``) to the telemetry log — not only the deny/warn counts
the legacy ``telemetry_event`` recorded. The span carries the hook basename as
``name``, the decision as ``status`` (success on a clean allow, ``deny`` /
``warn`` when those paths fire), a real ``ts_start``/``ts_end``/``duration_ms``,
and the join keys ``session_id`` (from the payload), ``spoke_run_id`` (from the
worktree file), and ``workflow_rev``.

Discipline is unchanged: opt-in via ``AI_TOOLKIT_TELEMETRY=1``, metadata only
(no path/command/payload leakage), zero stdout/stderr, and the hook's exit code
and output are byte-identical with telemetry on or off.

These tests subprocess the real hook scripts with Cursor/Claude-shaped payloads,
mirroring tests/unit/test_hook_telemetry.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
BLOCK_NO_VERIFY = HOOKS_DIR / "block-no-verify.sh"
CONSOLE_LOG_WARN = HOOKS_DIR / "console-log-warn.sh"
SECRETS_SCAN_REVERT = HOOKS_DIR / "secrets-scan-revert.sh"

BLOCK = 2
ALLOW = 0

DENY_COMMAND = "git commit --no-verify -m 'feat: x'"
BENIGN_COMMAND = "git status"


def _shell_payload(command: str, root: Path, *, session_id: str | None = None) -> str:
    payload: dict = {
        "hook_event_name": "beforeShellExecution",
        "command": command,
        "cwd": "",
        "workspace_roots": [str(root)],
    }
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload)


def _edit_payload(file_path: Path, new_string: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "afterFileEdit",
            "file_path": str(file_path),
            "edits": [{"old_string": "", "new_string": new_string}],
            "workspace_roots": [str(root)],
        }
    )


def _env(
    telemetry_dir: Path | None = None,
    *,
    enabled: bool = True,
    home: Path | None = None,
    workflow_rev: str | None = "testrev0",
) -> dict[str, str]:
    env = os.environ.copy()
    for var in (
        "AI_TOOLKIT_TELEMETRY",
        "AI_TOOLKIT_TELEMETRY_DIR",
        "AI_TOOLKIT_WORKFLOW_REV",
        "CURSOR_PROJECT_DIR",
        "TELEMETRY_PARENT_ID",
    ):
        env.pop(var, None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    if telemetry_dir is not None:
        env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    if home is not None:
        env["HOME"] = str(home)
    if workflow_rev is not None:
        env["AI_TOOLKIT_WORKFLOW_REV"] = workflow_rev
    return env


def _run(
    script: Path, payload: str, env: dict[str, str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _read_events(events_file: Path) -> list[dict]:
    return [json.loads(line) for line in events_file.read_text().splitlines()]


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    return tmp_path / "telemetry"


# ── one span per invocation, status reflects the decision ──


class TestHookSpanEmission:
    def test_allow_emits_single_success_hook_span(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        result = _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        assert result.returncode == ALLOW
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        span = events[0]
        assert span["kind"] == "hook"
        assert span["name"] == "block-no-verify.sh"
        assert span["status"] == "success"

    def test_deny_emits_deny_status_span(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(DENY_COMMAND, project_root)

        result = _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        assert result.returncode == BLOCK
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert events[0]["kind"] == "hook"
        assert events[0]["status"] == "deny"

    def test_warn_emits_warn_status_span(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _edit_payload(project_root / "d.py", 'print("hi")\n', project_root)

        result = _run(CONSOLE_LOG_WARN, payload, _env(telemetry_dir), cwd=project_root)

        assert result.returncode == ALLOW
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert events[0]["name"] == "console-log-warn.sh"
        assert events[0]["status"] == "warn"

    def test_span_carries_duration_and_ts(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert isinstance(span["duration_ms"], int)
        assert span["duration_ms"] >= 0
        assert span["ts_start"].endswith("Z")
        assert span["ts_end"].endswith("Z")


# ── join keys present ──────────────────────────────────────


class TestHookSpanJoinKeys:
    def test_span_has_all_join_keys(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        for key in ("session_id", "spoke_run_id", "workflow_rev", "span_id"):
            assert key in span

    def test_session_id_from_payload(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root, session_id="sess-77")

        _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["session_id"] == "sess-77"

    def test_spoke_run_id_from_worktree_file(self, project_root: Path, telemetry_dir: Path) -> None:
        ai_dir = project_root / ".ai-toolkit"
        ai_dir.mkdir()
        (ai_dir / "spoke-run-id").write_text("feature/21-foo+1700000000\n")
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["spoke_run_id"] == "feature/21-foo+1700000000"

    def test_workflow_rev_present(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        _run(
            BLOCK_NO_VERIFY, payload, _env(telemetry_dir, workflow_rev="abc1234"), cwd=project_root
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["workflow_rev"] == "abc1234"


# ── discipline: opt-in, invisibility, privacy, exit-code ───


class TestHookSpanDiscipline:
    def test_noop_when_disabled(self, project_root: Path, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        payload = _shell_payload(DENY_COMMAND, project_root)

        result = _run(
            BLOCK_NO_VERIFY, payload, _env(enabled=False, home=fake_home), cwd=project_root
        )

        assert result.returncode == BLOCK
        assert not (fake_home / ".ai-toolkit").exists()

    def test_exit_code_and_output_invariant_on_deny(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload(DENY_COMMAND, project_root)

        on = _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)
        off = _run(BLOCK_NO_VERIFY, payload, _env(enabled=False), cwd=project_root)

        assert on.returncode == off.returncode == BLOCK
        assert on.stdout == off.stdout
        assert on.stderr == off.stderr

    def test_exit_code_and_output_invariant_on_allow(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _shell_payload(BENIGN_COMMAND, project_root)

        on = _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)
        off = _run(BLOCK_NO_VERIFY, payload, _env(enabled=False), cwd=project_root)

        assert on.returncode == off.returncode == ALLOW
        assert on.stdout == off.stdout
        assert on.stderr == off.stderr

    def test_no_command_or_path_leak(self, project_root: Path, telemetry_dir: Path) -> None:
        command = "git commit --no-verify -m SECRETMARKER123"
        payload = _shell_payload(command, project_root)

        _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)

        content = (telemetry_dir / "events.jsonl").read_text()
        assert "SECRETMARKER123" not in content
        assert command not in content
        assert str(project_root) not in content


# ── trap composition: a hook with its own EXIT trap still works ──


class TestHookSpanTrapComposition:
    """secrets-scan-revert.sh sets its own `trap ... EXIT` for tempfile cleanup.

    Span-wrapping must compose with that — the hook must still emit a span AND
    still run its own cleanup. A no-op afterFileEdit (no secret) is enough to
    prove the span fires on the common early-exit path.
    """

    def test_revert_hook_emits_span(self, project_root: Path, telemetry_dir: Path) -> None:
        # An edit with no secret → the hook is a no-op that exits 0 early, but
        # it must still emit exactly one hook span.
        payload = _edit_payload(project_root / "ok.py", "x = 1\n", project_root)

        result = _run(SECRETS_SCAN_REVERT, payload, _env(telemetry_dir), cwd=project_root)

        assert result.returncode == ALLOW
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert events[0]["name"] == "secrets-scan-revert.sh"
        assert events[0]["kind"] == "hook"
