"""Unit tests for scripts/sync_manifest.py.

RED phase: sync_manifest.py does not exist yet. These tests encode the
contract for the sync manifest + stale-file GC + dry-run feature.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from sync_manifest import finalize

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_MANIFEST_SCRIPT = REPO_ROOT / "scripts" / "sync_manifest.py"
MANIFEST_NAME = ".ai-toolkit-manifest.json"


# ── Helpers ───────────────────────────────────────────────


def _write_manifest(target: Path, data: dict) -> None:
    (target / MANIFEST_NAME).write_text(json.dumps(data))


def _read_manifest(target: Path) -> dict:
    return json.loads((target / MANIFEST_NAME).read_text())


def _plant(target: Path, relpath: str) -> Path:
    """Create a file at target/relpath with parent dirs."""
    p = target / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("content\n")
    return p


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def target(tmp_path: Path) -> Path:
    """Target repo root as a subdirectory (so '..' escapes stay in tmp)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# ── First run / manifest shape ────────────────────────────


class TestFinalizeFirstRun:
    """Behavior with no pre-existing (or unusable) manifest."""

    def test_finalize_writes_manifest_with_sorted_files(self, target: Path) -> None:
        files = [".cursor/rules/b.mdc", ".cursor/rules/a.mdc"]

        finalize(str(target), "cursor", files, toolkit_rev="abc123")

        manifest = _read_manifest(target)
        assert manifest["tools"]["cursor"] == [
            ".cursor/rules/a.mdc",
            ".cursor/rules/b.mdc",
        ]

    def test_finalize_records_toolkit_rev(self, target: Path) -> None:
        finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="abc123")

        manifest = _read_manifest(target)
        assert manifest["toolkit_rev"] == "abc123"

    def test_finalize_manifest_has_no_timestamps(self, target: Path) -> None:
        """Manifest contains only toolkit_rev and tools (idempotency)."""
        finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="abc123")

        manifest = _read_manifest(target)
        assert set(manifest.keys()) == {"toolkit_rev", "tools"}

    def test_finalize_returns_no_deletions_on_first_run(self, target: Path) -> None:
        deleted = finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="abc123")

        assert deleted == []

    def test_finalize_tolerates_corrupt_manifest(self, target: Path) -> None:
        """A corrupt old manifest is treated as a first run: no deletions."""
        (target / MANIFEST_NAME).write_text("{not valid json!!!")
        _plant(target, ".cursor/rules/existing.mdc")

        deleted = finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="abc123")

        assert deleted == []
        assert (target / ".cursor/rules/existing.mdc").exists()
        assert _read_manifest(target)["tools"]["cursor"] == [".cursor/rules/a.mdc"]


# ── Per-tool scoping ──────────────────────────────────────


class TestFinalizePerToolScoping:
    """finalize for tool X only touches the X list."""

    def test_finalize_preserves_other_tool_lists(self, target: Path) -> None:
        _write_manifest(
            target,
            {
                "toolkit_rev": "old",
                "tools": {"copilot": [".github/instructions/x.instructions.md"]},
            },
        )

        finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="new")

        manifest = _read_manifest(target)
        assert manifest["tools"]["copilot"] == [".github/instructions/x.instructions.md"]

    def test_finalize_replaces_target_tool_list(self, target: Path) -> None:
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/old.mdc"]}},
        )

        finalize(str(target), "cursor", [".cursor/rules/new.mdc"], toolkit_rev="new")

        manifest = _read_manifest(target)
        assert manifest["tools"]["cursor"] == [".cursor/rules/new.mdc"]


# ── Stale-file GC ─────────────────────────────────────────


class TestFinalizeGC:
    """Paths in the old manifest but not in the new files list are GC'd."""

    def test_finalize_deletes_stale_file_from_disk(self, target: Path) -> None:
        _plant(target, ".cursor/rules/keep.mdc")
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {
                "toolkit_rev": "old",
                "tools": {"cursor": [".cursor/rules/keep.mdc", ".cursor/rules/stale.mdc"]},
            },
        )

        finalize(str(target), "cursor", [".cursor/rules/keep.mdc"], toolkit_rev="new")

        assert not (target / ".cursor/rules/stale.mdc").exists()
        assert (target / ".cursor/rules/keep.mdc").exists()

    def test_finalize_returns_deleted_paths(self, target: Path) -> None:
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {
                "toolkit_rev": "old",
                "tools": {"cursor": [".cursor/rules/keep.mdc", ".cursor/rules/stale.mdc"]},
            },
        )

        deleted = finalize(str(target), "cursor", [".cursor/rules/keep.mdc"], toolkit_rev="new")

        assert deleted == [".cursor/rules/stale.mdc"]

    def test_finalize_drops_stale_path_from_manifest(self, target: Path) -> None:
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {
                "toolkit_rev": "old",
                "tools": {"cursor": [".cursor/rules/keep.mdc", ".cursor/rules/stale.mdc"]},
            },
        )

        finalize(str(target), "cursor", [".cursor/rules/keep.mdc"], toolkit_rev="new")

        manifest = _read_manifest(target)
        assert ".cursor/rules/stale.mdc" not in manifest["tools"]["cursor"]

    def test_finalize_tolerates_stale_path_missing_from_disk(self, target: Path) -> None:
        """A stale manifest entry with no file on disk is dropped without error."""
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/ghost.mdc"]}},
        )

        finalize(str(target), "cursor", [".cursor/rules/keep.mdc"], toolkit_rev="new")

        manifest = _read_manifest(target)
        assert ".cursor/rules/ghost.mdc" not in manifest["tools"]["cursor"]

    def test_finalize_never_deletes_path_not_in_old_manifest(self, target: Path) -> None:
        """User files unknown to the manifest are never touched."""
        _plant(target, ".cursor/rules/my-own-rule.mdc")
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/keep.mdc"]}},
        )

        deleted = finalize(str(target), "cursor", [".cursor/rules/keep.mdc"], toolkit_rev="new")

        assert (target / ".cursor/rules/my-own-rule.mdc").exists()
        assert ".cursor/rules/my-own-rule.mdc" not in deleted


# ── Path safety ───────────────────────────────────────────


class TestFinalizeSafety:
    """Absolute paths and traversal sequences are refused."""

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",
            "../escape.md",
            ".cursor/../../escape.md",
        ],
    )
    def test_finalize_raises_on_unsafe_input_path(self, target: Path, bad_path: str) -> None:
        with pytest.raises(ValueError):
            finalize(str(target), "cursor", [bad_path], toolkit_rev="abc123")

    def test_finalize_raises_on_traversal_path_in_old_manifest(
        self, target: Path, tmp_path: Path
    ) -> None:
        """A malicious old manifest cannot cause deletion outside the target."""
        outside = tmp_path / "escape.txt"
        outside.write_text("precious\n")
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": ["../escape.txt"]}},
        )

        with pytest.raises(ValueError):
            finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="new")

        assert outside.exists()

    def test_finalize_skips_stale_path_under_symlinked_dir_escaping_target(
        self, target: Path, tmp_path: Path
    ) -> None:
        """A manifest entry under a symlink pointing outside the target is not deleted."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "evil.txt"
        outside_file.write_text("precious\n")
        (target / "link").symlink_to(outside_dir)
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": ["link/evil.txt"]}},
        )

        deleted = finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="new")

        assert outside_file.exists()
        assert "link/evil.txt" not in deleted


# ── Exclusions ────────────────────────────────────────────


class TestFinalizeExclusions:
    """Hook-reconciler-owned files and backups are never deleted."""

    @pytest.mark.parametrize(
        "protected",
        [
            ".cursor/hooks.json",
            ".claude/settings.json",
            ".cursor/hooks.json.bak",
        ],
    )
    def test_finalize_never_deletes_protected_path(self, target: Path, protected: str) -> None:
        _plant(target, protected)
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [protected]}},
        )

        deleted = finalize(str(target), "cursor", [".cursor/rules/a.mdc"], toolkit_rev="new")

        assert (target / protected).exists()
        assert protected not in deleted


# ── Host config protection (issue #333) ───────────────────


class TestFinalizeConfigProtection:
    """Existing host config files are never GC-deleted, made explicit.

    Copy-if-absent config files (pyproject.toml, ruff.toml, .gitignore,
    .editorconfig, .python-version) are host-owned. Even if a stale manifest
    lists one (e.g. a future toolkit revision recorded it, then a re-sync at a
    different revision no longer does), GC must never remove it — the guarantee
    is explicit in the protection set, not a side-effect of keeping the path out
    of the manifest.
    """

    @pytest.mark.parametrize(
        "config_path",
        [
            "pyproject.toml",
            "ruff.toml",
            ".gitignore",
            ".editorconfig",
            ".python-version",
        ],
    )
    def test_finalize_never_deletes_existing_host_config(
        self, target: Path, config_path: str
    ) -> None:
        _plant(target, config_path)
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"claude": [config_path]}},
        )

        deleted = finalize(str(target), "claude", [".claude/settings.json"], toolkit_rev="new")

        assert (target / config_path).exists()
        assert config_path not in deleted


# ── Dry run ───────────────────────────────────────────────


class TestFinalizeDryRun:
    """dry_run=True reports would-delete paths but touches nothing."""

    def test_finalize_dry_run_returns_would_delete_list(self, target: Path) -> None:
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/stale.mdc"]}},
        )

        deleted = finalize(
            str(target),
            "cursor",
            [".cursor/rules/keep.mdc"],
            toolkit_rev="new",
            dry_run=True,
        )

        assert deleted == [".cursor/rules/stale.mdc"]

    def test_finalize_dry_run_leaves_stale_file_on_disk(self, target: Path) -> None:
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/stale.mdc"]}},
        )

        finalize(
            str(target),
            "cursor",
            [".cursor/rules/keep.mdc"],
            toolkit_rev="new",
            dry_run=True,
        )

        assert (target / ".cursor/rules/stale.mdc").exists()

    def test_finalize_dry_run_does_not_modify_manifest(self, target: Path) -> None:
        _write_manifest(
            target,
            {"toolkit_rev": "old", "tools": {"cursor": [".cursor/rules/stale.mdc"]}},
        )
        before = (target / MANIFEST_NAME).read_bytes()

        finalize(
            str(target),
            "cursor",
            [".cursor/rules/keep.mdc"],
            toolkit_rev="new",
            dry_run=True,
        )

        assert (target / MANIFEST_NAME).read_bytes() == before

    def test_finalize_dry_run_writes_no_manifest_on_fresh_target(self, target: Path) -> None:
        finalize(
            str(target),
            "cursor",
            [".cursor/rules/a.mdc"],
            toolkit_rev="abc123",
            dry_run=True,
        )

        assert not (target / MANIFEST_NAME).exists()


# ── CLI ───────────────────────────────────────────────────


class TestCLI:
    """The finalize subcommand reads relpaths on stdin and prints deletions."""

    def test_cli_finalize_deletes_stale_and_prints_paths(self, target: Path) -> None:
        _plant(target, ".cursor/rules/stale.mdc")
        _write_manifest(
            target,
            {
                "toolkit_rev": "old",
                "tools": {"cursor": [".cursor/rules/keep.mdc", ".cursor/rules/stale.mdc"]},
            },
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SYNC_MANIFEST_SCRIPT),
                "finalize",
                str(target),
                "cursor",
                "rev123",
            ],
            input=".cursor/rules/keep.mdc\n",
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert ".cursor/rules/stale.mdc" in result.stdout.splitlines()
        assert not (target / ".cursor/rules/stale.mdc").exists()
