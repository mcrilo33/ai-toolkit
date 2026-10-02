"""Contract tests for the repo-root orca.yaml (issue #362, Orca migration S3).

Orca reads `orca.yaml` ahead of local hookSettings (03 spike #1/#2): `wait-for-setup` holds
the agent until the setup script exits, and `scripts.setup` / `scripts.archive` are the
provisioning and pre-removal seams. A path that does not exist or is not executable would
make Orca fail every spawn/removal, so the file is pinned here.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((REPO_ROOT / "orca.yaml").read_text())


def test_setup_holds_the_agent_until_provisioning_exits() -> None:
    assert CONFIG["setupAgentStartupPolicy"] == "wait-for-setup"


def test_setup_and_archive_point_at_the_provisioning_scripts() -> None:
    assert CONFIG["scripts"] == {
        "setup": "./scripts/provision-worktree.sh",
        "archive": "./scripts/archive-worktree.sh",
    }


def test_script_paths_exist_and_are_executable() -> None:
    for command in CONFIG["scripts"].values():
        path = REPO_ROOT / command
        assert path.is_file(), command
        assert os.access(path, os.X_OK), command
