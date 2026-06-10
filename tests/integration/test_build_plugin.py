"""Integration tests for scripts/build-cursor-plugin.sh.

Builds the Cursor plugin into a temporary directory and verifies the
manifest, component layout, hooks wiring, and idempotency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build-cursor-plugin.sh"
HOOKS_GENERATOR = REPO_ROOT / "scripts" / "hooks_generator.py"
SHARED_DIR = REPO_ROOT / "shared"
VERSION_FILE = REPO_ROOT / "VERSION"

# Rules with a metadata entry and an existing source .md
ALL_RULE_NAMES = {
    name
    for name in yaml.safe_load(
        (SHARED_DIR / "rules" / "metadata.yml").read_text()
    )
    if (SHARED_DIR / "rules" / f"{name}.md").exists()
}

# All skill directories in shared/skills/ that contain SKILL.md
ALL_SKILL_NAMES = {
    d.name
    for d in (SHARED_DIR / "skills").iterdir()
    if d.is_dir() and (d / "SKILL.md").exists()
}

# All agent .md files in shared/agents/ (excluding metadata.yml)
ALL_AGENT_NAMES = {
    f.stem
    for f in (SHARED_DIR / "agents").iterdir()
    if f.is_file() and f.suffix == ".md" and f.name != "metadata.yml"
}


def _run_build(output_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run build-cursor-plugin.sh into the given output dir."""
    return subprocess.run(
        ["bash", str(BUILD_SCRIPT), str(output_dir)],
        capture_output=True,
        text=True,
        check=True,
    )


def _tree_digest(root: Path) -> list[tuple[str, str]]:
    """Return sorted (relpath, sha256) pairs for every file under root."""
    digest: list[tuple[str, str]] = []
    for f in sorted(root.rglob("*")):
        if f.is_file():
            digest.append(
                (str(f.relative_to(root)), hashlib.sha256(f.read_bytes()).hexdigest())
            )
    return digest


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def plugin_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the plugin once into a temp dir shared by read-only tests."""
    out = tmp_path_factory.mktemp("plugin") / "cursor-plugin"
    _run_build(out)
    return out


# ── Manifest ──────────────────────────────────────────────


class TestPluginManifest:
    """Verify .cursor-plugin/plugin.json content."""

    def test_manifest_is_valid_json(self, plugin_dir: Path) -> None:
        manifest_path = plugin_dir / ".cursor-plugin" / "plugin.json"

        manifest = json.loads(manifest_path.read_text())

        assert manifest["name"] == "ai-toolkit"

    def test_version_matches_version_file_and_is_semver(
        self, plugin_dir: Path
    ) -> None:
        manifest = json.loads(
            (plugin_dir / ".cursor-plugin" / "plugin.json").read_text()
        )

        version = manifest["version"]

        assert version == VERSION_FILE.read_text().strip()
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)


# ── Components ────────────────────────────────────────────


class TestPluginComponents:
    """Verify rules, skills, and agents are emitted into the plugin."""

    def test_every_metadata_rule_emitted_as_mdc(self, plugin_dir: Path) -> None:
        generated = {f.stem for f in (plugin_dir / "rules").glob("*.mdc")}

        assert generated == ALL_RULE_NAMES

    def test_every_skill_emitted_with_skill_md(self, plugin_dir: Path) -> None:
        generated = {
            d.name
            for d in (plugin_dir / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }

        assert generated == ALL_SKILL_NAMES

    def test_every_agent_emitted(self, plugin_dir: Path) -> None:
        generated = {f.stem for f in (plugin_dir / "agents").glob("*.md")}

        assert generated == ALL_AGENT_NAMES

    def test_sample_rule_frontmatter_has_cursor_fields(
        self, plugin_dir: Path
    ) -> None:
        # guidelines has no globs field, whose ** value is not yaml.safe_load-able
        content = (plugin_dir / "rules" / "guidelines.mdc").read_text()

        parts = content.split("---", 2)
        fm = yaml.safe_load(parts[1])

        assert fm["description"]
        assert fm["alwaysApply"] is True


# ── Frontmatter round-trip guard ──────────────────────────


class TestFrontmatterRoundTrip:
    """EVERY emitted frontmatter block must be valid YAML.

    Guards the solo-cycle regression: a description containing ``: `` was
    emitted unquoted and broke yaml.safe_load — but only one sample file was
    checked, so it slipped through. This iterates every file with frontmatter.
    """

    def _files_with_frontmatter(self, plugin_dir: Path) -> list[Path]:
        files = sorted((plugin_dir / "rules").glob("*.mdc"))
        files += sorted((plugin_dir / "skills").glob("*/SKILL.md"))
        files += sorted((plugin_dir / "agents").glob("*.md"))
        assert files, "no emitted component files found in the plugin"
        return files

    def test_every_emitted_frontmatter_parses_as_yaml_dict(
        self, plugin_dir: Path
    ) -> None:
        for f in self._files_with_frontmatter(plugin_dir):
            content = f.read_text()
            if not content.startswith("---"):
                continue  # plain-copied file without injected frontmatter
            parts = content.split("---", 2)
            assert len(parts) >= 3, f"{f}: malformed frontmatter block"

            try:
                fm = yaml.safe_load(parts[1])
            except yaml.YAMLError as e:
                pytest.fail(f"{f.relative_to(plugin_dir)}: invalid YAML frontmatter: {e}")
            assert isinstance(fm, dict), (
                f"{f.relative_to(plugin_dir)}: frontmatter is not a mapping"
            )


# ── Hooks ─────────────────────────────────────────────────


class TestPluginHooks:
    """Verify hooks.json wiring and bundled scripts."""

    def test_hooks_json_commands_are_plugin_relative_and_exist(
        self, plugin_dir: Path
    ) -> None:
        hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())

        commands = [
            entry["command"]
            for entries in hooks["hooks"].values()
            for entry in entries
        ]

        assert commands, "hooks.json has no hook entries"
        for cmd in commands:
            assert cmd.startswith("./scripts/"), f"non-plugin-relative: {cmd}"
            script = plugin_dir / cmd
            assert script.is_file(), f"missing script: {cmd}"
            assert os.access(script, os.X_OK), f"not executable: {cmd}"

    def test_lib_utils_bundled(self, plugin_dir: Path) -> None:
        assert (plugin_dir / "scripts" / "lib" / "utils.sh").is_file()


# ── Idempotency ───────────────────────────────────────────


class TestPluginIdempotency:
    """Two builds into the same output must be byte-identical."""

    def test_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        out = tmp_path / "cursor-plugin"
        _run_build(out)
        first = _tree_digest(out)

        _run_build(out)
        second = _tree_digest(out)

        assert first == second


# ── hooks_generator backward compatibility ────────────────


class TestHooksGeneratorBackwardCompat:
    """The 3-arg CLI form must keep emitting .cursor/hooks/scripts/ paths."""

    def test_three_arg_invocation_unchanged(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(HOOKS_GENERATOR),
                str(SHARED_DIR / "hooks"),
                str(REPO_ROOT),
                "cursor",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        assert ".cursor/hooks/scripts/" in result.stdout
        assert "./scripts/" not in result.stdout
