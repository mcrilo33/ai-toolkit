"""Unit tests for shared/skills/hub/scripts/hub-afk.sh.

The unattended backlog-drain supervisor (issue #71): for a bounded window it keeps
the backlog draining with zero human input — plan + dispatch (batch-plan #70), then
auto-answer parked spokes (the one reasoning step), auto-land ready spokes, and reap
hung ones. These tests source the script (a source-guard keeps the supervisor loop
from running on import) and drive its layers directly:

  * the pure TIME layer — duration / window parsing, expiry, remaining;
  * the pure DECISION parser — ANSWER / ESCALATE extraction;
  * the TRANSCRIPT layer — extracting the prompt a spoke is parked on;
  * slot_state — against a throwaway git repo standing in for a spoke worktree;
  * the ANSWERER orchestration — with the answerer command and spoke-ready.sh
    stubbed, so a decision injects or escalates without a real `claude` or tmux.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_AFK = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-afk.sh"
SPOKE_READY = REPO_ROOT / "scripts" / "spoke-ready.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-afk.sh and invoke a shell expression against its functions.

    TZ=UTC is forced so the window clock is deterministic regardless of host TZ.
    """
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_AFK}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC).timestamp())


# ── the pure TIME layer ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("90", 5400),  # bare number ⇒ minutes
        ("30m", 1800),
        ("1h", 3600),
        ("2h", 7200),
        ("1h30m", 5400),
    ],
)
def test_parse_duration_worked_cases(spec: str, expected: int) -> None:
    result = _call(f"parse_duration {spec}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize("spec", ["", "0", "abc", "hm", "1x"])
def test_parse_duration_rejects_non_durations(spec: str) -> None:
    result = _call(f"parse_duration '{spec}'")

    assert result.returncode == 1


def test_compute_end_epoch_drain_is_sentinel() -> None:
    result = _call("compute_end_epoch drain 1000000")

    assert result.stdout.strip() == "drain"


def test_compute_end_epoch_duration_adds_to_now() -> None:
    result = _call("compute_end_epoch 1h 1000000")

    assert result.stdout.strip() == "1003600"


def test_compute_end_epoch_until_picks_next_occurrence() -> None:
    # 23:00 on 2026-06-17 → the next 07:00 is the following morning.
    now = _epoch(2026, 6, 17, 23, 0)
    expected = _epoch(2026, 6, 18, 7, 0)

    result = _call(f"compute_end_epoch until 07:00 {now}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_compute_end_epoch_until_today_when_still_ahead() -> None:
    now = _epoch(2026, 6, 17, 6, 0)
    expected = _epoch(2026, 6, 17, 7, 0)

    result = _call(f"compute_end_epoch until 07:00 {now}")

    assert result.stdout.strip() == str(expected)


def test_compute_end_epoch_rejects_garbage() -> None:
    result = _call("compute_end_epoch bogus 1000000")

    assert result.returncode == 1


@pytest.mark.parametrize(
    "state,now,expired",
    [
        ("1000000", "1000001", True),
        ("1000002", "1000001", False),
        ("drain", "9999999999", False),
        ("", "9999999999", False),
    ],
)
def test_window_expired(state: str, now: str, expired: bool) -> None:
    result = _call(f"window_expired '{state}' {now} && echo yes || echo no")

    assert result.stdout.strip() == ("yes" if expired else "no")


def test_minutes_remaining_counts_down() -> None:
    # 1500s = 25 minutes left → 25.
    result = _call("minutes_remaining 1000000 998500")

    assert result.stdout.strip() == "25"


def test_minutes_remaining_floors_at_zero() -> None:
    result = _call("minutes_remaining 1000000 1000999")

    assert result.stdout.strip() == "0"


def test_minutes_remaining_empty_for_drain() -> None:
    result = _call("minutes_remaining drain 1000000")

    assert result.stdout.strip() == ""


# ── the pure DECISION parser ──────────────────────────────────────────────────


# The answerer's real output carries actual newlines; command substitution preserves
# them when decide_and_act calls `parse_decision "$(run_answerer ...)"`. Feed the raw
# through an env var so the shell sees real newlines (not JSON-escaped \n).


def test_parse_decision_extracts_answer() -> None:
    raw = "Let me reason about this.\nThe contract is clear.\nANSWER: Proceed with option A."

    result = _call('parse_decision "$RAW"', env={"RAW": raw})

    assert result.stdout.strip() == "ANSWER\tProceed with option A."


def test_parse_decision_extracts_escalation() -> None:
    raw = "This touches main.\nESCALATE: irreversible — pushing to the default branch."

    result = _call('parse_decision "$RAW"', env={"RAW": raw})

    assert result.stdout.strip() == "ESCALATE\tirreversible — pushing to the default branch."


def test_parse_decision_takes_last_decision_line() -> None:
    raw = "ANSWER: draft\nrethinking\nANSWER: final answer"

    result = _call('parse_decision "$RAW"', env={"RAW": raw})

    assert result.stdout.strip() == "ANSWER\tfinal answer"


def test_parse_decision_empty_when_no_decision() -> None:
    raw = "I am still thinking and never concluded."

    result = _call('parse_decision "$RAW"', env={"RAW": raw})

    assert result.stdout.strip() == ""


# ── the TRANSCRIPT layer ──────────────────────────────────────────────────────


def _project_dir_for(projects_root: Path, wt_path: Path) -> Path:
    """Mirror the script's slug: non-alphanumerics in the worktree path → '-'."""
    import re

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_transcript(project_dir: Path, records: list[dict]) -> None:
    (project_dir / "session.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


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


def _gate_park_records(
    issue: int, plan: str = "Plan: do X then Y. Reply to approve."
) -> list[dict]:
    """Transcript of a PLAN-gate park: an assistant turn that prints the plan prose and
    runs `spoke-ready.sh --gate <issue>` (no AskUserQuestion), then that Bash's tool_result.
    """
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": plan},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "tu_gate",
                        "input": {"command": f"bash scripts/spoke-ready.sh --gate {issue}"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_gate",
                        "content": f"emitted gate/{issue}",
                    }
                ]
            },
        },
    ]


def test_extract_pending_question_reads_open_ask(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    wt = tmp_path / "wt"
    pd = _project_dir_for(projects, wt)
    _write_transcript(
        pd, [_ask_record("Which store?", [("Redis", "fast"), ("Postgres", "durable")])]
    )

    result = _call(
        f"extract_pending_question '{wt}'",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert "Q: Which store?" in result.stdout
    assert "Redis: fast" in result.stdout
    assert "Postgres: durable" in result.stdout


def test_extract_pending_question_reads_trailing_notification(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    wt = tmp_path / "wt"
    pd = _project_dir_for(projects, wt)
    _write_transcript(
        pd,
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Plan: do X. Reply to approve."}]},
            },
            {"type": "notification", "message": {"content": "waiting"}},
        ],
    )

    result = _call(
        f"extract_pending_question '{wt}'",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert "Plan: do X. Reply to approve." in result.stdout


def test_extract_pending_question_empty_when_working(tmp_path: Path) -> None:
    # A trailing user turn means the session moved on — not waiting.
    projects = tmp_path / "projects"
    wt = tmp_path / "wt"
    pd = _project_dir_for(projects, wt)
    _write_transcript(
        pd,
        [
            _ask_record("Which store?", [("Redis", "fast")]),
            {"type": "user", "message": {"content": [{"type": "text", "text": "Redis"}]}},
        ],
    )

    result = _call(
        f"extract_pending_question '{wt}'",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert result.stdout.strip() == ""


def test_extract_pending_question_returns_plan_on_gate_park(tmp_path: Path) -> None:
    # A PLAN-gate park has no AskUserQuestion (prose plan + a `spoke-ready.sh --gate`
    # Bash). The answerer still needs the plan to reason about, so extract returns it.
    projects = tmp_path / "projects"
    wt = tmp_path / "wt"
    pd = _project_dir_for(projects, wt)
    _write_transcript(pd, _gate_park_records(5))

    result = _call(
        f"extract_pending_question '{wt}'",
        env={"CLAUDE_PROJECTS_DIR": str(projects)},
    )

    assert "Plan: do X then Y. Reply to approve." in result.stdout


# ── slot_state against a throwaway "spoke" git repo ───────────────────────────


@pytest.fixture
def spoke_repo(tmp_path: Path) -> Path:
    """A minimal git repo standing in for a spoke worktree (one commit)."""
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


def test_slot_state_done_on_ready_tag_at_tip(spoke_repo: Path) -> None:
    subprocess.run(["git", "tag", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": "/nonexistent"})

    assert result.stdout.strip() == "done"


def test_slot_state_busy_when_no_marker_no_transcript(spoke_repo: Path) -> None:
    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": "/nonexistent"})

    assert result.stdout.strip() == "busy"


def test_slot_state_waiting_when_parked_on_question(spoke_repo: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which approach?", [("A", "simple")])])

    result = _call(f"slot_state '{spoke_repo}' 5", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "waiting"


def test_slot_state_waiting_on_gate_tag_at_tip(spoke_repo: Path, tmp_path: Path) -> None:
    # A spoke parked at its PLAN gate pushes gate/<issue> at the tip and prints prose (no
    # AskUserQuestion). The tag, not a pending question, marks it waiting — and that wins
    # over the idle-reap check even when the spoke has been idle past AFK_IDLE_MINUTES.
    subprocess.run(["git", "tag", "gate/5"], cwd=spoke_repo, check=True, capture_output=True)
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    # A plain assistant turn (no question, no notification) so extract_pending_question is
    # empty — the gate tag is the only waiting signal.
    _write_transcript(
        pd, [{"type": "assistant", "message": {"content": [{"type": "text", "text": "parked"}]}}]
    )
    os.utime(pd / "session.jsonl", (1_000_000_000, 1_000_000_000))  # idle far past the ceiling

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={"CLAUDE_PROJECTS_DIR": str(projects), "AFK_IDLE_MINUTES": "0"},
    )

    assert result.stdout.strip() == "waiting"


@pytest.mark.parametrize(
    "epoch,now,over",
    [
        ("1000000", "1011000", True),  # 183 min later > 180 ceiling
        ("1000000", "1001000", False),  # ~16 min later
        ("", "1001000", False),  # unknown epoch → never over
    ],
)
def test_spoke_over_ceiling(epoch: str, now: str, over: bool) -> None:
    result = _call(f"spoke_over_ceiling '{epoch}' '{now}' && echo yes || echo no")

    assert result.stdout.strip() == ("yes" if over else "no")


# ── the tmux inject: interactive-gate handling (issue #74, defect 1) ──────────
# A PLAN gate renders as an interactive AskUserQuestion MENU (tab/arrow/enter) that
# ignores typed free text, so a bare `send-keys -l <text>` never answers it. The fix
# is to send Esc FIRST — which cancels the menu, surfaces the questions as text, and
# opens a free-text prompt — then inject the literal answer and submit with Enter.


def _recording_tmux(tmp_path: Path) -> tuple[Path, Path]:
    """A tmux stub that appends each invocation's args to a log and exits 0."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    (fake_bin / "tmux").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    (fake_bin / "tmux").chmod(0o755)
    return fake_bin, log


def test_inject_answer_sends_escape_before_text_then_enter(tmp_path: Path) -> None:
    fake_bin, log = _recording_tmux(tmp_path)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "AFK_INJECT_MENU_PAUSE": "0"}

    result = _call("inject_answer 'afk:1' 'use Redis'", env=env)

    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    esc_idx = next(i for i, ln in enumerate(lines) if "Escape" in ln)
    text_idx = next(i for i, ln in enumerate(lines) if "use Redis" in ln)
    enter_idx = next(i for i, ln in enumerate(lines) if ln.split() and ln.split()[-1] == "Enter")
    assert esc_idx < text_idx < enter_idx, f"expected Esc → text → Enter, got: {lines}"


# ── inject verification: confirm the answer registered (issue #74, defect 2) ──
# A send-keys that silently no-ops (wrong target, busy pane, an unhandled menu)
# leaves the spoke parked indefinitely with no signal. inject_and_verify confirms
# the spoke's transcript advanced after injecting; if it didn't, it re-injects once
# and then fails so the caller escalates rather than leaving the spoke stuck.


def test_inject_and_verify_succeeds_when_transcript_advances(
    spoke_repo: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which store?", [("Redis", "fast")])])
    jsonl = pd / "session.jsonl"
    old = 1_000_000_000
    os.utime(jsonl, (old, old))  # backdate so the spoke's reaction is strictly newer
    # inject_answer is stubbed to advance the transcript (the spoke reacting to input).
    expr = (
        f'inject_answer() {{ printf "{{}}\\n" >> "{jsonl}"; return 0; }}; '
        f"inject_and_verify '{spoke_repo}' 'afk:1' 'use Redis'; echo RC=$?"
    )

    result = _call(
        expr, env={"CLAUDE_PROJECTS_DIR": str(projects), "AFK_INJECT_VERIFY_SECONDS": "0"}
    )

    assert "RC=0" in result.stdout, result.stderr


def test_inject_and_verify_reinjects_once_then_fails_when_stuck(
    spoke_repo: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which store?", [("Redis", "fast")])])
    calls = tmp_path / "calls.log"
    # inject_answer succeeds at the tmux level but the transcript never advances.
    expr = (
        f'inject_answer() {{ printf x >> "{calls}"; return 0; }}; '
        f"inject_and_verify '{spoke_repo}' 'afk:1' 'use Redis'; echo RC=$?"
    )

    result = _call(
        expr,
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_INJECT_VERIFY_SECONDS": "0",
            "AFK_INJECT_POLL_SECONDS": "1",
        },
    )

    assert "RC=1" in result.stdout
    assert calls.read_text() == "xx", "an unregistered answer must be re-injected once (2 attempts)"


# ── the ANSWERER orchestration (stubbed answerer + spoke-ready) ───────────────


@pytest.fixture
def stub_env(tmp_path: Path, spoke_repo: Path) -> dict[str, str]:
    """A waiting spoke + a recording spoke-ready stub + a fake gh, ready to drive
    decide_and_act. The answerer command is set per-test via AFK_ANSWERER_CMD.
    """
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which store?", [("Redis", "fast")])])

    # Recording stub for spoke-ready.sh: append its args to a log.
    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    # Fake gh so build_answerer_prompt is hermetic (no network).
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


def test_decide_and_act_escalates_to_blocked(spoke_repo: Path, stub_env: dict[str, str]) -> None:
    env = {**stub_env, "AFK_ANSWERER_CMD": "printf 'reasoning\\nESCALATE: needs a human'"}

    result = _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log
    assert "needs a human" in log


def test_decide_and_act_no_decision_escalates(spoke_repo: Path, stub_env: dict[str, str]) -> None:
    env = {**stub_env, "AFK_ANSWERER_CMD": "printf 'I never concluded.'"}

    _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log
    assert "no decision" in log


def test_decide_and_act_answer_without_pane_escalates(
    spoke_repo: Path, stub_env: dict[str, str]
) -> None:
    # The answerer decides, but no tmux pane maps to this throwaway path, so injection
    # fails and the supervisor fails safe to escalation rather than dropping the answer.
    env = {**stub_env, "AFK_ANSWERER_CMD": "printf 'ANSWER: do the thing'"}

    _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log
    assert "pane" in log


def test_decide_and_act_injects_and_emits_success_span(spoke_repo: Path, tmp_path: Path) -> None:
    # The happy path: a waiting spoke, an answerer that decides, a pane that maps to the
    # worktree (fake tmux), so the answer is injected and a `success` span is emitted —
    # not an escalation. This exercises the feature's single reasoning step end to end.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which store?", [("Redis", "fast")])])
    jsonl = pd / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # backdate so the reaction is newer

    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "Title\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    # Fake tmux: list-panes maps a pane to this worktree; send-keys succeeds and, on the
    # submitting Enter, advances the spoke's transcript — modelling the spoke reacting so
    # inject_and_verify confirms the answer registered.
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)

    tel_dir = tmp_path / "tel"
    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_ANSWERER_CMD": "printf 'ANSWER: use Redis'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AI_TOOLKIT_TELEMETRY": "1",
        "AI_TOOLKIT_TELEMETRY_DIR": str(tel_dir),
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    # Answered, not escalated.
    assert not ready_log.exists() or "--blocked" not in ready_log.read_text()
    span = json.loads((tel_dir / "events.jsonl").read_text().strip().splitlines()[-1])
    assert span["kind"] == "agent" and span["name"] == "afk-answer"
    assert span["status"] == "success"


def test_decide_and_act_consumes_gate_tag_on_inject(spoke_repo: Path, tmp_path: Path) -> None:
    # When the answerer approves a PLAN-gate park and the answer injects successfully, the
    # gate/<issue> tag must be consumed — otherwise the next tick re-reads it at the tip
    # (the spoke has not committed its first RED/GREEN yet) and re-answers the same gate.
    subprocess.run(["git", "tag", "gate/5"], cwd=spoke_repo, check=True, capture_output=True)
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, _gate_park_records(5))
    jsonl = pd / "session.jsonl"
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # backdate so the reaction is newer

    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    ready_stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "Title\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f'  send-keys) case "$*" in *Enter*) printf "{{}}\\n" >> "{jsonl}" ;; esac ;;\n'
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)

    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_ANSWERER_CMD": "printf 'ANSWER: approved, proceed'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    tag = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "refs/tags/gate/5"],
        cwd=spoke_repo,
        capture_output=True,
        text=True,
    )
    assert tag.returncode != 0, "the gate/5 tag must be consumed after a successful inject"


def test_decide_and_act_escalates_when_answer_does_not_register(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The answerer decides and a pane maps, but the inject never registers (the transcript
    # does not advance). The supervisor must re-inject and then escalate — never leave the
    # spoke silently parked (issue #74, defect 2).
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    _write_transcript(pd, [_ask_record("Which store?", [("Redis", "fast")])])

    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "Title\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)
    # Pane maps, send-keys succeeds, but the transcript is never advanced.
    (fake_bin / "tmux").write_text(
        f'#!/usr/bin/env bash\ncase "$1" in\n  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\nesac\nexit 0\n'
    )
    (fake_bin / "tmux").chmod(0o755)

    env = {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_ANSWERER_CMD": "printf 'ANSWER: use Redis'",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    log = ready_log.read_text()
    assert "--blocked 5" in log
    assert "register" in log


def test_build_answerer_prompt_includes_rule_and_question(
    tmp_path: Path, stub_env: dict[str, str]
) -> None:
    rule = tmp_path / "rule.md"
    rule.write_text("THE-AFK-RULE-MARKER")
    env = {**stub_env, "AFK_RULE_FILE": str(rule)}

    result = _call("build_answerer_prompt 5 'Which store should I use?'", env=env)

    assert "THE-AFK-RULE-MARKER" in result.stdout
    assert "Which store should I use?" in result.stdout
    assert "ANSWER:" in result.stdout and "ESCALATE:" in result.stdout


def test_afk_emit_decision_writes_dashboard_span(spoke_repo: Path, tmp_path: Path) -> None:
    tel_dir = tmp_path / "tel"
    env = {"AI_TOOLKIT_TELEMETRY": "1", "AI_TOOLKIT_TELEMETRY_DIR": str(tel_dir)}

    result = _call(f"afk_emit_decision '{spoke_repo}' success", env=env)

    assert result.returncode == 0, result.stderr
    events = (tel_dir / "events.jsonl").read_text().strip().splitlines()
    span = json.loads(events[-1])
    assert span["kind"] == "agent"
    assert span["name"] == "afk-answer"
    assert span["status"] == "success"


# ── auth-failure handling (issue #73) ─────────────────────────────────────────
# The answerer is the supervisor's own headless `claude`. If the subscription token
# can't refresh mid-run, its output is an auth error rather than a decision. The
# supervisor must recognize that, escalate the parked spoke to blocked/<issue> with an
# auth reason, and raise a stop flag so the loop halts instead of spinning into dead
# auth. `is_auth_failure` is the high-precision predicate (a false positive would halt
# the whole drain), so it matches multi-word auth signatures only.


@pytest.mark.parametrize(
    "text",
    [
        "Invalid API key · Please run /login",
        "authentication_error: OAuth token has expired",
        "API error: 401 Unauthorized",
        "Your credit balance is too low to access the Anthropic API.",
        "Please run `claude /login` to authenticate.",
    ],
)
def test_is_auth_failure_detects_auth_errors(text: str) -> None:
    result = _call('is_auth_failure "$RAW" && echo yes || echo no', env={"RAW": text})

    assert result.stdout.strip() == "yes"


@pytest.mark.parametrize(
    "text",
    [
        "ANSWER: Use Redis for the cache.",
        "ESCALATE: this touches main, a human should decide.",
        "ANSWER: yes, the /login route should require 2FA via OAuth.",
        "ANSWER: we should run the /login migration before deploy.",
        "ANSWER: see the docs to run the new /login flow first.",
        "I am still reasoning about the trade-offs and have not concluded.",
        "",
    ],
)
def test_is_auth_failure_ignores_normal_output(text: str) -> None:
    # A legitimate answer that merely mentions /login or OAuth must NOT trip detection —
    # a false positive would block a healthy spoke and halt the whole drain. The /login
    # signature is anchored to the CLI's "run /login" phrasing, so prose like "run the
    # /login migration" misses.
    result = _call('is_auth_failure "$RAW" && echo yes || echo no', env={"RAW": text})

    assert result.stdout.strip() == "no"


# The CLI prints credential failures to STDERR and exits NONZERO — the production failure
# mode. The stubs reproduce that (write the auth string to stderr, exit 1) so the test
# exercises the real path: run_answerer folds stderr in (2>&1) and decide_and_act gates
# the auth branch on the nonzero exit.


def test_decide_and_act_auth_failure_escalates_with_auth_reason(
    spoke_repo: Path, stub_env: dict[str, str]
) -> None:
    env = {
        **stub_env,
        "AFK_ANSWERER_CMD": "printf 'authentication_error: OAuth token expired' >&2; exit 1",
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    log = Path(env["_READY_LOG"]).read_text()
    assert "--blocked 5" in log
    assert "auth" in log.lower()


def test_decide_and_act_auth_failure_raises_stop_flag(
    spoke_repo: Path, stub_env: dict[str, str]
) -> None:
    # The stop flag is a process global set in decide_and_act (same shell as the loop),
    # so the supervisor can halt rather than spin. Echo it back after the call.
    env = {
        **stub_env,
        "AFK_ANSWERER_CMD": "printf 'Invalid API key · Please run /login' >&2; exit 1",
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5; echo \"FLAG=$_AFK_AUTH_FAILED\"", env=env)

    assert "FLAG=1" in result.stdout


def test_decide_and_act_healthy_answer_mentioning_auth_is_not_a_failure(
    spoke_repo: Path, stub_env: dict[str, str]
) -> None:
    # The answerer SUCCEEDS (exit 0) and its decision merely discusses oauth/login. The
    # nonzero-exit gate means this is a normal decision, NOT an auth-failure halt: the
    # stop flag stays 0 and no "could not refresh" auth block is emitted.
    env = {
        **stub_env,
        "AFK_ANSWERER_CMD": "printf 'ESCALATE: the oauth token expired bug needs a human'",
    }

    result = _call(f"decide_and_act '{spoke_repo}' 5; echo \"FLAG=$_AFK_AUTH_FAILED\"", env=env)

    assert "FLAG=0" in result.stdout
    log = Path(env["_READY_LOG"]).read_text()
    assert "could not refresh" not in log
    assert "human" in log  # the ordinary ESCALATE reason


# ── the --remote launcher (issue #73) ─────────────────────────────────────────
# `/afk --remote` launches a detached, caffeinate-wrapped `/afk drain` on a configured
# always-on Mac over SSH (Tailscale hostname), confirms the tmux session started, and
# prints the reattach command. The remote command is built purely (build_remote_launch_cmd)
# and ssh is stubbed via AFK_SSH so the orchestration runs without a real host.


def _ssh_recorder(tmp_path: Path, log_name: str = "ssh.log", *, exit_code: int = 0) -> Path:
    """An ssh stub that appends its args to a log and exits with exit_code."""
    log = tmp_path / log_name
    stub = tmp_path / "ssh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return stub


def test_build_remote_launch_cmd_contains_all_parts() -> None:
    result = _call("build_remote_launch_cmd '/home/me/ai-toolkit' 'afk' 'bash hub-afk.sh drain'")

    out = result.stdout
    assert "cd '/home/me/ai-toolkit'" in out
    assert "tmux new -d -s 'afk'" in out
    assert "caffeinate -s" in out
    assert "bash hub-afk.sh drain" in out


def test_build_remote_launch_cmd_preserves_drain_args() -> None:
    # AFK_REMOTE_DRAIN_CMD may carry args/flags; they must reach the remote command
    # unquoted so the remote shell runs them as separate words, not one mis-quoted arg.
    result = _call(
        "build_remote_launch_cmd /repo afk 'claude --dangerously-skip-permissions \"/afk drain\"'"
    )

    assert 'caffeinate -s claude --dangerously-skip-permissions "/afk drain"' in result.stdout


def test_remote_reattach_cmd() -> None:
    result = _call("remote_reattach_cmd mac-home afk")

    assert result.stdout.strip() == "ssh mac-home -t 'tmux attach -t afk'"


def test_remote_launch_requires_host() -> None:
    env = {"AFK_REMOTE_HOST": "", "AFK_REMOTE_REPO": "/repo", "AFK_REMOTE_CONF": "/nonexistent"}

    result = _call("remote_launch", env=env)

    assert result.returncode != 0
    assert "AFK_REMOTE_HOST" in result.stderr


def test_remote_launch_requires_repo() -> None:
    env = {"AFK_REMOTE_HOST": "mac-home", "AFK_REMOTE_REPO": "", "AFK_REMOTE_CONF": "/nonexistent"}

    result = _call("remote_launch", env=env)

    assert result.returncode != 0
    assert "AFK_REMOTE_REPO" in result.stderr


def test_remote_launch_invokes_ssh_and_prints_reattach(tmp_path: Path) -> None:
    ssh_stub = _ssh_recorder(tmp_path)
    env = {
        "AFK_REMOTE_HOST": "mac-home",
        "AFK_REMOTE_REPO": "/home/me/ai-toolkit",
        "AFK_REMOTE_SESSION": "afk",
        "AFK_SSH": str(ssh_stub),
        "AFK_REMOTE_CONF": "/nonexistent",
    }

    result = _call("remote_launch", env=env)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "ssh.log").read_text()
    assert "mac-home" in log  # launched on the host
    assert "caffeinate -s" in log  # kept awake for the drain
    assert "tmux new -d -s" in log
    # The default launched command runs the supervisor SCRIPT directly (self-driving,
    # unattended) — NOT an interactive `claude "/afk drain"` that would stall on a prompt.
    assert "hub-afk.sh drain" in log
    assert "has-session" in log  # confirmed the session is up
    assert "ssh mac-home -t 'tmux attach -t afk'" in result.stdout  # reattach hint


def test_remote_launch_fails_when_session_absent(tmp_path: Path) -> None:
    # ssh launch succeeds but the confirm (has-session) fails → non-zero, no false success.
    ssh_stub = tmp_path / "ssh"
    ssh_stub.write_text(
        '#!/usr/bin/env bash\ncase "$*" in *has-session*) exit 1 ;; *) exit 0 ;; esac\n'
    )
    ssh_stub.chmod(0o755)
    env = {
        "AFK_REMOTE_HOST": "mac-home",
        "AFK_REMOTE_REPO": "/repo",
        "AFK_SSH": str(ssh_stub),
        "AFK_REMOTE_CONF": "/nonexistent",
    }

    result = _call("remote_launch", env=env)

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_remote_launch_reads_conf_file_when_env_unset(tmp_path: Path) -> None:
    conf = tmp_path / "afk-remote"
    conf.write_text("AFK_REMOTE_HOST=mac-home\nAFK_REMOTE_REPO=/srv/ai-toolkit\n")
    ssh_stub = _ssh_recorder(tmp_path)
    env = {
        "AFK_REMOTE_HOST": "",
        "AFK_REMOTE_REPO": "",
        "AFK_SSH": str(ssh_stub),
        "AFK_REMOTE_CONF": str(conf),
    }

    result = _call("remote_launch", env=env)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "ssh.log").read_text()
    assert "mac-home" in log
    assert "/srv/ai-toolkit" in log


def test_remote_launch_env_overrides_conf_file(tmp_path: Path) -> None:
    conf = tmp_path / "afk-remote"
    conf.write_text("AFK_REMOTE_HOST=from-file\nAFK_REMOTE_REPO=/from/file\n")
    ssh_stub = _ssh_recorder(tmp_path)
    env = {
        "AFK_REMOTE_HOST": "from-env",
        "AFK_REMOTE_REPO": "/from/file",
        "AFK_SSH": str(ssh_stub),
        "AFK_REMOTE_CONF": str(conf),
    }

    result = _call("remote_launch", env=env)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "ssh.log").read_text()
    assert "from-env" in log
    assert "from-file" not in log


# ── in-flight scope exclusion (issue #74, defect 3) ───────────────────────────
# The supervisor must feed every live spoke's Scope into batch-plan so an overlapping
# ready issue is held back. A regression on the maiden run co-dispatched two spokes
# whose scopes overlapped. _inflight_scope_args reads each in-flight issue's Scope and
# emits a --inflight flag; an UNRESOLVABLE scope (gh failure / no Scope line) is treated
# as exclusive (--inflight *) so an unknown-scope spoke fails CLOSED, never co-dispatched.


def _gh_stub(tmp_path: Path, body: str) -> Path:
    """A fake `gh` whose `issue view` prints <body>; repo/graphql handled by callers.

    `%b` so any `\\n` in <body> expands to a real newline (a Scope: line on its own).
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "gh").write_text(f'#!/usr/bin/env bash\nprintf "%b\\n" "{body}"\n')
    (fake_bin / "gh").chmod(0o755)
    return fake_bin


def test_inflight_scope_args_passes_resolved_scope(tmp_path: Path) -> None:
    fake_bin = _gh_stub(tmp_path, "intro line\\nScope: a.py b.py")
    expr = 'inflight_issues() { printf "72\\n"; }; _inflight_scope_args'

    result = _call(expr, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.stdout.splitlines() == ["--inflight", "a.py b.py"]


def test_inflight_scope_args_marks_unresolved_scope_exclusive(tmp_path: Path) -> None:
    # No Scope: line in the body ⇒ the live spoke's footprint is unknown ⇒ exclusive,
    # so batch-plan holds back EVERY ready issue until it lands (fail closed).
    fake_bin = _gh_stub(tmp_path, "a body with no scope line")
    expr = 'inflight_issues() { printf "72\\n"; }; _inflight_scope_args'

    result = _call(expr, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.stdout.splitlines() == ["--inflight", "*"]


def test_dispatch_batch_holds_back_inflight_scope_overlap(tmp_path: Path) -> None:
    # End to end through the REAL batch-plan.sh: a live spoke (#72, Scope a.py) must
    # exclude the ready, overlapping #73 (a.py) while the disjoint #5 (d.py) dispatches.
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            [
                {"number": 73, "body": "Scope: a.py\n", "blockedBy": {"nodes": []}},
                {"number": 5, "body": "Scope: d.py\n", "blockedBy": {"nodes": []}},
            ]
        )
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1 $2" in\n'
        '  "issue view") printf "body\\nScope: a.py\\n" ;;\n'
        '  "repo view") echo "octo ai-toolkit" ;;\n'
        f'  "api graphql") cat "{backlog}" ;;\n'
        "esac\n"
    )
    (fake_bin / "gh").chmod(0o755)
    dispatched = tmp_path / "dispatched.log"
    wt_new = tmp_path / "wtnew.sh"
    wt_new.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{dispatched}"\n')
    wt_new.chmod(0o755)

    batch_plan = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "batch-plan.sh"
    expr = 'inflight_issues() { printf "72\\n"; }; inflight_worktrees() { :; }; dispatch_batch'
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BATCH_PLAN": str(batch_plan),
        "WT_NEW": str(wt_new),
    }

    result = _call(expr, env=env)

    assert result.returncode == 0, result.stderr
    landed = dispatched.read_text().split() if dispatched.exists() else []
    assert "5" in landed, "the disjoint ready issue must dispatch"
    assert "73" not in landed, "#73 overlaps the in-flight #72 (a.py) and must be held back"
    assert "72" not in landed, "the already-in-flight spoke must not be re-dispatched"


def test_dispatch_batch_stamps_mode_afk(tmp_path: Path) -> None:
    # An afk-supervised dispatch tags the spoke `mode=afk` by passing `--mode afk`
    # to worktree-new.sh, so langfuse_spoke_tree.py can distinguish drain-driven
    # spokes from hand-dispatched (attended) ones (issue #102).
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps([{"number": 5, "body": "Scope: d.py\n", "blockedBy": {"nodes": []}}])
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1 $2" in\n'
        '  "issue view") printf "body\\nScope: d.py\\n" ;;\n'
        '  "repo view") echo "octo ai-toolkit" ;;\n'
        f'  "api graphql") cat "{backlog}" ;;\n'
        "esac\n"
    )
    (fake_bin / "gh").chmod(0o755)
    dispatched = tmp_path / "dispatched.log"
    wt_new = tmp_path / "wtnew.sh"
    wt_new.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{dispatched}"\n')
    wt_new.chmod(0o755)

    batch_plan = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "batch-plan.sh"
    expr = "inflight_issues() { :; }; inflight_worktrees() { :; }; dispatch_batch"
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BATCH_PLAN": str(batch_plan),
        "WT_NEW": str(wt_new),
    }

    result = _call(expr, env=env)

    assert result.returncode == 0, result.stderr
    assert "--mode afk" in dispatched.read_text(), "afk dispatch must stamp mode=afk"


# ── auto-land scoping: only this run's dispatches (issue #74, defect 4) ────────
# auto_land gates on the ready/<issue> marker (the readiness contract), not on which run
# dispatched the spoke: a foreign ready/<issue> left by a parallel session is adopted and
# landed by default (#95). AFK_LAND_FOREIGN=0 restores the dispatched-only isolation for
# concurrent sessions (#74). A foreign spoke with NO ready/<issue> is still left alone —
# _ready_at_tip filters it out upstream of the foreign check.


def _land_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A land-script stub that records the issue it was asked to land."""
    land_log = tmp_path / "land.log"
    stub = tmp_path / "wtland.sh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "{land_log}"\n')
    stub.chmod(0o755)
    return stub, land_log


def test_auto_land_lands_foreign_ready_spoke_by_default(spoke_repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "tag", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    wt_land, land_log = _land_recorder(tmp_path)
    statedir = tmp_path / "statedir"  # empty: no dispatch-5.epoch ⇒ foreign

    expr = f'inflight_worktrees() {{ printf "{spoke_repo}\\t5\\n"; }}; auto_land'
    _call(expr, env={"WT_LAND": str(wt_land), "AFK_STATE_DIR": str(statedir)})

    assert land_log.read_text().split() == ["5"], (
        "a foreign ready spoke must be adopted and landed by default (the ready/N marker is "
        "the contract, not which run dispatched it)"
    )


def test_auto_land_skips_foreign_ready_spoke_when_opted_out(
    spoke_repo: Path, tmp_path: Path
) -> None:
    subprocess.run(["git", "tag", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    wt_land, land_log = _land_recorder(tmp_path)
    statedir = tmp_path / "statedir"  # empty: no dispatch-5.epoch ⇒ foreign
    expr = f'inflight_worktrees() {{ printf "{spoke_repo}\\t5\\n"; }}; auto_land'

    _call(
        expr,
        env={"WT_LAND": str(wt_land), "AFK_STATE_DIR": str(statedir), "AFK_LAND_FOREIGN": "0"},
    )

    assert not land_log.exists() or land_log.read_text().strip() == "", (
        "AFK_LAND_FOREIGN=0 restores the dispatched-only isolation: a foreign ready spoke "
        "must not be auto-landed"
    )


def test_auto_land_skips_foreign_without_ready_marker(spoke_repo: Path, tmp_path: Path) -> None:
    # No ready/5 tag at the tip: foreign and not ready ⇒ left alone even under the new default.
    wt_land, land_log = _land_recorder(tmp_path)
    statedir = tmp_path / "statedir"  # empty: no dispatch-5.epoch ⇒ foreign
    expr = f'inflight_worktrees() {{ printf "{spoke_repo}\\t5\\n"; }}; auto_land'

    _call(expr, env={"WT_LAND": str(wt_land), "AFK_STATE_DIR": str(statedir)})

    assert not land_log.exists() or land_log.read_text().strip() == "", (
        "a foreign spoke with no ready/N marker must be left alone (the marker is required)"
    )


def test_auto_land_lands_dispatched_ready_spoke(spoke_repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "tag", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    wt_land, land_log = _land_recorder(tmp_path)
    statedir = tmp_path / "statedir"
    statedir.mkdir()
    (statedir / "dispatch-5.epoch").write_text("1000\n")  # this run dispatched #5
    expr = f'inflight_worktrees() {{ printf "{spoke_repo}\\t5\\n"; }}; auto_land'

    _call(expr, env={"WT_LAND": str(wt_land), "AFK_STATE_DIR": str(statedir)})

    assert land_log.read_text().split() == ["5"], "a dispatched ready spoke must be landed"


def test_auto_land_lands_foreign_when_opted_in(spoke_repo: Path, tmp_path: Path) -> None:
    subprocess.run(["git", "tag", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    wt_land, land_log = _land_recorder(tmp_path)
    statedir = tmp_path / "statedir"  # empty, but opt-in is set
    expr = f'inflight_worktrees() {{ printf "{spoke_repo}\\t5\\n"; }}; auto_land'

    _call(
        expr,
        env={"WT_LAND": str(wt_land), "AFK_STATE_DIR": str(statedir), "AFK_LAND_FOREIGN": "1"},
    )

    assert land_log.read_text().split() == ["5"], "AFK_LAND_FOREIGN=1 lands a foreign spoke"


def test_clear_dispatch_epochs_drops_stale_entries(tmp_path: Path) -> None:
    statedir = tmp_path / "statedir"
    statedir.mkdir()
    (statedir / "dispatch-9.epoch").write_text("1000\n")
    expr = "_clear_dispatch_epochs; ls $(_afk_state_dir) 2>/dev/null | wc -l"

    result = _call(expr, env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "0", "arming a window must clear stale dispatch epochs"


# ── UNATTENDED marker wiring (issue #74, defect 5) ────────────────────────────
# The supervisor drops/removes an `unattended` marker under the state dir while a window
# is armed; anti-gutting-scan.sh reads it to fail closed on a test-gutting diff.


def test_afk_set_unattended_creates_marker(tmp_path: Path) -> None:
    statedir = tmp_path / "statedir"
    expr = "_afk_set_unattended; test -f $(_afk_unattended_marker) && echo present || echo absent"

    result = _call(expr, env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "present"


def test_afk_clear_unattended_removes_marker(tmp_path: Path) -> None:
    statedir = tmp_path / "statedir"
    statedir.mkdir()
    (statedir / "unattended").write_text("")
    expr = "_afk_clear_unattended; test -f $(_afk_unattended_marker) && echo present || echo absent"

    result = _call(expr, env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "absent"


# ── the HEARTBEAT layer (issue #107) ──────────────────────────────────────────
# A silent supervisor crash (exit 0 mid-tick) left .afk-state reading `draining` with no
# process behind it: --status echoed a healthy run that was gone (#107). The fix is a
# heartbeat the supervisor stamps each tick — "<pid> <last_tick_epoch>" under the git
# common dir — so a second shell (and the watchdog) can tell a LIVE supervisor from a
# stale state file by cross-checking pid liveness.


def test_afk_write_heartbeat_records_pid_and_epoch(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat"
    # afk_write_heartbeat stamps THIS process's pid and AFK_NOW; echo $$ to compare.
    expr = f'afk_write_heartbeat; printf "PID=%s\\n" "$$"; cat "{hb}"'

    result = _call(expr, env={"AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000000"})

    assert result.returncode == 0, result.stderr
    pid = next(ln[4:] for ln in result.stdout.splitlines() if ln.startswith("PID="))
    written = hb.read_text().strip()
    assert written == f"{pid} 1700000000", f"heartbeat must be '<pid> <epoch>', got {written!r}"


def test_afk_read_heartbeat_round_trips(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat"
    hb.write_text("4242 1700000000\n")

    result = _call("afk_read_heartbeat", env={"AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "4242 1700000000"


def test_afk_read_heartbeat_empty_when_absent(tmp_path: Path) -> None:
    hb = tmp_path / "nope"

    result = _call("afk_read_heartbeat", env={"AFK_HEARTBEAT": str(hb)})

    assert result.returncode == 0, result.stderr  # the function ran (not command-not-found)
    assert result.stdout.strip() == ""


def test_afk_clear_heartbeat_removes_file(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat"
    hb.write_text("4242 1700000000\n")
    expr = f'afk_clear_heartbeat; test -f "{hb}" && echo present || echo absent'

    result = _call(expr, env={"AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "absent"


def test_afk_pid_alive_true_for_running_process() -> None:
    # $$ is the sourcing shell — guaranteed alive while the expression runs.
    result = _call("_afk_pid_alive $$ && echo yes || echo no")

    assert result.stdout.strip() == "yes"


def test_afk_pid_alive_false_for_dead_process() -> None:
    # A subshell's pid is reaped by the time the outer shell checks it → not alive.
    result = _call('dead=$(sh -c "echo \\$$"); _afk_pid_alive "$dead" && echo yes || echo no')

    assert result.stdout.strip() == "no"


@pytest.mark.parametrize("pid", ["", "abc", "12x"])
def test_afk_pid_alive_false_for_empty_or_garbage(pid: str) -> None:
    result = _call(f"_afk_pid_alive '{pid}' && echo yes || echo no")

    assert result.stdout.strip() == "no"


def test_afk_supervisor_state_off_when_no_window(tmp_path: Path) -> None:
    # No .afk-state armed ⇒ off, regardless of any heartbeat.
    state = tmp_path / "state"  # absent
    hb = tmp_path / "heartbeat"  # absent

    result = _call("afk_supervisor_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "off"


def test_afk_supervisor_state_live_when_pid_alive(tmp_path: Path) -> None:
    # Window armed AND the heartbeat pid ($$, alive) ⇒ live.
    state = tmp_path / "state"
    state.write_text("drain\n")
    hb = tmp_path / "heartbeat"
    expr = f'printf "%s 1700000000\\n" "$$" > "{hb}"; afk_supervisor_state'

    result = _call(expr, env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "live"


def test_afk_supervisor_state_stale_when_pid_dead(tmp_path: Path) -> None:
    # Window armed but the heartbeat pid is gone (a reaped subshell pid) ⇒ stale.
    state = tmp_path / "state"
    state.write_text("drain\n")
    hb = tmp_path / "heartbeat"
    expr = (
        f'dead=$(sh -c "echo \\$$"); printf "%s 1700000000\\n" "$dead" > "{hb}"; '
        "afk_supervisor_state"
    )

    result = _call(expr, env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "stale"


def test_afk_supervisor_state_stale_when_no_heartbeat(tmp_path: Path) -> None:
    # Window armed but the supervisor never wrote (or cleared) its heartbeat ⇒ stale,
    # not live — a missing heartbeat is the absence of a live process, not proof of one.
    state = tmp_path / "state"
    state.write_text("drain\n")
    hb = tmp_path / "heartbeat"  # absent

    result = _call("afk_supervisor_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "stale"


def test_heartbeat_age_minutes_counts_up(tmp_path: Path) -> None:
    hb = tmp_path / "heartbeat"
    hb.write_text("4242 1700000000\n")  # 600s = 10 min before AFK_NOW

    result = _call(
        "_afk_heartbeat_age_minutes",
        env={"AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"},
    )

    assert result.stdout.strip() == "10"


def test_heartbeat_age_minutes_empty_when_absent(tmp_path: Path) -> None:
    hb = tmp_path / "nope"

    result = _call(
        "_afk_heartbeat_age_minutes", env={"AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"}
    )

    assert result.returncode == 0, result.stderr  # the function ran (not command-not-found)
    assert result.stdout.strip() == ""


# ── truthful --status (issue #107) ────────────────────────────────────────────
# The #107 symptom: a crashed supervisor (exited 0 mid-tick) left .afk-state reading
# `draining`, so --status echoed a healthy run that was gone. _status now cross-checks
# the heartbeat pid and reports STALE instead of the state file's lie.


def _armed_state(tmp_path: Path, value: str) -> Path:
    state = tmp_path / "state"
    state.write_text(f"{value}\n")
    return state


def test_status_reports_stale_when_supervisor_dead(tmp_path: Path) -> None:
    # Window armed (drain) but the heartbeat pid is gone ⇒ STALE, never `draining`.
    state = _armed_state(tmp_path, "drain")
    hb = tmp_path / "heartbeat"
    expr = f'dead=$(sh -c "echo \\$$"); printf "%s 1700000000\\n" "$dead" > "{hb}"; _status'

    result = _call(
        expr,
        env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"},
    )

    assert "STALE" in result.stdout
    assert "10m ago" in result.stdout  # 600s since the last tick
    assert "draining" not in result.stdout, "a dead supervisor must not report `draining` (#107)"


def test_status_reports_stale_when_no_heartbeat(tmp_path: Path) -> None:
    # Window armed but the supervisor never wrote a heartbeat ⇒ STALE, not `on`/`draining`.
    state = _armed_state(tmp_path, "drain")
    hb = tmp_path / "nope"

    result = _call(
        "_status",
        env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"},
    )

    assert "STALE" in result.stdout
    assert "draining" not in result.stdout


def test_status_reports_stale_for_dead_clock_bound_window(tmp_path: Path) -> None:
    # A clock-bound window still ahead, but the supervisor pid is gone ⇒ STALE, not the
    # "Nm remaining" line the state file alone would print.
    state = _armed_state(tmp_path, "1700003600")  # 1h after AFK_NOW
    hb = tmp_path / "heartbeat"
    expr = f'dead=$(sh -c "echo \\$$"); printf "%s 1700000000\\n" "$dead" > "{hb}"; _status'

    result = _call(
        expr,
        env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"},
    )

    assert "STALE" in result.stdout
    assert "remaining" not in result.stdout


def test_status_still_draining_when_supervisor_live(tmp_path: Path) -> None:
    # Window armed AND a live heartbeat pid ($$) ⇒ the normal `draining` line, no STALE.
    state = _armed_state(tmp_path, "drain")
    hb = tmp_path / "heartbeat"
    expr = f'printf "%s 1700000000\\n" "$$" > "{hb}"; _status'

    result = _call(
        expr,
        env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb), "AFK_NOW": "1700000600"},
    )

    assert "STALE" not in result.stdout
    assert "draining" in result.stdout


def test_status_off_unaffected_by_heartbeat(tmp_path: Path) -> None:
    # No window armed ⇒ off, even if a stale heartbeat file lingers from a prior run.
    state = tmp_path / "state"  # absent
    hb = tmp_path / "heartbeat"
    hb.write_text("4242 1700000000\n")

    result = _call("_status", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "/afk: off"


# ── the WATCHDOG: auto-restart a crashed supervisor (issue #107) ───────────────
# A silent supervisor crash leaves .afk-state armed with no process draining. The
# watchdog is a thin outer loop that, each interval, respawns the supervisor when the
# window is armed (afk_supervisor_state == stale) but no live pid is stamping the
# heartbeat. The respawn is a NO-ARG resume: it reads the persisted window and re-adopts
# in-flight spokes idempotently rather than re-dispatching. --off clears the state, so the
# watchdog observes `off` and exits without respawning.


def test_watchdog_tick_off_when_no_window(tmp_path: Path) -> None:
    state = tmp_path / "state"  # absent ⇒ off
    hb = tmp_path / "heartbeat"
    marker = tmp_path / "respawned"
    env = {
        "AFK_STATE": str(state),
        "AFK_HEARTBEAT": str(hb),
        "AFK_RESPAWN_CMD": f"touch {marker}",
    }

    result = _call("watchdog_tick", env=env)

    assert result.stdout.strip() == "off"
    assert not marker.exists(), "no window ⇒ the watchdog must not respawn"


def test_watchdog_tick_live_does_not_respawn(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("drain\n")
    hb = tmp_path / "heartbeat"
    marker = tmp_path / "respawned"
    # $$ (the sourcing shell) is a live pid ⇒ the supervisor is live ⇒ no respawn.
    expr = f'printf "%s 1700000000\\n" "$$" > "{hb}"; watchdog_tick'
    env = {
        "AFK_STATE": str(state),
        "AFK_HEARTBEAT": str(hb),
        "AFK_RESPAWN_CMD": f"touch {marker}",
    }

    result = _call(expr, env=env)

    assert result.stdout.strip() == "live"
    assert not marker.exists(), "a live supervisor must not be respawned"


def test_watchdog_tick_respawns_when_stale(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("drain\n")
    hb = tmp_path / "heartbeat"
    marker = tmp_path / "respawned"
    # A reaped subshell pid ⇒ stale ⇒ the watchdog respawns the supervisor.
    expr = f'dead=$(sh -c "echo \\$$"); printf "%s 1700000000\\n" "$dead" > "{hb}"; watchdog_tick'
    env = {
        "AFK_STATE": str(state),
        "AFK_HEARTBEAT": str(hb),
        "AFK_RESPAWN_CMD": f"touch {marker}",
    }

    result = _call(expr, env=env)

    assert result.stdout.strip() == "respawned"
    assert marker.exists(), "a crashed supervisor (armed window, dead pid) must be respawned"


def test_afk_self_points_at_hub_afk_script() -> None:
    result = _call("_afk_self")

    assert result.stdout.strip().endswith("hub-afk.sh")


def test_resume_launch_is_no_arg_resume() -> None:
    # The respawn must NOT carry a window spec — a no-arg launch resumes the persisted
    # .afk-state (re-adopting spokes), never re-arming a fresh window.
    result = _call("_afk_resume_launch")

    out = result.stdout
    assert "hub-afk.sh" in out
    for window_spec in ("drain", "until", "--once", "--watchdog"):
        assert window_spec not in out, f"resume must be bare (no '{window_spec}'): {out!r}"


def test_spawn_watchdog_skips_when_one_alive(tmp_path: Path) -> None:
    wf = tmp_path / "watchdog"
    marker = tmp_path / "spawned"
    # A live watchdog pid ($$) already recorded ⇒ no second watchdog is spawned.
    expr = f'printf "%s\\n" "$$" > "{wf}"; _afk_spawn_watchdog'
    env = {"AFK_WATCHDOG_FILE": str(wf), "AFK_WATCHDOG_SPAWN_CMD": f"touch {marker}"}

    _call(expr, env=env)

    assert not marker.exists(), "exactly one watchdog — a live one must not be duplicated"


def test_spawn_watchdog_spawns_when_none_alive(tmp_path: Path) -> None:
    wf = tmp_path / "watchdog"  # absent ⇒ no live watchdog
    marker = tmp_path / "spawned"
    env = {"AFK_WATCHDOG_FILE": str(wf), "AFK_WATCHDOG_SPAWN_CMD": f"touch {marker}"}

    _call("_afk_spawn_watchdog", env=env)

    assert marker.exists(), "no live watchdog ⇒ one must be spawned"


def test_spawn_watchdog_spawns_when_recorded_pid_dead(tmp_path: Path) -> None:
    wf = tmp_path / "watchdog"
    marker = tmp_path / "spawned"
    # A recorded but reaped pid ⇒ the prior watchdog is gone ⇒ respawn a fresh one.
    expr = f'dead=$(sh -c "echo \\$$"); printf "%s\\n" "$dead" > "{wf}"; _afk_spawn_watchdog'
    env = {"AFK_WATCHDOG_FILE": str(wf), "AFK_WATCHDOG_SPAWN_CMD": f"touch {marker}"}

    _call(expr, env=env)

    assert marker.exists(), "a dead recorded watchdog pid ⇒ a fresh watchdog must be spawned"


def test_watchdog_loop_exits_when_window_off(tmp_path: Path) -> None:
    # --off clears .afk-state; the watchdog's next pass sees `off`, exits, and never
    # respawns (AC: --off stops the watchdog). State is absent ⇒ the loop breaks at once.
    state = tmp_path / "state"  # absent ⇒ off
    hb = tmp_path / "heartbeat"
    wf = tmp_path / "watchdog"
    marker = tmp_path / "respawned"
    env = {
        "AFK_STATE": str(state),
        "AFK_HEARTBEAT": str(hb),
        "AFK_WATCHDOG_FILE": str(wf),
        "AFK_RESPAWN_CMD": f"touch {marker}",
        "AFK_WATCHDOG_SECONDS": "0",
    }

    result = _call("watchdog_loop", env=env)

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "with the window off the watchdog must exit without respawning"


# ── telemetry preflight (issue #108) ──────────────────────────────────────────
# The hub's posture is the INVERSE of the spoke's (#106 wt_otel_*_preflight, which
# warn-and-continue): for an unattended drain the dashboard is the single source of
# truth, so afk_telemetry_preflight ensures collector(:4317) + bridge(:4319) + auth are
# wired BEFORE the first dispatch and REFUSES to arm (non-zero, loud) when it can't —
# never a per-spawn warning that scrolls past unattended. It REUSES worktree-lib's
# launchers (wt_collector_launch / wt_bridge_launch) and probe (wt_port_listening), so
# the tests stub those three: each launcher echoes a marker and (faithfully) marks its
# port "up" via a tmp file the probe reads, so a successful launch flips the post-launch
# re-probe to up. AI_TOOLKIT_OTEL=0 is the sole opt-out (unset ⇒ enabled — the
# SSOT-for-unattended default), and a successful preflight EXPORTS the resolved auth so
# every dispatched spoke inherits working credentials.


def _telemetry_prelude(
    up_dir: Path, *, collector_up: bool, bridge_up: bool, launch_binds: bool = True
) -> str:
    """Shell overriding the three worktree-lib collaborators with file-backed stubs.

    wt_port_listening reports a port "up" iff a marker file named for it exists under
    up_dir, so a launcher that touches that file flips the post-launch re-probe to up.
    When launch_binds is False the launchers only echo (no touch), modelling a server
    that never binds — so the preflight observes it still down and must refuse.
    """
    bind_c = f'touch "{up_dir}/4317"' if launch_binds else ":"
    bind_b = f'touch "{up_dir}/4319"' if launch_binds else ":"
    lines = [
        f'wt_port_listening() {{ [ -e "{up_dir}/$1" ]; }}',
        f'wt_collector_launch() {{ echo "LAUNCHED-COLLECTOR $1"; {bind_c}; }}',
        f'wt_bridge_launch() {{ echo "LAUNCHED-BRIDGE $1"; {bind_b}; }}',
    ]
    if collector_up:
        lines.append(f'touch "{up_dir}/4317"')
    if bridge_up:
        lines.append(f'touch "{up_dir}/4319"')
    return "; ".join(lines)


def _run_preflight(
    tmp_path: Path,
    *,
    otel: str | None = None,
    auth: bool = True,
    collector_up: bool = False,
    bridge_up: bool = False,
    launch_binds: bool = True,
    tail: str = "",
) -> subprocess.CompletedProcess[str]:
    """Source hub-afk.sh, stub the launchers, and run afk_telemetry_preflight /repo.

    otel / auth are set in the shell (not the env dict) so the host environment never
    leaks in: otel=None unsets AI_TOOLKIT_OTEL (the enabled-by-default case). A fresh
    AFK_TELEMETRY_CONF path keeps the optional conf file out of the picture.
    """
    up_dir = tmp_path / "ports"
    up_dir.mkdir(exist_ok=True)
    otel_line = "unset AI_TOOLKIT_OTEL" if otel is None else f"export AI_TOOLKIT_OTEL={otel}"
    auth_line = "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH"
    prelude = _telemetry_prelude(
        up_dir, collector_up=collector_up, bridge_up=bridge_up, launch_binds=launch_binds
    )
    expr = f'{otel_line}; {auth_line}; {prelude}; afk_telemetry_preflight /repo; echo "RC=$?"{tail}'
    return _call(expr, env={"AFK_TELEMETRY_CONF": str(tmp_path / "no-conf")})


def test_preflight_launches_collector_and_bridge_when_down_and_authed(tmp_path: Path) -> None:
    # Enabled (otel unset), auth present, both ports down ⇒ bring both up, arm (RC 0).
    result = _run_preflight(tmp_path, auth=True, collector_up=False, bridge_up=False)

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "LAUNCHED-COLLECTOR /repo" in result.stdout
    assert "LAUNCHED-BRIDGE /repo" in result.stdout


def test_preflight_idempotent_when_both_already_up(tmp_path: Path) -> None:
    # Both already listening ⇒ never a second collector/bridge, still arm (RC 0).
    result = _run_preflight(tmp_path, auth=True, collector_up=True, bridge_up=True)

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "LAUNCHED" not in result.stdout, "a live collector/bridge must not be relaunched"


def test_preflight_refuses_when_auth_missing(tmp_path: Path) -> None:
    # No resolvable auth ⇒ collector/bridge would 401 silently, so REFUSE loudly and
    # never launch anything (the loud, SSOT-for-unattended behavior the issue wants).
    result = _run_preflight(tmp_path, auth=False, collector_up=False, bridge_up=False)

    assert "RC=0" not in result.stdout, "missing auth must refuse to arm, not pass"
    assert "LANGFUSE_BASIC_AUTH" in result.stderr
    assert "LAUNCHED" not in result.stdout, (
        "without auth the preflight must not launch a 401 server"
    )


def test_preflight_refuses_when_collector_wont_come_up(tmp_path: Path) -> None:
    # Authed, collector down, but its launch never binds :4317 ⇒ refuse to arm.
    result = _run_preflight(
        tmp_path, auth=True, collector_up=False, bridge_up=False, launch_binds=False
    )

    assert "RC=0" not in result.stdout, "a collector that won't come up must refuse to arm"
    assert "4317" in result.stderr or "collector" in result.stderr.lower()


def test_preflight_refuses_when_bridge_wont_come_up(tmp_path: Path) -> None:
    # Collector already up, bridge down and its launch never binds :4319 ⇒ refuse.
    result = _run_preflight(
        tmp_path, auth=True, collector_up=True, bridge_up=False, launch_binds=False
    )

    assert "RC=0" not in result.stdout, "a bridge that won't come up must refuse to arm"
    assert "4319" in result.stderr or "bridge" in result.stderr.lower()


def test_preflight_noop_when_otel_disabled(tmp_path: Path) -> None:
    # AI_TOOLKIT_OTEL=0 is the sole opt-out: a clean no-op even with no auth + ports down
    # — never launches, never refuses (so arming proceeds normally).
    result = _run_preflight(tmp_path, otel="0", auth=False, collector_up=False, bridge_up=False)

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" not in result.stderr


def test_preflight_exports_resolved_auth_for_spoke_inheritance(tmp_path: Path) -> None:
    # A successful preflight must EXPORT the wired config so every spoke dispatch_batch
    # spawns inherits it — proven by a child shell that sees only exported vars.
    child = (
        "; bash -c 'echo \"C_OTEL=$AI_TOOLKIT_OTEL C_AUTH=$LANGFUSE_BASIC_AUTH "
        "C_HOST=$LANGFUSE_HOST\"'"
    )
    result = _run_preflight(tmp_path, auth=True, collector_up=True, bridge_up=True, tail=child)

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_OTEL=1" in result.stdout, "spokes must inherit AI_TOOLKIT_OTEL=1 (the opt-in)"
    assert "C_AUTH=Basic-xyz" in result.stdout, "spokes must inherit working LANGFUSE_BASIC_AUTH"
    assert "C_HOST=" in result.stdout and "C_HOST= " not in result.stdout, "LANGFUSE_HOST exported"


def test_preflight_resolves_auth_from_conf_file(tmp_path: Path) -> None:
    # Env auth absent but the optional conf file (mirroring ~/.afk-remote) supplies it ⇒
    # resolve from the file, export it, and arm.
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-from-file"\n')
    up_dir = tmp_path / "ports"
    up_dir.mkdir(exist_ok=True)
    prelude = _telemetry_prelude(up_dir, collector_up=True, bridge_up=True)
    expr = (
        "unset AI_TOOLKIT_OTEL; unset LANGFUSE_BASIC_AUTH; "
        f'{prelude}; afk_telemetry_preflight /repo; echo "RC=$?"; '
        "bash -c 'echo \"C_AUTH=$LANGFUSE_BASIC_AUTH\"'"
    )

    result = _call(expr, env={"AFK_TELEMETRY_CONF": str(conf)})

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH=Basic-from-file" in result.stdout, "auth resolved + exported from the conf file"


def test_preflight_env_auth_wins_over_conf_file(tmp_path: Path) -> None:
    # An explicit env LANGFUSE_BASIC_AUTH outranks the conf file (same precedence as
    # _load_remote_conf): the env value is what spokes inherit.
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-from-file"\n')
    up_dir = tmp_path / "ports"
    up_dir.mkdir(exist_ok=True)
    prelude = _telemetry_prelude(up_dir, collector_up=True, bridge_up=True)
    expr = (
        "unset AI_TOOLKIT_OTEL; export LANGFUSE_BASIC_AUTH=Basic-from-env; "
        f'{prelude}; afk_telemetry_preflight /repo; echo "RC=$?"; '
        "bash -c 'echo \"C_AUTH=$LANGFUSE_BASIC_AUTH\"'"
    )

    result = _call(expr, env={"AFK_TELEMETRY_CONF": str(conf)})

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH=Basic-from-env" in result.stdout, "env auth must win over the conf file"


def test_arm_refuses_when_telemetry_cannot_be_wired(tmp_path: Path) -> None:
    # main()-level: arming a window when telemetry can't be wired must REFUSE — return
    # non-zero, write NO state file, and never reach the supervisor loop (so no spoke is
    # ever dispatched blind). Authed but the collector launch never binds :4317.
    #
    # The supervisor loop is fully neutered (supervise_tick / dispatch_batch / the
    # watchdog stubbed, and `sleep` made to exit) so EVEN A REGRESSION that armed anyway
    # cannot dispatch a real spoke or hang: it would just write state and exit, which the
    # assertions below flag as a failure.
    state = tmp_path / "state"
    up_dir = tmp_path / "ports"
    up_dir.mkdir(exist_ok=True)
    prelude = _telemetry_prelude(up_dir, collector_up=False, bridge_up=False, launch_binds=False)
    neuter = (
        "supervise_tick() { return 0; }; dispatch_batch() { :; }; "
        "_afk_spawn_watchdog() { :; }; afk_done() { return 1; }; sleep() { exit 0; }"
    )
    expr = (
        "unset AI_TOOLKIT_OTEL; export LANGFUSE_BASIC_AUTH=Basic-xyz; "
        f"{prelude}; {neuter}; main 30m"
    )

    result = _call(
        expr,
        env={"AFK_STATE": str(state), "AFK_TELEMETRY_CONF": str(tmp_path / "no-conf")},
    )

    assert result.returncode != 0, "arming with telemetry down must refuse (non-zero)"
    assert not state.exists(), "a refused arm must not write the state file (no blind dispatch)"
    assert "telemetry" in result.stderr.lower()
