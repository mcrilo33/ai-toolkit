"""Mirror test for scripts/worktree-otel-lib.sh (issue #353 split).

`worktree-lib.sh` grew past the anti-monolith budget, so its native-OTel preflight
machinery (the bridge/collector/watch-arm helpers) was extracted into this module,
sourced by the thin `worktree-lib.sh` entry. The behavioural depth for these helpers
still runs through the entry in ``test_worktree_lib.py``; this file is the module's
own mirror: it pins the extraction contract — the public functions live HERE, the
entry sources them so consumers keep sourcing ``worktree-lib.sh`` unchanged, and the
general #189 process probes deliberately stayed in core — plus one behavioural smoke
proving the module's functions are reachable through the entry.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WT_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"
MODULE = REPO_ROOT / "scripts" / "worktree-otel-lib.sh"

# The public helpers issue #353 moved out of worktree-lib.sh into this module.
OTEL_FUNCTIONS = (
    "wt_port_listening",
    "wt_bridge_launch",
    "wt_bridge_pid",
    "wt_bridge_source_mtime",
    "wt_bridge_kill",
    "wt_bridge_restart_if_stale",
    "wt_otel_bridge_preflight",
    "wt_collector_config_version",
    "wt_collector_launch",
    "wt_collector_running_version",
    "wt_collector_remove",
    "wt_collector_container_status",
    "wt_collector_recover_dead",
    "wt_collector_restart_if_stale",
    "wt_otel_collector_preflight",
    "wt_otel_watch_arm",
)

# The general #189 locale-hardened process probes must NOT migrate here: hub-afk.sh
# and the gh module depend on them, and test_process_probe_lint pins them to the
# EXEMPT worktree-lib.sh. Guard the carve-out so a future edit can't drift them out.
CORE_ONLY_PROBES = ("wt_pgrep", "wt_ps_start_epoch")


def _defines(text: str, fn: str) -> bool:
    return re.search(rf"^{re.escape(fn)}\(\)", text, re.MULTILINE) is not None


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    """Source the entry (which sources this module) and run a shell expression."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC"},
    )


def test_module_file_exists_and_is_executable() -> None:
    assert MODULE.is_file(), "scripts/worktree-otel-lib.sh missing"
    assert os.access(MODULE, os.X_OK), "worktree-otel-lib.sh is not executable"


def test_module_defines_the_otel_public_functions() -> None:
    text = MODULE.read_text()
    missing = [fn for fn in OTEL_FUNCTIONS if not _defines(text, fn)]
    assert missing == [], f"worktree-otel-lib.sh must define {missing}"


def test_otel_functions_are_not_duplicated_in_core() -> None:
    # The reverse of the #189 carve-out: a MOVE, not a copy — every OTel function is
    # defined ONLY in this module, never left behind (or re-copied) in the entry. A
    # duplicate in core would be dead code shadowed by the module (sourced last), so
    # no behaviour test catches it; this guard does.
    core = WT_LIB.read_text()
    duped = [fn for fn in OTEL_FUNCTIONS if _defines(core, fn)]
    assert duped == [], (
        f"{duped} are duplicated in worktree-lib.sh — they were extracted to "
        "worktree-otel-lib.sh and must not remain (or be re-copied) in the entry"
    )


def test_probe_helpers_stay_in_core_not_this_module() -> None:
    # The #189 carve-out: these are defined in worktree-lib.sh, never here.
    text = MODULE.read_text()
    leaked = [fn for fn in CORE_ONLY_PROBES if _defines(text, fn)]
    assert leaked == [], (
        f"{leaked} must stay defined in worktree-lib.sh (hub-afk + test_process_probe_lint "
        "depend on it), not migrate into worktree-otel-lib.sh"
    )


def test_entry_sources_the_module_so_consumers_are_unchanged() -> None:
    # End-to-end: sourcing ONLY the entry must make every OTel function available,
    # proving the 24 consumers keep sourcing worktree-lib.sh with no edit.
    query = "; ".join(f"declare -F {fn} >/dev/null || echo MISSING {fn}" for fn in OTEL_FUNCTIONS)
    result = _call(query)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"entry did not expose OTel functions: {result.stdout!r}"


def test_collector_preflight_reachable_through_entry_and_noops_without_gate() -> None:
    # Behavioural smoke: a moved function runs through the entry and honours its
    # AI_TOOLKIT_OTEL gate (no gate → no launch), proving the module is wired live.
    result = _call(
        "wt_port_listening() { return 1; }; "
        'wt_collector_launch() { echo "LAUNCHED $1"; }; '
        "unset AI_TOOLKIT_OTEL; "
        "wt_otel_collector_preflight /repo"
    )

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout, "preflight must no-op when AI_TOOLKIT_OTEL is unset"
