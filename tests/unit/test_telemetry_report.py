"""Unit tests for telemetry-report.sh against the span model (Issue #21, subtask 4 — RED).

telemetry-report.sh summarizes the event log. The log now holds unified spans
(``kind``/``name``/``status``/``phase``/``duration_ms``/…), not the legacy
``{ts,hook,decision,repo}`` lines. The report must summarize spans — and, for
backward compatibility, still read any legacy lines a pre-migration log may hold.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parents[2] / "scripts" / "telemetry-report.sh"


def _run(events_file: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    return subprocess.run(
        ["bash", str(REPORT), str(events_file)],
        capture_output=True,
        text=True,
        env=env,
    )


def _span(**over) -> str:
    base = {
        "span_id": "s1",
        "parent_id": None,
        "spoke_run_id": None,
        "session_id": None,
        "workflow_rev": "rev0",
        "repo": "demo",
        "branch": "main",
        "kind": "hook",
        "name": "secrets-scan.sh",
        "phase": None,
        "ts_start": "2026-06-13T12:00:00Z",
        "ts_end": "2026-06-13T12:00:00Z",
        "duration_ms": 0,
        "status": "success",
        "human": None,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
    }
    base.update(over)
    return json.dumps(base)


@pytest.fixture()
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


class TestReportSpans:
    def test_runs_and_summarizes_span_names_and_status(self, events_file: Path) -> None:
        events_file.write_text(
            "\n".join(
                [
                    _span(name="secrets-scan.sh", status="success"),
                    _span(name="block-no-verify.sh", status="deny"),
                    _span(kind="lifecycle", name="worktree-new", phase="spawn", status="success"),
                ]
            )
            + "\n"
        )

        result = _run(events_file)

        assert result.returncode == 0
        out = result.stdout
        assert "block-no-verify.sh" in out
        assert "worktree-new" in out
        assert "deny" in out

    def test_surfaces_cycle_phase_of_step_spans(self, events_file: Path) -> None:
        # Step spans share the constant name "solo-cycle"; the distinguishing
        # dimension is phase. The report must surface phases, not collapse them.
        events_file.write_text(
            "\n".join(
                [
                    _span(kind="step", name="solo-cycle", phase="red", status="success"),
                    _span(kind="step", name="solo-cycle", phase="push", status="warn"),
                ]
            )
            + "\n"
        )

        result = _run(events_file)

        assert result.returncode == 0
        assert "red" in result.stdout
        assert "push" in result.stdout

    def test_reports_total_event_count(self, events_file: Path) -> None:
        events_file.write_text("\n".join(_span() for _ in range(3)) + "\n")

        result = _run(events_file)

        assert result.returncode == 0
        assert "3" in result.stdout


class TestReportBackCompat:
    def test_still_reads_legacy_lines(self, events_file: Path) -> None:
        # A pre-migration log: {ts,hook,decision,repo}. The report must not crash
        # and should still surface the hook name + decision.
        legacy = json.dumps(
            {
                "ts": "2026-01-01T00:00:00Z",
                "hook": "old-hook.sh",
                "decision": "warn",
                "repo": "demo",
            }
        )
        events_file.write_text(legacy + "\n")

        result = _run(events_file)

        assert result.returncode == 0
        assert "old-hook.sh" in result.stdout
        assert "warn" in result.stdout

    def test_handles_mixed_legacy_and_span_lines(self, events_file: Path) -> None:
        legacy = json.dumps(
            {
                "ts": "2026-01-01T00:00:00Z",
                "hook": "old-hook.sh",
                "decision": "deny",
                "repo": "demo",
            }
        )
        events_file.write_text(legacy + "\n" + _span(name="new-hook.sh", status="success") + "\n")

        result = _run(events_file)

        assert result.returncode == 0
        assert "old-hook.sh" in result.stdout
        assert "new-hook.sh" in result.stdout


class TestReportEmpty:
    def test_missing_file_reports_no_events(self, tmp_path: Path) -> None:
        result = _run(tmp_path / "nope.jsonl")

        assert result.returncode == 0
        assert "No telemetry events" in result.stdout
