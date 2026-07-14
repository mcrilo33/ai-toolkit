"""Marker/state-plumbing tests (gate-broker-markers.sh, #275 partition).

See shared/skills/hub/scripts/gate-broker-markers.sh.
"""

import os
import subprocess
from pathlib import Path

import pytest
from _gate_broker_support import (
    _call,
)


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path, monkeypatch):
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


def test_afk_event_dir_is_under_the_state_dir(tmp_path: Path) -> None:
    result = _call("afk_event_dir", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.stdout.strip() == str(tmp_path / "st" / "events")


def test_afk_drain_event_issues_prints_distinct_issues_and_deletes(tmp_path: Path) -> None:
    events = tmp_path / "st" / "events"
    events.mkdir(parents=True)
    for name in ("100-5-gate", "101-5-park", "102-7-ready"):
        (events / name).touch()

    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["5", "7"], "duplicate events collapse to one issue each"
    assert not any(events.iterdir()), "every spool file is drained (deleted)"


def test_afk_drain_event_issues_drops_malformed_names(tmp_path: Path) -> None:
    events = tmp_path / "st" / "events"
    events.mkdir(parents=True)
    (events / "103-notanumber-x").touch()
    (events / "104-9-ready").touch()

    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.stdout.split() == ["9"], "a non-numeric issue field is skipped"
    assert not any(events.iterdir()), "malformed files are deleted too, never left to pile up"


def test_afk_drain_event_issues_empty_when_no_spool(tmp_path: Path) -> None:
    result = _call("afk_drain_event_issues", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ── issue #203 finding 1: re-answer ceiling on the same prompt ─────────────────
# A legitimately-escalated spoke (answerer ESCALATE, timeout, unconfirmable inject) stays
# parked on the SAME prompt; #171's blocked-at-tip→waiting fix had no ceiling, so every tick
# re-ran the full 900s reasoner to the same ESCALATE — a doom-loop that starved the tick.
# The ceiling caps attempts on the SAME (tip, prompt-signature); it resets when the prompt
# changes or the tip moves.


def test_reanswer_ceiling_caps_repeated_reasoning(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    calls = tmp_path / "answerer.calls"
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf x >> '{calls}'; printf 'ESCALATE: legitimately stuck'",
        "AFK_REANSWER_CEILING": "2",
        # #241: the ceiling now retries after a backoff; keep the backoff far beyond the 4 fast
        # ticks so this "caps at 2" assertion stays wall-clock-independent (no mid-test retry).
        "AFK_WARN_BACKOFF_BASE": "1000000",
    }

    for _ in range(4):
        result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
        assert result.returncode == 0, result.stderr

    n = calls.read_text().count("x") if calls.exists() else 0
    assert n == 2, (
        f"the reasoner must stop after 2 attempts on the same prompt within the backoff, ran {n}"
    )


def test_reanswer_ceiling_resets_after_tip_advances(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # Terminal only until the tip MOVES: a revived/committing spoke gets a fresh budget so a
    # once-exhausted gate is never permanently stuck.
    calls = tmp_path / "answerer.calls"
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf x >> '{calls}'; printf 'ESCALATE: stuck'",
        "AFK_REANSWER_CEILING": "1",
    }
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)  # attempt 1 → runs
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)  # exhausted → skipped
    assert calls.read_text().count("x") == 1, "ceiling=1 stops the second attempt"

    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "progress"],
        cwd=spoke_repo,
        check=True,
        env=git_env,
        capture_output=True,
    )

    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)  # tip moved → runs again
    assert calls.read_text().count("x") == 2, "a tip advance must reset the ceiling"


# ── issue #237 + #241 §5: mutation-void backs off (not terminal) + log-once ────


def test_broker_service_gate_mutation_void_backs_off_not_terminal(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # A reasoner that mutates the live tree (here an absolute-path write, an isolation bypass)
    # has its answer VOIDED. #241 §5: the void is no longer terminal-forever — it warns and
    # backs off. Within the backoff window (pinned huge here) the durable void marker caps the
    # reasoner at a single run across four fast ticks (the ceiling is 5, so only the void marker
    # can cap it) — and it WARNS instead of parking blocked/<issue>.
    calls = tmp_path / "answerer.calls"
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": (
            f"printf x >> '{calls}'; printf 'mutated' > '{spoke_repo}/tracked.txt'; "
            "printf 'ANSWER: go ahead'"
        ),
        "AFK_REANSWER_CEILING": "5",
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "1000000",  # keep the 4 fast ticks inside one backoff window
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    for _ in range(4):
        result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
        assert result.returncode == 0, result.stderr

    n = calls.read_text().count("x") if calls.exists() else 0
    assert n == 1, f"a mutation-void must run the reasoner once, then back off; ran {n}"
    _rl = Path(env["_READY_LOG"])
    assert not _rl.exists() or "--blocked 5" not in _rl.read_text(), "void warns, never parks"
    assert (statedir / "warned-5.txt").exists()


def test_broker_service_gate_mutation_void_retries_after_backoff(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # #241 §5: the void is NOT terminal-forever. Once the warned-retry backoff elapses, the void
    # marker is cleared for ONE supervised retry (the reasoner re-runs) — proof the void backs
    # off rather than staying terminal. (The sibling test pins the backoff huge so this
    # fall-through never fires; here it does.)
    calls = tmp_path / "answerer.calls"
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (spoke_repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=spoke_repo, check=True, capture_output=True)
    base = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": (
            f"printf x >> '{calls}'; printf 'mutated' > '{spoke_repo}/tracked.txt'; "
            "printf 'ANSWER: go ahead'"
        ),
        "AFK_REANSWER_CEILING": "5",
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1000"})
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1000"})
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1100"})

    n = calls.read_text().count("x") if calls.exists() else 0
    assert n == 2, f"the void must retry once the backoff elapses, not stay terminal; ran {n}"


def test_reanswer_ceiling_logs_terminal_once(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    # An already-terminal gate must log its "re-answer ceiling reached … terminal" line
    # exactly once across re-drains — not on every event wake (the #237 doom-loop symptom).
    env = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": "printf 'ESCALATE: legitimately stuck'",
        "AFK_REANSWER_CEILING": "2",
    }

    logs = ""
    for _ in range(5):
        result = _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env=env)
        assert result.returncode == 0, result.stderr
        logs += result.stderr

    n = logs.count("re-answer ceiling reached")
    assert n <= 1, f"a terminal gate must log the ceiling line at most once, got {n}"


# ── issue #241 S4: the re-answer ceiling backs off, never goes terminal ─────────
# Pre-#241 the ceiling was TERMINAL: once a spoke exhausted its attempts on the same (tip,
# prompt) the reasoner never ran again until a human intervened. #241 §5 makes it warn + retry
# on an exponential backoff — doom-loop safety is the growing curve, not abandonment.


def test_reanswer_ceiling_backs_off_and_retries(
    spoke_repo: Path, waiting_spoke_env: dict[str, str], tmp_path: Path
) -> None:
    calls = tmp_path / "answerer.calls"
    statedir = tmp_path / "sd"
    statedir.mkdir()
    base = {
        **waiting_spoke_env,
        "AFK_ANSWERER_CMD": f"printf x >> '{calls}'; printf 'ESCALATE: legitimately stuck'",
        "AFK_REANSWER_CEILING": "1",
        "AFK_STATE_DIR": str(statedir),
        "AFK_WARN_BACKOFF_BASE": "60",
        "AFK_JOURNAL_GH_COMMENT": "0",
    }

    # First run exhausts the ceiling (=1). A second tick at the SAME clock stays inside the
    # backoff (no re-run). A third tick past the 60s backoff takes ONE supervised retry.
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1000"})
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1000"})
    _call(f"broker_service_gate '{spoke_repo}' 5 unattended", env={**base, "AFK_NOW": "1100"})

    n = calls.read_text().count("x") if calls.exists() else 0
    assert n >= 2, f"the ceiling must retry after the backoff, not stay terminal; ran {n}"
    assert (statedir / "warned-5.txt").exists(), "the ceiling must warn"
    assert "ceiling" in (statedir / "decision-journal.jsonl").read_text()


# ── #249: network-outage state (offline-since + idle-clock refresh) ───────────
# The per-window outage marker and the idle/ceiling-clock refresh live in the shared core so
# both _afk_auth_is_dead callers in hub-afk.sh (reap_pass + _afk_service_auth_halt) reuse them.


def test_stamp_offline_since_records_first_tick_and_is_idempotent(tmp_path: Path) -> None:
    # The offline-since epoch anchors a CONSECUTIVE outage: the FIRST offline tick stamps it and
    # later ticks must NOT overwrite it, so --status reports the true outage duration.
    statedir = tmp_path / "sd"
    _call("stamp_offline_since", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})
    _call("stamp_offline_since", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "9999"})

    result = _call("read_offline_since", env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "1000", "the first offline tick's epoch is preserved"


def test_offline_minutes_reports_elapsed_since_first_offline(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    _call("stamp_offline_since", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})

    result = _call(
        "offline_minutes", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": str(1000 + 5 * 60)}
    )

    assert result.stdout.strip() == "5"


def test_offline_minutes_empty_without_marker(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()

    result = _call("offline_minutes", env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "", "no outage ⇒ no duration"


def test_clear_offline_since_drops_the_marker(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    _call("stamp_offline_since", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})
    _call("clear_offline_since", env={"AFK_STATE_DIR": str(statedir)})

    result = _call("read_offline_since", env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == ""


def test_clear_progress_state_also_clears_offline_since(tmp_path: Path) -> None:
    # A fresh /afk window must not inherit a prior run's outage marker (cleared alongside the
    # progress / answer-attempt epochs).
    statedir = tmp_path / "sd"
    _call("stamp_offline_since", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})
    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    result = _call("read_offline_since", env={"AFK_STATE_DIR": str(statedir)})

    assert result.stdout.strip() == "", "a fresh window drops the stale outage marker"


def test_clear_progress_state_also_clears_watchdog_firing_markers(tmp_path: Path) -> None:
    # #263: the watchdog's per-condition firing-dedup markers are per-window state — a leftover
    # would suppress a condition's first ledger firing in the next window (an autonomy under-count).
    statedir = tmp_path / "sd"
    statedir.mkdir()
    marker = statedir / "wd-fire-dedup-auto-land-skipped-5"
    marker.write_text("")

    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    assert not marker.exists(), "a fresh window drops the stale firing-dedup marker"


def test_refresh_offline_clocks_stamps_progress_and_answer_attempt(tmp_path: Path) -> None:
    # The idle-clock exclusion for an outage tick: every in-flight spoke gets a fresh progress
    # epoch (soft ceiling) AND answer-attempt epoch (idle clock), so the blackout is not counted
    # toward a reap when connectivity returns.
    statedir = tmp_path / "sd"
    expr = (
        'inflight_worktrees() { printf "/wt/5\\t5\\n/wt/7\\t7\\n"; }; _afk_refresh_offline_clocks'
    )

    _call(expr, env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1700000000"})

    for issue in ("5", "7"):
        assert (statedir / f"progress-{issue}.epoch").read_text().strip() == "1700000000"
        assert (statedir / f"answer-attempt-{issue}.epoch").read_text().strip() == "1700000000"
