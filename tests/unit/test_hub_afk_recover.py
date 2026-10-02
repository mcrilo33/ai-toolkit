"""Mirror tests for hub-afk-recover.sh (issue #307 split).

The RECOVER lane extracted from hub-afk.sh: reap / revive / nudge / dead-pane / finish-up --
crash-resume + liveness probes, the ledger completion signal, the #255 nudge counter, the
resume/nudge/finish-up prompts + resume/respawn, the #241 revive-first lane,
_reap_or_resume, the auth/net reap-prep probes, reap_pass, and recover_dead_panes. A
behaviour-neutral MOVE, so these tests assert the functions are reachable through the entry
and physically located in the module file.
"""

from __future__ import annotations

import os

import pytest
from _hub_afk_support import HUB_SCRIPTS_DIR, _call, function_source_file

MODULE = "hub-afk-recover.sh"

RECOVER_FUNCTIONS = [
    "reap_pass",
    "recover_dead_panes",
    "_reap_or_resume",
    "_revive_spoke",
    "resume_spoke",
    "_afk_finish_up_or_revive",
    "_afk_crash_reresume_or_escalate",
    "_afk_crash_escalate_or_park",
    "_afk_nudge_spoke",
    "_afk_auth_is_dead",
    "_afk_network_is_down",
    "_redispatch_exited_spoke",
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
