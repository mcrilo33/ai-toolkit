"""Mirror tests for hub-afk-state.sh (issue #351 split).

The persisted-STATE lane extracted from hub-afk.sh: window/time parsing (`parse_duration`,
`compute_end_epoch`, `window_expired`, `minutes_remaining`) plus every on-disk supervisor
record — the window state file, the #252 arm-generation token, the #252 synchronous-off
wait, the #150 landed tally + drain-complete handoff, the #107 heartbeat, and the #202 B
last-action label — and the liveness cross-checks built on top of them (`_afk_pid_alive`,
`afk_supervisor_state`, `_afk_heartbeat_age_minutes`). A behaviour-neutral MOVE, so these
tests assert the functions are reachable through the entry and physically located in the
module file.
"""

from __future__ import annotations

import os

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-state.sh"

STATE_FUNCTIONS = [
    "parse_duration",
    "compute_end_epoch",
    "window_expired",
    "minutes_remaining",
    "afk_state_file",
    "_afk_atomic_write",
    "afk_write_state",
    "afk_read_state",
    "afk_clear_state",
    "afk_arm_epoch_file",
    "afk_read_arm_epoch",
    "afk_write_arm_epoch",
    "afk_clear_arm_epoch",
    "afk_new_arm_token",
    "afk_arm_superseded",
    "afk_wait_supervisor_gone",
    "afk_landed_count_file",
    "afk_drain_complete_file",
    "afk_read_landed_count",
    "_afk_incr_landed",
    "_afk_clear_landed_count",
    "_afk_clear_drain_complete",
    "_afk_emit_drain_complete",
    "afk_heartbeat_file",
    "afk_write_heartbeat_pid",
    "afk_write_heartbeat",
    "afk_read_heartbeat",
    "afk_clear_heartbeat",
    "afk_heartbeat_epoch",
    "_afk_last_action_file",
    "_afk_set_last_action",
    "_afk_read_last_action",
    "_afk_clear_last_action",
    "_afk_pid_alive",
    "afk_supervisor_state",
    "_afk_heartbeat_age_minutes",
]


@pytest.mark.parametrize("fn", STATE_FUNCTIONS)
def test_state_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", STATE_FUNCTIONS)
def test_state_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_parse_duration_is_pure() -> None:
    # A pure helper: sanity that the moved code still parses the same duration grammar.
    assert _call("parse_duration 1h30m").stdout.strip() == "5400"
    assert _call("parse_duration 45").stdout.strip() == "2700"
