"""Unit tests for scripts/orca-lib.sh, the only file that shells out to `orca` (#363)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _orca_stub import install_orca_stub, orca_calls, stub_env

LIB = Path(__file__).resolve().parents[2] / "scripts" / "orca-lib.sh"
FAST = {
    "ORCA_SETTLE_SLEEP": "0",
    "ORCA_SETTLE_TRIES": "3",
    "ORCA_AGENT_SLEEP": "0",
    "ORCA_AGENT_TRIES": "3",
}


def _run(
    tmp_path: Path,
    snippet: str,
    *,
    scenario: dict | None = None,
    version: str = "1.4.218",
    stub: bool = True,
) -> tuple[subprocess.CompletedProcess, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **FAST)
    if stub:
        env.update(install_orca_stub(bindir, scenario=scenario, version=version))
        env = stub_env(bindir, env)
    else:
        env["PATH"] = f"{bindir}:/usr/bin:/bin"
    proc = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {snippet}'], capture_output=True, text=True, env=env
    )
    return proc, bindir


def test_require_version_passes_at_the_floor(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "orca_require_version")

    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("version", ["1.5.0", "1.10.0", "2.0.1"])
def test_require_version_passes_above_the_floor(tmp_path: Path, version: str) -> None:
    proc, _ = _run(tmp_path, "orca_require_version", version=version)

    assert proc.returncode == 0, proc.stderr


def test_require_version_fails_loud_below_the_floor(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "orca_require_version", version="1.4.217")

    assert proc.returncode == 1
    assert "1.4.217" in proc.stderr
    assert "1.4.218" in proc.stderr


def test_require_version_fails_loud_when_orca_is_missing(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "orca_require_version", stub=False)

    assert proc.returncode == 1
    assert "orca" in proc.stderr


def test_orca_json_always_passes_json_and_captures_stdout_and_rc(tmp_path: Path) -> None:
    scenario = {"worktree list": [{"rc": 3, "out": {"ok": False}, "stderr": "boom"}]}

    proc, bindir = _run(
        tmp_path,
        'orca_json worktree list; echo "rc=$ORCA_RC out=$ORCA_OUT err=$ORCA_ERR"',
        scenario=scenario,
    )

    assert "rc=3" in proc.stdout
    assert '"ok": false' in proc.stdout
    assert "err=boom" in proc.stdout
    assert orca_calls(bindir) == [["worktree", "list", "--json"]]


def test_orca_json_treats_ok_false_as_a_failure_even_with_exit_zero(tmp_path: Path) -> None:
    scenario = {"worktree list": [{"rc": 0, "out": {"ok": False}}]}

    proc, _ = _run(tmp_path, "orca_json worktree list", scenario=scenario)

    assert proc.returncode == 1


def test_extractors_read_worktree_and_dispatch_fields(tmp_path: Path) -> None:
    scenario = {
        "worktree create": [
            {"out": {"ok": True, "result": {"worktree": {"id": "r::/p", "path": "/p"}}}}
        ],
        "orchestration worker-start": [{"out": {"ok": True, "result": {"dispatchId": "ctx_9"}}}],
    }

    proc, _ = _run(
        tmp_path,
        'orca_json worktree create; echo "$(orca_wt_path) $(orca_wt_id)"; '
        "orca_json orchestration worker-start; orca_dispatch_id",
        scenario=scenario,
    )

    assert proc.stdout.split() == ["/p", "r::/p", "ctx_9"]


def test_settled_runtime_unavailable_checks_request_show_and_never_reissues(tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {
                "rc": 1,
                "out": {
                    "ok": False,
                    "error": {
                        "code": "runtime_unavailable",
                        "data": {"orchestrationRequestId": "req-1"},
                    },
                },
            }
        ],
        "orchestration request-show": [
            {"out": {"ok": True, "result": {"state": "pending"}}},
            {
                "out": {
                    "ok": True,
                    "result": {
                        "state": "completed",
                        "receipt": {"dispatchId": "ctx_late", "state": "ready"},
                    },
                }
            },
        ],
    }

    proc, bindir = _run(
        tmp_path,
        'orca_call_settled "" orchestration worker-start --spec x && orca_dispatch_id',
        scenario=scenario,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ctx_late"
    mutations = [c for c in orca_calls(bindir) if c[:2] == ["orchestration", "worker-start"]]
    assert len(mutations) == 1


def test_settled_without_a_request_id_falls_back_to_the_probe(tmp_path: Path) -> None:
    scenario = {
        "worktree create": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "runtime_unavailable"}}}
        ],
        "worktree list": [
            {
                "out": {
                    "ok": True,
                    "result": {"worktrees": [{"id": "r::/w", "path": "/w", "displayName": "n1"}]},
                }
            }
        ],
    }

    proc, bindir = _run(
        tmp_path,
        "probe() { orca_worktree_by_name n1; }; "
        "orca_call_settled probe worktree create --name n1 && orca_wt_path",
        scenario=scenario,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/w"
    assert [c for c in orca_calls(bindir) if c[:2] == ["worktree", "create"]] == [
        ["worktree", "create", "--name", "n1", "--json"]
    ]


def test_settled_gives_up_after_the_budget_without_reissuing(tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {
                "rc": 1,
                "out": {
                    "ok": False,
                    "error": {
                        "code": "outcome_unknown",
                        "data": {"orchestrationRequestId": "req-2"},
                    },
                },
            }
        ],
        "orchestration request-show": [{"out": {"ok": True, "result": {"state": "absent"}}}],
    }

    proc, bindir = _run(
        tmp_path, 'orca_call_settled "" orchestration worker-start', scenario=scenario
    )

    assert proc.returncode == 1
    assert len([c for c in orca_calls(bindir) if c[:2] == ["orchestration", "worker-start"]]) == 1


def test_settled_returns_a_definite_failure_at_once(tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "consumer_fenced"}}}
        ]
    }

    proc, bindir = _run(
        tmp_path, 'orca_call_settled "" orchestration worker-start', scenario=scenario
    )

    assert proc.returncode == 1
    assert [c[:2] for c in orca_calls(bindir)] == [["orchestration", "worker-start"]]


def test_run_id_prints_the_bound_run_and_fails_without_one(tmp_path: Path) -> None:
    bound, _ = _run(tmp_path / "a", "orca_run_id")
    unbound, _ = _run(
        tmp_path / "b",
        "orca_run_id",
        scenario={"orchestration run-current": [{"out": {"ok": True, "result": {"run": None}}}]},
    )

    assert bound.stdout.strip() == "run_stub"
    assert unbound.returncode == 1


def test_wait_agent_polls_until_claude_is_detected(tmp_path: Path) -> None:
    show = [
        {"out": {"ok": True, "result": {"terminal": {"agentIdentity": None}}}},
        {"out": {"ok": True, "result": {"terminal": {"agentIdentity": "claude"}}}},
    ]

    proc, bindir = _run(tmp_path, "orca_wait_agent term_1", scenario={"terminal show": show})

    assert proc.returncode == 0
    assert len(orca_calls(bindir)) == 2


def test_wait_agent_fails_when_no_agent_ever_appears(tmp_path: Path) -> None:
    show = [{"out": {"ok": True, "result": {"terminal": {"agentIdentity": None}}}}]

    proc, _ = _run(tmp_path, "orca_wait_agent term_1", scenario={"terminal show": show})

    assert proc.returncode == 1


def test_blocked_reason_is_extracted_from_any_depth(tmp_path: Path) -> None:
    out = '{"ok":false,"error":{"data":{"wait":{"blockedReason":"agent-trust-workspace"}}}}'

    proc, _ = _run(tmp_path, f"ORCA_OUT='{out}'; orca_blocked_reason")

    assert proc.stdout.strip() == "agent-trust-workspace"


def test_field_extraction_never_kills_a_set_e_caller_on_non_json(tmp_path: Path) -> None:
    proc, _ = _run(
        tmp_path,
        "set -e; ORCA_OUT='Error: closed the connection'; x=\"$(orca_wt_path)\"; echo alive",
    )

    assert proc.stdout.strip() == "alive"


def test_a_closed_connection_reported_only_on_stderr_settles_through_the_probe(
    tmp_path: Path,
) -> None:
    scenario = {
        "worktree create": [
            {"rc": 1, "out": "", "stderr": "orca: the runtime closed the connection"}
        ],
        "worktree list": [
            {
                "out": {
                    "ok": True,
                    "result": {"worktrees": [{"id": "r::/w", "path": "/w", "displayName": "n1"}]},
                }
            }
        ],
    }

    proc, _ = _run(
        tmp_path,
        "probe() { orca_worktree_by_name n1; }; orca_call_settled probe worktree create --name n1",
        scenario=scenario,
    )

    assert proc.returncode == 0, proc.stderr


def test_giving_up_leaves_the_original_error_in_orca_out(tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {
                "rc": 1,
                "out": {
                    "ok": False,
                    "error": {
                        "code": "outcome_unknown",
                        "data": {"orchestrationRequestId": "req-3"},
                    },
                },
            }
        ],
        "orchestration request-show": [{"out": {"ok": True, "result": {"state": "absent"}}}],
    }

    proc, _ = _run(
        tmp_path,
        'orca_call_settled "" orchestration worker-start; orca_field ".error.code"',
        scenario=scenario,
    )

    assert proc.stdout.strip() == "outcome_unknown"


def test_with_no_request_id_and_no_probe_an_unsettled_call_is_unknown_at_once(
    tmp_path: Path,
) -> None:
    scenario = {
        "orchestration worker-start": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "runtime_unavailable"}}}
        ]
    }

    proc, bindir = _run(
        tmp_path, 'orca_call_settled "" orchestration worker-start', scenario=scenario
    )

    assert proc.returncode == 1
    assert [c[:2] for c in orca_calls(bindir)] == [["orchestration", "worker-start"]]
