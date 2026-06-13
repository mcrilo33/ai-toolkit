"""Unit tests for the unified span emit helper (Issue #21, subtask 1 — RED).

The shared helper ``shared/hooks/lib/telemetry.sh`` defines ``telemetry_emit_span``,
the single source of truth for appending one **span** object per event to
``${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl``.

Frozen schema v1 (the contract for downstream issues B + C)::

    {
      "span_id", "parent_id",          # nesting
      "spoke_run_id",                  # ties all spans of one spoke
      "session_id",                    # join key to CC token cost
      "workflow_rev",                  # ai-toolkit short SHA at emit time
      "repo", "branch",
      "kind", "name", "phase",
      "ts_start", "ts_end", "duration_ms",
      "status",
      "human",                         # {type, wait_ms} or null
      "tokens_in", "tokens_out", "cost_usd"   # null at emit; filled by the
                                              # later correlation pass
    }

Discipline mirrors the existing ``telemetry_event()``: opt-in via
``AI_TOOLKIT_TELEMETRY=1``, metadata only (no paths / commands / messages /
payload content), zero bytes on stdout / stderr, no-op when unset.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "telemetry.sh"

# Every key the frozen schema v1 must carry on every span.
SCHEMA_KEYS = {
    "span_id",
    "parent_id",
    "spoke_run_id",
    "session_id",
    "workflow_rev",
    "repo",
    "branch",
    "kind",
    "name",
    "phase",
    "ts_start",
    "ts_end",
    "duration_ms",
    "status",
    "human",
    "tokens_in",
    "tokens_out",
    "cost_usd",
}


def _env(
    telemetry_dir: Path | None = None,
    *,
    enabled: bool = True,
    home: Path | None = None,
    workflow_rev: str | None = "testrev0",
    payload: str | None = None,
) -> dict[str, str]:
    """Ambient env with deterministic telemetry settings."""
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
    if payload is not None:
        env["INPUT"] = payload
    return env


def _emit(
    args: str,
    env: dict[str, str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Source the lib and call telemetry_emit_span with the given arg string."""
    script = f'source "{TELEMETRY_LIB}"; telemetry_emit_span {args}'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
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


# ── schema validity ───────────────────────────────────────


class TestSpanSchema:
    def test_emit_writes_one_valid_jsonl_span(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        result = _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert result.returncode == 0
        events = _read_events(telemetry_dir / "events.jsonl")
        assert len(events) == 1
        assert SCHEMA_KEYS.issubset(events[0].keys())

    def test_kind_name_phase_status_round_trip(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind step --name solo-cycle --phase red --status failure",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["kind"] == "step"
        assert span["name"] == "solo-cycle"
        assert span["phase"] == "red"
        assert span["status"] == "failure"

    def test_tokens_and_cost_null_at_emit(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["tokens_in"] is None
        assert span["tokens_out"] is None
        assert span["cost_usd"] is None

    def test_ts_fields_iso8601_utc(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        for field in ("ts_start", "ts_end"):
            ts = datetime.fromisoformat(span[field])
            assert ts.tzinfo is not None
            assert ts.utcoffset() == timedelta(0)

    def test_duration_ms_is_nonnegative_int(self, project_root: Path, telemetry_dir: Path) -> None:
        # A start clock of "0" ms means duration = now - 0 → a large positive int;
        # the contract is only that duration_ms is a non-negative integer.
        _emit(
            "--kind hook --name secrets-scan --status success --start-ms 0",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert isinstance(span["duration_ms"], int)
        assert span["duration_ms"] >= 0

    def test_default_phase_and_human_are_null(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["phase"] is None
        assert span["human"] is None

    def test_human_block_recorded_when_provided(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        _emit(
            "--kind human --name approval --status success "
            "--human-type approval --human-wait-ms 4200",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["human"] == {"type": "approval", "wait_ms": 4200}


# ── context resolution ────────────────────────────────────


class TestSpanContext:
    def test_repo_is_basename(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["repo"] == "sample-project"
        assert "/" not in span["repo"]

    def test_workflow_rev_from_env_override(self, project_root: Path, telemetry_dir: Path) -> None:
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, workflow_rev="abc1234"),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["workflow_rev"] == "abc1234"

    def test_session_id_extracted_from_payload(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        payload = json.dumps(
            {
                "session_id": "sess-XYZ",
                "transcript_path": "/secret/path/transcript.jsonl",
                "cwd": "/secret/cwd",
            }
        )
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["session_id"] == "sess-XYZ"

    def test_spoke_run_id_read_from_worktree_file(
        self, project_root: Path, telemetry_dir: Path
    ) -> None:
        ai_dir = project_root / ".ai-toolkit"
        ai_dir.mkdir()
        (ai_dir / "spoke-run-id").write_text("feature/21-foo+1700000000\n")

        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["spoke_run_id"] == "feature/21-foo+1700000000"

    def test_parent_id_from_env(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        env["TELEMETRY_PARENT_ID"] = "parent-span-1"
        _emit(
            "--kind hook --name secrets-scan --status success",
            env,
            cwd=project_root,
        )

        span = _read_events(telemetry_dir / "events.jsonl")[0]
        assert span["parent_id"] == "parent-span-1"

    def test_span_id_is_present_and_unique(self, project_root: Path, telemetry_dir: Path) -> None:
        env = _env(telemetry_dir)
        _emit("--kind hook --name a --status success", env, cwd=project_root)
        _emit("--kind hook --name b --status success", env, cwd=project_root)

        spans = _read_events(telemetry_dir / "events.jsonl")
        ids = {s["span_id"] for s in spans}
        assert all(s["span_id"] for s in spans)
        assert len(ids) == 2


# ── opt-in + invisibility + privacy ───────────────────────


class TestSpanDiscipline:
    def test_noop_when_disabled(self, project_root: Path, tmp_path: Path) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        result = _emit(
            "--kind hook --name secrets-scan --status success",
            _env(enabled=False, home=fake_home),
            cwd=project_root,
        )

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        assert not (fake_home / ".ai-toolkit").exists()

    def test_invisible_no_stdout_or_stderr(self, project_root: Path, telemetry_dir: Path) -> None:
        result = _emit(
            "--kind lifecycle --name worktree-new --phase spawn --status success",
            _env(telemetry_dir),
            cwd=project_root,
        )

        assert result.stdout == ""
        assert result.stderr == ""

    def test_no_payload_content_leaks(self, project_root: Path, telemetry_dir: Path) -> None:
        payload = json.dumps(
            {
                "session_id": "sess-1",
                "transcript_path": "/home/u/SECRETPATH/t.jsonl",
                "cwd": "/home/u/SECRETCWD",
                "tool_input": {"command": "echo SECRETCOMMAND123"},
            }
        )
        _emit(
            "--kind hook --name secrets-scan --status success",
            _env(telemetry_dir, payload=payload),
            cwd=project_root,
        )

        content = (telemetry_dir / "events.jsonl").read_text()
        assert "SECRETPATH" not in content
        assert "SECRETCWD" not in content
        assert "SECRETCOMMAND123" not in content
