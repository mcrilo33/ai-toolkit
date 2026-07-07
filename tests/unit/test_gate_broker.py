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


def test_worktree_fingerprint_tracks_only_tracked_content(spoke_repo: Path) -> None:
    # A content fingerprint of the TRACKED worktree content (issue #168): deterministic
    # across a no-op, UNCHANGED by a parked spoke's own untracked runtime writes (a
    # still-finishing push gate's `.testmondata`, OTel dumps under `.ai-toolkit/` — the
    # false-positive that burned three healthy reasoner runs), and changed ONLY by a
    # content edit of a tracked file.
    (spoke_repo / "a.txt").write_text("one")
    subprocess.run(["git", "add", "a.txt"], cwd=spoke_repo, check=True, capture_output=True)
    fp1 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    fp1b = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp1 and fp1 == fp1b, "fingerprint must be deterministic"

    (spoke_repo / ".testmondata").write_text("push-gate coverage db")
    (spoke_repo / ".testmondata-shm").write_text("wal")
    (spoke_repo / ".ai-toolkit" / "raw-bodies").mkdir(parents=True)
    (spoke_repo / ".ai-toolkit" / "raw-bodies" / "dump.json").write_text("{}")
    fp2 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp2 == fp1, "untracked spoke-runtime writes must NOT drift the fingerprint"

    (spoke_repo / "a.txt").write_text("one-edited")
    fp3 = _call(f"_broker_worktree_fingerprint '{spoke_repo}'").stdout.strip()
    assert fp3 != fp1, "a content edit of a tracked file must change the fingerprint"


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


def test_broker_service_gate_voids_answer_when_reasoner_mutates_tracked_content(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # The read-only guard, narrowed to TRACKED content (#168): a reasoner that mutates a
    # tracked file has its answer VOIDED and the gate escalated (unattended) — a tracked
    # mutation is never trusted, even alongside a plausible ANSWER. (Untracked runtime
    # drift no longer voids — see test_broker_service_gate_injects_despite_runtime_drift.)
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'mutated' > tracked.txt; printf 'ANSWER: go ahead'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log, f"a tracked mutation must escalate, not inject: {log}"
    assert "worktree" in log.lower() or "mutat" in log.lower(), log


def test_broker_service_gate_injects_despite_runtime_drift(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # Issue #168 headline regression: a parked spoke's own push gate writes `.testmondata`
    # during the reason step. That untracked runtime drift must NOT void a healthy answer —
    # the guard only cares about tracked content. The answer INJECTS; the gate does NOT
    # escalate to blocked.
    fake_bin = tmp_path / "bin"  # the waiting_spoke_env fake bin (holds gh); add tmux
    jsonl = _project_dir_for(tmp_path / "projects", spoke_repo) / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the inject's append advances it
    tmux_log = _fake_tmux_pane(fake_bin, spoke_repo, jsonl)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf x > .testmondata; printf 'ANSWER: use Redis'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    ready_log = Path(env["_READY_LOG"])
    ready_text = ready_log.read_text() if ready_log.exists() else ""
    assert "--blocked" not in ready_text, f"untracked runtime drift must not escalate: {ready_text}"
    assert "use Redis" in tmux_log.read_text(), (
        f"the healthy answer must inject despite the .testmondata write: {tmux_log.read_text()}"
    )


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


# ── #180: a spoke waiting on a background task is busy, not idle ───────────────


def _seed_task_output(tasks_root: Path, wt_path: Path, mtime: int) -> Path:
    """Write a harness background-task output file for `wt_path`, pinned to `mtime`.

    Mirrors the live layout <root>/claude-*/<munged-wt>/<session>/tasks/*.output.
    """
    import re

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = tasks_root / "claude-502" / slug / "sess1" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    out = d / "w3cadq3wh.output"
    out.write_text("running the review workflow...\n")
    os.utime(out, (mtime, mtime))
    return out


def test_spoke_idle_seconds_folds_in_task_output(spoke_repo: Path, tmp_path: Path) -> None:
    # A spoke waiting on a background workflow writes nothing to its transcript (#180), so the
    # idle clock must fold in the newest task-output mtime. Transcript pinned an hour stale, a
    # task-output write 100s ago: idle reads 100 (the fresher signal), not 3600.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_003_500)

    result = _call(
        f"_spoke_idle_seconds '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_NOW": "1000003600",
        },
    )

    assert result.stdout.strip() == "100", (
        f"a fresh task-output write must extend the idle clock: {result.stdout}{result.stderr}"
    )


def test_slot_state_task_output_keeps_live_spoke_busy(spoke_repo: Path, tmp_path: Path) -> None:
    # AC1 regression: a live-pane spoke with a stale transcript but a task-output file written
    # within AFK_IDLE_MINUTES is `busy`, not `reap` — the #168 healthy-spoke kill.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        )
        + "\n"
    )
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # stale transcript → would reap alone
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_003_500)  # fresh background work

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_NOW": "1000003600",
        },
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr


def test_slot_state_reaps_stale_spoke_without_task_evidence(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC2: a live-pane spoke with a stale transcript AND no task evidence still reaps — the
    # new signal must EXTEND the idle reference, never disable the ceiling.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        )
        + "\n"
    )
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    tasks_root = tmp_path / "tasks-root"  # exists but holds no output for this spoke

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_NOW": "1000003600",
        },
    )

    assert result.stdout.strip() == "reap", result.stdout + result.stderr


def test_spoke_idle_seconds_task_output_only_extends(spoke_repo: Path, tmp_path: Path) -> None:
    # #180 review: a task-output write only EXTENDS an existing reference. With no transcript
    # and no answer-attempt, a stale .output left in tmp by a PRIOR run at the same worktree
    # path must not fabricate a measurable idle age — the clock stays unmeasurable (empty).
    projects = tmp_path / "projects"
    _project_dir_for(projects, spoke_repo)  # project dir exists, but NO transcript written
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_000_000)  # stale prior-run output

    result = _call(
        f"_spoke_idle_seconds '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_NOW": "1000003600",
        },
    )

    assert result.stdout.strip() == "", (
        f"a task-output alone must not create measurability: {result.stdout!r}{result.stderr}"
    )


def test_slot_state_stale_task_output_does_not_reap_transcriptless_spoke(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #180 review: tmp is not cleared between runs, so a lingering .output from a prior
    # incarnation at a reused worktree path must NOT make a fresh, transcript-less spoke
    # reapable. With no transcript the idle clock stays unmeasurable -> busy, even though the
    # stale task-output is an hour old.
    projects = tmp_path / "projects"
    _project_dir_for(projects, spoke_repo)  # no transcript
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_000_000)  # stale prior-run output

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_NOW": "1000003600",
        },
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr


def test_slot_state_task_output_does_not_lift_hard_ceiling(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC3: the absolute hard ceiling (#133) is unchanged. A spoke past the hard wall-clock
    # ceiling reaps even with a brand-new task-output write — the signal extends idle, not the
    # ceiling. dispatch is stamped at an early clock, `now` is well past MAX_MINUTES x 3.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_019_900, 1_000_019_900))  # transcript itself is fresh
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_019_900)  # fresh background work too

    result = _call(
        f"AFK_NOW=1000000000 stamp_dispatch_epoch 5; slot_state '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_TASKS_ROOT": str(tasks_root),
            "AFK_SPOKE_MAX_MINUTES": "60",  # hard ceiling = 180 min
            "AFK_NOW": "1000020000",  # 333 min since dispatch → over the hard ceiling
        },
    )

    assert result.stdout.strip() == "reap", result.stdout + result.stderr


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


# ── issue #171: harden the answer path (freshness, timeouts, classifier gaps) ──


# subtask 1: the reasoner is bounded so a hung headless claude never freezes the tick ──


def test_run_answerer_delegates_to_shared_timeout(spoke_repo: Path, tmp_path: Path) -> None:
    # In production hub-afk.sh defines _afk_with_timeout (which tree-kills a wedged grandchild
    # so it can't hold run_answerer's capture pipe open); run_answerer must REUSE it and pass
    # the configured AFK_ANSWERER_TIMEOUT seconds, not roll its own bound. A stub echoes the
    # seconds it was handed and runs the command, proving both the delegation and the budget.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        '_afk_with_timeout() { echo "BOUND=$1"; shift; "$@"; }; run_answerer 5 \'q\'',
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_ANSWERER_TIMEOUT": "42",
            "AFK_ANSWERER_CMD": "printf 'ANSWER: ok'",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "BOUND=42" in result.stdout, (
        f"must delegate to _afk_with_timeout with the budget: {result.stdout}"
    )
    assert "ANSWER: ok" in result.stdout, (
        f"the answerer must still run under the bound: {result.stdout}"
    )


def test_answerer_timeout_rejects_zero_budget() -> None:
    # AFK_ANSWERER_TIMEOUT=0 (or non-numeric) must not disable the bound — `timeout 0` and
    # perl `alarm 0` both mean "no limit". _afk_answerer_timeout falls back to the default.
    for spec in ("0", "00", "abc", ""):
        got = _call("_afk_answerer_timeout", env={"AFK_ANSWERER_TIMEOUT": spec}).stdout.strip()
        assert got == "900", f"AFK_ANSWERER_TIMEOUT={spec!r} must fall back to 900, got {got}"
    ok = _call("_afk_answerer_timeout", env={"AFK_ANSWERER_TIMEOUT": "30"}).stdout.strip()
    assert ok == "30", ok


def test_broker_service_gate_escalates_when_answerer_times_out(
    spoke_repo: Path, waiting_spoke_env: dict[str, str]
) -> None:
    # A timed-out reasoner (the bound returns nonzero with no output) is a "no decision":
    # the gate must escalate to blocked/<issue> — the existing fail-safe — never hang.
    env = {**waiting_spoke_env, "AFK_ANSWERER_CMD": "printf 'ANSWER: should never run'"}

    result = _call(
        f"_afk_with_timeout() {{ return 124; }}; broker_service_gate '{spoke_repo}' 5 unattended",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log, f"a timed-out answerer must escalate: {log}"
    assert "no decision" in log.lower(), log


def test_run_answerer_standalone_fallback_bounds_a_slow_answerer(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Sourced standalone (no hub-afk _afk_with_timeout) on a coreutils-less host, the
    # self-contained fallback (perl alarm) must still bound the reasoner: a slow answerer is
    # killed before it prints, and run_answerer returns nonzero (→ no decision → escalate).
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    result = _call(
        "run_answerer 5 'q'; echo RC=$?",
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_ANSWERER_TIMEOUT": "1",
            "AFK_ANSWERER_CMD": "sleep 5; printf 'ANSWER: too late'",
        },
    )

    assert "too late" not in result.stdout, f"a slow answerer must be killed first: {result.stdout}"
    assert result.stdout.strip().splitlines()[-1] != "RC=0", (
        "a timed-out answerer must return nonzero"
    )


# subtask 2: a stale ESCALATE / no-decision must not strand an actively-working spoke ──


def test_broker_service_gate_drops_escalation_when_spoke_moves_on(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The answerer takes minutes; if the spoke moved on meanwhile (a human replied, the turn
    # resumed) an ESCALATE / no-decision must be DROPPED with a log, never stamped as a
    # spurious blocked/<N> on an actively-working spoke. Model "moved on" by having the
    # answerer advance the spoke's own transcript mid-reason, then escalate.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # pin old so the reasoner write advances it
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        # The reasoner bumps the spoke transcript (a human reply landed) then escalates.
        "AFK_ANSWERER_CMD": f"printf '{{}}\\n' >> '{jsonl}'; printf 'ESCALATE: needs a human'",
    }

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    ready_text = ready_log.read_text() if ready_log.exists() else ""
    assert "--blocked" not in ready_text, f"a moved-on spoke must not be escalated: {ready_text}"
    assert "dropping the escalation" in result.stderr.lower(), result.stderr


def test_spoke_moved_on_requires_a_confirmed_advance(spoke_repo: Path, tmp_path: Path) -> None:
    # The escalation gate must fail SAFE: it drops a real escalation ONLY on a demonstrated
    # transcript advance, never on an ambiguous probe (an empty/garbage baseline). Otherwise a
    # transient stat miss would silently swallow a blocked/<N> and strand the spoke unsurfaced.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    env = {"CLAUDE_PROJECTS_DIR": str(projects)}

    def moved_on(before: str) -> str:
        out = _call(f"_spoke_moved_on '{spoke_repo}' '{before}'; echo RC=$?", env=env)
        return out.stdout.strip().splitlines()[-1]

    assert moved_on("1000000000") == "RC=1", "unchanged mtime is not movement"
    os.utime(jsonl, (1_000_000_050, 1_000_000_050))
    assert moved_on("1000000000") == "RC=0", "a strictly newer write is movement"
    assert moved_on("") == "RC=1", "an empty baseline is not confident movement (fail safe)"
    assert moved_on("nope") == "RC=1", (
        "a non-numeric baseline is not confident movement (fail safe)"
    )


# subtask 3: blocked-at-tip over a still-parked spoke reads as waiting, not terminal ──


def test_slot_state_blocked_at_tip_with_pending_question_is_waiting(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A spurious blocked/<N> over a spoke still parked on a question must NOT read as terminal
    # 'done' (which stranded it — never re-answered, never reaped). With an extractable pending
    # question it reads 'waiting' (re-answerable); reconcile clears the tag once commits land.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )
    subprocess.run(
        ["git", "tag", "-f", "blocked/5"], cwd=spoke_repo, check=True, capture_output=True
    )

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr


def test_slot_state_blocked_at_tip_without_pending_stays_done(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The terminal reading is preserved when the spoke is NOT parked: a genuine blocked/<N>
    # with no extractable question/permission is still 'done'.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}
        )
        + "\n"
    )
    subprocess.run(
        ["git", "tag", "-f", "blocked/5"], cwd=spoke_repo, check=True, capture_output=True
    )

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "done", result.stdout + result.stderr


# subtask 4: the inject-verify budget default widened 20 -> 60 ──


def test_inject_verify_default_budget_is_60(spoke_repo: Path, tmp_path: Path) -> None:
    # A slow first token after submit must not read as "did not register" (which fed a false
    # escalation #3 then made sticky). Drive _transcript_advanced against a transcript that
    # never advances with an instant fake `sleep` that just counts calls: at poll=1 the loop
    # sleeps `budget` times, so the default budget shows up as 60 one-second polls.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sleeps = fake_bin / "sleeps.log"
    (fake_bin / "sleep").write_text(f'#!/usr/bin/env bash\necho x >> "{sleeps}"\nexit 0\n')
    (fake_bin / "sleep").chmod(0o755)

    result = _call(
        f"_transcript_advanced '{spoke_repo}' 1000000000; echo RC=$?",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_INJECT_POLL_SECONDS": "1",
        },
    )

    assert result.stdout.strip().splitlines()[-1] == "RC=1", result.stdout + result.stderr
    count = sleeps.read_text().count("x") if sleeps.exists() else 0
    assert count == 60, f"default inject-verify budget must be 60s (60 one-second polls): {count}"


# subtask 5: classify_permission tightening (find/-exec, chmod +x, bare pytest) ──


@pytest.mark.parametrize(
    "cmd,verdict",
    [
        ("find . -name foo -delete", "ESCALATE"),  # -delete can destroy files
        ("find /tmp -type f -exec cat {} +", "ESCALATE"),  # -exec can spawn anything
        ("find . -fprint /tmp/out", "ESCALATE"),  # -fprint writes to an arbitrary file
        ("find . -type f -fprintf /tmp/out '%p'", "ESCALATE"),  # -fprintf too
        ("find . -fls /tmp/out", "ESCALATE"),  # -fls writes a listing to a file
        ("find . -ok rm {} ;", "ESCALATE"),  # -ok spawns a process
        ("find . -type f -name '*.py'", "APPROVE"),  # a read-only find is fine
        ("find . -type f -print0", "APPROVE"),  # -print0 writes only to stdout — safe
        ("chmod +x /usr/local/bin/tool", "ESCALATE"),  # absolute path escapes the worktree
        ("chmod +x foo /usr/bin/bar", "ESCALATE"),  # a later absolute token escapes too
        ("chmod +x ../../../etc/cron.d/payload", "ESCALATE"),  # `..` traversal escapes the worktree
        ("chmod +x ./scripts/x.sh", "APPROVE"),  # relative in-tree self-op
        ("chmod +x scripts/x.sh", "APPROVE"),
        ("pytest", "ESCALATE"),  # a bare pytest is the full-suite ref-rewind hazard (#135)
        ("python -m pytest", "ESCALATE"),
        ("pytest tests/x.py", "APPROVE"),  # an argument scopes it
        ("python3 -m pytest tests/unit", "APPROVE"),
    ],
)
def test_classify_permission_tightened_cases(cmd: str, verdict: str) -> None:
    result = _call('classify_permission "$CMD" | cut -f1', env={"CMD": cmd})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == verdict, f"{cmd!r}: {result.stdout}"


# ── issue #175: the structured plan artifact replaces transcript extraction ────
# The gate park hands its plan to the broker through a scripted artifact
# (<wt>/.ai-toolkit/gate-<N>.md, written by spoke-ready.sh --gate) rather than the
# transcript heuristic. The gate route PREFERS the artifact when present (transcript
# fallback intact); _consume_gate_tag removes it alongside the tag.


def _gate_park_transcript(plan: str) -> str:
    """A gate-park transcript line: a prose plan + a spoke-ready --gate Bash, no AskUserQuestion."""
    return (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": plan},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "bash .ai-toolkit/scripts/spoke-ready.sh --gate 5"
                            },
                        },
                    ]
                },
            }
        )
        + "\n"
    )


def _tag_gate_at_head(wt: Path, issue: int) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "tag", "-a", f"gate/{issue}", "-m", "plan"],
        cwd=wt,
        check=True,
        env=env,
        capture_output=True,
    )


def _gate_broker_env(spoke_repo: Path, tmp_path: Path, *, prompt_log: Path) -> dict[str, str]:
    """Env for a gate-parked broker run: transcript plan + a prompt-capturing answerer.

    The answerer (AFK_ANSWERER_CMD) appends the prompt it receives on stdin to
    ``prompt_log`` then ESCALATEs, so a test can assert which plan the broker fed it.
    """
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(_gate_park_transcript("TRANSCRIPT PLAN prose"))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "T\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text("#!/usr/bin/env bash\ntrue\n")
    ready_stub.chmod(0o755)

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SPOKE_READY": str(ready_stub),
        "AFK_STATE_DIR": str(tmp_path / "sd"),
        "AFK_ANSWERER_CMD": f"cat >> '{prompt_log}'; printf 'ESCALATE: capture'",
    }


def test_read_gate_artifact_returns_plan(spoke_repo: Path) -> None:
    (spoke_repo / ".ai-toolkit").mkdir()
    (spoke_repo / ".ai-toolkit" / "gate-5.md").write_text("ARTIFACT PLAN: do the thing\n")

    result = _call(f"_read_gate_artifact '{spoke_repo}' 5")

    assert result.returncode == 0, result.stderr
    assert "ARTIFACT PLAN: do the thing" in result.stdout


def test_read_gate_artifact_empty_when_absent(spoke_repo: Path) -> None:
    result = _call(f"_read_gate_artifact '{spoke_repo}' 5")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        "no artifact → empty (the broker falls back to the transcript)"
    )


def test_read_gate_artifact_caps_at_4000_chars_not_bytes(spoke_repo: Path) -> None:
    # #175 review: the cap matches extract_pending_question (out[:4000] — CHARACTERS). A
    # multibyte plan must not be cut on bytes (head -c), which both truncates a valid plan
    # earlier than 4000 chars and can split a char mid-sequence.
    (spoke_repo / ".ai-toolkit").mkdir()
    (spoke_repo / ".ai-toolkit" / "gate-5.md").write_text("é" * 5000, encoding="utf-8")

    result = _call(f"_read_gate_artifact '{spoke_repo}' 5")

    assert result.returncode == 0, result.stderr
    # 4000 characters, not 4000 bytes (a byte cap yields 2000 'é' — each is 2 UTF-8 bytes).
    assert result.stdout.count("é") == 4000, (
        f"expected a 4000-CHARACTER cap, got {result.stdout.count('é')} chars"
    )


def test_broker_gate_route_prefers_artifact_over_transcript(
    spoke_repo: Path, tmp_path: Path
) -> None:
    prompt_log = tmp_path / "prompt.log"
    env = _gate_broker_env(spoke_repo, tmp_path, prompt_log=prompt_log)
    (spoke_repo / ".ai-toolkit").mkdir()
    (spoke_repo / ".ai-toolkit" / "gate-5.md").write_text("ARTIFACT PLAN: the real plan\n")
    _tag_gate_at_head(spoke_repo, 5)

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    prompt = prompt_log.read_text()
    assert "ARTIFACT PLAN: the real plan" in prompt, (
        "the broker must feed the reasoner the scripted artifact plan"
    )
    assert "TRANSCRIPT PLAN prose" not in prompt, (
        "the artifact must REPLACE transcript extraction when present"
    )


def test_broker_gate_route_falls_back_to_transcript_without_artifact(
    spoke_repo: Path, tmp_path: Path
) -> None:
    prompt_log = tmp_path / "prompt.log"
    env = _gate_broker_env(spoke_repo, tmp_path, prompt_log=prompt_log)
    _tag_gate_at_head(spoke_repo, 5)  # no artifact written

    result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)

    assert result.returncode == 0, result.stderr
    assert "TRANSCRIPT PLAN prose" in prompt_log.read_text(), (
        "with no artifact the transcript fallback must stay intact"
    )


def test_consume_gate_tag_removes_artifact(spoke_repo: Path) -> None:
    (spoke_repo / ".ai-toolkit").mkdir()
    artifact = spoke_repo / ".ai-toolkit" / "gate-5.md"
    artifact.write_text("plan\n")
    _tag_gate_at_head(spoke_repo, 5)

    result = _call(f"_consume_gate_tag '{spoke_repo}' 5")

    assert result.returncode == 0, result.stderr
    assert not artifact.exists(), "_consume_gate_tag must remove the plan artifact"
    tags = subprocess.run(
        ["git", "tag", "-l", "gate/5"], cwd=spoke_repo, capture_output=True, text=True
    )
    assert tags.stdout.strip() == "", "the local gate tag must also be dropped"
