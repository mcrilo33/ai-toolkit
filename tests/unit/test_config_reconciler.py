"""Unit tests for scripts/config_reconciler.py.

The config reconciler mirrors hooks_reconciler.py's owned-block model for
line-based config formats. For ``.gitignore`` it maintains a sentinel-marked
"managed" block: the block's interior is the ai-toolkit-owned set (replaced
wholesale on every sync), while every host-authored line outside the markers is
preserved byte-for-byte. These tests pin the convergence (idempotent),
self-healing (de-bloat), and host-preservation guarantees.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from config_reconciler import (
    BEGIN_MARKER,
    END_MARKER,
    reconcile_gitignore,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_RECONCILER_SCRIPT = REPO_ROOT / "scripts" / "config_reconciler.py"

MANAGED = ".env\n.env.*\n!.env.example\nnode_modules/\n"


def _managed_block_body(text: str) -> list[str]:
    """Return the lines strictly between the managed markers."""
    lines = text.split("\n")
    begin = lines.index(BEGIN_MARKER)
    end = lines.index(END_MARKER)
    return lines[begin + 1 : end]


# ── Fresh target (no host file) ───────────────────────────


class TestFreshFile:
    """A missing/empty host .gitignore is created as a single managed block."""

    def test_fresh_file_contains_both_markers(self) -> None:
        result = reconcile_gitignore("", MANAGED)

        assert BEGIN_MARKER in result
        assert END_MARKER in result

    def test_fresh_file_body_matches_managed(self) -> None:
        result = reconcile_gitignore("", MANAGED)

        assert _managed_block_body(result) == MANAGED.strip("\n").split("\n")

    def test_fresh_file_ends_with_newline(self) -> None:
        result = reconcile_gitignore("", MANAGED)

        assert result.endswith("\n")

    def test_fresh_file_has_no_host_lines(self) -> None:
        result = reconcile_gitignore("", MANAGED)

        assert result.index(BEGIN_MARKER.rstrip()) == 0 or result.startswith(BEGIN_MARKER)


# ── Host preservation ─────────────────────────────────────


class TestHostPreservation:
    """Lines outside the managed block are preserved byte-for-byte."""

    def test_host_lines_preserved(self) -> None:
        existing = "# my project\nbuild/\ncoverage.xml\n"

        result = reconcile_gitignore(existing, MANAGED)

        assert "# my project" in result
        assert "build/" in result
        assert "coverage.xml" in result

    def test_host_block_precedes_managed_block(self) -> None:
        existing = "build/\n"

        result = reconcile_gitignore(existing, MANAGED)
        lines = result.split("\n")

        assert lines.index("build/") < lines.index(BEGIN_MARKER)

    def test_host_pattern_not_removed_when_absent_from_managed(self) -> None:
        """Dropping a pattern from managed never touches a host-authored copy."""
        existing = "node_modules/\n"  # host also lists a pattern managed carries
        managed_without = ".env\n"  # managed no longer carries node_modules/

        result = reconcile_gitignore(existing, managed_without)
        host_part = result.split(BEGIN_MARKER)[0]

        assert "node_modules/" in host_part


# ── Idempotence ───────────────────────────────────────────


class TestIdempotence:
    """A second reconcile with the same managed set is a byte-for-byte no-op."""

    def test_fresh_file_is_fixed_point(self) -> None:
        once = reconcile_gitignore("", MANAGED)
        twice = reconcile_gitignore(once, MANAGED)

        assert once == twice

    def test_host_file_is_fixed_point(self) -> None:
        existing = "# header\nbuild/\n\ndist/\n"
        once = reconcile_gitignore(existing, MANAGED)
        twice = reconcile_gitignore(once, MANAGED)

        assert once == twice

    def test_five_runs_stable(self) -> None:
        text = "custom/\n"
        for _ in range(5):
            text = reconcile_gitignore(text, MANAGED)
        assert reconcile_gitignore(text, MANAGED) == text


# ── Field reconcile (add-new / drop-deprecated) ───────────


class TestFieldReconcile:
    """The managed block reflects the current managed set exactly."""

    def test_new_pattern_appears_in_block(self) -> None:
        first = reconcile_gitignore("", ".env\n")
        updated = reconcile_gitignore(first, ".env\n*.tmp\n")

        assert "*.tmp" in _managed_block_body(updated)

    def test_deprecated_pattern_removed_from_block(self) -> None:
        first = reconcile_gitignore("", ".env\n*.tmp\n")
        updated = reconcile_gitignore(first, ".env\n")

        assert "*.tmp" not in _managed_block_body(updated)


# ── Self-healing ──────────────────────────────────────────


class TestSelfHealing:
    """Duplicate/bloated managed blocks collapse to exactly one."""

    def test_duplicate_blocks_collapse_to_one(self) -> None:
        block = f"{BEGIN_MARKER}\n.env\n{END_MARKER}\n"
        bloated = f"build/\n{block}\n{block}\n{block}"

        result = reconcile_gitignore(bloated, MANAGED)

        assert result.count(BEGIN_MARKER) == 1
        assert result.count(END_MARKER) == 1
        assert "build/" in result

    def test_orphan_begin_marker_degrades_to_append(self) -> None:
        """A BEGIN with no matching END is left in place; a fresh block appends."""
        existing = f"build/\n{BEGIN_MARKER}\nhalf-written\n"

        result = reconcile_gitignore(existing, MANAGED)

        # Exactly one well-formed block appended; the reconcile is idempotent.
        assert result.count(END_MARKER) == 1
        assert reconcile_gitignore(result, MANAGED) == result


# ── CLI ───────────────────────────────────────────────────


class TestCLI:
    """The gitignore subcommand reads managed content on stdin."""

    def test_cli_fresh_target_writes_block(self, tmp_path: Path) -> None:
        missing = tmp_path / ".gitignore"

        result = subprocess.run(
            [sys.executable, str(CONFIG_RECONCILER_SCRIPT), "gitignore", str(missing)],
            input=MANAGED,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert BEGIN_MARKER in result.stdout
        assert END_MARKER in result.stdout

    def test_cli_preserves_existing_host_lines(self, tmp_path: Path) -> None:
        existing = tmp_path / ".gitignore"
        existing.write_text("build/\n")

        result = subprocess.run(
            [sys.executable, str(CONFIG_RECONCILER_SCRIPT), "gitignore", str(existing)],
            input=MANAGED,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "build/" in result.stdout
