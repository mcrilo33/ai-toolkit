"""Marker/state-plumbing tests (gate-broker-markers.sh, #275 partition).

See shared/skills/hub/scripts/gate-broker-markers.sh.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _gate_broker_support import (
    _call,
    _perm_env,
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


# ── #278: the queued-subtask channel (hub -> a live spoke) ────────────────────
# The drain's INBOUND channel: when a ready issue is packable into a spoke that is already
# running, the drain queues it here instead of spawning a second worktree (and its 20-minute
# first-push suite seed). The spoke drains the queue at its ready boundary. Keyed by the
# TARGET spoke, listing the routed issue numbers in dispatch order.


def test_queued_subtask_round_trips(tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)

    result = _call("read_queued_subtask 263", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["264"]


def test_queued_subtask_is_empty_before_anything_is_queued(tmp_path: Path) -> None:
    result = _call("read_queued_subtask 263", env={"AFK_STATE_DIR": str(tmp_path / "st")})

    assert result.returncode == 0, "a spoke with no queue reads empty, never errors"
    assert result.stdout.strip() == ""


def test_queued_subtask_reads_back_in_deterministic_ascending_order(tmp_path: Path) -> None:
    # Insertion order is deliberately NOT preserved (one file per issue buys atomicity at
    # that price). It costs nothing: the members are same-scope peers, batch-plan already
    # picked the one that matters (the LEADER, never queued), and the rest are independent
    # issues whose completion order does not move the wall clock. Ascending is deterministic,
    # which is what the spoke's re-anchor loop and the drain's logs actually need. Numeric,
    # so #40 sorts before #264 rather than lexically after it.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 270", env=env)
    _call("stamp_queued_subtask 263 40", env=env)
    _call("stamp_queued_subtask 263 264", env=env)

    result = _call("read_queued_subtask 263", env=env)

    assert result.stdout.split() == ["40", "264", "270"]


def test_clearing_one_entry_leaves_every_other_entry_untouched(tmp_path: Path) -> None:
    # The STRUCTURAL property that makes this channel race-free: entries are independent
    # files, so a clear unlinks its own and provably cannot rewrite — or lose — another.
    #
    # A line-based queue instead removes an entry by read-modify-write, so a concurrent
    # append from the hub landing between the read and the rewrite is silently dropped (the
    # drain routing #270 while the spoke clears the #264 it just shipped). That race cannot
    # be pinned by a sequential test — the interleaving IS the bug, and any sequential
    # ordering of the same calls passes on the racy design too — so it is designed out
    # rather than tested for. This asserts the invariant that does the designing-out: the
    # untouched entry keeps its inode, i.e. nothing rewrote it.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 263 270", env=env)
    survivor = tmp_path / "st" / "queued-263" / "270"
    before = survivor.stat().st_ino

    _call("clear_queued_subtask 263 264", env=env)

    assert survivor.stat().st_ino == before, "#270's entry is never rewritten by #264's clear"
    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["270"]


def test_queued_subtask_stamp_is_idempotent(tmp_path: Path) -> None:
    # The drain re-derives state every tick and may re-queue the same issue; a duplicate
    # entry would make the spoke re-anchor on an issue it already shipped.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 263 264", env=env)

    result = _call("read_queued_subtask 263", env=env)

    assert result.stdout.split() == ["264"]


def test_queued_subtask_is_keyed_per_spoke(tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 300 301", env=env)

    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["264"]
    assert _call("read_queued_subtask 300", env=env).stdout.split() == ["301"]


def test_clear_queued_subtask_drops_one_entry_and_keeps_the_rest(tmp_path: Path) -> None:
    # The spoke clears each issue as it ships it, so the queue drains one subtask at a time.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 263 270", env=env)

    _call("clear_queued_subtask 263 264", env=env)

    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["270"]


def test_clear_queued_subtask_without_an_issue_drops_the_whole_queue(tmp_path: Path) -> None:
    # The reclaim path (#278): a spoke that reached its terminal ready before the entry was
    # consumed has its queue dropped whole, and the issues fall back to a fresh dispatch.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 263 270", env=env)

    _call("clear_queued_subtask 263", env=env)

    assert _call("read_queued_subtask 263", env=env).stdout.strip() == ""


def test_clear_queued_subtask_of_the_last_entry_leaves_an_empty_queue(tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)

    _call("clear_queued_subtask 263 264", env=env)

    result = _call("read_queued_subtask 263", env=env)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_clear_queued_subtask_ignores_an_issue_not_in_the_queue(tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)

    result = _call("clear_queued_subtask 263 999", env=env)

    assert result.returncode == 0, "clearing an absent entry is a no-op, never an error"
    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["264"]


def test_clear_queued_subtask_does_not_match_a_number_substring(tmp_path: Path) -> None:
    # Clearing #4 must not eat #264 or #40. An entry is its own file named for the issue, so
    # the clear is an exact unlink and the substring hazard a grep-based queue would have
    # cannot arise at all.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)
    _call("stamp_queued_subtask 263 40", env=env)

    _call("clear_queued_subtask 263 4", env=env)

    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["40", "264"]


def test_queued_subtask_survives_a_fresh_arm(tmp_path: Path) -> None:
    # The channel is deliberately NOT per-window. Worktrees outlive `/afk off` (the drain
    # only tags them blocked/<N>, which re-arm reconciles away), so a spoke resuming in the
    # next window still owes its routed subtasks. Wiping the queue at arm — the way dispatch
    # epochs and progress state are wiped — would drop them and force each back through the
    # full fresh lifecycle this issue exists to avoid.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)

    _call("_clear_dispatch_epochs", env=env)
    _call("_clear_progress_state", env=env)

    assert _call("read_queued_subtask 263", env=env).stdout.split() == ["264"]


def test_queued_subtask_entry_lives_under_the_state_dir(tmp_path: Path) -> None:
    # The spoke-side reader (spoke-ready.sh) and worktree-new.sh cannot source this module, so
    # they INLINE the path — exactly as the outbound event spool already does. This pins the
    # path contract all three sides share: <state-dir>/queued-<spoke>/<issue>.
    env = {"AFK_STATE_DIR": str(tmp_path / "st")}
    _call("stamp_queued_subtask 263 264", env=env)

    assert (tmp_path / "st" / "queued-263" / "264").is_file()


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


# ── issue #283: the park-onset epoch tracks the CURRENT park episode ───────────
# park-onset is stamp-once while `waiting` and cleared only by a slot_state tick that observes
# the spoke past EVERY park check. Under dense permission traffic (#276) no tick ever observed
# "not parked", so many distinct park episodes FUSED into one onset that still held the original
# gate's timestamp 20 minutes later — and the watchdog's park-unanswered ceiling measured against
# it. note_park_episode re-stamps the onset whenever the pending park's SIGNATURE changes (the
# same content hash the broker already keys reanswer-<issue> on), so the onset names the episode
# actually pending rather than the oldest one in a fused run.


def _episode(sig: str, issue: str = "5") -> str:
    """A note_park_episode call with the park signature stubbed to `sig` (empty = unextractable)."""
    return f"_broker_park_signature() {{ printf '%s' '{sig}'; }}; note_park_episode /wt {issue}"


def test_note_park_episode_restamps_onset_when_signature_changes(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})

    _call(_episode("sigB"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1600"})

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1600", (
        "a changed park signature is a NEW episode — the onset restarts at it"
    )


def test_note_park_episode_holds_onset_when_signature_unchanged(tmp_path: Path) -> None:
    # The same park still pending is ONE episode: its onset must not creep forward each tick, or
    # a genuinely stranded park would never outlive the ceiling.
    statedir = tmp_path / "sd"
    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})

    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1600"})

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1000"


def test_note_park_episode_restamps_when_onset_missing_but_sig_record_survives(
    tmp_path: Path,
) -> None:
    # slot_state's clear_park_onset_epoch (detect.sh) drops the onset on a not-parked tick but
    # knows nothing about the sig record. A re-park raising the IDENTICAL dialog would then read
    # "signature unchanged" over a missing onset — so a missing onset re-stamps regardless.
    statedir = tmp_path / "sd"
    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})
    (statedir / "park-onset-5.epoch").unlink()

    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1600"})

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1600"


def test_note_park_episode_prints_the_current_episode_onset(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})

    result = _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1600"})

    assert result.stdout.strip() == "1000", "the caller reads the episode base off stdout"


def test_note_park_episode_restamps_when_the_tip_advanced_under_an_identical_park(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The episode key folds in the branch tip, exactly as the broker's own park record does
    # (_broker_reanswer_exhausted resets on a moved tip). Without it, a spoke that parks on a
    # dialog, is approved, commits, then parks on an IDENTICAL dialog reads "signature unchanged"
    # and inherits the FIRST park's onset — re-fusing episodes one layer below the bug #283 fixes.
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    expr = f"_broker_park_signature() {{ printf '%s' 'sigA'; }}; note_park_episode '{spoke_repo}' 5"
    _call(expr, env={**env, "AFK_NOW": "1000"})

    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "red"],
        cwd=spoke_repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"},
    )
    _call(expr, env={**env, "AFK_NOW": "1600"})

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1600", (
        "a park at a NEW tip is a new episode even when its signature is identical"
    )


def test_note_park_episode_noop_on_empty_signature(tmp_path: Path) -> None:
    # An unextractable park (_broker_park_signature fail-opens to empty) must leave the onset
    # exactly as slot_state's stamp-once left it: no episode claim we cannot substantiate.
    statedir = tmp_path / "sd"
    _call("stamp_park_onset_epoch 5", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})

    _call(_episode(""), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1600"})

    assert (statedir / "park-onset-5.epoch").read_text().strip() == "1000"
    assert not (statedir / "park-sig-5").exists(), "an empty signature records no episode"


def test_clear_progress_state_also_clears_the_park_sig_record(tmp_path: Path) -> None:
    # Per-window state, like park-onset itself: a leftover sig record would make the next
    # window's first park read as "signature unchanged" against a foreign episode.
    statedir = tmp_path / "sd"
    _call(_episode("sigA"), env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": "1000"})
    assert (statedir / "park-sig-5").exists()

    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "park-sig-5").exists()


# ── issue #288 AC3: answer-drop record (computed-then-dropped answers, no delivery) ────────────
# Distinct from reanswer-<issue> (the REASONER-RAN counter, armed BEFORE the outcome is known):
# this records only the SUBSET of attempts that ended in a DROP (never injected) — so the
# watchdog can tell "tried and still trying" from "tried and nothing has ever been deliverable"
# (the #277 shape). Keyed like _broker_reanswer_exhausted's own (tip, sig) counter, for the same
# reason: a changed tip or park content starts a fresh episode's count at 1.


def _drop(sig: str, wt: str, issue: str, reason: str) -> str:
    """A note_answer_drop call with the ORIGINAL park's signature passed explicitly — mirroring
    _broker_reanswer_exhausted's own already-captured-sig parameter, never a re-derived one
    (#288 review: recomputing it internally would attribute the drop to whichever park happens
    to be live at read time, not the one the answer was actually computed for)."""
    return f"note_answer_drop '{wt}' {issue} '{sig}' '{reason}'"


def _read_drop(sig: str, wt: str, issue: str) -> str:
    """read_answer_drop still reads the CURRENT live park's signature — a stubbed sig here
    models what is CURRENTLY pending at read time, independent of what note_answer_drop wrote."""
    return f"_broker_park_signature() {{ printf '%s' '{sig}'; }}; read_answer_drop '{wt}' {issue}"


def test_note_answer_drop_increments_within_the_same_tip_and_signature(
    spoke_repo: Path, tmp_path: Path
) -> None:
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_drop("sigA", str(spoke_repo), "5", "reason one"), env=env)
    _call(_drop("sigA", str(spoke_repo), "5", "reason two"), env=env)

    out = _call(_read_drop("sigA", str(spoke_repo), "5"), env=env).stdout.strip()

    count, _, reason = out.partition("\t")
    assert count == "2", f"a second drop on the SAME (tip, sig) must increment, got: {out!r}"
    assert reason == "reason two", "the LAST drop's own reason is what's kept"


def test_note_answer_drop_resets_on_a_new_signature(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_drop("sigA", str(spoke_repo), "5", "first episode"), env=env)

    _call(_drop("sigB", str(spoke_repo), "5", "second episode"), env=env)

    out = _call(_read_drop("sigB", str(spoke_repo), "5"), env=env).stdout.strip()
    assert out == "1\tsecond episode", "a changed park signature starts a fresh drop count"


def test_note_answer_drop_resets_on_a_tip_advance(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_drop("sigA", str(spoke_repo), "5", "first episode"), env=env)

    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "progress"],
        cwd=spoke_repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"},
    )
    _call(_drop("sigA", str(spoke_repo), "5", "second episode"), env=env)

    out = _call(_read_drop("sigA", str(spoke_repo), "5"), env=env).stdout.strip()
    assert out == "1\tsecond episode", (
        "a park at a NEW tip is a new episode even when its signature is identical"
    )


def test_read_answer_drop_empty_for_a_resolved_episode(spoke_repo: Path, tmp_path: Path) -> None:
    # A record for a PAST (tip, sig) must not leak into a query for the current one — mirrors
    # _broker_reanswer_exhausted's own stale-context handling.
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_drop("sigA", str(spoke_repo), "5", "old episode"), env=env)

    out = _call(_read_drop("sigB", str(spoke_repo), "5"), env=env).stdout.strip()
    assert out == "", "a different current signature reads as no drop on record"


def test_read_answer_drop_empty_when_none_recorded(spoke_repo: Path, tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    assert _call(f"read_answer_drop '{spoke_repo}' 5", env=env).stdout.strip() == ""


def test_clear_answer_drop_drops_the_record(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_drop("sigA", str(spoke_repo), "5", "x"), env=env)
    assert (statedir / "answer-drop-5").exists()

    _call("clear_answer_drop 5", env=env)

    assert not (statedir / "answer-drop-5").exists()


def test_clear_progress_state_also_clears_answer_drop(tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "answer-drop-5").write_text("abc\txyz\t1\treason\n")

    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "answer-drop-5").exists()


# ── issue #294: the served-permission-park marker (an APPROVE already delivered) ───────────────
# _decide_permission's APPROVE branches delivered approve_permission and recorded NOTHING about
# the park they served, so an UNCHANGED pending dialog — a pane that has not redrawn, or an
# approved `nohup ... &` whose gate keeps the gated tool_use unresolved — was re-approved on the
# next tick, bounded only by AFK_REANSWER_CEILING (exactly one duplicate keypress at the default
# 2, the #135/#188 concurrent-gate shape).
#
# Keyed like _broker_reanswer_exhausted's own (tip, sig) record PLUS the pending tool_use id. The
# id is what separates "the same dialog is still on screen" from "the spoke re-asked the identical
# command" — a repeatable safe command re-issued at the SAME tip (a failed push retried verbatim)
# is a NEW dialog with a NEW id, and a (tip, sig)-only marker would refuse to serve it forever:
# the tip cannot advance while the spoke is parked, so nothing would ever clear it.


def _serve(sig: str, wt: str, issue: str, tid: str) -> str:
    """note_permission_served takes the caller's ALREADY-CAPTURED park signature and the tool id
    captured BEFORE delivery — never re-derived (the #288 note_answer_drop lesson: re-deriving
    attributes the record to whichever park is live at call time, not the one that was served)."""
    return f"note_permission_served '{wt}' {issue} '{sig}' '{tid}'"


def _is_served(sig: str, wt: str, issue: str, tid: str) -> str:
    """The predicate reads the LIVE pending tool_use id; the stub models what the dialog is gating
    at READ time, independent of what note_permission_served recorded."""
    return (
        f"extract_pending_tool_id() {{ printf '%s' '{tid}'; }}; "
        f"_broker_permission_served '{wt}' {issue} '{sig}' && echo SERVED || echo FRESH"
    )


def test_permission_served_reads_back_the_same_tip_signature_and_tool_id(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The bug's shape: the identical dialog is still pending on the next tick. Same tip, same
    # signature, same gated tool_use → already served, so no second keypress.
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)

    out = _call(_is_served("sigA", str(spoke_repo), "5", "toolu_01"), env=env).stdout

    assert out.strip().splitlines()[-1] == "SERVED"


def test_permission_served_false_for_a_new_pending_tool_id(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The case a (tip, sig)-only key would strand: the spoke re-asks the IDENTICAL command at the
    # same tip (a failed push retried verbatim). Same tip, same signature — but a new tool_use, so
    # it is a genuinely new dialog and must be served.
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)

    out = _call(_is_served("sigA", str(spoke_repo), "5", "toolu_02"), env=env).stdout

    assert out.strip().splitlines()[-1] == "FRESH", (
        "an identical command re-asked at the same tip is a NEW dialog — never skip it"
    )


def test_permission_served_false_on_a_new_signature(spoke_repo: Path, tmp_path: Path) -> None:
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)

    out = _call(_is_served("sigB", str(spoke_repo), "5", "toolu_01"), env=env).stdout

    assert out.strip().splitlines()[-1] == "FRESH", "a changed park signature is a new park"


def test_permission_served_false_after_a_tip_advance(spoke_repo: Path, tmp_path: Path) -> None:
    # Issue item 3's "clear on tip advance", by the family's key-invalidation convention: a record
    # for a PAST tip stops matching, exactly as _broker_reanswer_exhausted / note_answer_drop do.
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)

    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "progress"],
        cwd=spoke_repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"},
    )
    out = _call(_is_served("sigA", str(spoke_repo), "5", "toolu_01"), env=env).stdout

    assert out.strip().splitlines()[-1] == "FRESH", "a park at a NEW tip is never already-served"


def test_permission_served_false_when_the_dialog_flushed_no_tool_id(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The #269 unflushed-dialog window: the pane shows the dialog but the gated tool_use is not in
    # the transcript, so there is no id to compare. Fail OPEN — never suppress on an unprovable
    # match (that path declines-as-unreadable and never approves anyway).
    env = {"AFK_STATE_DIR": str(tmp_path / "sd")}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)

    out = _call(_is_served("sigA", str(spoke_repo), "5", ""), env=env).stdout

    assert out.strip().splitlines()[-1] == "FRESH"


@pytest.mark.parametrize("sig,tid", [("", "toolu_01"), ("sigA", "")])
def test_note_permission_served_records_nothing_unsubstantiated(
    sig: str, tid: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # note_park_episode's posture: never claim a park we cannot substantiate. Without BOTH a
    # signature and the id of the tool_use we approved, there is no key — record nothing.
    statedir = tmp_path / "sd"

    _call(_serve(sig, str(spoke_repo), "5", tid), env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "served-5").exists()


def test_served_skip_due_inside_the_window_is_not_due(tmp_path: Path) -> None:
    # The skip is BACKOFF-PACED, never terminal: approve_permission verifies only that the
    # transcript mtime advanced, not that the dialog was consumed, so an approve whose keypress
    # never landed leaves the identical park pending. Inside the window the tick skips.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "served-5").write_text("abc\tsigA\ttoolu_01\t1000\n")

    result = _call(
        "_broker_served_skip_due 5 1030 && echo DUE || echo WAIT",
        env={"AFK_STATE_DIR": str(statedir), "AFK_SERVED_SKIP_SECONDS": "60"},
    )

    assert result.stdout.strip().splitlines()[-1] == "WAIT", "30s into a 60s window → still skip"


def test_served_skip_due_once_the_window_elapses(tmp_path: Path) -> None:
    # Once it elapses the marker is dropped for ONE supervised re-serve — the re-answer ceiling
    # and the #241 curve bound a standing failure from there, so this is never a strand.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "served-5").write_text("abc\tsigA\ttoolu_01\t1000\n")

    result = _call(
        "_broker_served_skip_due 5 1060 && echo DUE || echo WAIT",
        env={"AFK_STATE_DIR": str(statedir), "AFK_SERVED_SKIP_SECONDS": "60"},
    )

    assert result.stdout.strip().splitlines()[-1] == "DUE"


def test_served_skip_due_when_nothing_was_ever_served(tmp_path: Path) -> None:
    # Mirrors _afk_warned_due's "never armed → due": no record can never suppress a serve.
    result = _call(
        "_broker_served_skip_due 5 && echo DUE || echo WAIT",
        env={"AFK_STATE_DIR": str(tmp_path / "sd")},
    )

    assert result.stdout.strip().splitlines()[-1] == "DUE"


def test_clear_permission_served_drops_the_record(spoke_repo: Path, tmp_path: Path) -> None:
    statedir = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(statedir)}
    _call(_serve("sigA", str(spoke_repo), "5", "toolu_01"), env=env)
    assert (statedir / "served-5").exists()

    _call("clear_permission_served 5", env=env)

    assert not (statedir / "served-5").exists()


def test_clear_progress_state_also_clears_permission_served(tmp_path: Path) -> None:
    # Per-window state, like reanswer-* / answer-drop-*: a fresh arm starts with no served record.
    statedir = tmp_path / "sd"
    statedir.mkdir()
    (statedir / "served-5").write_text("abc\tsigA\ttoolu_01\t1000\n")

    _call("_clear_progress_state", env={"AFK_STATE_DIR": str(statedir)})

    assert not (statedir / "served-5").exists()


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


# ── issue #298: the park signature must hash PORTABLY (no bare shasum) ─────────
#
# _broker_park_signature hashed its basis with a bare `shasum -a 256`. shasum is a perl
# script that ships with macOS but NOT with a slim Linux image, where the coreutils
# spelling `sha256sum` is the one that exists. With shasum absent the pipeline yields
# EMPTY — and every reader treats an empty signature as "nothing extractable" and
# fail-opens: note_park_episode records no episode, and _broker_reanswer_exhausted never
# engages, so the #203/#269 re-answer ceiling silently stops bounding the reasoner and the
# doom-loop it exists to stop is back. worktree-lib's wt_sha256_stdin (shasum ||
# sha256sum) exists for exactly this and is in scope here: gate-broker.sh sources
# worktree-lib.sh before it sources this module.
#
# The `shasum` stub exits WITHOUT reading stdin, mirroring a genuinely missing binary —
# bash's own command-not-found leaves the piped bytes for the `||` fallback to consume, and
# a stub that drained stdin first would starve sha256sum and pass a broken fix.

_ABSENT_SHASUM_STUB = "#!/bin/sh\nexit 127\n"

# Resolved before any masking, from the test process's own (unmasked) PATH.
_REAL_SHASUM = shutil.which("shasum")


def _mask_shasum(fake_bin: Path) -> None:
    """Make PATH look like a slim Linux host: no `shasum`, a working GNU `sha256sum`.

    The sha256sum stub delegates to the REAL shasum by ABSOLUTE path, so the mask above
    cannot shadow it and the stub needs no interpreter of its own. That matters because
    wt_sha256_stdin discards the fallback's stderr: a stub that could fail to exec (a
    `python3 -c` shim, say) would red these tests with an empty digest and empty stderr —
    byte-identical to the defect under test, so a green fix would look broken. What is
    under test is that the CODE tries the sha256sum SPELLING, never how the stub hashes.
    """
    assert _REAL_SHASUM, "this suite already requires the macOS hasher (see pytestmark)"
    (fake_bin / "shasum").write_text(_ABSENT_SHASUM_STUB)
    (fake_bin / "shasum").chmod(0o755)
    (fake_bin / "sha256sum").write_text(f'#!/bin/sh\nexec "{_REAL_SHASUM}" -a 256\n')
    (fake_bin / "sha256sum").chmod(0o755)


def test_park_signature_still_hashes_when_shasum_is_absent(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The defect: on a shasum-less host the signature came back EMPTY, which the contract
    # reads as "ceiling never engages" — the #203/#269 doom-loop, re-enabled silently.
    env = _perm_env(tmp_path, spoke_repo, "git reset -q", "printf 'ESCALATE: unused'")
    _mask_shasum(tmp_path / "bin")

    result = _call(f"_broker_park_signature '{spoke_repo}' 5", env=env)

    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{64}", result.stdout.strip()), (
        "an absent shasum must fall back to sha256sum, not fail-open to an empty "
        f"signature that disables the re-answer ceiling: {result.stdout!r}{result.stderr}"
    )


def test_park_signature_is_identical_across_hasher_flavors(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The signature is persisted (_park_sig_file) and keys the re-answer ceiling, so the
    # digest must depend on the PARK, never on which hasher the host happens to ship —
    # otherwise the same unchanged park re-stamps its onset the moment the flavor differs.
    env = _perm_env(tmp_path, spoke_repo, "git reset -q", "printf 'ESCALATE: unused'")
    expr = f"_broker_park_signature '{spoke_repo}' 5"

    with_shasum = _call(expr, env=env)
    _mask_shasum(tmp_path / "bin")
    without_shasum = _call(expr, env=env)

    # Pin the baseline non-empty FIRST: _broker_park_signature returns empty with rc 0
    # whenever nothing is extractable, so should the park setup ever drift (a tmux-stub or
    # dialog-text change that stops _permission_pending firing) both calls would return ''
    # and a bare equality check would pass vacuously — inert forever, exactly when a real
    # flavor-dependent digest regression needs catching.
    assert re.fullmatch(r"[0-9a-f]{64}", with_shasum.stdout.strip()), (
        f"the park must produce a real digest to compare: {with_shasum.stdout!r}"
    )
    assert with_shasum.stdout.strip() == without_shasum.stdout.strip(), (
        "the digest must be hasher-flavor independent"
    )
