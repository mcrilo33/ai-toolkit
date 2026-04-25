"""Unit tests for scripts/metadata_parser.py."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from metadata_parser import parse, query  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def meta_file(tmp_path: Path) -> Path:
    """Return path to a temp metadata.yml with representative content."""
    content = textwrap.dedent("""\
        # Per-tool frontmatter for rules.
        guidelines:
          name: "Guidelines"
          description: "Agent behavior"
          applyTo: "**"
          alwaysApply: true

        code-quality:
          name: "Code Quality"
          description: "Design principles"
          applyTo: "**/*.py"
          globs: "**/*.py"
          paths: "**/*.py"
          alwaysApply: false

        python-style:
          name: "Python Standards"
          description: "Type annotations"
          applyTo: "**/*.py"
          globs: "**/*.py"
          paths: "**/*.py"
          alwaysApply: false
          cursor:
            alwaysApply: true
    """)
    p = tmp_path / "metadata.yml"
    p.write_text(content)
    return p


@pytest.fixture()
def meta_with_overrides(tmp_path: Path) -> Path:
    """Metadata with per-tool overrides for multiple tools."""
    content = textwrap.dedent("""\
        my-skill:
          name: "my-skill"
          description: "Shared description"
          cursor:
            description: "Cursor-specific description"
          claude:
            description: "Claude-specific description"
    """)
    p = tmp_path / "overrides.yml"
    p.write_text(content)
    return p


# ── parse() ───────────────────────────────────────────────


class TestParse:
    """Tests for the low-level YAML-like parser."""

    def test_parse_returns_all_items(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        assert set(items.keys()) == {"guidelines", "code-quality", "python-style"}

    def test_parse_defaults_unquoted(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        assert items["code-quality"]["__defaults"]["alwaysApply"] == "false"

    def test_parse_defaults_quoted_strings_stripped(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        assert items["guidelines"]["__defaults"]["name"] == "Guidelines"
        assert items["guidelines"]["__defaults"]["description"] == "Agent behavior"

    def test_parse_overrides_present(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        assert "cursor" in items["python-style"]["__overrides"]
        assert items["python-style"]["__overrides"]["cursor"]["alwaysApply"] == "true"

    def test_parse_no_overrides_when_absent(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        assert items["guidelines"]["__overrides"] == {}

    def test_parse_skips_comments(self, tmp_path: Path) -> None:
        content = textwrap.dedent("""\
            # This is a comment
            item-a:
              name: "A"
            # Another comment
        """)
        p = tmp_path / "comments.yml"
        p.write_text(content)

        items = parse(str(p))

        assert set(items.keys()) == {"item-a"}

    def test_parse_skips_blank_lines(self, tmp_path: Path) -> None:
        content = "item-a:\n  name: A\n\n\nitem-b:\n  name: B\n"
        p = tmp_path / "blanks.yml"
        p.write_text(content)

        items = parse(str(p))

        assert set(items.keys()) == {"item-a", "item-b"}

    def test_parse_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yml"
        p.write_text("")

        items = parse(str(p))

        assert items == {}

    def test_parse_single_quoted_values(self, tmp_path: Path) -> None:
        content = "item:\n  name: 'Single Quoted'\n"
        p = tmp_path / "single.yml"
        p.write_text(content)

        items = parse(str(p))

        assert items["item"]["__defaults"]["name"] == "Single Quoted"

    def test_parse_multiple_tool_overrides(self, meta_with_overrides: Path) -> None:
        items = parse(str(meta_with_overrides))
        skill = items["my-skill"]

        cursor_desc = skill["__overrides"]["cursor"]["description"]
        claude_desc = skill["__overrides"]["claude"]["description"]
        assert cursor_desc == "Cursor-specific description"
        assert claude_desc == "Claude-specific description"


# ── query() ───────────────────────────────────────────────


class TestQuery:
    """Tests for field selection and tool-specific merging."""

    def test_query_copilot_returns_correct_fields(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "copilot", ["name", "description", "applyTo"])

        names = [r[0] for r in results]
        assert "guidelines" in names
        assert "code-quality" in names

    def test_query_frontmatter_format(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "copilot", ["name", "description"])
        result_dict = dict(results)

        assert "name: Guidelines" in result_dict["guidelines"]
        assert "description: Agent behavior" in result_dict["guidelines"]

    def test_query_cursor_override_applied(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "cursor", ["description", "globs", "alwaysApply"])
        result_dict = dict(results)

        # python-style has cursor override: alwaysApply: true
        assert "alwaysApply: true" in result_dict["python-style"]

    def test_query_cursor_default_when_no_override(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "cursor", ["description", "globs", "alwaysApply"])
        result_dict = dict(results)

        # code-quality has no cursor override, so default alwaysApply: false
        assert "alwaysApply: false" in result_dict["code-quality"]

    def test_query_claude_fields(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "claude", ["paths"])
        result_dict = dict(results)

        assert "code-quality" in result_dict
        assert "paths: **/*.py" in result_dict["code-quality"]

    def test_query_skips_items_without_requested_fields(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        # guidelines has no "globs" or "paths" field
        results = query(items, "claude", ["paths"])
        names = [r[0] for r in results]

        assert "guidelines" not in names

    def test_query_unknown_tool_uses_defaults_only(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "unknown-tool", ["name", "description"])
        result_dict = dict(results)

        assert "guidelines" in result_dict
        assert "name: Guidelines" in result_dict["guidelines"]

    def test_query_override_replaces_default(self, meta_with_overrides: Path) -> None:
        items = parse(str(meta_with_overrides))

        cursor_results = dict(query(items, "cursor", ["name", "description"]))
        copilot_results = dict(query(items, "copilot", ["name", "description"]))

        assert "Cursor-specific description" in cursor_results["my-skill"]
        assert "Shared description" in copilot_results["my-skill"]

    def test_query_empty_fields_returns_nothing(self, meta_file: Path) -> None:
        items = parse(str(meta_file))

        results = query(items, "copilot", ["nonexistent_field"])

        assert results == []


# ── Real metadata files ──────────────────────────────────


class TestRealMetadata:
    """Smoke tests against the actual metadata.yml files in the repo."""

    REPO_ROOT = Path(__file__).resolve().parents[2]

    @pytest.mark.parametrize(
        "meta_path,tool,fields",
        [
            pytest.param(
                "shared/rules/metadata.yml",
                "copilot",
                "name,description,applyTo",
                id="rules-copilot",
            ),
            pytest.param(
                "shared/rules/metadata.yml",
                "cursor",
                "description,globs,alwaysApply",
                id="rules-cursor",
            ),
            pytest.param(
                "shared/rules/metadata.yml",
                "claude",
                "paths",
                id="rules-claude",
            ),
            pytest.param(
                "shared/skills/metadata.yml",
                "copilot",
                "name,description",
                id="skills-copilot",
            ),
            pytest.param(
                "shared/skills/metadata.yml",
                "cursor",
                "name,description",
                id="skills-cursor",
            ),
            pytest.param(
                "shared/prompts/metadata.yml",
                "copilot",
                "name,description,agent",
                id="prompts-copilot",
            ),
        ],
    )
    def test_parse_real_metadata_produces_results(
        self, meta_path: str, tool: str, fields: str
    ) -> None:
        full_path = self.REPO_ROOT / meta_path
        if not full_path.exists():
            pytest.skip(f"{meta_path} not found")

        items = parse(str(full_path))
        results = query(items, tool, [f.strip() for f in fields.split(",")])

        assert len(results) > 0, f"No results for {meta_path} / {tool} / {fields}"
        for name, fm in results:
            assert name, "Item name must not be empty"
            assert fm, "Frontmatter must not be empty"
