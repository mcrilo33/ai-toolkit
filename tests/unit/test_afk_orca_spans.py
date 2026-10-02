"""S7 measurement (#365): the drain emits Orca lifecycle / human / liveness spans per spoke.

Existing span kinds only (`human`, `lifecycle`, `script`), emitted with the spoke's worktree as
cwd so they land under its spoke_run_id; best-effort: an emit failure never fails the caller.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _hub_afk_support import _call
from _orca_stub import orca_park

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="hub-afk.sh targets macOS")


def _spoke(tmp_path: Path) -> Path:
    wt = tmp_path / "spoke"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature/311-x", str(wt)], check=True)
    (wt / ".ai-toolkit").mkdir()
    (wt / ".ai-toolkit" / "identity").write_text("issue=311\norca_dispatch_id=ctx_1\n")
    return wt


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "AI_TOOLKIT_TELEMETRY": "1",
        "AI_TOOLKIT_TELEMETRY_DIR": str(tmp_path / "tele"),
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": "",
        "AFK_STATE_DIR": str(tmp_path / "afk"),
        "AFK_NOW": "1000",
        **extra,
    }


def _spans(tmp_path: Path) -> list[dict]:
    log = tmp_path / "tele" / "events.jsonl"
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def test_an_acked_gate_reply_emits_a_human_gate_span_with_the_wait(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _spoke(tmp_path)
    orca_park(orca_bin, wt, question="plan?", qid="m1")

    result = _call(f'deliver_reply "{wt}" m1 approved 400000', env=_env(tmp_path))

    assert result.returncode == 0, result.stderr
    (span,) = [s for s in _spans(tmp_path) if s.get("kind") == "human"]
    assert span["name"] == "orca-gate-wait"
    assert span["human"] == {"type": "gate", "wait_ms": 600000}


def test_an_iso_created_at_still_yields_the_gate_wait(tmp_path: Path, orca_bin: Path) -> None:
    # Orca's messages carry created_at as ISO-8601 UTC, not epoch milliseconds.
    wt = _spoke(tmp_path)
    orca_park(orca_bin, wt, question="plan?", qid="m1")

    _call(f'deliver_reply "{wt}" m1 approved 1970-01-01T00:16:40Z', env=_env(tmp_path))

    (span,) = [s for s in _spans(tmp_path) if s.get("kind") == "human"]
    assert span["human"] == {"type": "gate", "wait_ms": 0}  # AFK_NOW=1000s == the ask time


def test_an_acked_permission_approve_emits_a_human_permission_span(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _spoke(tmp_path)
    orca_park(orca_bin, wt, state="waiting", tool="Bash", tool_input="git status")

    result = _call(f'approve_permission "{wt}"', env=_env(tmp_path))

    assert result.returncode == 0, result.stderr
    (span,) = [s for s in _spans(tmp_path) if s.get("kind") == "human"]
    assert span["human"] == {"type": "permission", "wait_ms": 999000}


def test_a_refused_reply_emits_no_human_span(tmp_path: Path, orca_bin: Path) -> None:
    wt = _spoke(tmp_path)
    orca_park(
        orca_bin,
        wt,
        question="plan?",
        qid="m1",
        extra={"orchestration reply": [{"rc": 1, "out": {"ok": False}}]},
    )

    result = _call(f'deliver_reply "{wt}" m1 approved 400000', env=_env(tmp_path))

    assert result.returncode == 1
    assert [s for s in _spans(tmp_path) if s.get("kind") == "human"] == []


def test_slot_state_emits_the_first_agent_state_once_and_liveness_on_change_only(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _spoke(tmp_path)
    orca_park(orca_bin, wt, state="working", liveness="live")
    env = _env(tmp_path)

    _call(f'slot_state "{wt}" 311; slot_state "{wt}" 311', env=env)
    orca_park(orca_bin, wt, state="working", liveness="exited")
    _call(f'slot_state "{wt}" 311', env=env)

    names = [s["name"] for s in _spans(tmp_path)]
    assert names.count("orca:first-agent-state") == 1
    assert names.count("orca:liveness-live") == 1
    assert names.count("orca:liveness-exited") == 1
    assert {s["kind"] for s in _spans(tmp_path)} == {"lifecycle", "script"}


def test_the_release_span_is_emitted_before_the_worker_is_released(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _spoke(tmp_path)
    log = tmp_path / "order.log"
    orca_park(orca_bin, wt, state="done", liveness="exited")
    done = tmp_path / "worktree-done.sh"
    done.write_text("#!/bin/sh\nexit 0\n")

    _call(
        f'_hi_span() {{ echo "span $*" >> "{log}"; }}; '
        f'orca_worker_abandon() {{ echo abandon >> "{log}"; }}; '
        f'orca_worker_release() {{ echo release >> "{log}"; }}; '
        f'_redispatch_exited_spoke "{wt}" 311',
        env=_env(tmp_path, WT_DONE=str(done)),
    )

    order = log.read_text().splitlines()
    assert "--kind lifecycle --name orca:release" in order[0]
    assert order[1:] == ["abandon", "release"]


def test_a_failing_emitter_never_fails_the_delivery_or_the_tick(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _spoke(tmp_path)
    orca_park(orca_bin, wt, question="plan?", qid="m1")
    broken = "telemetry_emit_span() { return 1; }; "

    reply = _call(f'{broken}deliver_reply "{wt}" m1 ok 400000', env=_env(tmp_path))
    tick = _call(f'{broken}slot_state "{wt}" 311', env=_env(tmp_path))

    assert reply.returncode == 0, reply.stderr
    assert tick.returncode == 0, tick.stderr
