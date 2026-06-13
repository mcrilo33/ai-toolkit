"""Shared helpers for the dashboard view-logic tests (Issue #23).

The dashboard lives in ``dashboard/`` — a self-contained subdirectory with its
own ``requirements.txt`` (streamlit + duckdb), deliberately NOT part of the
repo's Python packaging. To exercise its query layer from the suite without
adding it to ``sys.path`` (which would pollute import resolution for every other
test) we load ``dashboard/queries.py`` by file path via importlib.

Filename is underscore-prefixed so pytest does not collect it as a test module.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = _REPO_ROOT / "dashboard"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dashboard_spans.jsonl"


def load_queries() -> ModuleType:
    """Import ``dashboard/queries.py`` as a standalone module."""
    path = DASHBOARD_DIR / "queries.py"
    spec = importlib.util.spec_from_file_location("dashboard_queries", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load queries module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def store():
    """A :class:`SpanStore` built from the shared JSONL fixture."""
    queries = load_queries()
    return queries.SpanStore.from_jsonl(FIXTURE)
