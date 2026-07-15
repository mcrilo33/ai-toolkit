"""Unit tests for the per-spoke lifecycle transition log (issue #300, phase 1).

The library is the append+read core of #300's Option C: actors record transitions,
detectors read them. Phase 1 ships the lib alone — no call sites — so these tests
pin the contract the migration steps will build on: complete-line appends, torn-tail
tolerance, best-effort writes that never fail the caller, and read helpers whose
"unknown"/empty answers are safe non-firing bases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bash_session import BashSession, fresh_call

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSITION_LOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "transition-log.sh"

_SESSION: BashSession | None = None


def _session() -> BashSession:
    global _SESSION
    if _SESSION is None or not _SESSION.alive:
        _SESSION = BashSession(TRANSITION_LOG)
    return _SESSION


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return _session().call(fn_call, env=env)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"AFK_STATE_DIR": str(tmp_path / "state")}
    env.update(extra)
    return env


def _log_file(tmp_path: Path, issue: int) -> Path:
    return tmp_path / "state" / "transitions" / f"{issue}.jsonl"


# --- append ---


def test_transition_appends_one_complete_record(tmp_path: Path) -> None:
    env = _env(tmp_path)

    _call('afk_tlog_transition 42 dispatched worktree-new.sh spawn \'{"tip":"abc"}\'', env=env)

    lines = _log_file(tmp_path, 42).read_text().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("{") and line.endswith("}")
    assert '"kind":"transition"' in line
    assert '"to":"dispatched"' in line
    assert '"actor":"worktree-new.sh"' in line
    assert '"evidence":{"tip":"abc"}' in line


def test_event_appends_with_lane_and_episode(tmp_path: Path) -> None:
    env = _env(tmp_path)

    _call(
        "afk_tlog_event 7 answer_delivered gate-broker-answerer answer sig:100 '{\"attempt\":1}'",
        env=env,
    )

    line = _log_file(tmp_path, 7).read_text().splitlines()[0]
    assert '"kind":"event"' in line
    assert '"event":"answer_delivered"' in line
    assert '"lane":"answer"' in line
    assert '"episode":"sig:100"' in line


def test_run_field_taken_from_env(tmp_path: Path) -> None:
    env = _env(tmp_path, AFK_TLOG_RUN="feature/42-x+123")

    _call("afk_tlog_transition 42 working spoke turn", env=env)

    assert '"run":"feature/42-x+123"' in _log_file(tmp_path, 42).read_text()


def test_cause_is_json_escaped(tmp_path: Path) -> None:
    env = _env(tmp_path)

    _call("afk_tlog_transition 42 blocked broker 'said \"no\" back\\slash'", env=env)

    line = _log_file(tmp_path, 42).read_text().splitlines()[0]
    assert '\\"no\\"' in line
    assert "back\\\\slash" in line


def test_non_numeric_issue_writes_nothing_and_rc_zero(tmp_path: Path) -> None:
    env = _env(tmp_path)

    result = _call('afk_tlog_transition "../evil" working x y; echo rc=$?', env=env)

    assert "rc=0" in result.stdout
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").rglob("*.jsonl"))


def test_append_best_effort_on_unwritable_dir(tmp_path: Path) -> None:
    # The state dir is a FILE, so mkdir -p fails — the caller must still see rc 0
    # (a log write never fails the underlying operation, #300).
    blocker = tmp_path / "state"
    blocker.write_text("not a dir")
    env = _env(tmp_path)

    result = _call("afk_tlog_transition 42 working x y; echo rc=$?", env=env)

    assert "rc=0" in result.stdout


# --- read ---


def test_current_state_is_last_transition(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 dispatched a b", env=env)
    _call("afk_tlog_transition 42 working a b", env=env)

    result = _call("afk_current_state 42", env=env)

    assert result.stdout.strip() == "working"


def test_events_do_not_change_current_state(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 parked-gate a b", env=env)
    _call("afk_tlog_event 42 nudge hub-afk answer", env=env)

    result = _call("afk_current_state 42", env=env)

    assert result.stdout.strip() == "parked-gate"


def test_current_state_unknown_without_log(tmp_path: Path) -> None:
    result = _call("afk_current_state 999; echo rc=$?", env=_env(tmp_path))

    assert result.stdout.splitlines()[0] == "unknown"
    assert "rc=0" in result.stdout


def test_torn_trailing_line_is_ignored(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 working a b", env=env)
    with _log_file(tmp_path, 42).open("a") as f:
        f.write('{"v":1,"ts":999,"issue":42,"kind":"transition","to":"torn')

    result = _call("afk_current_state 42", env=env)

    assert result.stdout.strip() == "working"


def test_age_in_state_measures_from_last_transition(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 pushing a b", env=env)
    onset = int(_call("afk_state_onset 42", env=env).stdout.strip())

    result = _call(f"afk_age_in_state 42 {onset + 120}", env=env)

    assert result.stdout.strip() == "120"


def test_age_empty_when_state_unknown(tmp_path: Path) -> None:
    result = _call("afk_age_in_state 999; echo rc=$?", env=_env(tmp_path))

    assert result.stdout.splitlines()[0] == "rc=0"


def test_episode_comes_from_last_park_transition(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call('afk_tlog_transition 42 parked-gate a b "" sigA:100', env=env)
    _call("afk_tlog_transition 42 working a b", env=env)
    _call('afk_tlog_transition 42 parked-question a b "" sigB:200', env=env)

    result = _call("afk_current_episode 42", env=env)

    assert result.stdout.strip() == "sigB:200"


def test_last_service_event_filters_episode_and_lane(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_event 42 answer_computed broker answer sigA:100", env=env)
    _call("afk_tlog_event 42 answer_delivered broker answer sigA:100", env=env)
    _call("afk_tlog_event 42 approval_injected broker permission sigA:100", env=env)
    _call("afk_tlog_event 42 answer_computed broker answer sigB:200", env=env)

    result = _call("afk_last_service_event 42 sigA:100 answer", env=env)

    assert '"event":"answer_delivered"' in result.stdout
    assert "sigB" not in result.stdout


def test_lane_event_count_scoped_to_episode(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_event 42 a broker answer sigA:100", env=env)
    _call("afk_tlog_event 42 b broker answer sigA:100", env=env)
    _call("afk_tlog_event 42 c broker answer sigB:200", env=env)
    _call("afk_tlog_event 42 d broker land sigA:100", env=env)

    assert _call("afk_lane_event_count 42 answer sigA:100", env=env).stdout.strip() == "2"
    assert _call("afk_lane_event_count 42 answer", env=env).stdout.strip() == "3"
    assert _call("afk_lane_event_count 42 review", env=env).stdout.strip() == "0"


# --- evidence must never shadow top-level fields (validation findings, 2026-07-15) ---


def test_evidence_to_key_does_not_hijack_current_state(tmp_path: Path) -> None:
    env = _env(tmp_path)

    _call("afk_tlog_transition 42 working reconciler heal '{\"to\":\"somewhere\"}'", env=env)

    assert _call("afk_current_state 42", env=env).stdout.strip() == "working"


def test_evidence_ts_key_does_not_corrupt_age(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 pushing a b '{\"ts\":999}'", env=env)
    onset = int(_call("afk_state_onset 42", env=env).stdout.strip())

    assert onset > 1_700_000_000  # the real epoch, not evidence's 999
    assert _call(f"afk_age_in_state 42 {onset + 60}", env=env).stdout.strip() == "60"


def test_evidence_episode_key_does_not_mint_an_episode(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call('afk_tlog_transition 42 parked-gate a b "" sigA:100', env=env)
    _call("afk_tlog_transition 42 working a b '{\"episode\":\"bogus:999\"}'", env=env)

    assert _call("afk_current_episode 42", env=env).stdout.strip() == "sigA:100"


def test_evidence_lane_and_episode_do_not_count_against_other_lanes(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call(
        'afk_tlog_event 42 probe broker diagnostics sigC:300 \'{"episode":"sigA:100","lane":"answer"}\'',
        env=env,
    )

    assert _call("afk_lane_event_count 42 answer sigA:100", env=env).stdout.strip() == "0"
    assert _call("afk_last_service_event 42 sigA:100 answer", env=env).stdout.strip() == ""


def test_evidence_kind_transition_in_event_does_not_masquerade(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _call("afk_tlog_transition 42 working a b", env=env)
    _call(
        'afk_tlog_event 42 note broker answer "" \'{"kind":"transition","to":"fake"}\'',
        env=env,
    )

    assert _call("afk_current_state 42", env=env).stdout.strip() == "working"


# --- concurrency ---


def test_concurrent_appends_interleave_whole_lines(tmp_path: Path) -> None:
    # Two writers x 20 records each, no lock: O_APPEND single-line writes must
    # yield 40 COMPLETE lines (the atomicity contract the readers depend on).
    env = _env(tmp_path)

    # 40 concurrent writers, each a LARGE line (~3KB evidence) — well past the
    # ~1KB size where an unlocked printf tears (validation finding, 2026-07-15).
    # The mkdir lock must still yield exactly 40 complete, uncorrupted records:
    # size is irrelevant once writes are serialized.
    fresh_call(
        TRANSITION_LOG,
        'pad=$(printf "x%.0s" $(seq 1 3000)); '
        "for i in $(seq 1 20); do afk_tlog_event 42 w1-$i broker answer ep1 "
        "'{\"pad\":\"'$pad'\"}' & done; "
        "for i in $(seq 1 20); do afk_tlog_event 42 w2-$i broker land ep2 "
        "'{\"pad\":\"'$pad'\"}' & done; wait",
        env=env,
    )

    lines = _log_file(tmp_path, 42).read_text().splitlines()
    assert len(lines) == 40  # no lost or merged records
    assert all(ln.startswith("{") and ln.endswith("}") for ln in lines)
    assert all(len(ln) > 3000 for ln in lines)  # large lines survive intact
    # every record is a distinct writer's whole line (no interleaving)
    tags = sorted(ln.split('"event":"', 1)[1].split('"', 1)[0] for ln in lines)
    assert len(set(tags)) == 40


def test_large_line_written_whole(tmp_path: Path) -> None:
    # A single big record (free-text + big evidence) is written intact — the lock
    # makes line size a non-issue, replacing the old evidence-size cap.
    env = _env(tmp_path)

    fresh_call(
        TRANSITION_LOG,
        'pad=$(printf "x%.0s" $(seq 1 5000)); '
        "afk_tlog_transition 42 working a b '{\"pad\":\"'$pad'\"}'",
        env=env,
    )

    line = _log_file(tmp_path, 42).read_text().splitlines()[0]
    assert line.startswith("{") and line.endswith("}")
    assert len(line) > 5000
