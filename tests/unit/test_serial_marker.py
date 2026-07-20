"""Issue #328 — the `serial` marker and the conftest xdist fail-loud guard.

The full suite runs two-phase: `-n auto -m "not serial"` for the parallel-safe bulk,
then `-m serial` single-process for the ref-mutating tail (test-select.sh /
gate-sweep.sh). Two things back that split:

- the `serial` marker is registered in ``pyproject.toml`` so ``-m serial`` selects and
  the marker raises no ``PytestUnknownMarkWarning``; and
- ``tests/conftest.py`` installs a fail-loud collection guard (afk principle #2): if a
  ``serial``-marked test is ever collected while xdist is active — the bare ``-n auto``
  over the whole suite this quarantine exists to prevent — the run refuses instead of
  letting a ref-mutating test corrupt a shared worker.

The guard is exercised by copying the real conftest into a child dir and driving its
hook directly with fake configs/items, so it needs no installed xdist (unavailable in
this env) to prove both the fires-under-xdist and quiet-when-single-process paths.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


def test_serial_marker_is_registered() -> None:
    # `pytest --markers` lists markers registered in the ini/pyproject; an unregistered
    # `serial` would be absent here (and warn on use). Reads the REAL pyproject config.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers", "-p", "no:cacheprovider"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "@pytest.mark.serial" in proc.stdout, (
        f"the `serial` marker is not registered in pyproject.toml:\n{proc.stdout}\n{proc.stderr}"
    )


def _run_guard_child(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Copy the real conftest into a child dir and run a test that drives its
    `pytest_collection_modifyitems` guard with fake configs/items — no xdist needed."""
    childdir = tmp_path / "child"
    childdir.mkdir()
    assert _CONFTEST.exists()
    (childdir / "conftest.py").write_text(_CONFTEST.read_text())
    (childdir / "test_inner.py").write_text(
        "import types\n"
        "import conftest\n"
        "import pytest\n"
        "\n"
        "class _Item:\n"
        "    def __init__(self, serial):\n"
        "        self._serial = serial\n"
        "        self.nodeid = 'test_x.py::test_serial' if serial else 'test_x.py::test_plain'\n"
        "    def get_closest_marker(self, name):\n"
        "        return object() if (name == 'serial' and self._serial) else None\n"
        "\n"
        "def _cfg(numprocesses):\n"
        "    return types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=numprocesses))\n"
        "\n"
        "def test_guard_fires_under_xdist():\n"
        "    items = [_Item(True), _Item(False)]\n"
        "    with pytest.raises(pytest.UsageError, match='serial'):\n"
        "        conftest.pytest_collection_modifyitems(_cfg('auto'), items)\n"
        "\n"
        "def test_guard_quiet_single_process():\n"
        "    # -n absent -> numprocesses None -> guard returns, serial tests run.\n"
        "    conftest.pytest_collection_modifyitems(_cfg(None), [_Item(True)])\n"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(childdir)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_conftest_guard_fires_under_xdist_and_is_quiet_single_process(tmp_path: Path) -> None:
    child = _run_guard_child(tmp_path)
    assert child.returncode == 0, (
        "the conftest xdist fail-loud guard is missing or misbehaving:\n"
        f"{child.stdout}\n{child.stderr}"
    )
