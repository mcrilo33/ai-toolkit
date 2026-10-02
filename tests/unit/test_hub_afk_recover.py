"""Mirror tests for hub-afk-recover.sh (issue #307 split).

The RECOVER lane extracted from hub-afk.sh: reap / revive / nudge / dead-pane / finish-up --
crash-resume + liveness probes, the ledger completion signal, the #255 nudge counter, the
resume/nudge/finish-up prompts + resume/respawn, hang-forensics, the #241 revive-first lane,
_reap_or_resume, the auth/net reap-prep probes, reap_pass, and recover_dead_panes. A
behaviour-neutral MOVE, so these tests assert the functions are reachable through the entry
and physically located in the module file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-recover.sh"

RECOVER_FUNCTIONS = [
    "reap_pass",
    "recover_dead_panes",
    "_reap_or_resume",
    "_revive_spoke",
    "resume_spoke",
    "respawn_wedged_spoke",
    "_afk_finish_up_or_revive",
    "_afk_crash_reresume_or_escalate",
    "_afk_crash_escalate_or_park",
    "_afk_nudge_spoke",
    "_afk_auth_is_dead",
    "_afk_network_is_down",
    "_redispatch_dead_pane",
]


@pytest.mark.parametrize("fn", RECOVER_FUNCTIONS)
def test_recover_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", RECOVER_FUNCTIONS)
def test_recover_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_auth_is_dead_default_probe_runs_on_haiku() -> None:
    # #358: the auth probe spends an Opus call on a two-token `ok` liveness ping — pure
    # waste. The default AFK_AUTH_PROBE_CMD belongs on the cheap tier (Haiku), never the
    # retired claude-opus-4-8 literal.
    src = _call("declare -f _afk_auth_is_dead").stdout

    assert "claude-haiku-4-5" in src
    assert "claude-opus-4-8" not in src


# -- #373: the crash ladder keys on EFFECTIVE attendance (mode OR an armed drain) -------------
# An attended-dispatched spoke adopted by an armed drain used to take the warn-park fallback:
# no retry, no blocked/<issue>, and a message claiming a retry that was never scheduled.

_T0 = 1_700_000_000
_TICK_SECONDS = 300  # the drain's reconcile tick
_WATCHDOG_CEILING_SECONDS = 3600

_LADDER = (
    'resume_spoke() { printf "RESUMED\\n" >> "$RETRY_LOG"; }; '
    '_afk_crash_reresume_or_escalate "$WT" 5 "pane crashed again" resume_spoke'
)


def _mode_spoke(parent: Path, mode: str) -> Path:
    wt = parent / "spoke"
    (wt / ".ai-toolkit").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True, capture_output=True)
    (wt / ".ai-toolkit" / "mode").write_text(f"{mode}\n")
    return wt


def _ladder_env(tmp_path: Path, spoke: Path, *, armed: bool) -> dict[str, str]:
    """Env for one crash-ladder call; AFK_STATE is always explicit so host drain state never leaks."""
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    afk_state = tmp_path / "afk-state"
    if armed:
        afk_state.write_text("1700003600\n")
    ready = tmp_path / "spoke-ready.sh"
    ready.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{tmp_path / "ready.log"}"\n')
    ready.chmod(0o755)
    return {
        "WT": str(spoke),
        "AFK_STATE_DIR": str(statedir),
        "AFK_STATE": str(afk_state),
        "SPOKE_READY": str(ready),
        "RETRY_LOG": str(tmp_path / "retry.log"),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_NOW": str(_T0),
    }


def _blocked_emitted(tmp_path: Path) -> bool:
    ready_log = tmp_path / "ready.log"
    return ready_log.exists() and "--blocked 5" in ready_log.read_text()


def _drive_ticks_until_blocked(tmp_path: Path, env: dict[str, str]) -> int:
    """Step AFK_NOW a tick at a time through the default backoff; return elapsed seconds."""
    elapsed = 0
    while elapsed < _WATCHDOG_CEILING_SECONDS:
        _call(_LADDER, env={**env, "AFK_NOW": str(_T0 + elapsed)})
        if _blocked_emitted(tmp_path):
            break
        elapsed += _TICK_SECONDS
    return elapsed


def test_effective_attendance_afk_mode_is_afk(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "afk")

    result = _call(
        f'_afk_effective_attendance "{spoke}"', env={"AFK_STATE": str(tmp_path / "absent")}
    )

    assert result.stdout.strip() == "afk"


def test_effective_attendance_attended_with_armed_drain(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "attended")
    armed = tmp_path / "armed"
    armed.write_text("1700003600\n")

    result = _call(f'_afk_effective_attendance "{spoke}"', env={"AFK_STATE": str(armed)})

    assert result.stdout.strip() == "attended+armed"


def test_effective_attendance_attended_without_drain(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "attended")

    result = _call(
        f'_afk_effective_attendance "{spoke}"', env={"AFK_STATE": str(tmp_path / "absent")}
    )

    assert result.stdout.strip() == "attended"


def test_effective_attendance_worktreeless_is_attended_even_when_armed(tmp_path: Path) -> None:
    armed = tmp_path / "armed"
    armed.write_text("1700003600\n")

    result = _call('_afk_effective_attendance ""', env={"AFK_STATE": str(armed)})

    assert result.stdout.strip() == "attended"


def test_attended_under_armed_drain_reaches_blocked_before_watchdog_ceiling(
    tmp_path: Path,
) -> None:
    # Replay pin (#371): mode=attended, drain armed. Stepping AFK_NOW a tick at a time through
    # the DEFAULT reap-lane backoff, blocked/<issue> lands before the 3600s dead-pane ceiling.
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=True)

    elapsed = _drive_ticks_until_blocked(tmp_path, env)

    assert _blocked_emitted(tmp_path), f"no blocked/5 after {elapsed}s"
    assert elapsed < _WATCHDOG_CEILING_SECONDS


def test_attended_under_armed_drain_retries_before_escalating(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=True)

    _drive_ticks_until_blocked(tmp_path, env)

    assert (tmp_path / "retry.log").read_text().count("RESUMED") == 3


def test_attended_without_armed_drain_keeps_warn_and_wait(tmp_path: Path) -> None:
    # Narrowed #310 AC5 pin: no armed drain => the human is the wall, even with a spent budget.
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=False)
    statedir = Path(env["AFK_STATE_DIR"])
    (statedir / "warned-state-5").write_text("3\t1\n")

    _call(_LADDER, env=env)

    assert not _blocked_emitted(tmp_path)
    assert not (tmp_path / "retry.log").exists()
    assert (statedir / "warned-5.txt").exists()


def test_attended_warn_message_claims_no_unscheduled_retry(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=False)

    result = _call(_LADDER, env=env)

    assert "retried at low frequency" not in result.stderr
    assert "no automatic relaunch" in result.stderr


def test_armed_attended_retry_message_names_the_attempt(tmp_path: Path) -> None:
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=True)

    result = _call(_LADDER, env=env)

    assert "attempt 1/3" in result.stderr


@pytest.mark.parametrize("warned_state", [None, "3\t1\n"], ids=["retry", "escalate"])
def test_decision_journal_names_effective_attendance(
    tmp_path: Path, warned_state: str | None
) -> None:
    spoke = _mode_spoke(tmp_path, "attended")
    env = _ladder_env(tmp_path, spoke, armed=True)
    statedir = Path(env["AFK_STATE_DIR"])
    if warned_state is not None:
        (statedir / "warned-state-5").write_text(warned_state)

    _call(_LADDER, env=env)

    journal = (statedir / "decision-journal.jsonl").read_text()
    assert "effective attendance: mode=attended, drain armed=yes" in journal
