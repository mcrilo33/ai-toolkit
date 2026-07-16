"""Mirror tests for hub-afk-dispatch.sh (issue #307 split).

The DISPATCH lane extracted from hub-afk.sh: dispatch_batch, kickoff_for, the in-flight
scope args, the concurrency cap + stagger, the dispatch-failure ceiling, the #278 subtask
routing, and the afk:* status labels. A behaviour-neutral MOVE, so these tests assert the
functions are reachable through the entry and physically located in the module file.
"""

from __future__ import annotations

import os

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-dispatch.sh"

DISPATCH_FUNCTIONS = [
    "dispatch_batch",
    "kickoff_for",
    "_inflight_scope_args",
    "_afk_cores",
    "_afk_dispatch_cap",
    "_afk_dispatch_stagger",
    "_afk_dispatch_max_failures",
    "_afk_subtask_chain_max",
    "_afk_route_queued_subtasks",
    "afk_sync_status_labels",
]


@pytest.mark.parametrize("fn", DISPATCH_FUNCTIONS)
def test_dispatch_function_is_reachable_through_entry(fn: str) -> None:
    result = _call(f"type -t {fn}")

    assert result.stdout.strip() == "function", (
        f"{fn} is not defined after sourcing hub-afk.sh — the entry does not source "
        f"{MODULE}, or the function was dropped in the move"
    )


@pytest.mark.parametrize("fn", DISPATCH_FUNCTIONS)
def test_dispatch_function_lives_in_module_file(fn: str) -> None:
    src = function_source_file(fn)

    assert src.endswith(MODULE), (
        f"{fn} resolves from {src!r}, not {MODULE} — a behaviour-neutral move must place it "
        f"in the module, not leave it in the entry"
    )


def test_module_file_is_present_and_executable() -> None:
    mod = HUB_SCRIPTS_DIR / MODULE

    assert mod.is_file(), f"{MODULE} missing"
    assert os.access(mod, os.X_OK), f"{MODULE} is not executable"


def test_cores_is_a_positive_integer() -> None:
    # A pure helper: sanity that the moved code still evaluates to a valid core count.
    out = _call("_afk_cores").stdout.strip()

    assert out.isdigit() and int(out) >= 1, f"_afk_cores returned {out!r}"
