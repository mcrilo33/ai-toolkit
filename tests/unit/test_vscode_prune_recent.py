"""Unit tests for scripts/vscode-prune-recent.py (issue #103).

The teardown integration tests in test_worktree_done.py pin the end-to-end
contract over unencoded `ai-toolkit-N` paths. These cover the scrubber directly
on the edges those paths never exercise: a percent-encoded recent URI and the
state store's file mode surviving the atomic rewrite.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from urllib.parse import quote

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "vscode-prune-recent.py"


@pytest.fixture()
def prune():
    """Import scripts/vscode-prune-recent.py as a module (the file has a hyphen)."""
    spec = importlib.util.spec_from_file_location("vscode_prune_recent", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_storage(tmp_path: Path, data: dict, *, mode: int = 0o644) -> Path:
    storage = tmp_path / "storage.json"
    storage.write_text(json.dumps(data))
    storage.chmod(mode)
    return storage


def test_history_entry_with_percent_encoded_uri_is_pruned(prune, tmp_path: Path) -> None:
    # A worktree path with a space is stored percent-encoded in the history URI;
    # the scrub must decode before matching the raw filesystem path it is given.
    target = str(tmp_path / "My Worktree")
    storage = _write_storage(
        tmp_path,
        {
            "history.recentlyOpenedPathsList": {
                "entries": [{"folderUri": f"file://{quote(target)}"}]
            }
        },
    )

    assert prune.main([str(_MODULE_PATH), str(storage), target]) == 0

    entries = json.loads(storage.read_text())["history.recentlyOpenedPathsList"]["entries"]
    assert entries == []


def test_atomic_write_preserves_file_mode(prune, tmp_path: Path) -> None:
    # tempfile.mkstemp creates 0600; the rewrite must carry over the store's
    # original 0644 rather than silently tightening permissions.
    target = "/tmp/gone"
    storage = _write_storage(
        tmp_path,
        {"history.recentlyOpenedPathsList": {"entries": [{"folderUri": f"file://{target}"}]}},
        mode=0o644,
    )

    assert prune.main([str(_MODULE_PATH), str(storage), target]) == 0

    assert stat.S_IMODE(os.stat(storage).st_mode) == 0o644
