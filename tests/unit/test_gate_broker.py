"""Entry-lib tests for shared/skills/hub/scripts/gate-broker.sh (issue #275).

The behavior-neutral module split (#275) moved each stage's behavioral tests into
test_gate_broker_<module>.py; what remains exercises the entry lib itself: the fail-closed
module source loop, cross-module sourceability, the orchestrator, injector, and QCM.
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
    _ask_record,
    _bash_tool_record,
    _call,
    _fake_tmux_pane,
    _gate_bash_turn,
    _gate_broker_env,
    _gate_park_transcript,
    _gate_tool_result,
    _inject_env,
    _perm_env,
    _project_dir_for,
    _resumed_gate_transcript,
    _seed_transcript,
    _session,
    _spoke_await_review_turn,
    _spoke_coded_past_turn,
    _tag_gate_at_head,
    _user_record,
    _write_fake_tmux,
    _write_transcript,
)


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
        "extract_pending_question inject_and_verify _escalate_blocked; do "
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
    # transcript/pane helpers into) is ALWAYS a co-located sibling of gate-broker.sh, so it
    # must resolve from gate-broker's OWN location — not the inherited SCRIPT_DIR, which
    # points at a temp dir holding only hub-afk.sh. Without that, every moved helper is
    # undefined and the drain services nothing (issue #262). AFK_HUB_INJECT is emptied so
    # only the built-in resolution is exercised.
    result = _call(
        "for fn in _pane_shows_permission_prompt _transcript_mtime _spoke_jsonl "
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
            f'source "{GATE_BROKER}"; command -v _pane_shows_permission_prompt >/dev/null '
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


def test_inject_and_verify_registers_when_transcript_advances(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """A genuine submit records the answer as a user turn, and that is what proves delivery.

    #281 raised this stub's fidelity: the submitting Enter used to append a bare `{}`, which
    bumped the mtime without writing the type:"user" record a real Claude Code submit writes.
    The old contract accepted that (advance + a pane not showing the needle = delivered), so
    the stub never had to be honest. It does now — the appended user record is the sole proof.
    The scenario is unchanged; only the fake got truthful.
    """
    answer = "Approved — proceed."
    projects = tmp_path / "projects"
    jsonl = _seed_transcript(projects, spoke_repo)
    record_file = tmp_path / "user-record.json"
    record_file.write_text(_user_record(answer) + "\n")
    fake_bin = _write_fake_tmux(tmp_path, on_enter=f'cat "{record_file}" >> "{jsonl}"')

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", result.stdout + result.stderr


def test_inject_and_verify_rejects_advance_while_needle_still_in_pane(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """#201: a non-turn write bumps the newest jsonl while the paste sits unsubmitted.

    Transcript-advance alone must never score the delivery as success: the needle is
    still in the composer (and was NOT there pre-inject), so the injector must fall
    through to the bare-Enter retry and classify the surviving paste as wedged (rc 2)
    — never rc 0 ("injected answer into #182" while the spoke sat parked 25+ min).
    """
    projects = tmp_path / "projects"
    _seed_transcript(projects, spoke_repo)
    sidecar = _project_dir_for(projects, spoke_repo) / "sidecar.jsonl"  # #182's writer
    pasted = tmp_path / "pasted"
    # The paste wedges in the composer (state file) while a NON-TURN write bumps the
    # project dir's newest jsonl; every Enter is swallowed (the #123/#124 state) and
    # capture-pane keeps showing the answer — the composer never lets go.
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"; printf "{{}}\\n" >> "{sidecar}"',
        on_capture=f'[ -e "{pasted}" ] && echo "Approved — proceed with the plan."',
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 'Approved — proceed with the plan.'; echo RC=$?",
        env=_inject_env(projects, fake_bin),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=2", result.stdout + result.stderr


def test_inject_and_verify_succeeds_when_answer_lands_despite_pane_echo(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """A genuine submit ECHOES the message into the scrollback (`> text`), so the pane
    keeps showing the needle after a real success. The #201 composer-release check must
    accept that: transcript advanced AND the answer landed as a user record => rc 0 —
    never a false wedge that respawns a healthy pane mid-turn.
    """
    answer = 'Approved — proceed with "phase 2".'  # quotes: the JSON-escaped needle path
    projects = tmp_path / "projects"
    jsonl = _seed_transcript(projects, spoke_repo)
    pasted = tmp_path / "pasted"
    record_file = tmp_path / "user-record.json"
    record_file.write_text(_user_record(answer) + "\n")
    echo_file = tmp_path / "echo.txt"
    echo_file.write_text(f"> {answer}\n")
    # The paste shows in the pane, the submitting Enter appends the user record to the
    # session transcript, and the echo KEEPS the needle visible after.
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"',
        on_enter=f'[ -e "{pasted}" ] && cat "{record_file}" >> "{jsonl}"',
        on_capture=f'[ -e "{pasted}" ] && cat "{echo_file}"',
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", result.stdout + result.stderr


def test_inject_and_verify_confirms_repeated_canned_answer(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """The same canned answer already sits in an OLDER transcript record (a previous
    gate of this spoke). Delivery proof is the needle landing in bytes appended AFTER
    the pre-inject snapshot, so the stale copy must neither satisfy the check early
    nor disable it — a genuine submit with its echo still visible is rc 0, never a
    false wedge (#201 review).
    """
    answer = "Approved — proceed with the plan."
    projects = tmp_path / "projects"
    jsonl = _seed_transcript(projects, spoke_repo, content=_user_record(answer) + "\n")
    pasted = tmp_path / "pasted"
    record_file = tmp_path / "user-record.json"
    record_file.write_text(_user_record(answer) + "\n")
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"',
        on_enter=f'[ -e "{pasted}" ] && cat "{record_file}" >> "{jsonl}"',
        on_capture=f'[ -e "{pasted}" ] && echo "> {answer}"',
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", result.stdout + result.stderr


def test_inject_and_verify_wedge_with_preexisting_needle_escalates(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """The answer text is visible in the pane BEFORE the inject (an AskUserQuestion
    option label), the paste wedges, and #182's non-turn write bumps the mtime. The
    pane proves nothing either way (baseline_shows=1) and nothing landed in appended
    transcript bytes, so the injector must report a refuted delivery (rc 3) — never
    success, and never a wedge classified off a pre-existing pane match.
    """
    answer = "Approved — proceed with the plan."
    projects = tmp_path / "projects"
    _seed_transcript(projects, spoke_repo)
    sidecar = _project_dir_for(projects, spoke_repo) / "sidecar.jsonl"
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'printf "{{}}\\n" >> "{sidecar}"',
        on_capture=f'echo "> {answer}"',  # needle visible pre-inject and after
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=3", result.stdout + result.stderr


def test_inject_and_verify_ignores_needle_in_non_user_appended_record(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """A non-turn write that happens to QUOTE the answer text (a re-rendered question
    record with the option label, a foreign sidecar) is not delivery proof — only a
    type:"user" record is. The wedged paste must still classify as rc 2 (#201 review).
    """
    answer = "Approved — proceed with the plan."
    quoting_record = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
        ensure_ascii=False,
    )
    projects = tmp_path / "projects"
    _seed_transcript(projects, spoke_repo)
    sidecar = _project_dir_for(projects, spoke_repo) / "sidecar.jsonl"
    quote_file = tmp_path / "quote.json"
    quote_file.write_text(quoting_record + "\n")
    pasted = tmp_path / "pasted"
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"; cat "{quote_file}" >> "{sidecar}"',
        on_capture=f'[ -e "{pasted}" ] && echo "> {answer}"',
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=2", result.stdout + result.stderr


def test_broker_service_gate_escalates_wedge_despite_advanced_mtime(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    """#201 review (CONFIRMED): the non-turn write that triggers the wedge signature
    also advances the transcript past parked_mtime. The escalation freshness gate must
    not read that EXPLAINED advance as "spoke moved on" — dropping it would leave the
    gate tag in place and re-paste onto the wedged composer every tick, with no
    blocked/<issue> ever stamped.
    """
    pd = _project_dir_for(Path(waiting_spoke_env["CLAUDE_PROJECTS_DIR"]), spoke_repo)
    os.utime(pd / "session.jsonl", (1_000_000_000, 1_000_000_000))
    sidecar = pd / "sidecar.jsonl"
    pasted = tmp_path / "pasted"
    answer = "Approved — use Redis for the store."
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"; printf "{{}}\\n" >> "{sidecar}"',
        on_capture=f'[ -e "{pasted}" ] && echo "> {answer}"',
        pane_path=spoke_repo,
    )
    env = {
        **waiting_spoke_env,
        "PATH": f"{fake_bin}:{waiting_spoke_env['PATH']}",
        "AFK_ANSWERER_CMD": f"printf 'reasoning\\nANSWER: {answer}'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
    }
    # _spoke_pane_target canonicalizes via worktree-lib's wt_realpath; define it here
    # since these tests source gate-broker.sh on its own.
    expr = (
        'wt_realpath() { (cd "$1" 2>/dev/null && pwd -P) || true; }; '
        f"broker_service_gate '{spoke_repo}' 7 unattended"
    )

    result = _call(expr, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    _rl = Path(env["_READY_LOG"])
    log = _rl.read_text() if _rl.exists() else ""
    # #241: an unrecoverable wedge no longer parks blocked/<issue> — it warns-and-continues.
    assert "--blocked 7" not in log, log
    assert "WARNING: #7" in result.stderr, result.stderr
    assert "composer wedged" in result.stderr


def test_inject_and_verify_unobservable_pane_refutes_unproven_delivery(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """capture-pane starts erroring right after the paste (tmux busy, pane dying), and a
    non-turn write bumps the transcript. Nothing here proves the answer was submitted.

    #281 FLIPPED this case from rc 0 to rc 3. It used to degrade to the pre-#201 contract —
    an unreadable pane is no evidence of a wedge, so advance alone scored as delivered. But
    that is precisely the false-positive shape #281 is about: the advance is explained by the
    sidecar, the needle never landed in a user record, and "the pane is unreadable" is not
    evidence FOR delivery either. An unproven delivery is now REFUTED (rc 3), which #241 makes
    a warn-and-continue rather than a park, so the drain retries instead of logging an
    "injected answer" nobody received.

    The scan being AVAILABLE and finding nothing is what distinguishes this from
    test_inject_and_verify_degrades_to_advance_when_scan_unavailable, where rc 2 still
    degrades to advance-alone.
    """
    projects = tmp_path / "projects"
    _seed_transcript(projects, spoke_repo)
    sidecar = _project_dir_for(projects, spoke_repo) / "sidecar.jsonl"
    pasted = tmp_path / "pasted"
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"; printf "{{}}\\n" >> "{sidecar}"',
        on_capture=f'[ -e "{pasted}" ] && exit 1',  # readable pre-inject, then broken
    )

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 'Approved — proceed.'; echo RC=$?",
        env=_inject_env(projects, fake_bin),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=3", result.stdout + result.stderr


def test_inject_and_verify_degrades_to_advance_when_scan_unavailable(
    spoke_repo: Path, tmp_path: Path
) -> None:
    """The appended-bytes scan dies (broken python3): with the pane still echoing the
    needle after a genuine submit, delivery must degrade to the pre-#201 contract
    (advance alone => rc 0) — reading every echoed submit as a wedge would respawn
    healthy panes on every auto-answer.
    """
    answer = "Approved — proceed with the plan."
    projects = tmp_path / "projects"
    jsonl = _seed_transcript(projects, spoke_repo)
    pasted = tmp_path / "pasted"
    fake_bin = _write_fake_tmux(
        tmp_path,
        on_paste=f'touch "{pasted}"',
        on_enter=f'[ -e "{pasted}" ] && printf "{{}}\\n" >> "{jsonl}"',
        on_capture=f'[ -e "{pasted}" ] && echo "> {answer}"',
    )
    (fake_bin / "python3").write_text("#!/usr/bin/env bash\nexit 7\n")
    (fake_bin / "python3").chmod(0o755)

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 \"$ANSWER\"; echo RC=$?",
        env=_inject_env(projects, fake_bin, ANSWER=answer),
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", result.stdout + result.stderr


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


def test_present_qcm_injects_reviewer_reply(spoke_repo: Path, tmp_path: Path) -> None:
    # The interactive per-gate context owns present+capture+inject: it presents the QCM,
    # reads the human's reply HERE (stdin), and injects it into the spoke via the shared
    # injector — off the hub, off the pane. The hub is only NOTIFIED (hub-notify).
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(
        f"_broker_present_qcm '{spoke_repo}' 5 'This changes scope — your call'",
        env=env,
        stdin="Approved — proceed with option A.\n",
    )

    assert result.returncode == 0, result.stderr
    calls = tmux_log.read_text()
    assert "Approved — proceed with option A." in calls, f"the reply must be injected: {calls}"
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text(), "must not escalate"


def test_present_qcm_injects_reply_without_trailing_newline(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A reply typed then Ctrl-D (no trailing newline) is a genuine approval, not a defer:
    # `read` returns non-zero with $reply populated, and it must still be injected.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"_broker_present_qcm '{spoke_repo}' 5 'advice'", env=env, stdin="Go with Redis")

    assert result.returncode == 0, result.stderr
    assert "Go with Redis" in tmux_log.read_text(), "a newline-less reply must still inject"
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text()


def test_present_qcm_empty_reply_defers_to_block(spoke_repo: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_tmux_pane(fake_bin, spoke_repo, pd / "session.jsonl")
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(statedir),
    }

    result = _call(f"_broker_present_qcm '{spoke_repo}' 5 'your call'", env=env, stdin="\n")

    assert result.returncode == 0, result.stderr
    assert "--blocked 5" in ready_log.read_text(), "an empty reply defers the gate (escalate)"


def test_broker_service_gate_attended_presents_qcm_on_human_decision(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # End to end: the shared core reasons (one-shot), the reasoner escalates (human call),
    # and ATTENDED mode routes to the interactive QCM instead of blocking — the human's
    # reply is injected and the spoke proceeds. Unattended would have blocked/<issue>.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(statedir),
        "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: this is genuinely your call'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(
        f"broker_service_gate '{spoke_repo}' 5 attended",
        env=env,
        stdin="Use Redis.\n",
    )

    assert result.returncode == 0, result.stderr
    assert "Use Redis." in tmux_log.read_text(), "attended human-decision must inject the reply"
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text(), (
        "attended mode must present a QCM, not block like unattended"
    )


def test_gate_answer_landed_true_after_genuine_reply(spoke_repo: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    (_project_dir_for(projects, spoke_repo) / "s.jsonl").write_text(
        _resumed_gate_transcript("PLAN")
    )

    result = _call(
        f"_gate_answer_landed '{spoke_repo}' && echo LANDED || echo NO",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert result.stdout.strip().splitlines()[-1] == "LANDED", result.stdout + result.stderr


def test_gate_answer_landed_false_while_still_parked(spoke_repo: Path, tmp_path: Path) -> None:
    # Only the plan + gate Bash, no reply yet → still parked, must NOT read as landed.
    projects = tmp_path / "projects"
    (_project_dir_for(projects, spoke_repo) / "s.jsonl").write_text(_gate_park_transcript("PLAN"))

    result = _call(
        f"_gate_answer_landed '{spoke_repo}' && echo LANDED || echo NO",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert result.stdout.strip().splitlines()[-1] == "NO", result.stdout + result.stderr


# ── issue #312: retire an abandoned PLAN-gate episode at the moved-on drop ─────────────────────
# A spoke that emits gate/<n> then keeps coding (#117) leaves the tag at the tip and the park
# onset aging: the moved-on drop wrote answer-drop-<n> and returned, retiring nothing, so the
# watchdog fired park-undeliverable off the stale onset (#312) and burned a reasoner run every
# tick. The broker now RETIRES the episode at the drop when the transcript proves the spoke coded
# past the gate: consume the tag, credit+clear the onset, drop the warned backoff and the drop
# ledger, and journal `gate-abandoned` so the outcome is auditable (#241).

# Force the moved-on drop deterministically; _still_parked_same / _spoke_still_parked have their
# own tests, and an instant stub answerer cannot advance the transcript the way a minutes-long
# real reasoner run does.
_MOVED_ON = "_still_parked_same() { return 1; }; _spoke_still_parked() { return 1; }; "


def _retire_gate_env(spoke_repo: Path, tmp_path: Path, projects: Path) -> dict[str, str]:
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text("#!/usr/bin/env bash\ntrue\n")
    ready_stub.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text("#!/usr/bin/env bash\ntrue\n")
    (fake_bin / "gh").chmod(0o755)
    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_ANSWERER_CMD": "printf 'reasoning\\nANSWER: Approved -- proceed.'",
    }


def _gate_tag_list(spoke_repo: Path) -> str:
    return subprocess.run(
        ["git", "tag", "--list", "gate/5"], cwd=spoke_repo, capture_output=True, text=True
    ).stdout.strip()


def test_retire_abandoned_gate_park_clears_all_episode_state(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The helper the drop path delegates to: consume the tag, clear the onset + drop ledger, and
    # journal `gate-abandoned`. Consuming the tag is what stops the same (tip, sig) being re-served.
    sd = tmp_path / "sd"
    sd.mkdir()
    _tag_gate_at_head(spoke_repo, 5)
    (sd / "park-onset-5.epoch").write_text("1000\n")
    (sd / "answer-drop-5").write_text("tip\tsig\t3\tno longer parked on that prompt\n")
    env = {"AFK_STATE_DIR": str(sd), "AFK_JOURNAL_GH_COMMENT": "0", "AFK_NOW": "2000"}

    result = _call(f"_retire_abandoned_gate_park '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    assert _gate_tag_list(spoke_repo) == "", "the abandoned gate tag must be consumed"
    assert not (sd / "park-onset-5.epoch").exists(), "the park onset must be credited + cleared"
    assert not (sd / "answer-drop-5").exists(), "the drop ledger must be cleared"
    assert "gate-abandoned" in (sd / "decision-journal.jsonl").read_text()


def test_broker_service_gate_retires_gate_the_spoke_coded_past(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC1/AC2 replay: a gate park the spoke coded past (#117), reached at the moved-on drop, is
    # retired — the gate tag is consumed so the next tick reads the spoke as busy, not a gate to
    # re-serve, and the retirement is journaled.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [_gate_bash_turn("PLAN -- then keeps coding"), _spoke_coded_past_turn()],
    )
    _tag_gate_at_head(spoke_repo, 5)
    env = _retire_gate_env(spoke_repo, tmp_path, projects)

    result = _call(f"{_MOVED_ON} broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert _gate_tag_list(spoke_repo) == "", "the abandoned gate tag must be retired at the drop"
    journal = tmp_path / "sd" / "decision-journal.jsonl"
    assert journal.exists() and "gate-abandoned" in journal.read_text()


def test_broker_service_gate_does_not_retire_a_bare_gate_park(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A compliant park (gate + tool_result + the agent loop's trailing "awaiting review" text
    # turn, no WRITE past the gate) → not the #117 shape → today's plain moved-on drop is preserved
    # (tag stays, a drop is recorded). Retiring here would strand a real awaiting park and discard
    # a pending answer/amendment (the #312 review's blocker).
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN awaiting a reply"),
            _gate_tool_result(is_error=False),
            _spoke_await_review_turn(),
        ],
    )
    _tag_gate_at_head(spoke_repo, 5)
    env = _retire_gate_env(spoke_repo, tmp_path, projects)

    result = _call(f"{_MOVED_ON} broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert _gate_tag_list(spoke_repo) == "gate/5", "a bare gate park must NOT be retired"
    assert (tmp_path / "sd" / "answer-drop-5").exists(), "a plain moved-on drop is still recorded"


def test_broker_service_gate_204_self_heal_unchanged_on_typed_reply(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC5 regression: a TYPED reply after the gate is the #204 path — the top self-heal consumes
    # the tag WITHOUT running the answerer, and this stays DISTINCT from the #312 retirement (no
    # answerer run, no `gate-abandoned` journal line).
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN -- a human then approves"),
            {"type": "user", "promptSource": "typed", "message": {"content": "Approved -- go."}},
        ],
    )
    _tag_gate_at_head(spoke_repo, 5)
    calls = tmp_path / "answerer.calls"
    env = _retire_gate_env(spoke_repo, tmp_path, projects)
    env["AFK_ANSWERER_CMD"] = f"printf x >> '{calls}'; printf 'ESCALATE: x'"

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert _gate_tag_list(spoke_repo) == "", "the #204 typed-reply self-heal must consume the tag"
    assert not calls.exists(), "the self-heal short-circuits before the answerer runs"
    journal = tmp_path / "sd" / "decision-journal.jsonl"
    assert not journal.exists() or "gate-abandoned" not in journal.read_text()


def test_gate_answer_landed_false_for_synthetic_post_park_turn(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A harness-injected (non-typed) user turn after the park must NOT read as an answer,
    # or the broker would tear down the gate on a spoke still awaiting its first approval.
    projects = tmp_path / "projects"
    synth = (
        json.dumps(
            {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}}
        )
        + "\n"
    )
    (_project_dir_for(projects, spoke_repo) / "s.jsonl").write_text(
        _gate_park_transcript("PLAN") + synth
    )

    result = _call(
        f"_gate_answer_landed '{spoke_repo}' && echo LANDED || echo NO",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert result.stdout.strip().splitlines()[-1] == "NO", result.stdout + result.stderr


def test_broker_consumes_stale_tag_when_answer_already_landed(
    spoke_repo: Path, tmp_path: Path
) -> None:
    prompt_log = tmp_path / "prompt.log"
    env = _gate_broker_env(spoke_repo, tmp_path, prompt_log=prompt_log)
    # A genuine reply already landed after the park (a late / external / attended inject).
    pd = _project_dir_for(Path(env["CLAUDE_PROJECTS_DIR"]), spoke_repo)
    (pd / "session.jsonl").write_text(_resumed_gate_transcript("stale PLAN prose"))
    (spoke_repo / ".ai-toolkit").mkdir()
    artifact = spoke_repo / ".ai-toolkit" / "gate-5.md"
    artifact.write_text("stale plan\n")
    _tag_gate_at_head(spoke_repo, 5)

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    tags = subprocess.run(
        ["git", "tag", "-l", "gate/5"], cwd=spoke_repo, capture_output=True, text=True
    )
    assert tags.stdout.strip() == "", "the stale gate tag must be consumed"
    assert not artifact.exists(), "the spent plan artifact must be dropped too"
    assert not prompt_log.exists(), "a resumed spoke must NOT be re-answered"


def test_broker_self_heal_clears_park_onset_and_warned_backoff(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #288 AC1/AC4: the #204 self-heal is the exact moment the broker proves the park episode
    # ended (an outside-broker approval landed); it must END the episode right there — clearing
    # the STALE park-onset epoch (not waiting for a later slot_state tick to observe not-parked,
    # #277's false-fire gap) and the answer-lane warned-retry backoff (so a later re-park on the
    # same tip gets a fresh ceiling, not one inherited from the now-resolved episode's exhausted
    # retries).
    prompt_log = tmp_path / "prompt.log"
    env = _gate_broker_env(spoke_repo, tmp_path, prompt_log=prompt_log)
    statedir = Path(env["AFK_STATE_DIR"])
    statedir.mkdir(parents=True, exist_ok=True)
    (statedir / "park-onset-5.epoch").write_text("1000\n")  # stale onset from the resolved park
    (statedir / "warned-state-5").write_text("2\t9999999999\n")  # an armed, not-yet-due backoff
    (statedir / "warned-5.txt").write_text("1000\tre-answer ceiling reached\n")
    (statedir / "answer-drop-5").write_text(
        "deadbeef\tsigA\t2\tstale drop from the resolved park\n"
    )
    pd = _project_dir_for(Path(env["CLAUDE_PROJECTS_DIR"]), spoke_repo)
    (pd / "session.jsonl").write_text(_resumed_gate_transcript("stale PLAN prose"))
    (spoke_repo / ".ai-toolkit").mkdir()
    (spoke_repo / ".ai-toolkit" / "gate-5.md").write_text("stale plan\n")
    _tag_gate_at_head(spoke_repo, 5)

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert not (statedir / "park-onset-5.epoch").exists(), (
        "the resolved episode's park-onset must be cleared, not left to go stale"
    )
    assert not (statedir / "warned-state-5").exists(), (
        "the answer-lane warned-retry backoff must be cleared"
    )
    assert not (statedir / "warned-5.txt").exists(), (
        "the human-facing warned record must be cleared too"
    )
    assert not (statedir / "answer-drop-5").exists(), (
        "a re-park on the same tip/signature must not inherit the resolved episode's drop record"
    )
    assert not prompt_log.exists(), "a resumed spoke must NOT be re-answered"


# ── issue #294 AC2: an already-served permission park is not re-dispatched ─────────────────────
# broker_service_gate dispatched _decide_permission on every tick a dialog was pending, so an
# approve already delivered for THIS exact park bought nothing: the identical dialog was decided
# and re-approved. The served check sits BEFORE the re-answer ceiling — like the _broker_gate_voided
# check above it, and for the same reason: a skipped tick must not burn ceiling budget, or the
# ceiling would exhaust on a healthy approve and warn + arm the #241 backoff over a spoke that
# never failed at anything.


def _served_probe(spoke_repo: Path, calls: Path) -> str:
    """Count dispatches into the permission decider without running it — the routing decision IS
    the unit here (whether the identical dialog is decided a second time)."""
    return (
        f"_decide_permission() {{ printf 'x' >> '{calls}'; }}; "
        f"broker_service_gate '{spoke_repo}' 5 unattended"
    )


def _seed_served(spoke_repo: Path, env: dict[str, str], tool_id: str = "tu_1") -> str:
    """Record the live park as already-approved, exactly as a delivered approve would."""
    sig = _call(f"_broker_park_signature '{spoke_repo}' 5", env=env).stdout.strip()
    assert sig, "the park must carry a signature to key the served record on"
    _call(f"note_permission_served '{spoke_repo}' 5 '{sig}' {tool_id}", env=env)
    return sig


def test_broker_service_gate_skips_the_dispatch_for_an_already_served_park(
    spoke_repo: Path, tmp_path: Path
) -> None:
    env = _perm_env(tmp_path, spoke_repo, "git reset -q; git add tests/x.py", "printf 'x'")
    _seed_served(spoke_repo, env)
    calls = tmp_path / "dispatch.calls"

    result = _call(_served_probe(spoke_repo, calls), env=env)

    assert result.returncode == 0, result.stderr
    assert not calls.exists(), "the identical dialog must not be decided (and re-approved) twice"


def test_a_served_skip_does_not_burn_the_reanswer_ceiling(spoke_repo: Path, tmp_path: Path) -> None:
    # Pins the check's PLACEMENT, not just its effect: were it after the ceiling, the skipped tick
    # would still bump the counter, and a couple of stale-pane ticks would exhaust it and warn.
    env = _perm_env(tmp_path, spoke_repo, "git reset -q; git add tests/x.py", "printf 'x'")
    _seed_served(spoke_repo, env)

    _call(_served_probe(spoke_repo, tmp_path / "dispatch.calls"), env=env)

    statedir = Path(env["_STATEDIR"])
    assert not (statedir / "reanswer-5").exists(), (
        "a skipped tick must not count as a re-answer attempt against the ceiling"
    )
    assert not (statedir / "warned-state-5").exists(), (
        "a healthy served park must never arm the warned-retry backoff (#274)"
    )


def test_broker_service_gate_serves_the_park_again_once_the_tip_advances(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The legitimate case the served marker must not break (issue item 2): once the tip moves, a
    # pending dialog is a new occurrence and is decided normally on the next tick.
    env = _perm_env(tmp_path, spoke_repo, "git reset -q; git add tests/x.py", "printf 'x'")
    _seed_served(spoke_repo, env)
    calls = tmp_path / "dispatch.calls"

    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "progress"],
        cwd=spoke_repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"},
    )
    _call(_served_probe(spoke_repo, calls), env=env)

    assert calls.read_text() == "x", "a park at a NEW tip must be served normally"


def test_broker_service_gate_serves_a_changed_command_on_the_next_tick(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The signature dimension of the same guarantee: the spoke asks for something ELSE at the same
    # tip. The gated tool_use keeps its id here, so only the signature moves — proving the sig is
    # load-bearing in the key and not carried by the id alone.
    env = _perm_env(tmp_path, spoke_repo, "git reset -q; git add tests/x.py", "printf 'x'")
    _seed_served(spoke_repo, env)
    calls = tmp_path / "dispatch.calls"

    jsonl = _project_dir_for(Path(env["CLAUDE_PROJECTS_DIR"]), spoke_repo) / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git status --short")) + "\n")
    _call(_served_probe(spoke_repo, calls), env=env)

    assert calls.read_text() == "x", "a DIFFERENT pending command is a different park"
