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
    "_afk_scope_line_of",
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


# ── _afk_scope_line_of — the single writer of Scope-line semantics (#356/#5) ──
# Factored out of _inflight_scope_args so the dispatcher and the land lane's cross-scope
# guard (hub-afk-land.sh) share ONE extractor — not two `sed` copies kept equal by a test.
# The land lane resolves both its own and its siblings' Scope: line through this helper.


@pytest.mark.parametrize(
    "body,expected",
    [
        pytest.param("Scope: a b c", "a b c", id="single-line"),
        pytest.param("intro\nScope: x/y.sh z/w.sh\nGate: plan", "x/y.sh z/w.sh", id="mid-body"),
        pytest.param("  scope:   lowercased  ", "lowercased", id="case-insensitive-and-trimmed"),
        pytest.param("no scope declared here", "", id="no-scope-line"),
        pytest.param("Scope: *", "*", id="exclusive-star"),
        pytest.param("Scope: a\nScope: b", "a", id="first-line-wins"),
    ],
)
def test_scope_line_of_extracts(body: str, expected: str) -> None:
    # `$'...'` lets the coprocess interpret the embedded newlines before the helper reads it.
    literal = "$'" + body.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
    result = _call(f"_afk_scope_line_of {literal}")

    assert result.stdout.strip() == expected, result.stdout + result.stderr


def test_inflight_scope_args_routes_through_the_shared_extractor() -> None:
    # The refactor must keep _inflight_scope_args deriving its scope from _afk_scope_line_of:
    # stub the body fetch + the extractor, and prove the emitted --inflight value is the
    # extractor's output (so the two can never drift — one writer, #5).
    expr = (
        "inflight_issues() { printf '77\\n'; }; "
        "_afk_with_timeout() { printf 'Scope: shared/x.sh\\n'; }; "
        "_afk_scope_line_of() { printf 'SENTINEL\\n'; }; "
        "_inflight_scope_args"
    )
    result = _call(expr)

    assert result.stdout.split() == ["--inflight", "SENTINEL"], result.stdout + result.stderr
