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
