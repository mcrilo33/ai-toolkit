"""Park/gate detection tests (gate-broker-detect.sh, #275 partition).

See shared/skills/hub/scripts/gate-broker-detect.sh.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from _gate_broker_support import (
    _DISPLAY_CASE,
    _PERMISSION_PROMPT,
    _agent_ps_stub,
    _ask_record,
    _bash_tool_record,
    _call,
    _fake_tmux_pane,
    _gate_bash_turn,
    _gate_broker_env,
    _gate_tool_result,
    _install_fake_claude,
    _project_dir_for,
    _seed_task_output,
    _spoke_activity_turn,
    _tag_gate_at_head,
    _write_transcript,
)


def _dead_agent_bin(tmp_path: Path, spoke_repo: Path, *, capture: str = "") -> Path:
    """A PATH bin whose tmux maps a pane to <spoke_repo> and reports a pane pid, but whose ps
    shows NO agent under it: the #301 incident shape — a pane very much alive (list-panes maps
    it), running a bare shell after the reboot killed claude. `capture` optionally renders a
    stale permission dialog left in the scrollback. Returns the bin dir to prepend to PATH.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  capture-pane) printf "%s\\n" "{capture}" ;;\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f"{_DISPLAY_CASE}"
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin, agent_alive=False)  # the pane pid has no claude descendant
    return fake_bin


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path, monkeypatch):
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


DETECT_SURFACE = (
    "_transcript_idle_seconds",
    "_task_output_mtime",
    "_spoke_idle_seconds",
    "extract_pending_question",
    "_is_seed_replay",
    "slot_state",
    "spoke_over_ceiling",
    "_gate_parked",
    "_gate_answer_landed",
    "_gate_spoke_coded_past",
    "_gate_artifact_path",
    "_read_gate_artifact",
    "_spoke_still_parked",
    "_spoke_moved_on",
)


def test_detect_module_surface_loads() -> None:
    # The detect module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-detect.sh in (a missing module would
    # leave these undefined and the drain could not detect a parked gate).
    fns = " ".join(DETECT_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_extract_pending_question_drops_failed_gate_emission(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A gate Bash whose tool_result is_error (a deny), then the spoke keeps working: the
    # emission failed, so no park was ever established — extract must return empty.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN PROSE that must not latch a phantom park"),
            _gate_tool_result(is_error=True),
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.stdout.strip() == "", (
        f"a denied gate emission must not latch a phantom park: {result.stdout!r}"
    )


def test_extract_pending_question_keeps_plan_on_successful_gate(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A gate Bash whose tool_result is a SUCCESS (no is_error): a real park — extract still
    # returns the plan prose so the answerer has it to reason about.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("REAL PLAN PROSE for a genuine park"),
            _gate_tool_result(is_error=False),
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert "REAL PLAN PROSE" in result.stdout, (
        f"a successful gate park must still surface the plan: {result.stdout!r}"
    )


def test_extract_pending_question_keeps_plan_before_tool_result(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Backward-compat: a gate Bash with NO tool_result yet (the emission is still resolving, or
    # a fixture omits it) stays a park — the latch clears only on a POSITIVE is_error signal.
    projects = tmp_path / "projects"
    _write_transcript(projects, spoke_repo, [_gate_bash_turn("PLAN awaiting its result")])

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert "PLAN awaiting its result" in result.stdout, result.stdout


def test_slot_state_busy_after_failed_gate_emission(spoke_repo: Path, tmp_path: Path) -> None:
    # The incident shape end to end: a denied gate emission, no gate/<N> tag at the tip, the
    # spoke still working. slot_state must read `busy`, not `waiting` — so the watchdog never
    # answers a phantom park.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN PROSE"),
            _gate_tool_result(is_error=True),
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={"CLAUDE_PROJECTS_DIR": str(projects), "AFK_NOW": "1000000100"},
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr


# ── #313: a TYPED string-content reply un-latches the PLAN gate ────────────────
# Claude Code records a real reply — a human typing in the pane AND the broker's own
# tmux-injected approval — as a STRING-content user turn, not a text block. The pre-#313
# extractor cleared only `pending` on such a turn, so `gate_plan` stayed latched for the
# whole life of the spoke: every post-approval tick returned the stale plan → slot_state
# `waiting` → the answer lane recomputed and every answer was dropped by the #247
# tree-changed guard, burning reasoner runs until park-undeliverable fired.


def _typed_string_reply(text: str = "Approved, proceed with the plan.") -> dict:
    """A genuine reply as Claude Code records it on submit: a STRING-content user turn
    carrying promptSource == "typed" (a human pane reply or the broker's tmux inject)."""
    return {"type": "user", "promptSource": "typed", "message": {"content": text}}


def test_extract_pending_question_clears_plan_on_typed_string_reply(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC1: gate emission, then a typed string-content approval, then the spoke resumes work.
    # The reply resolved the gate → extract must return empty (no phantom park).
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN PROSE that must not latch a phantom park"),
            _typed_string_reply(),
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.stdout.strip() == "", (
        f"a typed string-content reply must un-latch the gate plan: {result.stdout!r}"
    )


def test_slot_state_busy_after_typed_string_gate_reply(spoke_repo: Path, tmp_path: Path) -> None:
    # AC1/AC4 end to end: the injected approval landed as a string-content turn and the spoke
    # keeps working. slot_state must read `busy`, not `waiting` — so no park onset is stamped
    # and the answer lane never services (consumes zero reasoner runs on) the retired gate.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN PROSE"),
            _typed_string_reply(),
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={"CLAUDE_PROJECTS_DIR": str(projects), "AFK_NOW": "1000000100"},
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr


def test_extract_pending_question_keeps_plan_without_reply(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC2 regression: a genuinely-unanswered gate park — the emission, then the spoke's own
    # assistant activity but NO user reply of any shape — still extracts the plan so the
    # answerer keeps its reasoning payload.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("REAL PLAN PROSE for a genuine park"),
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert "REAL PLAN PROSE" in result.stdout, (
        f"an unanswered gate park must still surface the plan: {result.stdout!r}"
    )


def test_extract_pending_question_keeps_plan_on_nontyped_string_reply(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The typed guard: only a promptSource == "typed" string turn is a genuine reply. A
    # string-content user turn WITHOUT it (every synthetic harness turn is non-typed) must
    # NOT false-clear a still-unanswered park — mirrors _gate_answer_landed (#204).
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("REAL PLAN PROSE awaiting a real reply"),
            {"type": "user", "message": {"content": "a synthetic non-typed string turn"}},
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert "REAL PLAN PROSE" in result.stdout, (
        f"a non-typed string turn must not un-latch the park: {result.stdout!r}"
    )


def test_extract_pending_question_clears_plan_on_list_content_reply(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # AC3 regression: the pre-existing list-content text-block un-latch is unchanged — a user
    # turn whose LIST content carries a text block still resolves the gate.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN PROSE that a list-content reply must clear"),
            {"type": "user", "message": {"content": [{"type": "text", "text": "Approved."}]}},
            _spoke_activity_turn(),
        ],
    )

    result = _call(
        f"extract_pending_question '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.stdout.strip() == "", (
        f"a list-content text-block reply must still un-latch the plan: {result.stdout!r}"
    )


# ── issue #312: _gate_spoke_coded_past — the #117 keeps-coding proof ───────────────────────────
# _gate_answer_landed (#204) proves a TYPED reply landed after the gate. The #117 shape — the
# spoke emits gate/<n> then keeps coding WITHOUT a reply — leaves no typed turn, so a distinct
# detector proves it: assistant activity AFTER the gate emission means the spoke coded past the
# gate. The moved-on drop uses this to RETIRE the abandoned episode instead of aging it. Fail-
# closed: no transcript / no python3 / no post-gate assistant activity → rc 1 (never retire on
# an ambiguous read).


def _coded_past(spoke_repo: Path, projects: Path) -> str:
    return (
        _call(
            f"_gate_spoke_coded_past '{spoke_repo}' && echo CODED || echo NO",
            env={"CLAUDE_PROJECTS_DIR": str(projects)},
        )
        .stdout.strip()
        .splitlines()[-1]
    )


def test_gate_spoke_coded_past_true_after_keeps_coding(spoke_repo: Path, tmp_path: Path) -> None:
    # The #117 shape: a gate emission, then the spoke's OWN assistant work with no reply.
    projects = tmp_path / "projects"
    _write_transcript(
        projects, spoke_repo, [_gate_bash_turn("PLAN — then I keep coding"), _spoke_activity_turn()]
    )

    assert _coded_past(spoke_repo, projects) == "CODED"


def test_gate_spoke_coded_past_false_for_bare_park(spoke_repo: Path, tmp_path: Path) -> None:
    # Only the plan + gate Bash, and the gate's own tool_result — no assistant turn after the
    # emission. The spoke is genuinely parked; retiring it would strand a real gate.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [_gate_bash_turn("PLAN awaiting approval"), _gate_tool_result(is_error=False)],
    )

    assert _coded_past(spoke_repo, projects) == "NO"


def test_gate_spoke_coded_past_false_for_typed_reply_only(spoke_repo: Path, tmp_path: Path) -> None:
    # A typed reply then nothing is the #204 path (handled by _gate_answer_landed + the top
    # self-heal), NOT the keeps-coding shape — coded-past must stay false so the two do not conflate.
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        spoke_repo,
        [
            _gate_bash_turn("PLAN — a human then approves"),
            {
                "type": "user",
                "promptSource": "typed",
                "message": {"content": "Approved — proceed."},
            },
        ],
    )

    assert _coded_past(spoke_repo, projects) == "NO"


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


def test_slot_state_permission_park_beats_ceiling(spoke_repo: Path, tmp_path: Path) -> None:
    # #246: a spoke parked on a permission dialog must classify `waiting` — never `reap` —
    # even when it is over BOTH the wall-clock ceiling (AFK_SPOKE_MAX_MINUTES) and the idle
    # ceiling (AFK_IDLE_MINUTES). Pre-fix the ceiling reap preceded park detection, so the
    # over-ceiling park was reaped + revived, re-raising the same dialog forever.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    # An unresolved Bash tool_use → extract_pending_command non-empty → _permission_pending true.
    jsonl.write_text(json.dumps(_bash_tool_record("git reset -q; git add tests/x.py")) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # stale transcript → also over the idle ceiling

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
    (statedir / "dispatch-5.epoch").write_text("1000\n")  # dispatched long ago ⇒ over the ceiling

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_STATE_DIR": str(statedir),
            "AFK_NOW": "1000000000",  # ~31700 min since dispatch → well over the ceiling AND idle
        },
    )

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr


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
        # #241: an ESCALATE verdict now routes to the reasoner (stubbed) instead of parking.
        # The mechanical ESCALATE verdict is still recorded to decisions.log for codification.
        "AFK_ANSWERER_CMD": "printf 'ANSWER: DENY: use git restore instead'",
        "AFK_JOURNAL_GH_COMMENT": "0",
        # Zero the inject verify timings: this stub's tmux never advances the transcript, so
        # the deny-path inject would otherwise burn the full 60s x2 verify budget (real spokes
        # respond, so this is a test-only bound).
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
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


# ── #263/#304: the un-landed clock reads the recorded terminal transition ──────
# The watchdog's auto-land-skipped ceiling measures from the done epoch, which #304 retires to a
# projection of the ready/accepted transition slot_state records (not a stamped file). The
# round-trip: a ready-at-tip read records the transition, and read_done_epoch projects its onset.
def test_slot_state_ready_at_tip_feeds_read_done_epoch_via_the_log(
    spoke_repo: Path, tmp_path: Path
) -> None:
    statedir = tmp_path / "afk-state"
    subprocess.run(["git", "tag", "-f", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"}

    result = _call(f"slot_state '{spoke_repo}' 5", env=env)
    onset = _call("read_done_epoch 5", env=env).stdout.strip()

    assert result.stdout.strip() == "done", result.stdout + result.stderr
    assert not (statedir / "done-5.epoch").exists(), "the epoch file is retired to the log"
    assert onset.isdigit() and int(onset) > 0, f"read_done_epoch must project the onset: {onset!r}"


def test_slot_state_busy_spoke_does_not_stamp_done_epoch(spoke_repo: Path, tmp_path: Path) -> None:
    # A working spoke with no terminal marker must not stamp a done epoch (nothing to land yet).
    statedir = tmp_path / "afk-state"
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        )
        + "\n"
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr
    assert not (statedir / "done-5.epoch").exists()


def test_note_tip_progress_clears_done_epoch_on_tip_advance(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A revived spoke (tip moved past a terminal marker) must drop the stale done epoch so its
    # next done read re-stamps fresh — otherwise the watchdog would measure from a pre-revival
    # transition and false-fire.
    statedir = tmp_path / "afk-state"
    statedir.mkdir(parents=True)
    (statedir / "tip-5").write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")  # a stale tip
    (statedir / "done-5.epoch").write_text("1700000000\n")

    _call(f"_afk_note_tip_progress '{spoke_repo}' 5", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "done-5.epoch").exists()


def test_note_tip_progress_clears_done_epoch_on_first_sighting(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The revival gap: a done-epoch can be live with NO recorded tip (fresh arm wiped tip-<issue>,
    # then a first tick saw the spoke already ready-at-tip and stamped via slot_state's early
    # return, never running this fn). If that spoke is later revived, the first non-terminal
    # sighting here must drop the stale epoch so a re-ready re-stamps fresh — else condition 4
    # measures from the pre-revival transition and false-fires (#263).
    statedir = tmp_path / "afk-state"
    statedir.mkdir(parents=True)
    (statedir / "done-5.epoch").write_text("1700000000\n")  # live epoch, tip-5 absent

    _call(f"_afk_note_tip_progress '{spoke_repo}' 5", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "done-5.epoch").exists()


# ── #265: slot_state stamps the park-onset clock the first tick it reads waiting ───
# The watchdog's park-unanswered never-attempted branch measures its ceiling from this onset,
# NOT from zero — so a fresh park gets the full HUB_WATCHDOG_PARK_CEILING before it may fire.
# A parked spoke stamps it once on its first waiting read; a not-parked spoke clears it so a
# later re-park re-stamps fresh. Mirrors the #263 done-epoch machinery.
def test_slot_state_gate_parked_stamps_park_onset(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "afk-state"
    subprocess.run(["git", "tag", "gate/5"], cwd=spoke_repo, check=True, capture_output=True)
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "parked"}]}}
        )
        + "\n"
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr
    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1700000000"


def test_slot_state_question_parked_stamps_park_onset(spoke_repo: Path, tmp_path: Path) -> None:
    # A permission/question park (no gate tag) still gets an onset stamp — the false-fire
    # window is structural to every park type, not just the PLAN gate the incident hit.
    statedir = tmp_path / "afk-state"
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr
    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1700000000"


# ── #301: a DEAD agent is a crash, not a park — slot_state must not read `waiting` ──
#
# tmux scrollback and git tags OUTLIVE the agent. When a reboot kills claude but the pane
# survives (bare shell), the dialog claude last rendered is still captured and any gate/<issue>
# tag is still at the tip — so every waiting signal still fires. Before #301 slot_state read that
# as `waiting`, the answer lane tried to inject into the shell (ST1 now refuses, but every tick
# fired a spurious "answer did not register — escalating"), and recover_dead_panes SKIPPED the
# `waiting` state so the crash was never revived. The gate/<issue> case is the exact #296/#299
# shape: a git tag is the most durable phantom-park source there is.


def test_slot_state_gate_parked_dead_agent_is_not_waiting(spoke_repo: Path, tmp_path: Path) -> None:
    """The #296/#299 shape: gate/<issue> at the tip, but the agent is gone."""
    subprocess.run(["git", "tag", "gate/5"], cwd=spoke_repo, check=True, capture_output=True)
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}})
        + "\n"
    )
    fake_bin = _dead_agent_bin(tmp_path, spoke_repo)

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() != "waiting", (
        "a gate tag that outlived its agent is a crash, not a park — reading it `waiting` makes "
        f"the answer lane serve a dead shell and hides it from recover_dead_panes: {result.stdout}"
    )


def test_slot_state_permission_dead_agent_is_not_waiting(spoke_repo: Path, tmp_path: Path) -> None:
    """A stale permission dialog in a dead pane's scrollback must not read as a live park."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    jsonl = pd / "session.jsonl"
    jsonl.write_text(json.dumps(_bash_tool_record("git reset -q")) + "\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))
    fake_bin = _dead_agent_bin(tmp_path, spoke_repo, capture=_PERMISSION_PROMPT)

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_NOW": "1000000000",
        },
    )

    assert result.stdout.strip() != "waiting", (
        f"a permission dialog left in a crashed pane's scrollback is not a live park: {result.stdout}"
    )


def test_slot_state_gate_parked_live_agent_stays_waiting(spoke_repo: Path, tmp_path: Path) -> None:
    """Preservation: a gate-parked spoke whose agent IS alive still classifies `waiting`."""
    subprocess.run(["git", "tag", "gate/5"], cwd=spoke_repo, check=True, capture_output=True)
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}})
        + "\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        f'  list-panes) printf "afk:1\\t%s\\n" "{spoke_repo}" ;;\n'
        f"{_DISPLAY_CASE}"
        "esac\nexit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin, agent_alive=True)

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "waiting", (
        f"a live agent parked at the gate is a genuine park: {result.stdout}{result.stderr}"
    )


def test_spoke_still_parked_is_false_for_a_dead_agent(spoke_repo: Path, tmp_path: Path) -> None:
    """_spoke_still_parked drives _reap_or_resume: a dead agent showing a stale dialog must read
    NOT parked, so the reaper revives it instead of routing it back to the answerer (#246)."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(json.dumps(_bash_tool_record("git reset -q")) + "\n")
    fake_bin = _dead_agent_bin(tmp_path, spoke_repo, capture=_PERMISSION_PROMPT)

    result = _call(
        f"_spoke_still_parked '{spoke_repo}' 5 && echo PARKED || echo NOT_PARKED",
        env={
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    assert result.stdout.strip() == "NOT_PARKED", (
        "a dead agent has no live park to service — _spoke_still_parked must fail toward "
        f"'moved on' so the reaper revives rather than re-answers: {result.stdout}{result.stderr}"
    )


def test_stamp_park_onset_epoch_once_is_idempotent(tmp_path: Path) -> None:
    # Stamp-once: the onset clock is fixed at the FIRST waiting tick and never resets while the
    # spoke stays parked, so the full ceiling elapses before the watchdog can fire.
    statedir = tmp_path / "afk-state"
    env = {"AFK_STATE_DIR": str(statedir)}

    _call("stamp_park_onset_epoch_once 5", env={**env, "AFK_NOW": "1700000000"})
    _call("stamp_park_onset_epoch_once 5", env={**env, "AFK_NOW": "1700009999"})  # second tick

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1700000000"


def test_slot_state_busy_spoke_clears_park_onset(spoke_repo: Path, tmp_path: Path) -> None:
    # A spoke that has moved on (no park signal) must drop a stale onset so a later re-park
    # measures the watchdog ceiling from the NEW onset, not the prior park's.
    statedir = tmp_path / "afk-state"
    statedir.mkdir(parents=True)
    (statedir / "park-onset-5.epoch").write_text("1700000000\n")  # a stale onset
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        )
        + "\n"
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr
    assert not (statedir / "park-onset-5.epoch").exists()


def test_clear_progress_state_drops_park_onset(tmp_path: Path) -> None:
    # Per-window state: a leftover onset from a prior drain window would suppress a fresh
    # park's full grace in the next window. A fresh arm wipes it, like done-*.epoch.
    statedir = tmp_path / "afk-state"
    statedir.mkdir(parents=True)
    (statedir / "park-onset-5.epoch").write_text("1700000000\n")

    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "park-onset-5.epoch").exists()


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
        ("pytest -q", "ESCALATE"),  # #203: flags alone still run the whole suite
        ("pytest -x", "ESCALATE"),
        ("python -m pytest -q --tb=short", "ESCALATE"),  # only flags → full suite
        ("pytest -k foo", "ESCALATE"),  # -k's value is not a scoping path → full collection
        ("pytest -m slow", "ESCALATE"),  # -m's value likewise
        ("pytest -p no:cacheprovider", "ESCALATE"),  # -p's value likewise
        ("pytest tests/x.py", "APPROVE"),  # a NON-FLAG arg (path/node-id) scopes it
        ("pytest -q tests/x.py", "APPROVE"),  # flags + a path is fine
        ("pytest -k foo tests/x.py", "APPROVE"),  # a real path alongside -k is fine
        ("pytest tests", "APPROVE"),  # a bare dir target scopes it
        ("python3 -m pytest tests/unit", "APPROVE"),
    ],
)
def test_classify_permission_tightened_cases(cmd: str, verdict: str) -> None:
    result = _call('classify_permission "$CMD" | cut -f1', env={"CMD": cmd})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == verdict, f"{cmd!r}: {result.stdout}"


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


# ── #289: stat flavor ordering (the CI-red bug class already fixed once in #132) ──
#
# Every stat fallback must try the GNU spelling FIRST. GNU stat's `-f` means "display
# filesystem status" and takes no inline format, so a BSD-first `stat -f %m F || stat -c %Y F`
# chain has GNU read `%m` as a missing file operand: it errors on %m yet still PRINTS a
# multi-line filesystem-status block for F and exits nonzero -- so the `||` fallback fires
# too and the capture holds the garbage block AND the epoch. GNU-first inverts this: BSD
# fails the `-c` probe CLEANLY (usage error, empty stdout), so only one answer is ever
# captured. These stubs simulate both flavors, so each pins the ordering on any host.

_GNU_STAT_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "-c" ]; then\n'
    '  case "$2" in\n'
    "    %Y) echo 1000003500; exit 0 ;;\n"
    "    %s) echo 4096; exit 0 ;;\n"
    "  esac\n"
    "fi\n"
    'if [ "$1" = "-f" ]; then\n'
    '  echo "  File: \\"$3\\""\n'
    '  echo "    ID: b505c8e079f9471 Namelen: 255     Type: ext2/ext3"\n'
    '  echo "  Block size: 4096       Fundamental block size: 4096"\n'
    '  echo "stat: cannot read file system information for $2" >&2\n'
    "  exit 1\n"
    "fi\n"
    "exit 1\n"
)

_BSD_STAT_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "-c" ]; then echo "stat: illegal option -- c" >&2; exit 1; fi\n'
    'if [ "$1" = "-f" ]; then\n'
    '  case "$2" in\n'
    "    %m) echo 1000003500; exit 0 ;;\n"
    "    %z) echo 4096; exit 0 ;;\n"
    "  esac\n"
    "fi\n"
    "exit 1\n"
)


def _stat_stub_path(tmp_path: Path, stub_body: str) -> str:
    """Install a fake `stat` and return a PATH with it in front of the real one."""
    bindir = tmp_path / "stat-stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "stat"
    stub.write_text(stub_body)
    stub.chmod(0o755)
    return f"{bindir}:{os.environ['PATH']}"


def _seed_transcript(projects: Path, wt: Path) -> None:
    _project_dir_for(projects, wt).joinpath("session.jsonl").write_text("{}\n")


@pytest.mark.parametrize("stub", [_GNU_STAT_STUB, _BSD_STAT_STUB], ids=["gnu", "bsd"])
def test_transcript_idle_seconds_survives_both_stat_flavors(
    spoke_repo: Path, tmp_path: Path, stub: str
) -> None:
    # The mtime capture must hold the bare epoch on both flavors: under GNU the fs-status
    # block must never leak in, or the `$(afk_now) - mtime` arithmetic chokes on a
    # multi-line string and the caller's RC goes wrong (the four red test_gate_broker nodes).
    projects = tmp_path / "projects"
    _seed_transcript(projects, spoke_repo)

    result = _call(
        f"_transcript_idle_seconds '{spoke_repo}'",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1000003600",
            "PATH": _stat_stub_path(tmp_path, stub),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "100", (
        f"idle must read as a bare epoch delta: {result.stdout!r}{result.stderr}"
    )


@pytest.mark.parametrize("stub", [_GNU_STAT_STUB, _BSD_STAT_STUB], ids=["gnu", "bsd"])
def test_task_output_mtime_survives_both_stat_flavors(
    spoke_repo: Path, tmp_path: Path, stub: str
) -> None:
    # Same ordering contract on the task-output clock: a polluted capture would make the
    # `[ "$mt" -gt "$newest" ]` comparison error and mis-read the reaper's busy signal.
    tasks_root = tmp_path / "tasks-root"
    _seed_task_output(tasks_root, spoke_repo, 1_000_003_500)

    result = _call(
        f"_task_output_mtime '{spoke_repo}'",
        env={
            "AFK_TASKS_ROOT": str(tasks_root),
            "PATH": _stat_stub_path(tmp_path, stub),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1000003500", (
        f"task-output mtime must be a bare epoch: {result.stdout!r}{result.stderr}"
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


# ── #304 (#300 step 5): slot_state is a pure read + a RECONCILER ──────────────────────────────
# Observing a spoke no longer silently stamps the done epoch: the reconciler records the terminal /
# park state as a VISIBLE `actor:reconciler` transition, and read_done_epoch projects its onset from
# the log. A divergence between the log tail and ground truth (a tag at the tip) appends a corrective
# record; a cold start (empty log) rebuilds one record marked lossy.


def _transitions(state_dir: Path, issue: int) -> list[str]:
    p = state_dir / "transitions" / f"{issue}.jsonl"
    return p.read_text().splitlines() if p.is_file() else []


def test_slot_state_ready_at_tip_records_a_ready_transition_not_a_done_epoch(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #2/#3: the done epoch is RETIRED — a ready-at-tip spoke records a VISIBLE ready transition
    # instead of a silent done-5.epoch stamp.
    statedir = tmp_path / "afk-state"
    subprocess.run(["git", "tag", "-f", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"},
    )

    assert result.stdout.strip() == "done", result.stdout + result.stderr
    assert not (statedir / "done-5.epoch").exists(), "the done epoch is retired to the log"
    lines = _transitions(statedir, 5)
    assert any('"to":"ready"' in ln and '"actor":"reconciler"' in ln for ln in lines), lines


def test_slot_state_ready_read_is_idempotent_second_tick_writes_nothing(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #2: steady state — a second read of an already-recorded terminal appends no new record.
    statedir = tmp_path / "afk-state"
    subprocess.run(["git", "tag", "-f", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)
    env = {"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"}

    _call(f"slot_state '{spoke_repo}' 5", env=env)
    first = _transitions(statedir, 5)
    _call(f"slot_state '{spoke_repo}' 5", env=env)
    second = _transitions(statedir, 5)

    assert second == first, "an unchanged terminal must not re-record on a steady read"


def test_slot_state_busy_spoke_writes_no_state_epoch(spoke_repo: Path, tmp_path: Path) -> None:
    # #2: a working spoke stamps neither the done nor the park-onset epoch (the state-inference side
    # effects the issue removes) and records no terminal/park transition.
    statedir = tmp_path / "afk-state"
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}
        )
        + "\n"
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "busy", result.stdout + result.stderr
    assert not (statedir / "done-5.epoch").exists()
    assert not (statedir / "park-onset-5.epoch").exists()
    assert not any('"actor":"reconciler"' in ln for ln in _transitions(statedir, 5))


def test_reconciler_records_a_divergence_naming_the_stale_log_state(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # #3: the log tail says `dispatched` but a ready tag sits at the tip — the reconciler appends a
    # corrective ready record whose evidence names the divergence (expected dispatched, got ready).
    statedir = tmp_path / "afk-state"
    d = statedir / "transitions"
    d.mkdir(parents=True)
    (d / "5.jsonl").write_text(
        '{"v":1,"ts":1699990000,"issue":5,"kind":"transition","to":"dispatched",'
        '"actor":"worktree-new.sh","cause":"spawn"}\n'
    )
    subprocess.run(["git", "tag", "-f", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)

    _call(
        f"slot_state '{spoke_repo}' 5",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"},
    )

    recon = [ln for ln in _transitions(statedir, 5) if '"actor":"reconciler"' in ln]
    assert len(recon) == 1, recon
    assert '"to":"ready"' in recon[0]
    assert '"dispatched"' in recon[0], "the record must name the divergence it corrected"
    assert '"lossy":true' not in recon[0], "a log with prior history is not a lossy rebuild"


def test_reconciler_cold_start_rebuilds_one_lossy_record(spoke_repo: Path, tmp_path: Path) -> None:
    # #4: no log at all + a ready tag at the tip → exactly one reconciler record, marked lossy
    # (the history before the log existed is unknowable).
    statedir = tmp_path / "afk-state"
    subprocess.run(["git", "tag", "-f", "ready/5"], cwd=spoke_repo, check=True, capture_output=True)

    _call(
        f"slot_state '{spoke_repo}' 5",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"},
    )

    recon = [ln for ln in _transitions(statedir, 5) if '"actor":"reconciler"' in ln]
    assert len(recon) == 1, recon
    assert '"to":"ready"' in recon[0]
    assert '"lossy":true' in recon[0], "a cold-start rebuild has no prior history to trust"


def test_slot_state_question_park_records_a_parked_transition_with_an_episode(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A live question park records a VISIBLE `parked` transition carrying the episode key the broker's
    # lane events use (via _gb_episode_key), so the watchdog's episode-keyed service reads go live.
    statedir = tmp_path / "afk-state"
    projects = tmp_path / "projects"
    _write_transcript(
        projects, spoke_repo, [_ask_record("Ship it?", [("yes", "ship"), ("no", "hold")])]
    )

    result = _call(
        f"slot_state '{spoke_repo}' 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    )

    assert result.stdout.strip() == "waiting", result.stdout + result.stderr
    parked = [
        ln
        for ln in _transitions(statedir, 5)
        if '"to":"parked"' in ln and '"actor":"reconciler"' in ln
    ]
    assert len(parked) == 1, _transitions(statedir, 5)
    # The transition's episode must be the EXACT key _gb_episode_key resolves — the same key the
    # broker stamps on its answer-lane events — not merely present (the agreement property #304 AC1
    # relies on for the watchdog's episode-keyed service reads).
    episode = re.search(r'"episode":"([^"]+)"', parked[0])
    assert episode, parked[0]
    key = _call(
        "_gb_episode_key 5",
        env={
            "AFK_STATE_DIR": str(statedir),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "AFK_NOW": "1700000000",
        },
    ).stdout.strip()
    assert episode.group(1) == key, f"transition episode {episode.group(1)!r} != broker key {key!r}"


def test_note_park_context_re_stamps_the_onset_on_a_signature_change(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # WARNING-1 regression guard: _afk_note_park_context must DELEGATE the coupled onset+sig roll-over
    # to note_park_episode (park-sig's single owner). A drain-side park-sig write decoupled from the
    # onset re-stamp would let a waiting->waiting signature change strand episode A's onset on episode
    # B — the #276/#283 fused-onset false-fire. Seed episode A (stale onset + sigA), then observe a
    # NEW signature (sigB) and require the onset to re-stamp.
    statedir = tmp_path / "afk-state"
    statedir.mkdir(parents=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=spoke_repo, capture_output=True, text=True
    ).stdout.strip()
    (statedir / "park-onset-5.epoch").write_text("1700000000\n")  # episode A onset (stale)
    (statedir / "park-sig-5").write_text(f"{head}\tsigA\n")  # episode A signature
    prelude = "_broker_park_signature() { printf '%s' 'sigB'; }"  # a NEW park episode

    _call(
        f"{prelude}; _afk_note_park_context '{spoke_repo}' 5",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700009999"},
    )

    onset = (statedir / "park-onset-5.epoch").read_text().strip()
    assert onset == "1700009999", (
        f"a changed signature must re-stamp the onset, not keep A's: {onset}"
    )
