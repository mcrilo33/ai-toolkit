"""Unit tests for shared/skills/hub/scripts/gate-broker.sh.

The gate broker is the shared core (issue #155): detect a parked gate, extract its
prompt, reason about it, classify the decision, inject the answer, log it — plus the
mode-agnostic ``broker_service_gate`` orchestrator that both the unattended ``/afk``
adapter and the attended reviewer drive. Subtask A extracts this core out of
``hub-afk.sh`` (which now sources it) with the unattended behavior unchanged; these
tests source ``gate-broker.sh`` DIRECTLY to prove the core stands on its own.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# gate-broker.sh, like hub-afk.sh, targets the macOS control plane (BSD stat / tmux).
pytestmark = pytest.mark.skipif(
    subprocess.run(["stat", "-f", "%m", "."], capture_output=True).returncode != 0,
    reason="gate-broker.sh requires BSD stat (-f %m) and the macOS tmux hub (#129)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_BROKER = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "gate-broker.sh"


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the state dir so no test touches the real hub state (mirrors test_hub_afk)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source gate-broker.sh directly and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{GATE_BROKER}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


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


# ── the shared orchestrator: broker_service_gate ──────────────────────────────


def _project_dir_for(projects_root: Path, wt_path: Path) -> Path:
    import re

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ask_record(question: str, options: list[tuple[str, str]]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "AskUserQuestion",
                    "id": "tu_1",
                    "input": {
                        "questions": [
                            {
                                "question": question,
                                "options": [
                                    {"label": label, "description": desc} for label, desc in options
                                ],
                            }
                        ]
                    },
                }
            ]
        },
    }


@pytest.fixture
def spoke_repo(tmp_path: Path) -> Path:
    wt = tmp_path / "spoke"
    wt.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(cmd, cwd=wt, check=True, env=env, capture_output=True)
    return wt


@pytest.fixture
def waiting_spoke_env(tmp_path: Path, spoke_repo: Path) -> dict[str, str]:
    """A spoke parked on a question + a recording spoke-ready stub + a fake gh."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )

    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "Title\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "_READY_LOG": str(ready_log),
    }


def test_broker_service_gate_unattended_escalates_on_escalate(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # The unattended adapter over the shared core: a human-decision (ESCALATE) parks
    # the spoke as blocked/<issue> — the same fail-safe /afk has always had.
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: needs a human'"}

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log
    assert "needs a human" in log


def test_broker_service_gate_defaults_to_unattended(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # Called with no mode arg, it behaves as the unattended adapter (back-compat with
    # decide_and_act, which passes no third argument through its thin wrapper).
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ESCALATE: human call'"}

    result = _call(f"broker_service_gate '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    assert "--blocked 5" in Path(env["_READY_LOG"]).read_text()


# ── the hardened injector submits (no stranded paste) ─────────────────────────


def test_inject_and_verify_registers_when_transcript_advances(
    spoke_repo: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Fake tmux: the submitting Enter advances the transcript, so inject_and_verify
    # confirms the answer registered (rc 0) — the paste was submitted, not stranded.
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)

    result = _call(
        f"inject_and_verify '{spoke_repo}' afk:1 'Approved — proceed.'; echo RC=$?",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_INJECT_MENU_PAUSE": "0",
            "AFK_INJECT_VERIFY_SECONDS": "0",
        },
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", result.stdout + result.stderr
