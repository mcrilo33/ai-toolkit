"""Integration tests for syncing the hub/spoke/land workflow scripts.

`sync-to-repo.sh` must install the parallel-worktrees workflow into any target so
the hub/spoke/land flow works there with no manual setup. The four worktree
scripts and hub-status.sh land in the target's ``.ai-toolkit/scripts/`` — the
canonical location the hub/start-task/land skills reference in both the
ai-toolkit checkout and a synced target (consistent with ``.ai-toolkit/mcp/``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"

# Workflow scripts and their source locations in the toolkit checkout.
# spoke-push.sh ships alongside the worktree scripts so the spoke's PUSH step
# runs as one allowlistable process (issue #37); spoke-ready.sh ships too so
# marker emission (ready/N, gate/N) is one allowlistable command (issue #45).
WORKTREE_SCRIPTS = (
    "worktree-new.sh",
    "worktree-land.sh",
    "worktree-done.sh",
    "worktree-lib.sh",
    "spoke-push.sh",
    "spoke-ready.sh",
)
SOURCES = {name: REPO_ROOT / "scripts" / name for name in WORKTREE_SCRIPTS}
HUB_SCRIPTS_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"
SOURCES["hub-status.sh"] = HUB_SCRIPTS_DIR / "hub-status.sh"
SOURCES["hub-ready-watch.sh"] = HUB_SCRIPTS_DIR / "hub-ready-watch.sh"
SOURCES["hub-night.sh"] = HUB_SCRIPTS_DIR / "hub-night.sh"
# Co-installed so the worktree scripts can source it as a sibling for lifecycle
# telemetry (it also lives under .claude/hooks/lib/ for the hooks).
SOURCES["telemetry.sh"] = REPO_ROOT / "shared" / "hooks" / "lib" / "telemetry.sh"

INSTALLED = {name: f".ai-toolkit/scripts/{name}" for name in SOURCES}


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo to sync into."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _run_sync(target: Path, tool: str = "all") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(target), tool],
        capture_output=True,
        text=True,
        check=True,
    )


class TestWorkflowScriptSync:
    """Sync installs the worktree scripts + hub-status.sh into .ai-toolkit/scripts/."""

    MANIFEST_NAME = ".ai-toolkit-manifest.json"

    def test_all_scripts_installed(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for rel in INSTALLED.values():
            assert (target_repo / rel).is_file(), f"{rel} not installed"

    def test_installed_scripts_are_executable(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for rel in INSTALLED.values():
            assert os.access(target_repo / rel, os.X_OK), f"{rel} not executable"

    def test_installed_files_match_source(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for name, rel in INSTALLED.items():
            assert (target_repo / rel).read_bytes() == SOURCES[name].read_bytes(), (
                f"{rel} differs from source"
            )

    def test_scripts_recorded_in_manifest_for_every_tool(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        for tool in ("copilot", "cursor", "claude"):
            for rel in INSTALLED.values():
                assert rel in manifest["tools"][tool], f"{tool}: {rel} missing from manifest"

    def test_resync_is_byte_identical(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = {rel: (target_repo / rel).read_bytes() for rel in INSTALLED.values()}

        _run_sync(target_repo, "all")

        second = {rel: (target_repo / rel).read_bytes() for rel in INSTALLED.values()}
        assert first == second

    def test_dry_run_does_not_install(self, target_repo: Path) -> None:
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "claude", "--dry-run"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert not (target_repo / ".ai-toolkit" / "scripts").exists()
        for rel in INSTALLED.values():
            assert f"[dry-run] would write {rel}" in result.stdout
