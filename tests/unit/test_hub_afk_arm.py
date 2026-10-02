"""Mirror tests for hub-afk-arm.sh (issue #307 split).

The ARM-time lane extracted from hub-afk.sh: the --remote launch, the telemetry preflight,
the sleep-inhibitor/power status warnings, and the arm-time liveness probes + preconditions
+ the ONE arm verdict + the self-check. A behaviour-neutral MOVE, so these tests assert the
functions are reachable through the entry and physically located in the module file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file
from _orca_stub import install_orca_stub

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
    "afk_arm_orca_guard",
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


def test_arm_claude_check_default_probe_runs_on_haiku() -> None:
    # #358: the auth probe spends an Opus call on a two-token `ok` liveness ping — pure
    # waste. The default AFK_AUTH_PROBE_CMD belongs on the cheap tier (Haiku), never the
    # retired claude-opus-4-8 literal.
    src = _call("declare -f _afk_arm_claude_check").stdout

    assert "claude-haiku-4-5" in src
    assert "claude-opus-4-8" not in src


# --- Orca arm guard (#363) ------------------------------------------------------
# Dispatch is Orca-only since #363, but the drain still supervises tmux panes:
# recover_dead_panes would treat a pane-less Orca spoke as crashed and tear it down or
# relaunch it in tmux. So arming is refused, naming #365 (S6), until the supervisor moves to Orca.


def _orca_env(tmp_path: Path, **kw: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = install_orca_stub(bindir, **kw)
    return {**env, "PATH": f"{bindir}:{os.environ['PATH']}"}


def test_arm_refuses_with_a_message_naming_365_even_with_a_healthy_orca(tmp_path: Path) -> None:
    result = _call("afk_arm_orca_guard; echo RC=$?", env=_orca_env(tmp_path))

    assert "RC=1" in result.stdout
    assert "#365" in result.stderr


@pytest.mark.parametrize("version", ["1.4.217", "0.9.0"])
def test_arm_refuses_on_an_orca_below_the_floor_before_the_interim_guard(
    tmp_path: Path, version: str
) -> None:
    result = _call("afk_arm_orca_guard; echo RC=$?", env=_orca_env(tmp_path, version=version))

    assert "RC=1" in result.stdout
    assert "1.4.218" in result.stderr


def test_arm_refuses_when_the_orca_runtime_does_not_answer(tmp_path: Path) -> None:
    scenario = {
        "worktree list": [{"rc": 1, "out": {"ok": False, "error": {"code": "runtime_unavailable"}}}]
    }

    result = _call("afk_arm_orca_guard; echo RC=$?", env=_orca_env(tmp_path, scenario=scenario))

    assert "RC=1" in result.stdout
    assert "Orca" in result.stderr


def test_arm_guard_honours_the_precheck_opt_out(tmp_path: Path) -> None:
    result = _call(
        "afk_arm_orca_guard; echo RC=$?", env={**_orca_env(tmp_path), "AFK_ARM_PRECHECK": "0"}
    )

    assert "RC=0" in result.stdout
