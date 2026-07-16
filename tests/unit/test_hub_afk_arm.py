"""Mirror tests for hub-afk-arm.sh (issue #307 split).

The ARM-time lane extracted from hub-afk.sh: the --remote launch, the telemetry preflight,
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
    "remote_launch",
    "build_remote_launch_cmd",
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


def test_build_remote_launch_cmd_is_pure() -> None:
    # A pure helper: sanity that the moved code still renders the detached tmux launch.
    out = _call("build_remote_launch_cmd /repo afk 'bash x drain'").stdout

    assert "cd '/repo'" in out
    assert "tmux new -d -s 'afk'" in out
    assert "caffeinate -s bash x drain" in out
