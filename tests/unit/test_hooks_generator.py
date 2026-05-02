"""Unit tests for scripts/hooks_generator.py."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from hooks_generator import (  # noqa: E402
    generate_claude,
    generate_copilot,
    generate_cursor,
    parse_hooks_metadata,
)


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def hooks_meta(tmp_path: Path) -> Path:
    """Return path to a temp hooks metadata.yml."""
    content = textwrap.dedent("""\
        block-no-verify:
          event: preToolUse
          matcher: Bash
          description: "Block --no-verify"
          tier: 1
          claude:
            if: "Bash(git * --no-verify *)"

        post-edit-format:
          event: postToolUse
          matcher: "Edit|Write"
          description: "Auto-format edited files"
          tier: 1
          copilot:
            matcher: "edit|create"

        desktop-notify:
          event: stop
          description: "macOS notification"
          tier: 1
          copilot:
            event: agentStop
    """)
    p = tmp_path / "metadata.yml"
    p.write_text(content)
    return p


@pytest.fixture()
def hooks_data(hooks_meta: Path) -> dict:
    """Parsed hooks metadata."""
    return parse_hooks_metadata(str(hooks_meta))


# ── parse_hooks_metadata() ────────────────────────────────


class TestParseHooksMetadata:
    """Tests for the hooks metadata parser."""

    def test_returns_all_hooks(self, hooks_data: dict) -> None:
        assert set(hooks_data.keys()) == {
            "block-no-verify",
            "post-edit-format",
            "desktop-notify",
        }

    def test_defaults_parsed(self, hooks_data: dict) -> None:
        d = hooks_data["block-no-verify"]["__defaults"]
        assert d["event"] == "preToolUse"
        assert d["matcher"] == "Bash"
        assert d["tier"] == "1"

    def test_overrides_parsed(self, hooks_data: dict) -> None:
        claude = hooks_data["block-no-verify"]["__overrides"].get("claude", {})
        assert claude["if"] == "Bash(git * --no-verify *)"

    def test_copilot_override(self, hooks_data: dict) -> None:
        copilot = hooks_data["post-edit-format"]["__overrides"].get("copilot", {})
        assert copilot["matcher"] == "edit|create"


# ── generate_copilot() ───────────────────────────────────


class TestGenerateCopilot:
    """Tests for Copilot hook config generation."""

    def test_version_1(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        assert result["version"] == 1

    def test_has_hooks_key(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        assert "hooks" in result

    def test_pretooluse_hooks(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        pre = result["hooks"].get("preToolUse", [])
        assert len(pre) == 1
        assert pre[0]["type"] == "command"
        assert "block-no-verify.sh" in pre[0]["bash"]

    def test_posttooluse_hooks(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        post = result["hooks"].get("postToolUse", [])
        assert len(post) == 1
        assert "post-edit-format.sh" in post[0]["bash"]

    def test_stop_mapped_to_agentstop(self, hooks_data: dict) -> None:
        """Copilot maps canonical 'stop' to 'agentStop'."""
        result = generate_copilot(hooks_data)
        assert "agentStop" in result["hooks"]
        assert "stop" not in result["hooks"]

    def test_timeout_default(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        assert pre["timeoutSec"] == 30

    def test_script_path_prefix(self, hooks_data: dict) -> None:
        result = generate_copilot(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        assert pre["bash"].startswith("./.github/hooks/scripts/")


# ── generate_cursor() ────────────────────────────────────


class TestGenerateCursor:
    """Tests for Cursor hook config generation."""

    def test_version_1(self, hooks_data: dict) -> None:
        result = generate_cursor(hooks_data)
        assert result["version"] == 1

    def test_pretooluse_has_matcher(self, hooks_data: dict) -> None:
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"]
        assert pre[0]["matcher"] == "Bash"

    def test_posttooluse_has_matcher(self, hooks_data: dict) -> None:
        result = generate_cursor(hooks_data)
        post = result["hooks"]["postToolUse"]
        assert post[0]["matcher"] == "Edit|Write"

    def test_stop_event_name(self, hooks_data: dict) -> None:
        """Cursor uses 'stop' directly."""
        result = generate_cursor(hooks_data)
        assert "stop" in result["hooks"]

    def test_script_path_prefix(self, hooks_data: dict) -> None:
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        assert pre["command"].startswith("./.cursor/hooks/scripts/")

    def test_no_type_field(self, hooks_data: dict) -> None:
        """Cursor format uses 'command' not 'type' + 'bash'."""
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        assert "type" not in pre
        assert "command" in pre


# ── generate_claude() ────────────────────────────────────


class TestGenerateClaude:
    """Tests for Claude hook config generation."""

    def test_event_names_pascalcase(self, hooks_data: dict) -> None:
        result = generate_claude(hooks_data)
        assert "PreToolUse" in result
        assert "PostToolUse" in result
        assert "Stop" in result

    def test_pretooluse_grouped_by_matcher(self, hooks_data: dict) -> None:
        result = generate_claude(hooks_data)
        pre = result["PreToolUse"]
        matchers = [g.get("matcher") for g in pre]
        assert "Bash" in matchers

    def test_if_condition_present(self, hooks_data: dict) -> None:
        result = generate_claude(hooks_data)
        pre = result["PreToolUse"]
        bash_group = next(g for g in pre if g.get("matcher") == "Bash")
        hook = bash_group["hooks"][0]
        assert hook["if"] == "Bash(git * --no-verify *)"

    def test_nested_hooks_array(self, hooks_data: dict) -> None:
        """Claude format nests hooks inside matcher groups."""
        result = generate_claude(hooks_data)
        for event_groups in result.values():
            for group in event_groups:
                assert "hooks" in group
                for handler in group["hooks"]:
                    assert handler["type"] == "command"

    def test_script_path_uses_claude_project_dir(self, hooks_data: dict) -> None:
        result = generate_claude(hooks_data)
        pre = result["PreToolUse"]
        bash_group = next(g for g in pre if g.get("matcher") == "Bash")
        cmd = bash_group["hooks"][0]["command"]
        assert "$CLAUDE_PROJECT_DIR" in cmd

    def test_stop_no_matcher(self, hooks_data: dict) -> None:
        """Stop hooks have no matcher (fires on every stop)."""
        result = generate_claude(hooks_data)
        stop = result["Stop"]
        assert len(stop) == 1
        assert stop[0].get("matcher") is None
