"""Unit tests for the red→issue automation in .github/workflows/ci.yml.

Issue #129 subtask 2: a red CI run must land in the backlog instead of an
unread checks tab. The workflow gains a ``report-red`` job that fires only
when a gate job failed and, per failing job, either updates the existing
open auto-filed issue (stable ``CI red: <job>`` title marker — no duplicates
on consecutive red runs) or creates one referencing the run and commit.

These tests pin the workflow contract by parsing the YAML; the live behavior
is exercised on a scratch-branch PR per the issue's acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

GATE_JOBS = {"test", "shellcheck", "sync-idempotency"}


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    # YAML 1.1 trap: safe_load parses the workflow's `on:` key as boolean
    # True, so triggers live under workflow[True], not workflow["on"].
    return yaml.safe_load(CI_YML.read_text())


@pytest.fixture(scope="module")
def report_red(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]["report-red"]


def test_report_red_job_exists(workflow: dict[str, Any]) -> None:
    assert "report-red" in workflow["jobs"]


def test_report_red_needs_every_gate_job(report_red: dict[str, Any]) -> None:
    # It must observe all gate jobs, so any single red job triggers it.
    assert set(report_red["needs"]) == GATE_JOBS


def test_report_red_runs_only_on_failure(report_red: dict[str, Any]) -> None:
    # Exact match: "success() || failure()" etc. would also contain failure().
    assert report_red["if"] == "failure()"


def test_report_red_issues_write_is_job_scoped(
    workflow: dict[str, Any], report_red: dict[str, Any]
) -> None:
    # Only the reporter may write issues; the workflow default stays read-only.
    assert report_red["permissions"]["issues"] == "write"
    assert workflow["permissions"] == {"contents": "read"}


def test_report_red_updates_existing_issue_before_creating(
    report_red: dict[str, Any],
) -> None:
    # Dedup contract: search open issues by the stable "CI red: <job>" title
    # marker and comment on a hit; only a miss may create a new issue.
    script = "\n".join(step.get("run", "") for step in report_red["steps"])
    assert "CI red:" in script
    assert "gh issue list" in script
    assert "gh issue comment" in script
    assert "gh issue create" in script
    assert script.index("gh issue list") < script.index("gh issue create")


def test_report_red_references_run_and_commit(report_red: dict[str, Any]) -> None:
    script = "\n".join(step.get("run", "") for step in report_red["steps"])
    assert "github.run_id" in script or "GITHUB_RUN_ID" in script
    assert "github.sha" in script or "GITHUB_SHA" in script


# --- CI is the gate (#378) -------------------------------------------------------


def test_every_branch_push_triggers_ci(workflow: dict[str, Any]) -> None:
    # `on:` parses as boolean True (YAML 1.1). main and PRs stay covered.
    triggers = {str(k): v for k, v in workflow.items()}["True"]
    assert triggers["push"]["branches"] == ["**"]
    assert "pull_request" in triggers


def test_newer_push_cancels_the_older_run_of_the_same_branch(
    workflow: dict[str, Any],
) -> None:
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "ci-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] is True


def test_pytest_suite_runs_two_phase_under_xdist(workflow: dict[str, Any]) -> None:
    # The suite is parallel-safe (#385): the bulk runs under `-n auto -m "not serial"`, then the
    # isolation-escaping `serial` tail runs single-process (#328). Both phases always run, and
    # exit 5 (no tests collected) is green, so one red phase cannot hide the other's failures.
    runs = [s.get("run", "") for s in workflow["jobs"]["test"]["steps"]]
    (script,) = [r for r in runs if "pytest" in r]
    lines = [ln.strip() for ln in script.splitlines()]
    parallel = [ln for ln in lines if "-n auto" in ln]
    serial = [ln for ln in lines if "-m serial" in ln]
    assert len(parallel) == 1 and '-m "not serial"' in parallel[0]
    assert "--durations=25" in parallel[0]
    assert len(serial) == 1 and "-n " not in serial[0]
    assert "par_rc" in script and "ser_rc" in script
    assert script.count("-eq 5") == 2


def test_workflow_can_be_rerun_on_a_branch_without_a_noise_commit(
    workflow: dict[str, Any],
) -> None:
    # `gh workflow run CI --ref <branch>` needs workflow_dispatch (#385 proof runs). PyYAML
    # parses the bare `on` key as boolean True.
    keyed: dict[Any, Any] = workflow
    assert "workflow_dispatch" in keyed[True]


def test_test_job_timeout_leaves_headroom_over_the_parallel_run(
    workflow: dict[str, Any],
) -> None:
    # A timeout is reported `cancelled`, which the ready/land gate reads as a failure. The
    # xdist run targets < 5 min; the limit only bounds a hang and must never be tight.
    assert 10 <= workflow["jobs"]["test"]["timeout-minutes"] <= 20


def test_report_red_files_issues_only_for_main_and_prs(
    report_red: dict[str, Any],
) -> None:
    # Every branch is gated now; a red WIP branch must not file a backlog issue.
    condition = report_red["steps"][0]["if"]
    assert "refs/heads/main" in condition
    assert "pull_request" in condition
