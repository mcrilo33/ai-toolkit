"""Mirror tests for hub-afk-land.sh (issue #307 split).

The LAND lane extracted from hub-afk.sh: auto_land, the land-retry and #285 conflict-
resolution lanes, the review-gate consult, answer_pass, the ready/blocked tip probes, and
the #285 conflict-resolve prompt. This is a behaviour-neutral MOVE, so these tests assert
the functions are (a) reachable through the entry lib and (b) physically located in the
module file — the logic itself stays covered by test_hub_afk.py.
"""

from __future__ import annotations

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-land.sh"

# Representative public + helper functions the land lane owns.
LAND_FUNCTIONS = [
    "auto_land",
    "answer_pass",
    "_ready_at_tip",
    "_blocked_at_tip",
    "_afk_review_verdict",
    "_afk_phase_max_seconds",
    "_afk_run_with_heartbeat",
    "_afk_land_retry_max",
    "_afk_route_conflict_resolution",
    "_afk_conflict_resolve_prompt",
    "_afk_hub_is_dirty",
    "_afk_stash_hub",
    "_afk_restore_hub",
    "_afk_escalate_land_precondition",
]


@pytest.mark.parametrize("fn", LAND_FUNCTIONS)
def test_land_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", LAND_FUNCTIONS)
def test_land_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE
    import os

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_phase_max_seconds_defaults_hold() -> None:
    # A pure helper: sanity that the moved code still evaluates (default + numeric guard).
    assert _call("_afk_phase_max_seconds").stdout.strip() == "900"
    assert _call("_afk_phase_max_seconds", env={"AFK_PHASE_MAX_SECONDS": "5"}).stdout.strip() == "5"
    assert (
        _call("_afk_phase_max_seconds", env={"AFK_PHASE_MAX_SECONDS": "x"}).stdout.strip() == "900"
    ), "a non-numeric override falls back to the 900 default"
