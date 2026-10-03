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

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

MACOS_JOB = "shell-control-plane-macos"
GATE_JOBS = {"test", "shellcheck", "sync-idempotency", "pyright", MACOS_JOB}


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


def test_pytest_suite_runs_serially_until_the_suite_is_xdist_safe(
    workflow: dict[str, Any],
) -> None:
    # `-n auto` surfaced a new timing flake on every CI attempt (#378, epic #377), so the Linux
    # suite stays single-process; CI is the gate and must be reproducible. Re-enable xdist
    # (and the `serial` tail, #328) only once #377 lands, updating this test with it.
    runs = [s.get("run", "") for s in workflow["jobs"]["test"]["steps"]]
    pytest_runs = [r for r in runs if "pytest" in r]
    assert pytest_runs == ["python -m pytest tests/ -q"]
    assert not any("-n " in r or "xdist" in r for r in runs)
    # Serial takes ~9-10 min: a 10-minute limit times out (reported `cancelled`, which the
    # ready/land gate reads as a failure) on ~40% of runs, so the timeout must leave headroom.
    assert workflow["jobs"]["test"]["timeout-minutes"] >= 15


def test_report_red_files_issues_only_for_main_and_prs(
    report_red: dict[str, Any],
) -> None:
    # Every branch is gated now; a red WIP branch must not file a backlog issue.
    condition = report_red["steps"][0]["if"]
    assert "refs/heads/main" in condition
    assert "pull_request" in condition


def test_no_job_is_allowed_to_fail_the_run_silently(workflow: dict[str, Any]) -> None:
    # The ready/land gate reads the RUN-level conclusion, which a `continue-on-error` job cannot
    # turn red. Every job is therefore part of "CI green" only while none of them carries it (#387).
    soft = [name for name, job in workflow["jobs"].items() if "continue-on-error" in job]
    assert soft == []


def test_the_macos_job_is_a_blocking_job_of_the_one_workflow(workflow: dict[str, Any]) -> None:
    # One workflow means one run-level conclusion for ready/land to read: the macOS job must live
    # here (not in a second workflow the `--workflow CI` query never sees) and gate the run.
    job = workflow["jobs"][MACOS_JOB]
    assert job["runs-on"] == "macos-15"
    assert job["name"] == "Shell control-plane (macOS, fr_FR.UTF-8)"


def test_report_red_names_the_pyright_and_macos_jobs(report_red: dict[str, Any]) -> None:
    env = report_red["steps"][0]["env"]
    assert env["RESULT_PYRIGHT"] == "${{ needs.pyright.result }}"
    assert env["RESULT_MACOS"] == "${{ needs['shell-control-plane-macos'].result }}"
    script = report_red["steps"][0]["run"]
    assert '"$RESULT_PYRIGHT" = "failure" ] && report "Pyright"' in script
    assert '"$RESULT_MACOS" = "failure" ] && report "Shell control-plane (macOS)"' in script


def test_pyright_job_checks_the_whole_repo_with_a_pinned_version(
    workflow: dict[str, Any],
) -> None:
    # Whole repo, not changed files: the commit gauntlet only checks the files a commit touches,
    # so errors in untouched files surfaced weeks later (#387). Config comes from pyproject.toml.
    job = workflow["jobs"]["pyright"]
    runs = [s.get("run", "") for s in job["steps"]]
    assert any("pip install -r requirements-dev.txt" in r for r in runs)
    assert any(re.search(r"pip install .*pyright==\d+\.\d+\.\d+", r) for r in runs)
    assert [r for r in runs if r.strip().startswith("pyright")] == ["pyright"]


def test_pyright_resolves_the_shared_test_helpers_through_extra_paths() -> None:
    # `_orca_stub` lives in tests/unit and is imported by tests/integration after a runtime
    # sys.path tweak; extraPaths mirrors that, so no per-import ignore is needed (#387).
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["pyright"]
    drain = (REPO_ROOT / "tests" / "integration" / "test_drain_simulation.py").read_text()
    assert "tests/unit" in config["extraPaths"]
    assert "pyright: ignore" not in drain
