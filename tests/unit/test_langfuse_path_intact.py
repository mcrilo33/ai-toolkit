"""Guard the Langfuse push path after the Streamlit-dashboard removal (Issue #90).

Issue #90 deletes the Streamlit renderer (``dashboard/app.py`` etc.) and its
pull-only DuckDB backing (``telemetry.store`` + the dashboard's ``telemetry.queries``
query layer). The push path — transcript/raw-body parsers feeding the Langfuse
pipeline — must remain fully importable, and the deleted modules must be gone for
good so nothing silently re-imports them.

This is the RED guard: before deletion ``telemetry.store`` / ``telemetry.queries``
still import, so the "removed" assertions fail.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

# The transitive closure of the Langfuse push path. None of these may import the
# deleted DuckDB store / dashboard query layer.
LANGFUSE_PATH_MODULES = (
    "telemetry.langfuse_spoke_tree",
    "telemetry.langfuse_rollup",
    "telemetry.langfuse_message_bridge",
    "telemetry.measure_context_cost",
    "telemetry.request_body",
)

# Pull-only modules that back the Streamlit renderer only — removed by #90.
REMOVED_MODULES = (
    "telemetry.store",
    "telemetry.queries",
)


@pytest.mark.parametrize("module", LANGFUSE_PATH_MODULES)
def test_langfuse_path_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_pull_only_module_is_removed(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
