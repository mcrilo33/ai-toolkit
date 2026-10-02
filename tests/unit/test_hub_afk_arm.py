"""Mirror tests for hub-afk-arm.sh (issue #307 split).

The ARM-time lane extracted from hub-afk.sh: the telemetry preflight,
the sleep-inhibitor/power status warnings, and the arm-time liveness probes + preconditions
+ the ONE arm verdict + the self-check. A behaviour-neutral MOVE, so these tests assert the
functions are reachable through the entry and physically located in the module file.
"""

from __future__ import annotations

import os

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-arm.sh"

ARM_FUNCTIONS = [
    "afk_telemetry_enabled",
    "afk_resolve_telemetry_auth",
    "afk_telemetry_preflight",
    "afk_arm_preconditions",
    "afk_arm_selfcheck",
    "afk_warn_power",
    "_afk_arm_judge_check",
    "_afk_arm_gh_check",
]


@pytest.mark.parametrize("fn", ARM_FUNCTIONS)
def test_arm_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", ARM_FUNCTIONS)
def test_arm_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_arm_claude_check_default_probe_runs_on_haiku() -> None:
    # #358: the auth probe spends an Opus call on a two-token `ok` liveness ping — pure
    # waste. The default AFK_AUTH_PROBE_CMD belongs on the cheap tier (Haiku), never the
    # retired claude-opus-4-8 literal.
    src = _call("declare -f _afk_arm_claude_check").stdout

    assert "claude-haiku-4-5" in src
    assert "claude-opus-4-8" not in src
