"""Regression: the dashboard must import under ``streamlit run`` (Issue #75, Bug 1).

``streamlit run dashboard/app.py`` puts only ``dashboard/`` on ``sys.path`` — never
the sibling ``scripts/`` dir that holds the ``telemetry`` package. ``app.py`` does
``import queries`` and ``dashboard/queries.py`` top-level-imports ``telemetry.*``, so a
launch crashed with ``ModuleNotFoundError: No module named 'telemetry'`` on every page
load. The fix makes ``queries.py`` self-insert ``scripts/`` before those imports, so any
entrypoint resolves the package.

The check runs in a *subprocess* with a clean environment (no inherited ``PYTHONPATH``)
and only ``dashboard/`` injected onto ``sys.path`` — a faithful simulation of streamlit's
import context. ``streamlit`` itself is stubbed with a ``MagicMock`` because its real
import is unavailable in the base test env and is irrelevant to this bug.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = _REPO_ROOT / "dashboard"
SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _import_app_with_only_dashboard_on_path() -> subprocess.CompletedProcess[str]:
    """Import ``dashboard/app.py`` in a fresh interpreter mimicking ``streamlit run``."""
    code = textwrap.dedent(
        f"""
        import sys
        from unittest.mock import MagicMock

        # Simulate `streamlit run dashboard/app.py`: only the script's own dir is added,
        # and the scripts/ dir holding the telemetry package is deliberately absent.
        sys.path[:] = [p for p in sys.path if p not in ("", {str(SCRIPTS_DIR)!r})]
        sys.modules["streamlit"] = MagicMock()
        sys.path.insert(0, {str(DASHBOARD_DIR)!r})

        import app  # must not raise ModuleNotFoundError: No module named 'telemetry'
        print("IMPORT_OK")
        """
    )
    # A fresh interpreter must not inherit scripts/ via PYTHONPATH from the parent suite.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )


def test_app_imports_with_only_dashboard_on_sys_path() -> None:
    result = _import_app_with_only_dashboard_on_path()

    assert "IMPORT_OK" in result.stdout, (
        "dashboard/app.py must import with only dashboard/ on sys.path "
        f"(streamlit's context).\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    assert result.returncode == 0, result.stderr
