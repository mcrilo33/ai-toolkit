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


def _call(
    fn_call: str, *, env: dict[str, str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Source gate-broker.sh directly and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{GATE_BROKER}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
        input=stdin,
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


@pytest.mark.parametrize(
    "spec,rc",
    [
        ("Bash(git status:*)", "0"),  # a read-only git verb is allowed
        ("Bash(git diff)", "0"),
        ("Bash(git push:*)", "1"),  # a scoped-but-MUTATING git verb must be rejected
        ("Bash(git commit:*)", "1"),
        ("Bash(git reset:*)", "1"),
        ("Bash(rm -rf /)", "1"),  # a scoped Bash must not smuggle arbitrary commands
    ],
)
def test_assert_readonly_tools_vets_scoped_bash_verb(spec: str, rc: str) -> None:
    result = _call(f'assert_readonly_tools "{spec}"; echo RC=$?', env={})
    assert result.stdout.strip().splitlines()[-1] == f"RC={rc}", f"{spec}: {result.stdout}"


def test_worktree_fingerprint_detects_deletion(spoke_repo: Path) -> None:
    (spoke_repo / "keep.txt").write_text("data")
    subprocess.run(["git", "add", "keep.txt"], cwd=spoke_repo, check=True, capture_output=True)
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    (spoke_repo / "keep.txt").unlink()
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()

    assert fp1 and fp2 != fp1, "deleting a tracked file must change the fingerprint"


def test_broker_service_gate_escalates_when_fingerprint_unavailable(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # Fail-safe: a git worktree whose fingerprint comes back empty (tooling absent) can't
    # be verified read-only, so the gate escalates rather than trusting the answer. Force
    # the empty fingerprint by overriding the fingerprint fn after sourcing.
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ANSWER: go ahead'"}

    result = _call(
        "_broker_worktree_fingerprint() { printf ''; }; "
        f"broker_service_gate '{spoke_repo}' 5 unattended",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log, f"unverifiable read-only must escalate: {log}"
    assert "fingerprint" in log.lower(), log


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


# ── subtask C: attended QCM surface + interactive per-gate resolver ────────────


def _fake_tmux_pane(fake_bin: Path, wt: Path, jsonl: Path) -> Path:
    """A tmux stub: list-panes maps a pane to <wt>; the submitting Enter advances the
    spoke transcript so inject_and_verify confirms; every send-keys is logged."""
    log = fake_bin / "tmux.log"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{wt}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    return log


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


# ── subtask D: automatable-decisions log + codification pass ───────────────────


def _bash_tool_record(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "id": "tu_1", "input": {"command": command}}
            ]
        },
    }


_PERMISSION_PROMPT = "Bash command\n  git reset -q\nDo you want to proceed?\n❯ 1. Yes\n  2. No"


@pytest.mark.parametrize(
    "cmd",
    [
        "git reset -q; git add tests/x.py",
        "git reset HEAD -- tests/other.py; git add tests/other.py",
        "git reset;   git add a/b/c.py",
    ],
)
def test_decision_signature_collides_across_arg_variation(cmd: str) -> None:
    # The signature normalises a command to its verb skeleton so recurrences of the SAME
    # shape (different files/flags) collide into one automatable signature.
    result = _call(f'_broker_decision_signature permission "$CMD"', env={"CMD": cmd})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "git-reset+git-add", result.stdout


def test_log_decision_appends_tsv_and_digest_reflects(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"}

    r = _call('log_decision 5 permission "git reset -q; git add tests/x.py" APPROVE', env=env)
    assert r.returncode == 0, r.stderr

    line = (statedir / "decisions.log").read_text().strip()
    fields = line.split("\t")
    assert fields[1] == "5" and fields[2] == "permission"
    assert fields[3] == "git-reset+git-add" and fields[4] == "APPROVE"

    # The B-subtask reader consumes exactly this format.
    digest = _call("read_decisions_digest 5", env={"AFK_STATE_DIR": str(statedir)})
    assert "git-reset+git-add" in digest.stdout and "APPROVE" in digest.stdout


def test_codify_proposes_rule_for_recurring_unanimous_signature(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    log = statedir / "decisions.log"
    # The #149 git-reset self-stage case: the same signature auto-approved twice.
    log.write_text(
        "1\t5\tpermission\tgit-reset+git-add\tAPPROVE\n"
        "2\t7\tpermission\tgit-reset+git-add\tAPPROVE\n"
        "3\t9\tpermission\tgit-push+origin\tESCALATE\n"  # single occurrence → no rule
        "4\t11\tpermission\tgit-clean\tAPPROVE\n"  # conflicting decisions → no rule
        "5\t12\tpermission\tgit-clean\tESCALATE\n"
    )
    env = {"AFK_STATE_DIR": str(statedir)}

    result = _call("codify_decisions 2", env=env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "git-reset+git-add" in out and "APPROVE" in out, out
    assert "git-push+origin" not in out, "a single occurrence must not become a rule"
    assert "git-clean" not in out, "a conflicting signature must not become a rule"


def test_decide_permission_logs_auto_approve(spoke_repo: Path, tmp_path: Path) -> None:
    # Integration: the #149 git-reset self-stage auto-approve is recorded to the
    # automatable-decisions log with its signature, so codify can later graduate it.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git reset -q; git add tests/x.py")) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_log = fake_bin / "tmux.log"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{tmux_log}"\n'
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = statedir / "decisions.log"
    assert log.exists(), "a safe auto-approve must be logged"
    fields = log.read_text().strip().split("\t")
    assert fields[2] == "permission" and fields[3] == "git-reset+git-add" and fields[4] == "APPROVE"


# ── issue #164: the reasoner transcript must not pollute the spoke's session ────
#
# Regression from #155-B: the read-only reasoner runs headless `claude` with cwd = the
# spoke's worktree, so ITS OWN session transcript lands in the SAME
# ~/.claude/projects/<munged-wt>/ dir as the spoke's. `_spoke_jsonl` picked the newest
# jsonl there — the answerer's own transcript — so `_still_parked_same` always saw the
# transcript "move", every AFK answer was dropped as stale, and the spoke sat stranded.
#
# These tests drive the behaviour through the DEFAULT reasoner command (they do NOT
# override AFK_ANSWERER_CMD) so they exercise the exact surface the fix changes. A fake
# `claude` on PATH models the real CLI's session persistence: like the real binary it
# writes its own transcript into the project dir for its cwd — UNLESS invoked with
# `--no-session-persistence`, which suppresses the write. So the fix (adding that flag to
# the default reasoner command — option 2, killing the write at source) flips these from
# RED to GREEN exactly as it does for the real CLI; a downstream `_spoke_jsonl` filter
# (option 3) would satisfy them too, since every assertion is on the spoke's resolved
# transcript, not on how the pollution was avoided.


def _install_fake_claude(fake_bin: Path, decision: str) -> None:
    """Install a fake ``claude`` that models real session-transcript persistence.

    It writes its own transcript into the project dir for its cwd (mirroring the real CLI's
    ``<projects>/<munged-cwd>/`` layout, resolved like the broker via ``CLAUDE_PROJECTS_DIR``
    / ``~/.claude/projects``) UNLESS ``--no-session-persistence`` is present, then prints the
    decision. A ``gh`` stub is also installed so ``build_answerer_prompt`` stays hermetic.
    """
    reasoner_record = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "reasoning about the gate"}]},
        }
    )
    (fake_bin / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "persist=1\n"
        'for a in "$@"; do [ "$a" = "--no-session-persistence" ] && persist=0; done\n'
        "cat >/dev/null 2>&1\n"  # consume the reasoner prompt on stdin
        'if [ "$persist" -eq 1 ]; then\n'
        # Guard: never fall back to the real ~/.claude store — a caller that forgets
        # CLAUDE_PROJECTS_DIR must fail loudly here, not pollute the developer's machine.
        '  base="${CLAUDE_PROJECTS_DIR:?fake claude needs CLAUDE_PROJECTS_DIR}"\n'
        "  slug=\"$(pwd | sed 's/[^A-Za-z0-9]/-/g')\"\n"
        '  mkdir -p "$base/$slug"\n'
        f"  printf '%s\\n' '{reasoner_record}' > \"$base/$slug/reasoner-transcript.jsonl\"\n"
        "fi\n"
        f"printf '%s' '{decision}'\n"
    )
    (fake_bin / "claude").chmod(0o755)
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)


@pytest.fixture
def reasoner_env(spoke_repo: Path, tmp_path: Path) -> dict[str, str]:
    """A spoke parked on a question + a fake `claude` reasoner on PATH (default command)."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    spoke_jsonl = pd / "session.jsonl"
    spoke_jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(spoke_jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_claude(fake_bin, "ANSWER: go ahead")

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "_SPOKE_JSONL": str(spoke_jsonl),
        "_FAKE_BIN": str(fake_bin),
    }


def test_run_answerer_does_not_pollute_spoke_jsonl(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # After the reasoner runs, the spoke's OWN transcript must still be the one
    # `_spoke_jsonl` resolves — not the reasoner's fresh transcript.
    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; _spoke_jsonl '{spoke_repo}'",
        env=reasoner_env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == reasoner_env["_SPOKE_JSONL"], (
        f"_spoke_jsonl must resolve the spoke's own transcript, not the reasoner's: {result.stdout}"
    )


def test_still_parked_same_survives_reasoner_transcript(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # `_still_parked_same` must judge freshness against the spoke's transcript alone: a
    # reasoner write during the reason step is NOT the spoke moving on. Snapshot the clock,
    # run the reasoner (which writes its own transcript), then assert the spoke still reads
    # as parked on the same question.
    question = "Q: Which store?\n  - Redis: fast"

    result = _call(
        f"before=\"$(_transcript_mtime '{spoke_repo}')\"; "
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; "
        f'_still_parked_same \'{spoke_repo}\' 5 0 "$QUESTION" "$before"; echo RC=$?',
        env={**reasoner_env, "QUESTION": question},
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=0", (
        f"a reasoner write must not make the spoke read as 'moved on': {result.stdout}{result.stderr}"
    )


def test_extract_pending_question_ignores_reasoner_transcript(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # The reasoner transcript carries no AskUserQuestion; if `extract_pending_question`
    # read it instead of the spoke's, the park would vanish. It must keep returning the
    # spoke's real question after the reasoner runs.
    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; extract_pending_question '{spoke_repo}'",
        env=reasoner_env,
    )

    assert "Which store?" in result.stdout, (
        f"extract_pending_question must read the spoke's transcript, not the reasoner's: {result.stdout}"
    )


def test_spoke_idle_seconds_not_refreshed_by_reasoner_write(
    spoke_repo: Path, reasoner_env: dict[str, str]
) -> None:
    # The reaper's idle clock keys off the spoke's transcript mtime. A reasoner write must
    # not reset it, or a genuinely-stranded spoke never ages out. With "now" pinned an hour
    # past the spoke's last write, idle must read ~3600s regardless of the reasoner's fresh
    # transcript.
    result = _call(
        f"run_answerer 5 'q' '{spoke_repo}' >/dev/null; _spoke_idle_seconds '{spoke_repo}' 5",
        env={**reasoner_env, "AFK_NOW": "1000003600"},
    )

    assert result.stdout.strip() == "3600", (
        f"a reasoner write must not refresh the reaper's idle clock: {result.stdout}{result.stderr}"
    )


def test_broker_service_gate_injects_despite_reasoner_transcript(
    spoke_repo: Path, reasoner_env: dict[str, str], tmp_path: Path
) -> None:
    # End to end: a parked spoke, a reasoner that ANSWERS (and writes its own transcript
    # mid-answer). The answer must be INJECTED, not dropped as stale — the #164 stranding.
    fake_bin = Path(reasoner_env["_FAKE_BIN"])
    _install_fake_claude(fake_bin, "ANSWER: Approved — use Redis.")
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, Path(reasoner_env["_SPOKE_JSONL"]))
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        **reasoner_env,
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert "dropping the stale answer" not in result.stderr, (
        f"the answer must not be dropped as stale: {result.stderr}"
    )
    assert "Approved — use Redis." in tmux_log.read_text(), (
        f"the reasoner's answer must be injected into the spoke: {tmux_log.read_text()}"
    )
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text(), (
        "a healthy answer must inject, not escalate to blocked"
    )


def test_decide_permission_logs_escalate_verdict(spoke_repo: Path, tmp_path: Path) -> None:
    # BOTH classifier verdicts are logged, not just APPROVE: a risky `git reset --hard`
    # (which shares the signature git-reset+git-add with the safe `git reset -q`) is
    # recorded as ESCALATE, so codify sees the conflict and never proposes it as unanimous.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git reset --hard; git add tests/x.py")) + "\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" "{_PERMISSION_PROMPT}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    statedir = tmp_path / "sd"
    statedir.mkdir()
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_STATE_DIR": str(statedir),
        "SPOKE_READY": str(ready_stub),
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    fields = (statedir / "decisions.log").read_text().strip().split("\t")
    assert fields[3] == "git-reset+git-add" and fields[4] == "ESCALATE", fields
    # The safe + destructive variants now conflict → codify proposes no rule for it.
    (statedir / "decisions.log").write_text(
        "1\t5\tpermission\tgit-reset+git-add\tAPPROVE\n"
        "2\t7\tpermission\tgit-reset+git-add\tESCALATE\n"
    )
    codify = _call("codify_decisions 2", env={"AFK_STATE_DIR": str(statedir)})
    assert "git-reset+git-add" not in codify.stdout, "a flag-dependent conflict must not codify"
