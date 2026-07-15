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
from _gate_broker_support import _project_dir_for, _resumed_gate_transcript

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


def _detect(
    prelude: str, call: str, *, env: dict[str, str] | None = None, state_dir: Path | None = None
) -> int:
    """Run a detector with the drain readers stubbed by `prelude`; return its rc.

    `state_dir` pins AFK_STATE_DIR: the park-unanswered detector notes the park EPISODE (a real
    write) before measuring, so without it a detector test would stamp the LIVE hub state dir.
    """
    e = {"AFK_NOW": NOW}
    if state_dir is not None:
        e["AFK_STATE_DIR"] = str(state_dir)
    if env:
        e.update(env)
    return _call(f"{prelude}; {call}", env=e).returncode


# Condition 1 — park unanswered
# AC1/AC2 (#265): the never-attempted branch measures against PARK ONSET, not zero. The
# answer-attempt epoch is stamped only at answer DELIVERY (minutes into the answerer's run), so
# a zero-grace floor false-fired 1s after every fresh park. A freshly parked spoke with no
# attempt stays quiet; only once the park itself outlives the ceiling may it fire.
# The lane stub (#283): the ceiling applies to ANSWER-lane parks only, so every ceiling-arithmetic
# test below declares one. The lane itself is pinned separately, against real probes.
_GATE_LANE = "_wd_park_lane() { echo gate; }"


def test_park_unanswered_quiet_when_fresh_park_never_attempted(tmp_path: Path) -> None:
    fresh = str(int(NOW) - 60)  # parked 60s ago (< 600s ceiling)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {fresh}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_fires_when_park_onset_stale_never_attempted(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)  # parked > 600s ago, still no answer → a real shortfall
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 0


def test_park_unanswered_fires_when_attempt_is_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)  # > 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 0


def test_park_unanswered_quiet_when_attempt_is_fresh(tmp_path: Path) -> None:
    fresh = str(int(NOW) - 60)  # < 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        f"read_answer_attempt() {{ echo {fresh}; }}; read_park_onset_epoch() {{ echo {fresh}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_quiet_when_not_waiting(tmp_path: Path) -> None:
    prelude = f'{_GATE_LANE}; slot_state() {{ echo busy; }}; read_answer_attempt() {{ echo ""; }}'
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_end_to_end_real_stamp_feeds_real_detector(tmp_path: Path) -> None:
    # End-to-end (no stubbed epoch reader): the REAL slot_state stamps park-onset on a gate park,
    # and the REAL detector reads it back — a one-sided rename of the park-onset file would make
    # the read miss and this fire fail. First tick stamps onset at an old AFK_NOW; a later tick
    # past the ceiling, with no answer ever delivered, fires the never-attempted branch.
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    statedir = tmp_path / "afk-state"
    old = str(int(NOW) - 700)  # onset 700s before NOW (> 600s ceiling)

    # Tick 1: real slot_state reads the gate park as waiting and stamps park-onset-5.epoch=old.
    stamp = _call(f"slot_state '{wt}' 5", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": old})
    assert stamp.stdout.strip() == "waiting", stamp.stdout + stamp.stderr
    assert (statedir / "park-onset-5.epoch").read_text().strip() == old

    # Tick 2: the real detector (no read_park_onset_epoch stub) reads that onset and fires.
    fire = _call(
        f"_wd_detect_park_unanswered '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": NOW},
    )
    assert fire.returncode == 0, fire.stdout + fire.stderr


# AC3 (#265): the firing reason reports the MEASURED age + which branch fired — never the
# constant "> 600s" that made a 1-second-old park's ledger + auto-filed defect claim a 600s
# breach. The never-attempted branch measures from park onset; the stale-attempt branch from the
# last delivery.
def test_park_unanswered_reason_never_attempted_reports_onset_age(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(f"{prelude}; _wd_park_unanswered_reason /wt 5 {NOW}").stdout
    assert "never-attempted" in out
    assert "700s" in out  # the measured park age, not a constant ceiling


def test_park_unanswered_reason_stale_attempt_reports_delivery_age(tmp_path: Path) -> None:
    old = str(int(NOW) - 900)
    prelude = (
        f"{_GATE_LANE}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(f"{prelude}; _wd_park_unanswered_reason /wt 5 {NOW}").stdout
    assert "stale-attempt" in out
    assert "900s" in out


# ── issue #283: the ceiling measures the CURRENT park episode, not the last delivery ──────────
# The #276 false-fire: a spoke answered ONE plan gate, then worked productively for ten minutes
# under normal tier-3 judge traffic. The detector gated on `slot_state == waiting` (true for ANY
# park, including the permission dialogs the BROKER owns) and then measured from
# answer-attempt-<issue>.epoch — an epoch that ages monotonically while the spoke works. At the
# tick, the "park" was a transient permission dialog the broker cleared 12s later.


def _tag(wt: Path, name: str) -> None:
    subprocess.run(["git", "tag", "-f", name], cwd=wt, check=True, capture_output=True)


def _journal(state_dir: Path, issue: str, ts: int, park: str = "permission") -> None:
    """Append one broker decision-journal record — the drain's own 'I serviced this' evidence."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "decision-journal.jsonl").write_text(
        f'{{"ts":{ts},"issue":"{issue}","park":"{park}",'
        f'"decision":"hook auto-approved: pytest","reversibility":"reversible"}}\n'
    )


# The lane probe — structural, in _broker_park_signature's precedence order. NOT derived from the
# signature's output: a gate-tagged park whose plan artifact is unreadable hashes to EMPTY, which
# would read as `unknown` and (under "unknown never fires") silence #265's never-attempted branch.
def test_park_lane_reads_a_permission_dialog_first(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")  # a gate tag is ALSO at the tip — permission still wins, as the broker does
    out = _call(f"_permission_pending() {{ return 0; }}; _wd_park_lane '{wt}' 5").stdout
    assert out.strip() == "permission"


def test_park_lane_reads_a_gate_tag_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    out = _call(f"_permission_pending() {{ return 1; }}; _wd_park_lane '{wt}' 5").stdout
    assert out.strip() == "gate"


def test_park_lane_reads_a_pending_question(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    prelude = (
        "_permission_pending() { return 1; }; extract_pending_question() { echo 'which one?'; }"
    )
    assert _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip() == "question"


def test_park_lane_unknown_when_nothing_is_extractable(tmp_path: Path) -> None:
    # slot_state's `waiting` derives from these same three probes, so waiting-with-no-lane means
    # the park resolved between the two calls — a race, not a strand. The detector stays quiet.
    wt = _git_repo(tmp_path)
    prelude = "_permission_pending() { return 1; }; extract_pending_question() { echo ''; }"
    assert _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip() == "unknown"


def test_park_lane_gate_survives_an_unreadable_plan_artifact(tmp_path: Path) -> None:
    # The lane must NOT be read off _broker_park_signature: a gate tag with no readable artifact
    # hashes to empty. _gate_parked is a tag-at-tip check, immune to artifact readability.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    prelude = (
        "_permission_pending() { return 1; }; _read_gate_artifact() { echo ''; }; "
        "extract_pending_question() { echo ''; }"
    )
    sig = _call(f"{prelude}; _broker_park_signature '{wt}' 5").stdout.strip()
    lane = _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip()
    assert sig == "", "precondition: this park's signature is genuinely unextractable"
    assert lane == "gate", "the lane comes from the tag at the tip, not from the signature"


def test_park_unanswered_fires_on_a_gate_park_with_an_unreadable_artifact(tmp_path: Path) -> None:
    # #265's never-attempted strand must keep firing through the new lane gate: a real gate park,
    # no delivery ever, onset past the ceiling, and nothing servicing it.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"_read_gate_artifact() {{ echo ''; }}; extract_pending_question() {{ echo ''; }}; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 0, "a genuinely stranded gate park still fires"


def test_park_unanswered_quiet_when_the_park_is_a_permission_dialog(tmp_path: Path) -> None:
    # The answer ceiling is the ANSWER lane's. Permission dialogs are the broker's lane, with its
    # own timers — the watchdog must never answer them (#271).
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 0; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1


def test_last_decision_ts_reads_a_record_written_by_the_real_journal_writer(
    tmp_path: Path,
) -> None:
    # End-to-end across the module boundary: the REAL _broker_journal_line writes the record and
    # the REAL extractor reads it back. _wd_last_decision_ts parses the journal's field ORDER (ts
    # first), so a writer-side reorder would otherwise silently return empty for every issue —
    # killing the servicing suppression and resurrecting the #276 false-fire with no test failure.
    sd = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW}
    _call('_broker_journal_line 5 permission "hook auto-approved: pytest" reversible', env=env)

    out = _call("_wd_last_decision_ts 5", env=env).stdout

    assert out.strip() == NOW, "the extractor must track the real journal writer's format"


def test_last_decision_ts_does_not_confuse_a_longer_issue_number(tmp_path: Path) -> None:
    sd = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW}
    _call('_broker_journal_line 15 permission "approved" reversible', env=env)

    assert _call("_wd_last_decision_ts 5", env=env).stdout.strip() == "", (
        "issue 15's record must not read as servicing for issue 5"
    )


def test_park_unanswered_quiet_when_the_drain_decided_recently(tmp_path: Path) -> None:
    # AC1: a broker decision for this issue since the delivery is proof the drain is SERVICING the
    # spoke. "Being handled" and "abandoned" must be distinguishable.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    _journal(sd, "5", int(NOW) - 12)  # the broker approved a tier-3 command 12s ago
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_unanswered_quiet_when_progress_advanced_recently(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}; "
        f"read_progress_epoch() {{ echo {int(NOW) - 30}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1, "a progress-epoch advance is servicing evidence too"


def test_park_unanswered_fires_when_the_delivery_is_genuinely_stranded(tmp_path: Path) -> None:
    # AC2: answer delivered, the SAME park persists past the ceiling, and nothing has touched the
    # issue since — no broker decisions, no progress. That is a real shortfall and must fire.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    old = int(NOW) - 700
    _journal(sd, "5", old, park="answer")  # the delivery's OWN record — not evidence of servicing
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0


def test_park_unanswered_ignores_a_delivery_that_predates_the_current_episode(
    tmp_path: Path,
) -> None:
    # An answer delivered BEFORE the current park began cannot count against it: the base is the
    # episode onset, so a fresh episode is quiet however old the last lifetime delivery is.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {int(NOW) - 5000}; }}; "
        f"read_park_onset_epoch() {{ echo {int(NOW) - 60}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1, "the episode began 60s ago; a 5000s-old delivery belongs to a past park"


def test_park_unanswered_quiet_on_the_276_replay(tmp_path: Path) -> None:
    # The whole #276 shape end to end: the gate was answered at T0, the broker has been deciding
    # for the issue since (one 12s before this tick), a transient permission dialog is pending at
    # the tick, and the answer is 608s old — just past the 600s ceiling. It must NOT fire.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    t0 = int(NOW) - 608
    _journal(sd, "5", int(NOW) - 12)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 0; }}; "
        f"read_answer_attempt() {{ echo {t0}; }}; read_park_onset_epoch() {{ echo {t0 - 225}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1, "a healthy, actively-brokered spoke is not an afk defect"


# AC5: the ledger line must carry the MEASURED base — which epoch, the episode, the lane — so a
# future false positive is diagnosable from the ledger alone, without re-deriving the timeline.
def test_park_unanswered_reason_names_the_base_episode_and_lane(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    sd.mkdir()
    old = int(NOW) - 900
    (sd / "park-sig-5").write_text("deadbeef\tabc123def456\n")
    prelude = (
        f"_permission_pending() {{ return 1; }}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(
        f"{prelude}; _wd_park_unanswered_reason '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW},
    ).stdout

    assert "stale-attempt" in out and "900s" in out
    assert f"answer-attempt@{old}" in out, "names WHICH epoch was measured, and its value"
    assert "abc123de" in out, "names the current park episode's signature"
    assert "lane=gate" in out


def _head(wt: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()


# ── issue #288 AC2: never-attempted suppression when the drain HAS attempted + is paced ────────
# answer-attempt-<issue>.epoch (stamped only at DELIVERY) is blind to every pre-inject drop path.
# Two records the re-answer-ceiling code ALREADY writes prove otherwise: reanswer-<issue> (an
# attempt genuinely ran on the CURRENT (tip, sig)) and an armed, not-yet-due warned-retry backoff
# (the drain is paced to retry, not abandoned). Together, firing "never-attempted" would be a lie.


def test_park_unanswered_never_attempted_suppressed_when_attempted_and_backing_off(
    tmp_path: Path,
) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "reanswer-5").write_text(f"{_head(wt)}\tsigA\t1\n")
    (sd / "warned-state-5").write_text(f"1\t{int(NOW) + 300}\n")  # armed, not due for 300s
    old = str(int(NOW) - 700)  # past the 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1, "an attempted + backing-off park must not fire under the never-attempted label"


def test_park_unanswered_genuinely_unserviced_still_fires(tmp_path: Path) -> None:
    # AC2's counter-case: no reanswer record at all -> the suppression must not engage.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0, "a genuinely untouched park must still fire"


def test_park_unanswered_fires_when_attempted_but_backoff_already_due(tmp_path: Path) -> None:
    # A reanswer record alone is not enough: the retry must still be PENDING (not yet due) or the
    # drain has simply stopped, and the fire must go through.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "reanswer-5").write_text(f"{_head(wt)}\tsigA\t1\n")
    (sd / "warned-state-5").write_text(f"1\t{int(NOW) - 10}\n")  # already due
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0, (
        "a backoff that is already due is not 'paced to retry' — the fire must go through"
    )


# ── issue #288 AC3: park-undeliverable — serviced but never deliverable ────────────────────────
# A never-attempted park with a DROP on record (answers were computed but every one was dropped
# before injection) surfaces under its own honest reason instead of the misleading
# "never-attempted" label — and instead of silence, the other #277 failure mode.


def test_park_undeliverable_fires_when_drops_are_on_record(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(
        f"{_head(wt)}\tsigA\t3\tno longer parked on that prompt (spoke moved on)\n"
    )
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0


def test_park_undeliverable_quiet_without_a_drop_record(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_undeliverable_quiet_when_a_delivery_already_landed_in_episode(tmp_path: Path) -> None:
    # A delivery inside the current episode is the stale-attempt branch's turf, not this one.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t1\tstale tree\n")
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_undeliverable_reason_names_the_count_and_last_verdict(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(
        f"{_head(wt)}\tsigA\t3\tno longer parked on that prompt (spoke moved on)\n"
    )
    old = int(NOW) - 700
    prelude = (
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(
        f"{prelude}; _wd_park_undeliverable_reason '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW},
    ).stdout

    assert "park-undeliverable" in out
    assert "3" in out
    assert "no longer parked on that prompt (spoke moved on)" in out
    assert "700s" in out


def test_park_undeliverable_refreshes_a_stale_onset_before_checking_delivery(
    tmp_path: Path,
) -> None:
    # #288 review: _wd_detect_park_undeliverable must refresh park-onset (via _wd_park_base's
    # note_park_episode) BEFORE checking _wd_park_attempt_in_episode. A resolved prior episode
    # (A) left a stale onset + delivery on file; a NEW episode (B, a different signature) starts
    # with a fresh drop. Tick 1 (the transition) must refresh the onset and stay quiet (too
    # fresh); tick 2, once B itself ages past the ceiling, must fire — not stay silently masked
    # by A's stale delivery epoch, which a pre-fix direct read would still be comparing against.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    t1 = int(NOW) - 700
    old_onset = t1 - 500  # episode A resolved well before B began
    (sd / "park-onset-5.epoch").write_text(f"{old_onset}\n")
    (sd / "park-sig-5").write_text(f"{_head(wt)}\told-sig\n")
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigB\t1\tspoke moved on\n")
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigB'; }; "
        f"read_answer_attempt() {{ echo {old_onset}; }}"
    )

    rc1 = _detect(
        prelude,
        f"_wd_detect_park_undeliverable '{wt}' 5 {t1}",
        env={"AFK_NOW": str(t1)},
        state_dir=sd,
    )
    assert rc1 == 1, "a just-transitioned episode must not fire before it ages"
    assert (sd / "park-onset-5.epoch").read_text().strip() == str(t1), (
        "the onset must be refreshed to the NEW episode's start, not left at episode A's"
    )

    rc2 = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc2 == 0, "the fresh episode must fire once it ages past the ceiling, not stay masked"


def test_run_conditions_fires_park_undeliverable_instead_of_park_unanswered(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t2\tspoke moved on\n")
    onset = str(int(NOW) - 700)
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(sd),
        "AFK_NOW": NOW,
    }
    prelude = (
        f'inflight_worktrees() {{ printf "{wt}\\t5\\n"; }}; '
        f"{_GATE_LANE}; _broker_park_signature() {{ printf '%s' 'sigA'; }}; "
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    line = ledger.read_text()
    assert '"condition":"park-undeliverable"' in line
    assert '"condition":"park-unanswered"' not in line, (
        "a serviced-but-undeliverable park must not ALSO fire the misleading never-attempted label"
    )


def test_classify_park_undeliverable_defaults_to_afk_defect() -> None:
    assert _call("_wd_classify park-undeliverable 5").stdout.strip() == "afk-defect"


# ── issue #288 AC1: the #204 self-heal must end the park episode there, not at a lucky tick ────
def test_self_heal_clears_park_onset_so_a_following_tick_is_quiet(tmp_path: Path) -> None:
    # The #277 shape: a gate park, >=2 computed-then-dropped answer passes (recorded on
    # answer-drop-5, no injection landed), then an outside-broker typed approval lands. The
    # self-heal must clear park-onset right there so a watchdog tick immediately after — before
    # any lucky slot_state tick would otherwise have observed "not parked" — stays quiet.
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    (sd / "park-onset-5.epoch").write_text(old + "\n")
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t2\tno longer parked (spoke moved on)\n")
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "session.jsonl").write_text(_resumed_gate_transcript("stale PLAN prose"))
    env = {"AFK_STATE_DIR": str(sd), "CLAUDE_PROJECTS_DIR": str(projects), "AFK_NOW": NOW}

    heal = _call(f"broker_service_gate '{wt}' 5 unattended", env=env)
    assert heal.returncode == 0, heal.stderr
    assert not (sd / "park-onset-5.epoch").exists(), "the self-heal must clear the resolved onset"

    quiet_unanswered = _call(f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", env=env)
    assert quiet_unanswered.returncode == 1, "a tick right after the self-heal must not fire"
    quiet_undeliverable = _call(f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", env=env)
    assert quiet_undeliverable.returncode == 1


# Condition 2 — dead / idle pane
# The #290 guards read the drain state, the last-action record and the drain state dir, so every
# condition-2 test pins all three off the LIVE host hub: unpinned, a real armed drain on this host
# would make `_wd_drain_state` read `live` and a stray done-<issue>.epoch would suppress a fire.
def _dead_pane_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "AFK_NOW": NOW,
        "AFK_STATE": str(tmp_path / "absent-afk-state"),  # no drain armed ⇒ _wd_drain_state = off
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_LAST_ACTION": str(tmp_path / "absent-last-action"),
    }
    env.update(extra)
    return env


_NO_DONE_EPOCH = 'read_done_epoch() { echo ""; }'


def test_dead_idle_fires_when_pane_dead_and_progress_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)  # > 3600s ceiling
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo busy; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 0


def test_dead_idle_quiet_when_pane_alive(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo "hub:0"; }; slot_state() { echo busy; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 1


def test_dead_idle_quiet_when_done(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo done; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 1


# ── #290: condition 2 is blind to a successful land's teardown window ──────────
# On #284 the watchdog fired "reaper missed a dead/idle pane" while auto_land was 13 minutes into
# the land's push gate and seconds from removing the worktree. The land had already consumed the
# ready/284 tag (so LIVE slot_state stopped reading `done`) and killed the tmux window, leaving
# exactly the detector's firing shape on a spoke that was landing successfully. Two facts each
# suppress it: the durable done-<N>.epoch (a terminal classification, checked in the detector), and
# a land running RIGHT NOW (a servicing defer, gated in the dispatcher so it cannot clear the
# firing-dedup marker mid-land — the #263 double-count hazard).
_LAND_IN_FLIGHT_284 = "_wd_land_in_flight 284 " + NOW


def test_land_in_flight_when_a_live_drain_is_landing_this_issue(tmp_path: Path) -> None:
    # auto_land stamps `land #<issue>` BEFORE the synchronous land, so it names the issue for the
    # land's whole duration — the primary signal.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    result = _call(f"_wd_drain_state() {{ echo live; }}; {_LAND_IN_FLIGHT_284}", env=env)

    assert result.returncode == 0, result.stderr


def test_land_in_flight_false_when_the_drain_is_landing_another_issue(tmp_path: Path) -> None:
    last_action = tmp_path / "last-action"
    last_action.write_text("land #999\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    assert (
        _call(f"_wd_drain_state() {{ echo live; }}; {_LAND_IN_FLIGHT_284}", env=env).returncode == 1
    )


def test_land_in_flight_false_when_the_drain_crashed_mid_land(tmp_path: Path) -> None:
    # A stale last-action outlives a crashed drain: the record still says `land #284` but nothing is
    # running it. Only a LIVE drain counts — else a crash would silence condition 2 forever.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    assert (
        _call(f"_wd_drain_state() {{ echo stale; }}; {_LAND_IN_FLIGHT_284}", env=env).returncode
        == 1
    )


def _land_log(tmp_path: Path, issue: str, age: int) -> Path:
    """Write <state-dir>/land-<issue>.log with an mtime `age` seconds before NOW."""
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / f"land-{issue}.log"
    log.write_text("running the push gate...\n")
    os.utime(log, (int(NOW) - age, int(NOW) - age))
    return log


def test_land_in_flight_when_the_land_log_is_fresh(tmp_path: Path) -> None:
    # The fallback signal for a clobbered/stale last-action: the land's own log is still being
    # written. The drain is off here, so ONLY the log mtime can carry the verdict.
    _land_log(tmp_path, "284", age=60)

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 0


def test_land_in_flight_false_when_the_land_log_is_stale(tmp_path: Path) -> None:
    # The defer is BOUNDED, not permanent: a land log untouched past HUB_WATCHDOG_LAND_ACTIVE
    # (900s) is a finished/abandoned land, not one in flight — else a single land log on disk would
    # silence condition 2 for that issue forever.
    _land_log(tmp_path, "284", age=1000)

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_land_in_flight_false_when_the_land_log_mtime_is_in_the_future(tmp_path: Path) -> None:
    # Clock skew / a corrupted timestamp: a future mtime is unmeasurable, not fresh. Negating the
    # staleness check on it would defer until wall-clock caught up to the bogus stamp — an
    # unbounded silence, not the documented 900s window. Fail toward firing.
    _land_log(tmp_path, "284", age=-86400)  # dated a day AHEAD of NOW

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_land_in_flight_false_when_nothing_signals_a_land(tmp_path: Path) -> None:
    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_dead_idle_quiet_when_the_done_epoch_is_stamped(tmp_path: Path) -> None:
    # AC1 (the detector half): the stamped done epoch records that the spoke reached terminal
    # state. A land consuming the ready/284 tag flips LIVE slot_state off `done` while the worktree
    # still exists — the durable epoch does not follow it, so this shape is never a reaper miss.
    # Post-done staleness is condition 4's (mergeable-skipped) ceiling.
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo busy; }; '
        f"read_done_epoch() {{ echo {int(NOW) - 4600}; }}; "
        f"read_progress_epoch() {{ echo {int(NOW) - 4459}; }}"
    )

    assert (
        _detect(prelude, "_wd_detect_dead_idle /wt 284 " + NOW, env=_dead_pane_env(tmp_path)) == 1
    )


# The #284 replay, through the DISPATCHER — the servicing defer lives there, not in the detector.
def _dead_pane_dispatch_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = _dead_pane_env(
        tmp_path,
        HUB_WATCHDOG_LEDGER=str(tmp_path / "l.jsonl"),
        HUB_WATCHDOG_LANDMARK_REPO=str(tmp_path / "no-landmark-repo"),
        # The revive seam MUST be stubbed: the default runs `nohup claude --continue` for real.
        HUB_WATCHDOG_REVIVE_CMD=f'printf "%s" "$2" > {tmp_path / "revived"}',
    )
    env.update(extra)
    return env


def _dead_pane_dispatch_prelude(*, done_epoch: str, drain: str = "off") -> str:
    """The #284 tick shape: worktree in-flight, ready tag consumed, window killed, progress stale."""
    return (
        'inflight_worktrees() { printf "/the/wt\\t284\\n"; }; '
        f"_wd_drain_state() {{ echo {drain}; }}; "
        '_spoke_pane_target() { echo ""; }; '  # the land killed the tmux window
        "slot_state() { echo busy; }; "  # the land consumed ready/284 ⇒ no longer `done`
        f'read_done_epoch() {{ echo "{done_epoch}"; }}; '
        f"read_progress_epoch() {{ echo {int(NOW) - 4459}; }}; "
        'read_answer_attempt() { echo ""; }'
    )


def test_run_conditions_defers_dead_pane_during_a_land_teardown(tmp_path: Path) -> None:
    # AC1: the full #284 shape — done epoch stamped, land mid-flight, ready tag consumed, window
    # killed, worktree still present, progress epoch past the ceiling ⇒ NO fire and NO revive.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path, AFK_LAST_ACTION=str(last_action))
    prelude = _dead_pane_dispatch_prelude(done_epoch=str(int(NOW) - 4600), drain="live")

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert not ledger.exists(), "a successful land's teardown is not a reaper miss"
    assert not (tmp_path / "revived").exists(), "never revive into a worktree being removed"


def test_run_conditions_defer_keeps_the_dead_pane_dedup_marker_during_a_land(
    tmp_path: Path,
) -> None:
    # The amendment's rationale (#263): the defer must NOT clear a prior firing's dedup marker.
    # Clearing it mid-land would let a subsequently-failed land re-fire and double-count in the
    # ledger — which is exactly why the defer is gated in the dispatcher rather than the detector
    # (a detector returning 1 falls into the else-branch, which clears).
    marker = tmp_path / "wd-fire-dedup-dead-pane-284"  # dir == dirname(ledger)
    marker.write_text("")
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_dispatch_env(tmp_path, AFK_LAST_ACTION=str(last_action))
    prelude = _dead_pane_dispatch_prelude(done_epoch=str(int(NOW) - 4600), drain="live")

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert marker.exists(), "a servicing defer must neither fire nor clear the firing marker"


def test_run_conditions_reads_the_done_epoch_before_slot_state_clears_it(tmp_path: Path) -> None:
    # The ordering contract, against the REAL readers (no read_done_epoch / slot_state stub — those
    # are exactly what hid this). A non-terminal slot_state read DELETES the done epoch
    # (_afk_note_tip_progress -> clear_done_epoch, deliberate per #263), and the park detectors call
    # slot_state ahead of condition 2 every tick. So a done epoch read at condition-2 time is always
    # already gone: the dispatcher must pre-read it at the top of the iteration and pass it in.
    # Here the land has consumed ready/284, so real slot_state reads non-terminal and clears the
    # epoch mid-tick — yet the spoke must still be classified terminal and NOT fire.
    wt = _git_repo(tmp_path)
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir()
    (state_dir / "done-284.epoch").write_text("1784066007\n")  # stamped at the ready transition
    (state_dir / "progress-284.epoch").write_text(str(int(NOW) - 4459) + "\n")
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    # No pane (the land killed the window); the drain is off and no land log exists, so
    # _wd_land_in_flight CANNOT defer — the done-epoch guard is the only thing that can hold.
    prelude = (
        f"inflight_worktrees() {{ printf '{wt}\\t284\\n'; }}; "
        '_wd_drain_state() { echo off; }; _spoke_pane_target() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert not (state_dir / "done-284.epoch").exists(), (
        "precondition: the real slot_state must clear the epoch mid-tick, else this proves nothing"
    )
    assert not ledger.exists(), "the done epoch must be read before slot_state destroys it"
    assert not (tmp_path / "revived").exists()


def test_run_conditions_fires_dead_pane_on_a_genuine_reaper_miss(tmp_path: Path) -> None:
    # AC2: the complement — no done epoch, no land in flight, drain not servicing the issue, pane
    # gone with progress past the ceiling. A real abandoned spoke still fires AND still revives.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert '"condition":"dead-pane"' in ledger.read_text()
    assert (tmp_path / "revived").read_text() == "284"


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
# Staleness is measured from the DONE epoch (stamped when slot_state first reads done, #263),
# not the progress epoch — a spoke parked > ceiling BEFORE going ready must not false-fire.
def test_mergeable_skipped_fires_when_done_open_and_stale(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)  # done > 900s ago, un-landed → a real skip
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 0


def test_mergeable_skipped_quiet_when_freshly_done(tmp_path: Path) -> None:
    # AC4 (the #263 false-fire): a spoke that parked long BEFORE going ready is `done` only
    # recently — its done epoch is within the ceiling, so condition 4 stays quiet even though a
    # progress-epoch base would already read stale. auto_land gets its full ceiling to land.
    wt = _git_repo(tmp_path)
    fresh = str(int(NOW) - 100)  # done 100s ago (< 900s ceiling)
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {fresh}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


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
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


def test_mergeable_skipped_quiet_when_issue_closed(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


# Condition 4 (#285) — probe mergeability, defer while the LAND lane is mid-backoff, and fire a
# DISTINCT `conflicted-land` reason (naming the conflicting files) instead of the false "mergeable
# branch un-landed" when the branch actually conflicts with the base.
def _git(wt: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(wt), *args], check=True, env=env, capture_output=True, text=True
    ).stdout


def _base_branch(wt: Path) -> str:
    return _git(wt, "symbolic-ref", "--short", "HEAD").strip()


def _conflicting_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose checked-out feature branch CONFLICTS with the base branch on README.md."""
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    _git(wt, "checkout", "-qb", "feature/5-x")
    (wt / "README.md").write_text("spoke side\n")
    _git(wt, "commit", "-qam", "feat: spoke readme")
    _git(wt, "checkout", "-q", base)
    (wt / "README.md").write_text("hub side\n")
    _git(wt, "commit", "-qam", "chore: hub readme")
    _git(wt, "checkout", "-q", "feature/5-x")
    return wt, base


def _mergeable_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose feature branch is cleanly mergeable into the base (disjoint files)."""
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    _git(wt, "checkout", "-qb", "feature/5-x")
    (wt / "spoke.txt").write_text("spoke only\n")
    _git(wt, "add", "spoke.txt")
    _git(wt, "commit", "-qm", "feat: spoke file")
    return wt, base


def test_land_conflicts_names_the_conflicting_file(tmp_path: Path) -> None:
    wt, base = _conflicting_repo(tmp_path)
    out = _call(f"_wd_land_conflicts '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout
    assert "README.md" in out


def test_land_conflicts_empty_when_mergeable(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    out = _call(f"_wd_land_conflicts '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout
    assert out.strip() == ""


def _run_conditions_ledger(
    wt: Path, base: str, tmp_path: Path, *, servicing_next: int | None = None
) -> str:
    """Drive _wd_run_conditions for one done+stale+open spoke; return the intervention ledger text.

    The ledger + state dir + fire-dedup marker are FIXED under tmp_path, so repeated calls
    accumulate in the same ledger — the dedup / servicing-toggle tests rely on that. When
    ``servicing_next`` is given, the drain's LAND-lane backoff is armed to that epoch (a future
    epoch models mid-service); otherwise any prior servicing record is cleared.
    """
    ledger = tmp_path / "ledger.jsonl"
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    land_lane = statedir / "warned-state-5-land"
    if servicing_next is not None:
        land_lane.write_text(f"2\t{servicing_next}\n")
    elif land_lane.exists():
        land_lane.unlink()
    old = str(int(NOW) - 1000)
    prelude = (
        f'inflight_worktrees() {{ printf "%s\\t5\\n" "{wt}"; }}; '
        f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    )
    env = {
        "AFK_NOW": NOW,
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_CMD": "true",  # stub the landmark so no real tag is written
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "nolandmarks"),
        "AI_TOOLKIT_BASE_BRANCH": base,
        "AFK_STATE_DIR": str(statedir),
    }
    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)
    return ledger.read_text() if ledger.exists() else ""


def test_run_conditions_fires_conflicted_land_naming_files_on_conflict(tmp_path: Path) -> None:
    wt, base = _conflicting_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path)
    assert '"condition":"conflicted-land"' in ledger
    assert "README.md" in ledger
    assert '"condition":"auto-land-skipped"' not in ledger


def test_run_conditions_fires_auto_land_skipped_when_truly_mergeable(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path)
    assert '"condition":"auto-land-skipped"' in ledger
    assert '"condition":"conflicted-land"' not in ledger


def test_run_conditions_defers_while_land_lane_mid_backoff(tmp_path: Path) -> None:
    # AC5: while the drain's LAND lane has a FRESH armed retry (future-dated warned-state-5-land),
    # the watchdog must NOT fire condition 4 — no false "conflicted-land"/"skipped" escalation while
    # the drain is still servicing the land. Mirrors the answer-lane servicing defer.
    wt, base = _conflicting_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path, servicing_next=int(NOW) + 500)
    assert "conflicted-land" not in ledger and "auto-land-skipped" not in ledger


def test_conflicted_land_dedups_across_servicing_toggle(tmp_path: Path) -> None:
    # #285 review / #263: a single persistent conflict must ledger EXACTLY ONCE across an
    # arm->service->elapse toggle. The servicing defer must not clear the fire-dedup marker, or the
    # post-service tick would re-fire and double-count — corrupting the autonomy score.
    wt, base = _conflicting_repo(tmp_path)
    _run_conditions_ledger(wt, base, tmp_path)  # tick 1: not servicing → fire once
    _run_conditions_ledger(
        wt, base, tmp_path, servicing_next=int(NOW) + 500
    )  # tick 2: servicing → defer
    ledger = _run_conditions_ledger(wt, base, tmp_path)  # tick 3: elapsed → still deduped
    assert ledger.count('"condition":"conflicted-land"') == 1


# Condition 5 — supervisor dead
def test_supervisor_dead_fires_when_drain_state_stale(tmp_path: Path) -> None:
    prelude = "_wd_drain_state() { echo stale; }"
    assert _call(f"{prelude}; _wd_detect_supervisor_dead").returncode == 0
    prelude_live = "_wd_drain_state() { echo live; }"
    assert _call(f"{prelude_live}; _wd_detect_supervisor_dead").returncode == 1


# ── interventions fire the seam with the worktree + issue ─────────────────────
def test_intervene_answer_invokes_the_seam(tmp_path: Path) -> None:
    marker = tmp_path / "answered"
    # An idle drain (no fresh attempt, no live supervisor) is not mid-service → the seam fires.
    # AFK_STATE_DIR isolates read_answer_attempt off the live host state dir (else a real drain's
    # answer-attempt-5.epoch could read "fresh" under the past-pinned AFK_NOW and defer the seam).
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f'printf "%s %s" "$1" "$2" > {marker}',
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_intervene_answer /the/wt 5", env=env)

    assert marker.read_text() == "/the/wt 5"


# AC4 (#265): the watchdog must NOT run a second decide_and_act while the supervisor is
# mid-service on the SAME park — a duplicate answer races the in-flight answerer (the #89
# stale-answer/strand hazard + a wasted high-effort run). Two mid-service signals defer it.
def test_intervene_answer_defers_when_attempt_stamp_fresh(tmp_path: Path) -> None:
    # A fresh answer-delivery stamp ⇒ the supervisor delivered within the ceiling → defer.
    marker = tmp_path / "answered"
    fresh = str(int(NOW) - 60)  # delivered 60s ago (< 600s ceiling)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_NOW": NOW,
    }

    _call(f"read_answer_attempt() {{ echo {fresh}; }}; _wd_intervene_answer /the/wt 5", env=env)

    assert not marker.exists()


def test_intervene_answer_defers_when_live_drain_last_action_names_issue(tmp_path: Path) -> None:
    # No delivery stamp yet (the never-attempted window), but the drain is live and its
    # last-action names this issue ⇒ the answerer is mid-reasoning → defer.
    marker = tmp_path / "answered"
    last_action = tmp_path / "last-action"
    last_action.write_text("answer #5\n")
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_LAST_ACTION": str(last_action),
        "AFK_NOW": NOW,
    }
    prelude = '_wd_drain_state() { echo live; }; read_answer_attempt() { echo ""; }'

    _call(f"{prelude}; _wd_intervene_answer /the/wt 5", env=env)

    assert not marker.exists()


def test_intervene_answer_fires_when_last_action_names_other_issue(tmp_path: Path) -> None:
    # A live drain whose last-action names a DIFFERENT issue is not servicing this park → fire.
    marker = tmp_path / "answered"
    last_action = tmp_path / "last-action"
    last_action.write_text("answer #9\n")
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_LAST_ACTION": str(last_action),
        "AFK_NOW": NOW,
    }
    prelude = '_wd_drain_state() { echo live; }; read_answer_attempt() { echo ""; }'

    _call(f"{prelude}; _wd_intervene_answer /the/wt 5", env=env)

    assert marker.read_text() == "x"


# AC4 (#283): the answer intervention belongs to the ANSWER lane. A permission dialog is the
# broker's, with its own classifier, timers and re-answer ceiling — injecting an answer into one is
# how the watchdog ends up answering a park it does not own (#271), interrupting a live tool call
# (#89). The detector already refuses to fire on a permission park; this is the second lock, for
# any direct caller.
def test_intervene_answer_defers_on_a_permission_dialog(tmp_path: Path) -> None:
    marker = tmp_path / "answered"
    wt = _git_repo(tmp_path)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),  # drain off ⇒ the #265 guard never defers
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    prelude = "_permission_pending() { return 0; }"

    _call(f"{prelude}; _wd_intervene_answer '{wt}' 5", env=env)

    assert not marker.exists(), "a permission dialog is the broker's lane — never inject into it"


def test_intervene_answer_answers_a_gate_park(tmp_path: Path) -> None:
    # The complement: a real answer-lane park still gets the intervention.
    marker = tmp_path / "answered"
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    prelude = "_permission_pending() { return 1; }"

    _call(f"{prelude}; _wd_intervene_answer '{wt}' 5", env=env)

    assert marker.read_text() == "x"


def test_landmark_intervention_never_lands_only_marks(tmp_path: Path) -> None:
    # Escalate-only: the default raises a needs-human-land tag and NEVER runs a land.
    wt = _git_repo(tmp_path)

    _call(f"_wd_intervene_landmark '{wt}' 5")

    tags = subprocess.run(["git", "tag"], cwd=wt, capture_output=True, text=True).stdout
    assert "needs-human-land/5" in tags


# ── #263: the escalation self-clears once the drain lands the branch ───────────
def test_clear_landed_landmarks_removes_tag_when_issue_closed(tmp_path: Path) -> None:
    # A needs-human-land/<issue> raised by condition 4 dangles after the drain lands the branch;
    # the sweep drops it once the issue is CLOSED (landed), so a human is not pointed at
    # already-shipped work. A still-open issue keeps its tag — a human still owes that land.
    repo = _git_repo(tmp_path)
    for issue in (5, 7):
        subprocess.run(
            ["git", "tag", f"needs-human-land/{issue}"], cwd=repo, check=True, capture_output=True
        )
    env = {
        "HUB_WATCHDOG_LANDMARK_REPO": str(repo),
        # issue 5 has landed (closed); issue 7 is still open.
        "HUB_WATCHDOG_ISSUE_STATE_CMD": '[ "$1" = 5 ] && echo closed || echo open',
    }

    _call("_wd_clear_landed_landmarks", env=env)

    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout
    assert "needs-human-land/5" not in tags  # closed/landed → swept
    assert "needs-human-land/7" in tags  # still open → kept


def test_clear_landed_landmarks_keeps_tag_when_issue_state_ambiguous(tmp_path: Path) -> None:
    # Fail-safe: an ambiguous issue-state read (gh down, query failure, empty output) must KEEP
    # the escalation — never delete an un-landed human-land marker on a transient outage. Here
    # the state command echoes nothing, mirroring _wd_issue_open's fail-open-to-open path.
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "needs-human-land/9"], cwd=repo, check=True, capture_output=True)
    env = {"HUB_WATCHDOG_LANDMARK_REPO": str(repo), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo ''"}

    _call("_wd_clear_landed_landmarks", env=env)

    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout
    assert "needs-human-land/9" in tags  # ambiguous state → kept (fail-safe)


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


# ── #263: one ledger firing per condition+issue while unresolved ───────────────
def _ledger_lines(ledger: Path) -> list[str]:
    return [ln for ln in ledger.read_text().splitlines() if ln.strip()]


def test_fire_dedupes_repeat_firing_of_same_condition_issue(tmp_path: Path) -> None:
    # A persistent condition (or an in-flight land racing condition 4) must log ONE intervention,
    # not one per tick — else the #251 autonomy score is double-penalized (#263).
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)
    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)  # a later tick, same unresolved cond

    assert len(_ledger_lines(ledger)) == 1


def test_fire_reappends_after_the_firing_marker_is_cleared(tmp_path: Path) -> None:
    # Dedup is scoped to "while unresolved": once the condition clears, a genuine recurrence fires
    # afresh.
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)
    _call("_wd_clear_fired dead-pane 5", env=env)  # condition resolved
    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)  # recurs → re-fires

    assert len(_ledger_lines(ledger)) == 2


def test_clear_landed_landmarks_clears_the_condition4_firing_marker(tmp_path: Path) -> None:
    # A landed issue drops out of the in-flight loop, so the dispatcher's else-clear never runs for
    # it — the sweep must re-arm its condition-4 firing marker so a future genuine skip re-fires.
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "needs-human-land/5"], cwd=repo, check=True, capture_output=True)
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-auto-land-skipped-5"  # dir == dirname(ledger)
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LANDMARK_REPO": str(repo),
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed",
    }

    _call("_wd_clear_landed_landmarks", env=env)

    assert not marker.exists()


# ── the dispatcher: firing dedup re-arms when a condition resolves (#263) ──────
def test_run_conditions_clears_firing_marker_when_condition_resolves(tmp_path: Path) -> None:
    # #263: a detector that does NOT fire this tick clears its firing marker, so a genuinely
    # resolved-then-recurring condition re-fires rather than staying deduped forever.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-park-unanswered-5"  # a firing from a prior tick
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "AFK_NOW": NOW,
    }
    # The spoke is now busy (no park, not done) → no detector fires → the else-clears run.
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        'slot_state() { echo busy; }; _spoke_pane_target() { echo "hub:0"; }; '
        'read_answer_attempt() { echo ""; }; read_progress_epoch() { echo ' + NOW + "; }; "
        'read_done_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert not marker.exists()


def test_run_conditions_dedupes_ledger_but_keeps_intervening(tmp_path: Path) -> None:
    # The safety invariant of the dedup: across repeated ticks of the SAME unresolved condition,
    # the ledger records ONE firing but the scripted intervention still runs EVERY tick (#263) —
    # log once, keep intervening. A refactor folding the intervene call into _wd_fire's early
    # return would silently break this.
    ledger = tmp_path / "l.jsonl"
    answers = tmp_path / "answers"  # one line per answer intervention
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ANSWER_CMD": f"printf 'x\\n' >> {answers}",
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        # Drain off so the AC4 mid-service guard never defers (hermetic vs a real armed drain).
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        # #283: pin the state dir so a REAL decision-journal record cannot read as servicing.
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    # A parked spoke never answered, parked past the ceiling → park-unanswered fires; the same
    # park persists across ticks (onset stays stale, #265).
    onset = str(int(NOW) - 700)
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        f"{_GATE_LANE}; "  # #283: the answer ceiling applies to answer-lane parks only
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        # Progress stays EMPTY: a fresh progress epoch now reads as the drain servicing the spoke.
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)  # tick 1: fire + intervene
    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)  # tick 2: deduped fire, intervene

    assert len(_ledger_lines(ledger)) == 1  # one ledger firing across both ticks
    assert answers.read_text().count("x") == 2  # but intervened on both ticks


# ── the dispatcher: detect → fire → intervene, end to end ─────────────────────
def test_run_conditions_fires_supervisor_dead_from_passed_state(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rearm = tmp_path / "rearmed"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_REARM_CMD": f"touch {rearm}",
        # Isolate the landmark sweep off the real repo (a nonexistent path → git no-ops).
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
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
        # Isolate the landmark sweep off the real repo (a nonexistent path → git no-ops).
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        # Drain off so the AC4 mid-service guard never defers (hermetic vs a real armed drain).
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        # #283: the servicing check reads the drain's decision journal out of the state dir —
        # pin it at a scratch path so a REAL journal record for this issue cannot suppress the fire.
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    onset = str(
        int(NOW) - 700
    )  # parked past the ceiling (#265) so the never-attempted branch fires
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        f"{_GATE_LANE}; "  # #283: the answer ceiling applies to answer-lane parks only
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        # A live pane is what keeps dead-idle quiet here; progress must stay EMPTY, since a fresh
        # progress epoch now reads as the drain servicing the spoke and would suppress the fire.
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    line = ledger.read_text()
    assert '"condition":"park-unanswered"' in line
    assert answered.exists(), "a park firing must invoke the answer intervention"
    # #283 AC5: the MEASURED base reaches the ledger line itself — not just the reason function —
    # so a future false positive is diagnosable from the ledger alone.
    assert f"base=park-onset@{onset}" in line, "the ledger line names which epoch was measured"
    assert "lane=gate" in line


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


def test_defect_count_is_single_line_when_no_afk_defects(tmp_path: Path) -> None:
    # An existing ledger with zero afk-defect lines: grep -c prints "0" AND exits 1, so a naive
    # `&& grep || echo 0` would double-print "0\n0" and corrupt the report. Must be exactly "0".
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"class":"novel-decision"}'])

    result = _call("_wd_defect_count", env={"HUB_WATCHDOG_LEDGER": str(ledger)})

    assert result.stdout.strip() == "0"
    assert result.stdout.count("0") == 1, f"exactly one '0', not a double-print: {result.stdout!r}"
