"""Mirror tests for hub-afk-supervise.sh (issue #307 split).

The runtime-SUPERVISION lane extracted from hub-afk.sh: the sleep inhibitor, the watchdog +
respawn, the self-update / self-deploy protocol, the respawn crash-loop guard + kill-wedged,
and the restart-survival re-arm. A behaviour-neutral MOVE, so these tests assert the
functions are reachable through the entry and physically located in the module file — plus
that the self-update scope + validated-source set now name every hub-afk-<lane>.sh module.
"""

from __future__ import annotations

import os

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-supervise.sh"

SUPERVISE_FUNCTIONS = [
    "watchdog_loop",
    "watchdog_tick",
    "_afk_arm_inhibitor",
    "_afk_heartbeat_wedged",
    "_afk_exec_self_copy",
    "_afk_self_deploy",
    "_afk_selfupdate_scope_paths",
    "_afk_selfupdate_source_scripts",
    "_afk_respawn_allowed",
    "afk_reconcile",
]

ALL_MODULES = ("land", "dispatch", "arm", "supervise", "recover")


@pytest.mark.parametrize("fn", SUPERVISE_FUNCTIONS)
def test_supervise_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", SUPERVISE_FUNCTIONS)
def test_supervise_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


@pytest.mark.parametrize("mod", ALL_MODULES)
def test_selfupdate_scope_names_every_module(mod: str) -> None:
    # Constraint 5: a land of any hub-afk-<lane>.sh must redeploy the running drain, so its
    # basename must be in the default AFK_SELFUPDATE_SCOPE set.
    scope = _call("_afk_selfupdate_scope_paths").stdout.split()

    assert f"hub-afk-{mod}.sh" in scope, (
        f"hub-afk-{mod}.sh is not in AFK_SELFUPDATE_SCOPE — a land of it would not self-deploy"
    )


@pytest.mark.parametrize("mod", ALL_MODULES)
def test_selfupdate_source_scripts_validates_every_module(mod: str) -> None:
    # #250 finding 5: a scope entry needs a validated source path, or a broken version deploys
    # unchecked. Every module must be in the bash-n-validated source set.
    out = _call("_afk_selfupdate_source_scripts /root").stdout

    assert f"/root/shared/skills/hub/scripts/hub-afk-{mod}.sh" in out, (
        f"hub-afk-{mod}.sh is not in _afk_selfupdate_source_scripts — it would deploy unvalidated"
    )
