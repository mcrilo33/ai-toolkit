"""Shared bash-coprocess harness for the hub-afk module test suite (issue #307).

`hub-afk.sh` is split into hub-afk-<lane>.sh functional modules (dispatch / recover / land /
arm / supervise), mirroring the #275 gate-broker split. Each module has a mirror test file
(test_hub_afk_<lane>.py) that sources the ENTRY lib (hub-afk.sh) — which in turn sources the
modules — and drives the module's functions, exactly as the gate-broker module tests source
gate-broker.sh. This module owns the one shared coprocess so the multi-thousand-line source
cost is paid once (issue #276), not once per module test file.

The large legacy test_hub_afk.py keeps its own private `_session`; it is intentionally left
untouched by the split.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from bash_session import BashSession, fresh_call

# hub-afk.sh targets the macOS control plane (BSD `stat -f %m`, the tmux hub) — the same gate
# test_hub_afk.py and the gate-broker suite carry (#129).
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="hub-afk.sh requires BSD stat (-f %m) and the macOS tmux hub (#129)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_SCRIPTS_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"
HUB_AFK = HUB_SCRIPTS_DIR / "hub-afk.sh"

# The gh lifecycle-label mirror is forced OFF for every call (parity with test_hub_afk.py): a
# reap/escalation exercising _afk_escalate_blocked must not fire a REAL `gh issue edit`.
_BASE_ENV = {"AI_TOOLKIT_GH_LIFECYCLE_LABELS": "0"}

# Source-time resolution keys (issue #276): read while hub-afk.sh is being SOURCED to locate
# its own dir and the helpers it resolves once. A call overriding one cannot reuse the already
# sourced coprocess, so it routes to a fresh source. Mirrors test_hub_afk.py's set.
_FRESH_SOURCE_KEYS = frozenset({"SCRIPT_DIR", "AFK_WT_LIB", "AFK_GATE_BROKER", "AFK_INGEST_BIN"})

_SESSION: BashSession | None = None


def _session() -> BashSession:
    """The module-suite-scoped bash that sources hub-afk.sh (and its modules) once."""
    global _SESSION
    if _SESSION is None or not _SESSION.alive:
        _SESSION = BashSession(HUB_AFK, base_env=_BASE_ENV)
    return _SESSION


def _call(
    fn_call: str, *, env: dict[str, str] | None = None, fresh: bool = False
) -> subprocess.CompletedProcess[str]:
    """Invoke a shell expression against hub-afk.sh's (module-sourced) functions.

    Reuses one coprocess that sources hub-afk.sh once (issue #276); routes to a fresh source
    when fresh=True or env overrides a source-time resolution key.
    """
    if fresh or (env and _FRESH_SOURCE_KEYS.intersection(env)):
        return fresh_call(HUB_AFK, fn_call, env=env, base_env=_BASE_ENV)
    return _session().call(fn_call, env=env)


def function_source_file(fn: str) -> str:
    """Return the source path bash records for a function definition (`shopt -s extdebug`).

    The entry sources every module, so each function resolves; `declare -F <fn>` under
    extdebug prints "<name> <lineno> <file>" — the file column proves the function physically
    lives in its module, catching an accidental re-merge into the entry.
    """
    result = _call(f"shopt -s extdebug; declare -F {fn}")
    line = result.stdout.strip()
    return line.split(" ", 2)[2] if line.count(" ") >= 2 else ""
