"""Unit tests for the opt-in hook telemetry log.

Spec under test (the unified span model — see docs/telemetry-span-schema.md):

* Opt-in via ``AI_TOOLKIT_TELEMETRY=1``; when unset / not "1", NO telemetry
  file or directory is created.
* Events append to
  ``${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl`` —
  one JSON span per line.
* Each span carries ONLY metadata: ``kind`` (``hook`` here), ``name`` (basename
  of the hook script), ``status`` (success/warn/deny), ``repo`` (basename of the
  project root — never the full path), and ``ts_start``/``ts_end``. No command
  strings, no messages, no file contents (messages may quote the blocked command
  → secret-leak risk).
* ``deny()`` records status=deny, ``warn()`` records status=warn. Telemetry
  failures must never change a hook's exit code or output.

These tests subprocess the real hook scripts with Cursor-shaped payloads,
mirroring tests/unit/test_commit_hooks.py and tests/unit/test_cursor_hooks.py.
The richer per-span coverage lives in tests/unit/test_hook_span.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
BLOCK_NO_VERIFY = HOOKS_DIR / "block-no-verify.sh"
CONSOLE_LOG_WARN = HOOKS_DIR / "console-log-warn.sh"

BLOCK = 2
ALLOW = 0

DENY_COMMAND = "git commit --no-verify -m 'feat: x'"


def _cursor_shell_payload(command: str, root: Path) -> str:
    """Cursor beforeShellExecution shape: top-level command + workspace_roots."""
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": "",
            "workspace_roots": [str(root)],
        }
    )


def _cursor_edit_payload(file_path: Path, new_string: str, root: Path) -> str:
    """Cursor afterFileEdit shape: top-level file_path + edits[]."""
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
) -> dict[str, str]:
    """A copy of the ambient env with deterministic telemetry settings.

    Pops any inherited telemetry vars and CURSOR_PROJECT_DIR (which would
    override payload-based project-root resolution), then applies overrides.
    """
    env = os.environ.copy()
    env.pop("AI_TOOLKIT_TELEMETRY", None)
    env.pop("AI_TOOLKIT_TELEMETRY_DIR", None)
    env.pop("CURSOR_PROJECT_DIR", None)
    if enabled:
        env["AI_TOOLKIT_TELEMETRY"] = "1"
    if telemetry_dir is not None:
        env["AI_TOOLKIT_TELEMETRY_DIR"] = str(telemetry_dir)
    if home is not None:
        env["HOME"] = str(home)
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
    """A fake project root with a distinctive basename and a .git marker."""
    root = tmp_path / "sample-project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def telemetry_dir(tmp_path: Path) -> Path:
    """Target telemetry directory — deliberately NOT created up front."""
    return tmp_path / "telemetry"


# ── opt-in gate ───────────────────────────────────────────


class TestTelemetryOptIn:
    def test_deny_writes_event_when_telemetry_enabled(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)
        env = _env(telemetry_dir)

        result = _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        assert result.returncode == BLOCK
        events_file = telemetry_dir / "events.jsonl"
        assert events_file.exists()
        events = _read_events(events_file)
        assert len(events) == 1
        assert events[0]["kind"] == "hook"
        assert events[0]["status"] == "deny"
        assert events[0]["name"] == "block-no-verify.sh"
        assert "ts_start" in events[0]
        assert "repo" in events[0]

    def test_no_telemetry_file_when_disabled(self, project_root: Path, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)
        env = _env(enabled=False, home=fake_home)

        result = _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        assert result.returncode == BLOCK  # the deny itself is unaffected
        assert not (fake_home / ".ai-toolkit").exists()


# ── event content ─────────────────────────────────────────


class TestTelemetryEvents:
    def test_warn_writes_warn_decision(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _cursor_edit_payload(project_root / "d.py", 'print("hi")\n', project_root)
        env = _env(telemetry_dir)

        result = _run(CONSOLE_LOG_WARN, payload, env, cwd=project_root)

        assert result.returncode == ALLOW
        assert "print()" in result.stderr  # the warn path actually fired
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert events[0]["status"] == "warn"
        assert events[0]["name"] == "console-log-warn.sh"

    def test_multiple_events_append(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)
        env = _env(telemetry_dir)

        _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)
        _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 2
        assert events[0]["status"] == "deny"
        assert events[1]["status"] == "deny"

    def test_ts_field_is_iso8601_utc(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)
        env = _env(telemetry_dir)

        _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        events = _read_events(telemetry_dir / "events.jsonl")
        ts = datetime.fromisoformat(events[0]["ts_start"])
        assert ts.tzinfo is not None
        assert ts.utcoffset() == timedelta(0)


# ── privacy hygiene ───────────────────────────────────────


class TestTelemetryHygiene:
    def test_event_never_contains_command_string(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        command = "git commit --no-verify -m SECRETMARKER123"
        payload = _cursor_shell_payload(command, project_root)
        env = _env(telemetry_dir)

        result = _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        assert result.returncode == BLOCK
        content = (telemetry_dir / "events.jsonl").read_text()
        assert "SECRETMARKER123" not in content
        assert command not in content

    def test_repo_field_is_basename_not_full_path(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)
        env = _env(telemetry_dir)

        _run(BLOCK_NO_VERIFY, payload, env, cwd=project_root)

        events = _read_events(telemetry_dir / "events.jsonl")
        assert events[0]["repo"] == "sample-project"
        assert "/" not in events[0]["repo"]

    def test_hook_behavior_unchanged_with_telemetry(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = _cursor_shell_payload(DENY_COMMAND, project_root)

        with_telemetry = _run(BLOCK_NO_VERIFY, payload, _env(telemetry_dir), cwd=project_root)
        without_telemetry = _run(BLOCK_NO_VERIFY, payload, _env(enabled=False), cwd=project_root)

        assert with_telemetry.returncode == BLOCK
        assert with_telemetry.returncode == without_telemetry.returncode
        assert with_telemetry.stdout == without_telemetry.stdout
        assert with_telemetry.stderr == without_telemetry.stderr
