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


# ── subtask B: read-only-worktree reasoner + evidence + mutation guard ─────────


def test_reasoner_allowed_tools_are_read_only() -> None:
    # The reasoner runs the code-review/Explore posture: Read/Grep/Glob (+ a narrow
    # read-only git helper), NEVER Edit/Write/NotebookEdit. The guard rejects any
    # mutating tool or a bare unrestricted Bash.
    tools = _call("reasoner_allowed_tools")
    assert tools.returncode == 0, tools.stderr
    listed = tools.stdout.strip()
    assert "Read" in listed and "Grep" in listed and "Glob" in listed
    for banned in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        assert banned not in listed, f"{banned} must not be in the reasoner allowlist: {listed}"

    ok = _call('assert_readonly_tools "$(reasoner_allowed_tools)"; echo RC=$?')
    assert ok.stdout.strip().splitlines()[-1] == "RC=0", ok.stdout + ok.stderr

    for bad in ("Read,Write", "Read,Bash", "Edit"):
        rej = _call(f'assert_readonly_tools "{bad}"; echo RC=$?', env={})
        assert rej.stdout.strip().splitlines()[-1] == "RC=1", f"{bad} must be rejected"


def test_worktree_fingerprint_detects_mutation(spoke_repo: Path) -> None:
    # A content fingerprint of the LIVE worktree (tracked + untracked, not just HEAD):
    # stable across a no-op, changes on a new file AND on a content edit of a file.
    (spoke_repo / "a.txt").write_text("one")
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    fp1b = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp1 and fp1 == fp1b, "fingerprint must be deterministic"

    (spoke_repo / "b.txt").write_text("two")
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp2 != fp1, "a new untracked file must change the fingerprint"

    (spoke_repo / "a.txt").write_text("one-edited")
    fp3 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp3 != fp2, "a content edit of an existing file must change the fingerprint"


def test_reasoner_runs_in_worktree_cwd(spoke_repo: Path, tmp_path: Path) -> None:
    # The reasoner is seeded with cwd = the spoke's worktree so its read-only tools
    # verify against real state. A `pwd` answerer proves the cwd.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    real = subprocess.run(
        ["bash", "-c", "cd '%s' && pwd -P" % spoke_repo], capture_output=True, text=True
    ).stdout.strip()

    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}'",
        env={"AFK_ANSWERER_CMD": "pwd -P", "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert real in result.stdout, f"reasoner cwd should be the worktree: {result.stdout}"


def test_broker_service_gate_voids_answer_when_reasoner_mutates_worktree(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # The read-only guard: a reasoner that mutates the live worktree has its answer
    # VOIDED and the gate escalated (unattended) — a mutation is never trusted, even
    # when the reasoner also emitted a plausible ANSWER.
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "touch pwned-by-reasoner; printf 'ANSWER: go ahead'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log, f"a worktree mutation must escalate, not inject: {log}"
    assert "worktree" in log.lower() or "mutat" in log.lower(), log


def test_reasoner_prompt_has_readonly_posture_and_evidence(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        "build_answerer_prompt 5 'Which store?' '/some/worktree'",
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    low = result.stdout.lower()
    assert "read-only" in low, "the prompt must state the reasoner has read-only worktree access"
    assert "evidence" in low, "the prompt must ask the reasoner to cite worktree evidence"
    assert "prior gate decisions" in low or "decisions-digest" in low, "digest section missing"


def test_read_decisions_digest_reflects_prior_outcomes(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "statedir"
    statedir.mkdir()
    # The decisions log line format shared with subtask D's writer:
    # <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>
    (statedir / "decisions.log").write_text(
        "1700000000\t5\tpermission\tgit-reset-self-stage\tAPPROVE\n"
        "1700000001\t9\tplan\tsome-other\tANSWER\n"
    )
    env = {"AFK_STATE_DIR": str(statedir)}

    hit = _call("read_decisions_digest 5", env=env)
    assert hit.returncode == 0, hit.stderr
    assert "git-reset-self-stage" in hit.stdout, hit.stdout
    assert "some-other" not in hit.stdout, "digest must be scoped to this spoke's issue"

    miss = _call("read_decisions_digest 5", env={"AFK_STATE_DIR": str(tmp_path / "empty")})
    assert miss.returncode == 0, miss.stderr
    assert miss.stdout.strip() == "", "no log ⇒ empty digest (D populates it)"
