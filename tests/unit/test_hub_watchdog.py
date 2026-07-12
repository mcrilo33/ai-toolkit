"""Unit tests for shared/skills/hub/scripts/hub-watchdog.sh.

hub-watchdog.sh is the tier-2 OS-level supervision daemon (issue #251): a self-looping,
pidfile-singleton, source-recycling process with its own heartbeat that cross-checks the
tier-1 /afk drain and (in later subtasks) turns each intervention into a defect signal. This
subtask is the daemon SKELETON — these tests pin the loop/pidfile/heartbeat/self-recycle
scaffold and the drain-state cross-check, modelled on test_hub_otel_watch.py's daemon suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# hub-watchdog.sh cross-checks the macOS afk hub (kill -0, BSD tooling) like its siblings.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="hub-watchdog.sh targets the macOS afk hub"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_WATCHDOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-watchdog.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-watchdog.sh and invoke a shell expression against its functions.

    HUB_WATCHDOG_FILE defaults OFF here: this host has an authed `gh`, so a default-on defect
    file would fire a real hub-agent/gh against the live repo. The filing tests opt back in with
    HUB_WATCHDOG_FILE=1 + the HUB_WATCHDOG_SCOPER_CMD / _DEDUP_CMD / _LABEL_CMD stubs.
    """
    full_env = {**os.environ, "TZ": "UTC", "HUB_WATCHDOG_FILE": "0"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_WATCHDOG}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke hub-watchdog.sh DIRECTLY (the CLI dispatch, BASH_SOURCE == $0)."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(HUB_WATCHDOG), *args], capture_output=True, text=True, env=full_env
    )


def _drain_pattern_stub(tmp_path: Path, pattern: str) -> str:
    """A _wd_drain_state stub scripted per tick: 'L'=live, 'S'=stale, anything else=off.

    Ticks beyond the pattern read as off, so every loop terminates. The tick count persists
    in a file the test can assert on.
    """
    ticks = tmp_path / "ticks"
    return (
        f'TICKS="{ticks}"; PATTERN="{pattern}"; '
        "_wd_drain_state() { "
        'n=$(( $(cat "$TICKS" 2>/dev/null || echo 0) + 1 )); printf "%s" "$n" > "$TICKS"; '
        'c="${PATTERN:$((n-1)):1}"; '
        'case "$c" in L) echo live ;; S) echo stale ;; *) echo off ;; esac; }'
    )


_LOOP_ENV = "; ".join(["export HUB_WATCHDOG_INTERVAL=0", "export HUB_WATCHDOG_IDLE_TICKS=3"])


# ── the drain-state cross-check ────────────────────────────────────────────────


def test_drain_state_off_when_no_state_file(tmp_path: Path) -> None:
    result = _call(
        "_wd_drain_state",
        env={"AFK_STATE": str(tmp_path / "absent"), "AFK_HEARTBEAT": str(tmp_path / "absent")},
    )

    assert result.stdout.strip() == "off", result.stderr


def test_drain_state_live_when_heartbeat_pid_alive(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")  # this test process is alive

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "live", result.stderr


def test_drain_state_stale_when_armed_but_pid_dead(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    # A pid that is (almost certainly) not a live process — a very high number.
    hb.write_text("999999 1783879781\n")

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "stale", result.stderr


# ── the daemon loop ────────────────────────────────────────────────────────────


def test_loop_ticks_and_writes_heartbeat_on_each_live_tick(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_HEARTBEAT="{hb}"',
        "_wd_loop",  # empty baseline ⇒ no self-recycle
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    # Two live ticks then off to exhaustion: _wd_tick logged the drain state twice.
    assert result.stdout.count("drain supervisor is live") == 2
    assert hb.read_text().split()[0].isdigit(), "heartbeat stamped <pid> <epoch>"


def test_loop_exits_after_idle_ticks_when_drain_off(tmp_path: Path) -> None:
    parts = [_drain_pattern_stub(tmp_path, ""), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3", "torn down after exactly the idle grace"
    assert "drain supervisor is" not in result.stdout, "no tick ran while the drain was off"


def test_loop_live_tick_resets_idle_counter(tmp_path: Path) -> None:
    # live, off, off, live, then off to exhaustion → the mid-pattern live tick resets the idle
    # counter, so the loop survives to tick 7 and ticked twice. Would exit at tick 4 otherwise.
    parts = [_drain_pattern_stub(tmp_path, "LOOL"), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "7"
    assert result.stdout.count("drain supervisor is live") == 2


def test_loop_reexecs_when_source_changed(tmp_path: Path) -> None:
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        "_wd_source_hash() { echo CHANGED; }",
        "_wd_reexec() { echo REEXEC; exit 0; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" in result.stdout


def test_loop_no_reexec_on_empty_hash(tmp_path: Path) -> None:
    # A transient hasher failure returns '' — must NOT be read as a change (that would
    # spuriously re-exec and re-stamp an empty baseline).
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        "_wd_source_hash() { echo ''; }",
        "_wd_reexec() { echo REEXEC; exit 1; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" not in result.stdout


# ── the daemon singleton ───────────────────────────────────────────────────────


def test_daemon_refuses_second_start_while_pid_alive(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'printf "%s" "$$" > "{pidfile}"',  # a LIVE pid (our own shell)
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr
    assert not (tmp_path / "ticks").exists(), "the loop never ticked"
    assert pidfile.exists(), "the other daemon's pidfile is left intact"


def test_daemon_reclaims_stale_pidfile_and_removes_own_on_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, ""),  # all-off → 3 ticks then teardown
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'sleep 0.01 & _dead=$!; wait "$_dead"; printf "%s" "$_dead" > "{pidfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3"
    assert not pidfile.exists(), "the daemon removes its own pidfile on exit"


def test_daemon_appends_loop_output_to_logfile(tmp_path: Path) -> None:
    logfile = tmp_path / "wd.log"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{tmp_path / "wd.pid"}"',
        f'export HUB_WATCHDOG_LOG="{logfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "drain supervisor is live" in logfile.read_text()
    assert "drain supervisor is live" not in result.stdout, (
        "loop output goes to the log, not stdout"
    )


# ── CLI dispatch ───────────────────────────────────────────────────────────────


def test_cli_status_reports_drain_and_watchdog_state(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")

    result = _run(
        "--status",
        env={
            "AFK_STATE": str(state),
            "AFK_HEARTBEAT": str(hb),
            "HUB_WATCHDOG_PIDFILE": str(tmp_path / "absent.pid"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "drain=live" in result.stdout
    assert "watchdog=off" in result.stdout


def test_cli_daemon_dispatch_reports_already_running(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    pidfile.write_text(str(os.getpid()))

    result = _run(
        "--daemon",
        env={"HUB_WATCHDOG_PIDFILE": str(pidfile), "HUB_WATCHDOG_LOG": str(tmp_path / "wd.log")},
    )

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr


def test_cli_once_stamps_heartbeat_and_ticks(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    result = _run(
        "--once",
        env={"AFK_STATE": str(tmp_path / "absent"), "HUB_WATCHDOG_HEARTBEAT": str(hb)},
    )

    assert result.returncode == 0, result.stderr
    assert "tick: drain supervisor is off" in result.stdout
    fields = hb.read_text().split()
    assert len(fields) == 2 and fields[0].isdigit(), f"heartbeat is '<pid> <epoch>': {fields}"


# ── the 5 detectors + interventions (issue #251, subtask 3) ───────────────────
# The daemon skeleton observes; these detectors decide WHEN the drain fell short and fire the
# scripted intervention + a defect record. Detectors are pure predicates over the drain's own
# state readers (slot_state / read_answer_attempt / read_progress_epoch / _spoke_pane_target),
# stubbed inline here; the git-marker detectors run against a throwaway repo. `now` is pinned
# via AFK_NOW so the grace-margin arithmetic is deterministic.

NOW = "1783880000"


def _git_repo(tmp_path: Path, name: str = "wt") -> Path:
    wt = tmp_path / name
    wt.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "c1"]):
        subprocess.run(cmd, cwd=wt, check=True, env=env, capture_output=True)
    return wt


def _detect(prelude: str, call: str, *, env: dict[str, str] | None = None) -> int:
    """Run a detector with the drain readers stubbed by `prelude`; return its rc."""
    e = {"AFK_NOW": NOW}
    if env:
        e.update(env)
    return _call(f"{prelude}; {call}", env=e).returncode


# Condition 1 — park unanswered
def test_park_unanswered_fires_when_never_attempted(tmp_path: Path) -> None:
    prelude = 'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }'
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW) == 0


def test_park_unanswered_fires_when_attempt_is_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)  # > 600s ceiling
    prelude = f"slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo {old}; }}"
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW) == 0


def test_park_unanswered_quiet_when_attempt_is_fresh(tmp_path: Path) -> None:
    fresh = str(int(NOW) - 60)  # < 600s ceiling
    prelude = f"slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo {fresh}; }}"
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW) == 1


def test_park_unanswered_quiet_when_not_waiting(tmp_path: Path) -> None:
    prelude = 'slot_state() { echo busy; }; read_answer_attempt() { echo ""; }'
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW) == 1


# Condition 2 — dead / idle pane
def test_dead_idle_fires_when_pane_dead_and_progress_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)  # > 3600s ceiling
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo busy; }; '
        f"read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW) == 0


def test_dead_idle_quiet_when_pane_alive(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo "hub:0"; }; slot_state() { echo busy; }; '
        f"read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW) == 1


def test_dead_idle_quiet_when_done(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo done; }; '
        f"read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW) == 1


# Condition 3 — stale blocked marker (real git)
def test_stale_marker_fires_when_blocked_is_ancestor_of_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "c2"],
        cwd=wt,
        check=True,
        env=env,
        capture_output=True,
    )

    assert _call(f"_wd_detect_stale_marker '{wt}' 5").returncode == 0


def test_stale_marker_quiet_when_blocked_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env, capture_output=True)

    assert _call(f"_wd_detect_stale_marker '{wt}' 5").returncode == 1


# Condition 4 — mergeable branch the drain skipped (escalate-only)
def test_mergeable_skipped_fires_when_done_open_and_stale(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)  # > 900s ceiling
    prelude = f"slot_state() {{ echo done; }}; read_progress_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 0


def test_mergeable_skipped_quiet_when_blocked_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env0 = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env0, capture_output=True)
    old = str(int(NOW) - 1000)
    prelude = f"slot_state() {{ echo done; }}; read_progress_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


def test_mergeable_skipped_quiet_when_issue_closed(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)
    prelude = f"slot_state() {{ echo done; }}; read_progress_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


# Condition 5 — supervisor dead
def test_supervisor_dead_fires_when_drain_state_stale(tmp_path: Path) -> None:
    prelude = "_wd_drain_state() { echo stale; }"
    assert _call(f"{prelude}; _wd_detect_supervisor_dead").returncode == 0
    prelude_live = "_wd_drain_state() { echo live; }"
    assert _call(f"{prelude_live}; _wd_detect_supervisor_dead").returncode == 1


# ── interventions fire the seam with the worktree + issue ─────────────────────
def test_intervene_answer_invokes_the_seam(tmp_path: Path) -> None:
    marker = tmp_path / "answered"
    env = {"HUB_WATCHDOG_ANSWER_CMD": f'printf "%s %s" "$1" "$2" > {marker}'}

    _call("_wd_intervene_answer /the/wt 5", env=env)

    assert marker.read_text() == "/the/wt 5"


def test_landmark_intervention_never_lands_only_marks(tmp_path: Path) -> None:
    # Escalate-only: the default raises a needs-human-land tag and NEVER runs a land.
    wt = _git_repo(tmp_path)

    _call(f"_wd_intervene_landmark '{wt}' 5")

    tags = subprocess.run(["git", "tag"], cwd=wt, capture_output=True, text=True).stdout
    assert "needs-human-land/5" in tags


# ── _wd_fire writes the intervention-ledger ───────────────────────────────────
def test_fire_appends_a_ledger_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    result = _call("_wd_fire park-unanswered 5 'drain fell short'", env=env)

    assert result.returncode == 0, result.stderr
    line = ledger.read_text().strip()
    assert '"condition":"park-unanswered"' in line
    assert '"issue":"5"' in line
    assert f'"ts":{NOW}' in line
    assert "FIRING [park-unanswered] #5" in result.stdout  # _wd_log → stdout (daemon tees to log)


# ── the dispatcher: detect → fire → intervene, end to end ─────────────────────
def test_run_conditions_fires_supervisor_dead_from_passed_state(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rearm = tmp_path / "rearmed"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_REARM_CMD": f"touch {rearm}",
        "AFK_NOW": NOW,
    }
    # inflight_worktrees stubbed empty so only the global supervisor check runs.
    prelude = "inflight_worktrees() { :; }"

    _call(f"{prelude}; _wd_run_conditions {NOW} stale", env=env)

    assert '"condition":"supervisor-dead"' in ledger.read_text()
    assert rearm.exists(), "supervisor-dead must re-arm the drain"


def test_run_conditions_fires_park_and_invokes_answer_seam(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    answered = tmp_path / "answered"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ANSWER_CMD": f"touch {answered}",
        "AFK_NOW": NOW,
    }
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ' + NOW + "; }"
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert '"condition":"park-unanswered"' in ledger.read_text()
    assert answered.exists(), "a park firing must invoke the answer intervention"


# ── the instrument: classify + file the defect (issue #251, subtask 4) ─────────
# Every firing is classified {afk-defect|novel-decision} so a genuine human-call escalation is
# not mis-filed as an afk bug; afk-defects file (deduped) via a headless bug-scoper. Filing is
# gated by HUB_WATCHDOG_FILE (default OFF in _call) so no test hits the live gh/hub-agent.


def test_classify_defaults_to_afk_defect() -> None:
    assert _call("_wd_classify dead-pane 5").stdout.strip() == "afk-defect"


def test_classify_seam_overrides_to_novel_decision(tmp_path: Path) -> None:
    env = {"HUB_WATCHDOG_CLASSIFY_CMD": "echo novel-decision"}
    assert _call("_wd_classify park-unanswered 5", env=env).stdout.strip() == "novel-decision"


def test_fire_records_the_class_in_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)

    assert '"class":"afk-defect"' in ledger.read_text()


def test_afk_defect_firing_dispatches_the_scoper(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_FILE": "1",  # opt back in
        "HUB_WATCHDOG_DEDUP_CMD": "true",  # no open dup (empty stdout)
        "HUB_WATCHDOG_LABEL_CMD": "true",
        "HUB_WATCHDOG_SCOPER_CMD": f'printf "%s %s" "$1" "$2" > {scoped}',
        "AFK_STATE_DIR": str(tmp_path / "state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)

    assert scoped.read_text() == "dead-pane 5", "afk-defect must dispatch the bug-scoper"


def test_novel_decision_firing_does_not_file(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_LEDGER": str(tmp_path / "l.jsonl"),
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_CLASSIFY_CMD": "echo novel-decision",
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_fire park-unanswered 5 'a real human call'", env=env)

    assert not scoped.exists(), "a novel human decision must NOT be filed as an afk bug"


def test_file_defect_dedups_within_the_run(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    env = {
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_DEDUP_CMD": "true",
        "HUB_WATCHDOG_LABEL_CMD": "true",
        "HUB_WATCHDOG_SCOPER_CMD": f"echo x >> {calls}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call(
        "_wd_file_defect dead-pane 5 r; _wd_file_defect dead-pane 5 r; _wd_file_defect dead-pane 5 r",
        env=env,
    )

    assert calls.read_text().count("x") == 1, "one defect per condition+issue per run"


def test_file_defect_skips_when_open_defect_exists(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_DEDUP_CMD": "echo 999",  # a matching open issue exists
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call("_wd_file_defect dead-pane 5 r", env=env)

    assert not scoped.exists(), "an already-open afk-defect must not be duplicated"


def test_file_defect_off_by_gate_does_not_dispatch(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_FILE": "0",  # gated off
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call("_wd_file_defect dead-pane 5 r", env=env)

    assert not scoped.exists(), "HUB_WATCHDOG_FILE=0 suppresses filing"


# ── the autonomy score + report (issue #251, subtask 5) ────────────────────────
# score = 1 - interventions/spokes; a ZERO-firing run scores 1.000 - the pass criterion for
# "afk autonomous on this backlog". These pin the arithmetic + the morning report.


def _seed_spokes(state_dir: Path, issues: list[int]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for n in issues:
        (state_dir / f"dispatch-{n}.epoch").write_text("1783880000\n")


def _seed_ledger(ledger: Path, lines: list[str]) -> None:
    ledger.write_text("".join(f"{line}\n" for line in lines))


def test_spokes_serviced_counts_dispatch_epochs(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9])

    result = _call("_wd_spokes_serviced", env={"AFK_STATE_DIR": str(state)})

    assert result.stdout.strip() == "3", result.stderr


def test_intervention_count_counts_ledger_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"condition":"a"}', '{"condition":"b"}'])

    result = _call("_wd_intervention_count", env={"HUB_WATCHDOG_LEDGER": str(ledger)})

    assert result.stdout.strip() == "2", result.stderr


def test_autonomy_score_is_one_on_a_zero_firing_run(tmp_path: Path) -> None:
    # The pass criterion: spokes serviced, no firing ⇒ afk was autonomous ⇒ 1.000.
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9, 11])
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(tmp_path / "absent.jsonl")}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "1.000"


def test_autonomy_score_reflects_intervention_ratio(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9, 11])  # 4 spokes
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"condition":"dead-pane"}'])  # 1 firing → 1 - 1/4 = 0.75
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(ledger)}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "0.750"


def test_autonomy_score_clamps_at_zero(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5])  # 1 spoke
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"c":1}', '{"c":2}', '{"c":3}'])  # 3 firings → 1 - 3 < 0 ⇒ 0.000
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(ledger)}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "0.000"


def test_report_prints_the_summary_line(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7])
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"class":"afk-defect"}'])
    env = {
        "AFK_STATE_DIR": str(state),
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_NO_TELEMETRY": "1",
    }

    result = _call("_wd_report", env=env)

    assert "interventions=1" in result.stdout
    assert "defects_filed=1" in result.stdout
    assert "spokes_serviced=2" in result.stdout
    assert "autonomy_score=0.500" in result.stdout


def test_cli_report_on_zero_firing_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9])
    result = _run(
        "--report",
        env={
            "AFK_STATE_DIR": str(state),
            "HUB_WATCHDOG_LEDGER": str(tmp_path / "none.jsonl"),
            "HUB_WATCHDOG_NO_TELEMETRY": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "autonomy_score=1.000" in result.stdout
