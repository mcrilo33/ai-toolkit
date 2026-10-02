"""Entry-lib tests for shared/skills/hub/scripts/gate-broker.sh (issue #275).

The behavior-neutral module split (#275) moved each stage's behavioral tests into
test_gate_broker_<module>.py; what remains exercises the entry lib itself: the fail-closed
module source loop, cross-module sourceability, the orchestrator, the Orca delivery verbs, and QCM.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from _gate_broker_support import (
    GATE_BROKER,
    HUB_INJECT,
    WT_LIB,
    _call,
    _session,
    _tag_gate_at_head,
)
from _orca_stub import install_forbidden_stubs, orca_calls, orca_park
from _stubs import write_stub


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path, monkeypatch):
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


# ── the core is sourceable on its own ─────────────────────────────────────────


def test_gate_broker_defines_the_core() -> None:
    # Sourcing the module alone must define the shared-core public surface — the proof
    # the core stands on its own, not just as a fragment of hub-afk.sh.
    result = _call(
        "for fn in broker_service_gate parse_decision classify_permission "
        "extract_pending_question deliver_answer _escalate_blocked; do "
        'command -v "$fn" >/dev/null || { echo "missing: $fn"; exit 1; }; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_gate_broker_lib_is_sourced_once_per_module() -> None:
    # Issue #276: the coprocess sources gate-broker.sh exactly ONCE and reuses it — the whole
    # point of the source-once harness. Multiple _call invocations must not re-parse the
    # multi-thousand-line lib.
    session = _session()
    _call("true")
    _call("true")
    assert session.source_count == 1


def test_hub_inject_loads_under_foreign_script_dir(tmp_path: Path) -> None:
    # The /afk self-copy supervisor runs hub-afk.sh from a temp dir and passes that dir
    # down as SCRIPT_DIR; gate-broker.sh inherits it. hub-inject.sh (which #255 split the
    # transcript locators into) is ALWAYS a co-located sibling of gate-broker.sh, so it
    # must resolve from gate-broker's OWN location — not the inherited SCRIPT_DIR, which
    # points at a temp dir holding only hub-afk.sh. Without that, every moved helper is
    # undefined and the drain services nothing (issue #262). AFK_HUB_INJECT is emptied so
    # only the built-in resolution is exercised.
    result = _call(
        "for fn in deliver_text _transcript_mtime _spoke_jsonl "
        '_transcript_sizes; do command -v "$fn" >/dev/null || { echo "missing: $fn"; '
        "exit 1; }; done; echo OK",
        env={"SCRIPT_DIR": str(tmp_path), "AFK_HUB_INJECT": ""},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "command not found" not in result.stderr, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_hub_inject_resolves_via_own_dir_without_toplevel(tmp_path: Path) -> None:
    # Lock the PRIMARY resolution mechanism (issue #262): gate-broker must find its
    # co-located hub-inject.sh from its OWN _GB_DIR even when the _AFK_TOPLEVEL fallback is
    # unavailable — a synced-layout self-copy launched from a cwd OUTSIDE any git repo, with
    # a foreign SCRIPT_DIR and no override. Without _GB_DIR this strands, so the later
    # _AFK_TOPLEVEL candidates only LOOK like a safety net; this guards against a future
    # change that drops _GB_DIR but keeps the toplevel fallback.
    env = {
        **os.environ,
        "TZ": "UTC",
        "SCRIPT_DIR": str(tmp_path / "fake"),
        "AFK_HUB_INJECT": "",
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{GATE_BROKER}"; command -v deliver_text >/dev/null '
            "&& command -v _transcript_sizes >/dev/null && echo OK",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "command not found" not in result.stderr, result.stderr
    assert result.stdout.strip() == "OK"


def test_missing_required_module_fails_closed(tmp_path: Path) -> None:
    # #275 / #211: the entry lib sources gate-broker-*.sh modules that back the deny-wall.
    # A required module that cannot be resolved must NOT leave a partial API that silently
    # drops the wall (the #262 no-wall bypass-spoke failure). Copy the entry lib ALONE into a
    # module-less dir (no gate-broker-*.sh sibling) and point _AFK_TOPLEVEL at a nonexistent
    # tree so no resolution candidate hits: the fail-closed override must make
    # afk_danger_guard_decide DENY and afk_permission_hook_decide a silent no-op.
    broker = tmp_path / "gate-broker.sh"
    broker.write_bytes(GATE_BROKER.read_bytes())  # NB: no gate-broker-*.sh copied alongside
    env = {
        **os.environ,
        "TZ": "UTC",
        "AFK_WT_LIB": str(WT_LIB),
        "AFK_HUB_INJECT": str(HUB_INJECT),
        "_AFK_TOPLEVEL": str(tmp_path / "no-such-toplevel"),
    }
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "cwd": str(tmp_path)}
    )

    deny = subprocess.run(
        ["bash", "-c", f'source "{broker}"; afk_danger_guard_decide'],
        capture_output=True,
        text=True,
        env=env,
        input=payload,
        cwd=str(tmp_path),
    )
    assert '"permissionDecision":"deny"' in deny.stdout, deny.stdout + deny.stderr
    assert "failing closed" in deny.stdout

    allow = subprocess.run(
        ["bash", "-c", f'source "{broker}"; afk_permission_hook_decide'],
        capture_output=True,
        text=True,
        env=env,
        input=payload,
        cwd=str(tmp_path),
    )
    assert allow.stdout.strip() == "", allow.stdout  # never auto-approve when broken


def test_parse_decision_extracts_answer() -> None:
    result = _call("parse_decision 'reasoning here\nANSWER: use Redis'")

    assert result.returncode == 0, result.stderr
    kind, _, text = result.stdout.strip().partition("\t")
    assert kind == "ANSWER"
    assert text == "use Redis"


@pytest.mark.parametrize(
    "cmd,verdict",
    [
        ("git add tests/x.py", "APPROVE"),
        ("git reset -q; git add tests/x.py", "APPROVE"),
        ("git push origin main", "ESCALATE"),
        ("rm -rf tests", "ESCALATE"),
    ],
)
def test_classify_permission_via_broker(cmd: str, verdict: str) -> None:
    result = _call('classify_permission "$CMD" | cut -f1', env={"CMD": cmd})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == verdict


def test_broker_service_gate_unattended_warns_on_escalate(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # #241: a human-decision (ESCALATE) reply no longer parks the spoke blocked/<issue> — the
    # unattended adapter WARNS loudly and keeps the spoke serviced (retried on the backoff).
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: needs a human'"}

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text() if Path(env["_READY_LOG"]).exists() else ""
    assert "--blocked 5" not in log, "an ESCALATE reply must warn-and-continue, not park"
    assert "WARNING: #5" in result.stderr, result.stderr


def test_broker_service_gate_defaults_to_unattended(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # Called with no mode arg, it behaves as the unattended adapter (back-compat with
    # decide_and_act, which passes no third argument through its thin wrapper).
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ESCALATE: human call'"}

    result = _call(f"broker_service_gate '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    _rl = Path(env["_READY_LOG"])
    assert not _rl.exists() or "--blocked 5" not in _rl.read_text()
    assert "WARNING: #5" in result.stderr, result.stderr


def test_build_qcm_writes_structured_surface(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir)}

    r = _call(
        "build_qcm 7 'PLAN: extract the core then wire it' 'This is a scope call — your decision'",
        env=env,
    )
    assert r.returncode == 0, r.stderr

    surface = _call("_broker_qcm_surface 7", env=env).stdout.strip()
    txt = Path(surface).read_text()
    assert "PLAN: extract the core then wire it" in txt, txt
    assert "scope call" in txt, "the reviewer advice must appear in the surface"
    assert "reply" in txt.lower(), "the freeform-escape instruction must appear"


def test_present_qcm_empty_reply_defers_to_block(
    spoke_repo: Path, tmp_path: Path, orca_bin: Path
) -> None:
    orca_park(orca_bin, spoke_repo, question="Q: Which store?\n  - Redis: fast")
    env, ready_log = _qcm_env(tmp_path)

    result = _call(f"_broker_present_qcm '{spoke_repo}' 5 'your call'", env=env, stdin="\n")

    assert result.returncode == 0, result.stderr
    assert "--blocked 5" in ready_log.read_text(), "an empty reply defers the gate (escalate)"
    assert not _calls_to(orca_bin, "orchestration", "reply")


@pytest.mark.parametrize(
    "typed,body",
    [
        pytest.param(
            "Approved — proceed with option A.\n", "Approved — proceed with option A.", id="newline"
        ),
        # Typed then Ctrl-D: `read` returns non-zero with $reply populated; still a real approval.
        pytest.param("Go with Redis", "Go with Redis", id="ctrl-d-no-newline"),
    ],
)
def test_present_qcm_replies_to_the_recorded_question(
    spoke_repo: Path, tmp_path: Path, orca_bin: Path, typed: str, body: str
) -> None:
    # The interactive per-gate context presents the QCM, reads the human's reply HERE (stdin) and
    # delivers it through the shared deliver_answer: an Orca reply to the recorded question id.
    orca_park(orca_bin, spoke_repo, question="Q: Which store?\n  - Redis: fast", qid="m_q5")
    env, ready_log = _qcm_env(tmp_path)

    result = _call(
        f"_broker_present_qcm '{spoke_repo}' 5 'This changes scope -- your call'",
        env=env,
        stdin=typed,
    )

    assert result.returncode == 0, result.stderr
    replies = _calls_to(orca_bin, "orchestration", "reply")
    assert [(_flag(c, "--id"), _flag(c, "--body")) for c in replies] == [("m_q5", body)]
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text(), "must not escalate"


def test_broker_service_gate_attended_presents_qcm_on_human_decision(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], orca_bin: Path
) -> None:
    # End to end: the reasoner escalates (human call), ATTENDED mode routes to the interactive QCM
    # instead of blocking, and the human's reply is delivered to the spoke's question.
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: this is genuinely your call'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 attended", env=env, stdin="Use Redis.\n")

    assert result.returncode == 0, result.stderr
    replies = _calls_to(orca_bin, "orchestration", "reply")
    assert [_flag(c, "--body") for c in replies] == ["Use Redis."]
    assert "--blocked" not in _read(env["_READY_LOG"]), "attended must present a QCM"


def _calls_to(orca_bin: Path, noun: str, verb: str) -> list[list[str]]:
    return [c for c in orca_calls(orca_bin) if c[:2] == [noun, verb]]


def _read(path: str | Path) -> str:
    return Path(path).read_text() if Path(path).exists() else ""


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _qcm_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Env with a recording spoke-ready stub; returns it with the log the stub appends to."""
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    write_stub(ready_stub, f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    return {"SPOKE_READY": str(ready_stub)}, ready_log


def test_gate_answer_is_delivered_by_reply_to_the_recorded_question_id(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], orca_bin: Path, tmp_path: Path
) -> None:
    # The answer lane replies to the inbox question id recorded at the park -- never types into
    # a terminal (no tmux, no `terminal send`): the reply ack is the proof of delivery.
    orca_park(orca_bin, spoke_repo, question="Q: Which store?\n  - Redis: fast", qid="m_gate_7")
    (tmp_path / "forbid").mkdir()
    forbidden = install_forbidden_stubs(tmp_path / "forbid")
    env = {
        **waiting_spoke_env,
        "PATH": f"{forbidden.parent}:{waiting_spoke_env['PATH']}",
        "AFK_ANSWERER_CMD": "printf 'reasoning\\nANSWER: use Redis'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    replies = _calls_to(orca_bin, "orchestration", "reply")
    assert [(_flag(c, "--id"), _flag(c, "--body")) for c in replies] == [("m_gate_7", "use Redis")]
    assert not _calls_to(orca_bin, "terminal", "send"), (
        "a question reply never types into a terminal"
    )
    assert forbidden.read_text() == "", "the Orca path must never call tmux"


def test_unacked_delivery_warns_and_continues_without_resending(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], orca_bin: Path
) -> None:
    refused = {
        "rc": 1,
        "out": {"ok": False, "error": {"code": "message_not_found"}},
        "stderr": "no\n",
    }
    orca_park(
        orca_bin,
        spoke_repo,
        question="Q: Which store?\n  - Redis: fast",
        extra={"orchestration reply": [refused]},
    )
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'reasoning\\nANSWER: use Redis'"}

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert "WARNING: #5" in result.stderr, result.stderr
    assert "--blocked 5" not in _read(env["_READY_LOG"])
    assert len(_calls_to(orca_bin, "orchestration", "reply")) == 1, "an unacked reply is not resent"
    assert not _calls_to(orca_bin, "terminal", "send"), "no fallback typing after a refused reply"


def test_agent_state_alone_never_makes_a_gate_tag_a_park(spoke_repo: Path, orca_bin: Path) -> None:
    # A gate/<n> tag at the tip with a WORKING agent and an empty inbox is not a park: only an
    # unread inbox question (or a waiting permission dialog) classifies a spoke as waiting.
    orca_park(orca_bin, spoke_repo, state="working")
    _tag_gate_at_head(spoke_repo, 5)

    result = _call(f"slot_state '{spoke_repo}' 5")

    assert result.stdout.strip().splitlines()[-1] == "busy", result.stdout + result.stderr
