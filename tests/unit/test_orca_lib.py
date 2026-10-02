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


def test_the_worktree_probe_is_scoped_to_the_given_repo(tmp_path: Path) -> None:
    scenario = {
        "worktree list": [
            {
                "out": {
                    "ok": True,
                    "result": {"worktrees": [{"id": "r::/w", "path": "/w", "displayName": "n1"}]},
                }
            }
        ]
    }

    proc, bindir = _run(
        tmp_path, "orca_worktree_by_name n1 path:/the/repo && orca_wt_path", scenario=scenario
    )

    assert proc.stdout.strip() == "/w"
    assert orca_calls(bindir) == [["worktree", "list", "--repo", "path:/the/repo", "--json"]]


# --- the drain's reads and writes (#365) -----------------------------------------------------


def _wt(tmp_path: Path, **identity: str) -> Path:
    wt = tmp_path / "wt"
    (wt / ".ai-toolkit").mkdir(parents=True, exist_ok=True)
    (wt / ".ai-toolkit" / "identity").write_text(
        "".join(f"{k}={v}\n" for k, v in identity.items())
    )
    return wt


def _ps(path: Path, *agents: dict) -> dict:
    return {"out": {"ok": True, "result": {"worktrees": [{"path": str(path), "agents": list(agents)}]}}}


def _workers(*rows: dict) -> dict:
    return {"out": {"ok": True, "result": {"workers": list(rows)}}}


def _worker(did: str, path: Path, verdict: str = "live", handle: str = "term_w", **extra) -> dict:
    return {
        "dispatchId": did,
        "taskId": "task_1",
        "agentTerminalHandle": handle,
        "resource": {"worktreeId": f"repo::{path}"},
        "projection": {"liveness": {"verdict": verdict}},
        **extra,
    }


@pytest.mark.parametrize(
    ("agents", "want"),
    [
        ([{"state": "working"}], "working"),
        ([{"state": "done"}, {"state": "working"}], "working"),
        ([{"state": "working"}, {"state": "waiting"}], "waiting"),
        ([{"state": "done"}], "done"),
        ([], "none"),
    ],
)
def test_agent_state_reads_ps(tmp_path: Path, agents: list[dict], want: str) -> None:
    wt = _wt(tmp_path)

    proc, _ = _run(tmp_path, f'orca_agent_state "{wt}"', scenario={"worktree ps": [_ps(wt, *agents)]})

    assert proc.stdout.strip() == want
    assert proc.returncode == 0


def test_agent_state_is_unknown_when_ps_fails_or_omits_the_worktree(tmp_path: Path) -> None:
    wt = _wt(tmp_path)
    failed = {"worktree ps": [{"rc": 1, "out": {"ok": False}}]}
    unlisted = {"worktree ps": [{"out": {"ok": True, "result": {"worktrees": []}}}]}

    for scenario in (failed, unlisted):
        proc, _ = _run(tmp_path, f'orca_agent_state "{wt}"', scenario=scenario)

        assert proc.stdout.strip() == "unknown"
        assert proc.returncode == 2


def test_agent_field_returns_the_waiting_agents_tool_input(tmp_path: Path) -> None:
    wt = _wt(tmp_path)
    agents = [
        {"state": "working", "toolName": "Read", "toolInput": "/a"},
        {"state": "waiting", "toolName": "Bash", "toolInput": {"command": "git push"}},
    ]

    proc, _ = _run(
        tmp_path,
        f'orca_agent_field "{wt}" toolName; orca_agent_field "{wt}" toolInput',
        scenario={"worktree ps": [_ps(wt, *agents)]},
    )

    assert proc.stdout.splitlines() == ["Bash", '{"command":"git push"}']


def test_tick_cache_makes_one_ps_call_until_reset(tmp_path: Path) -> None:
    wt = _wt(tmp_path)
    snippet = (
        f'orca_tick_reset; orca_agent_state "{wt}"; x="$(orca_agent_state "{wt}")"; '
        f'orca_agent_field "{wt}" state; orca_tick_reset; orca_agent_state "{wt}"'
    )

    _, bindir = _run(tmp_path, snippet, scenario={"worktree ps": [_ps(wt, {"state": "done"})]})

    assert orca_calls(bindir).count(["worktree", "ps", "--json"]) == 2


def test_worker_liveness_matches_the_identity_dispatch_then_the_path(tmp_path: Path) -> None:
    wt = _wt(tmp_path, orca_dispatch_id="ctx_new")
    other = tmp_path / "other"
    rows = _workers(
        _worker("ctx_old", wt, "exited"), _worker("ctx_new", wt, "live"), _worker("ctx_x", other)
    )

    by_id, _ = _run(tmp_path, f'orca_worker_liveness "{wt}"', scenario={"orchestration worker-list": [rows]})
    bare = _wt(tmp_path / "b")
    by_path, _ = _run(
        tmp_path / "b",
        f'orca_worker_liveness "{bare}"',
        scenario={"orchestration worker-list": [_workers(_worker("ctx_9", bare, "unverifiable"))]},
    )

    assert by_id.stdout.strip() == "live"
    assert by_path.stdout.strip() == "unverifiable"


def test_worker_liveness_of_an_absent_record_is_unknown_never_exited(tmp_path: Path) -> None:
    wt = _wt(tmp_path)
    failed = {"orchestration worker-list": [{"rc": 1, "out": {"ok": False}}]}
    absent = {"orchestration worker-list": [_workers()]}

    for scenario, rc in ((failed, 2), (absent, 1)):
        proc, _ = _run(tmp_path, f'orca_worker_liveness "{wt}"', scenario=scenario)

        assert proc.stdout.strip() == "unknown"
        assert proc.returncode == rc


def test_inbox_question_joins_the_sender_handle_to_the_spoke(tmp_path: Path) -> None:
    wt = _wt(tmp_path, run_id="run_1")
    inbox = {
        "out": {
            "ok": True,
            "result": {
                "messages": [
                    {"id": "m_other", "type": "question", "from_handle": "term_z", "body": "no", "created_at": 1},
                    {"id": "m_late", "type": "question", "from_handle": "term_w", "body": "b", "created_at": 9},
                    {"id": "m_early", "type": "question", "from_handle": "term_w", "body": "plan?", "created_at": 5},
                ]
            },
        }
    }
    scenario = {"orchestration worker-list": [_workers(_worker("ctx_1", wt))], "orchestration check": [inbox]}

    proc, bindir = _run(
        tmp_path,
        f'orca_inbox_question "{wt}" id; orca_inbox_question "{wt}" body',
        scenario=scenario,
    )

    assert proc.stdout.splitlines() == ["m_early", "plan?"]
    assert ["orchestration", "check", "--peek", "--types", "question", "--run", "run_1", "--json"] in orca_calls(bindir)


def test_inbox_question_is_rc1_when_empty_and_rc2_when_unknown(tmp_path: Path) -> None:
    wt = _wt(tmp_path)
    workers = {"orchestration worker-list": [_workers(_worker("ctx_1", wt))]}
    empty = {**workers, "orchestration check": [{"out": {"ok": True, "result": {"messages": []}}}]}
    broken = {**workers, "orchestration check": [{"rc": 1, "out": {"ok": False}}]}

    for scenario, rc in ((empty, 1), (broken, 2)):
        proc, _ = _run(tmp_path, f'orca_inbox_question "{wt}" id', scenario=scenario)

        assert proc.returncode == rc
        assert proc.stdout == ""


def test_every_mutation_carries_json_and_its_own_retry_request(tmp_path: Path) -> None:
    snippet = (
        'orca_reply m1 yes run_1; orca_send_text term_w hello 15 1 >/dev/null; '
        "orca_worker_stop d1; orca_worker_abandon d2; orca_worker_release d3"
    )

    _, bindir = _run(tmp_path, snippet)

    calls = orca_calls(bindir)
    assert [c[:2] for c in calls] == [
        ["orchestration", "reply"],
        ["terminal", "send"],
        ["orchestration", "worker-stop"],
        ["orchestration", "worker-abandon"],
        ["orchestration", "worker-release"],
    ]
    ids = [c[c.index("--retry-request") + 1] for c in calls]
    assert all(c[-1] == "--json" for c in calls)
    assert len(set(ids)) == 5


def test_send_text_prints_the_observed_stages_in_one_call(tmp_path: Path) -> None:
    proc, bindir = _run(tmp_path, "orca_send_text term_w hi 20 1")

    assert proc.stdout.strip() == "input_accepted,turn_started"
    (call,) = orca_calls(bindir)
    assert call[:7] == ["terminal", "send", "--terminal", "term_w", "--text", "hi", "--enter"]
    assert call[call.index("--wait-submit") + 1] == "20"


def test_send_text_without_enter_or_wait_sends_only_the_text(tmp_path: Path) -> None:
    _, bindir = _run(tmp_path, "orca_send_text term_w x 0 0 >/dev/null")

    (call,) = orca_calls(bindir)
    assert "--enter" not in call
    assert "--wait-submit" not in call


def test_send_text_failure_is_rc1_and_never_resent(tmp_path: Path) -> None:
    scenario = {"terminal send": [{"rc": 1, "out": {"ok": False, "error": {"code": "terminal_gone"}}}]}

    proc, bindir = _run(tmp_path, "orca_send_text term_w hi", scenario=scenario)

    assert proc.returncode == 1
    assert len(orca_calls(bindir)) == 1


def test_worker_done_sends_the_outcome_with_the_dispatch_and_task(tmp_path: Path) -> None:
    wt = _wt(tmp_path, orca_dispatch_id="ctx_1")
    scenario = {"orchestration worker-list": [_workers(_worker("ctx_1", wt))]}

    proc, bindir = _run(tmp_path, f'orca_worker_done "{wt}" succeeded "ready/365"', scenario=scenario)

    assert proc.returncode == 0, proc.stderr
    send = next(c for c in orca_calls(bindir) if c[:2] == ["orchestration", "send"])
    assert send[send.index("--type") + 1] == "worker_done"
    assert send[send.index("--outcome") + 1] == "succeeded"
    assert send[send.index("--dispatch-id") + 1] == "ctx_1"
    assert send[send.index("--task-id") + 1] == "task_1"


def test_worker_done_without_a_recorded_dispatch_is_a_quiet_rc1(tmp_path: Path) -> None:
    wt = _wt(tmp_path)

    proc, bindir = _run(tmp_path, f'orca_worker_done "{wt}" failed "blocked/365"')

    assert proc.returncode == 1
    assert orca_calls(bindir) == []
