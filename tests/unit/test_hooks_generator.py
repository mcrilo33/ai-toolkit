"""Unit tests for scripts/hooks_generator.py."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from hooks_generator import (
    _cursor_matcher,
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
def cursor_dedicated_meta(tmp_path: Path) -> Path:
    """Metadata exercising the Cursor dedicated-event overrides.

    Mirrors the real migration: canonical (Claude/Copilot) event/matcher stays
    at the top level; a nested ``cursor:`` block remaps the hook onto a
    dedicated event with a command-regex (beforeShellExecution) or dedicated
    tool token (afterFileEdit) matcher.
    """
    content = textwrap.dedent("""\
        commit-quality:
          event: preToolUse
          matcher: Bash
          description: "Validate commit message"
          tier: 2
          claude:
            if: "Bash(git commit *)"
          cursor:
            event: beforeShellExecution
            matcher: "git commit"

        block-no-verify:
          event: preToolUse
          matcher: Bash
          description: "Block --no-verify"
          tier: 1
          cursor:
            event: beforeShellExecution
            matcher: ""

        quality-gate:
          event: postToolUse
          matcher: "Edit|Write"
          description: "Lint + typecheck"
          tier: 1
          cursor:
            event: afterFileEdit
            matcher: "Write|TabWrite"
    """)
    p = tmp_path / "metadata.yml"
    p.write_text(content)
    return p


@pytest.fixture()
def cursor_dedicated_data(cursor_dedicated_meta: Path) -> dict:
    return parse_hooks_metadata(str(cursor_dedicated_meta))


@pytest.fixture()
def cursor_mcp_meta(tmp_path: Path) -> Path:
    """Metadata exercising the Cursor MCP events (review-stamp feature).

    beforeMCPExecution / afterMCPExecution are dedicated events: the matcher
    is an MCP tool-name filter, never run through the Bash->Shell token
    translation. ``failClosed: "true"`` must surface as a boolean entry field.
    """
    content = textwrap.dedent("""\
        review-stamp-guard:
          event: preToolUse
          matcher: Bash
          description: "Gate approve_review behind a review window"
          tier: 1
          cursor:
            event: beforeMCPExecution
            matcher: "approve_review"
            failClosed: "true"

        mcp-audit:
          event: postToolUse
          matcher: Bash
          description: "Audit MCP tool results"
          tier: 2
          cursor:
            event: afterMCPExecution
            matcher: "Bash|approve_review"
    """)
    p = tmp_path / "metadata.yml"
    p.write_text(content)
    return p


@pytest.fixture()
def cursor_mcp_data(cursor_mcp_meta: Path) -> dict:
    return parse_hooks_metadata(str(cursor_mcp_meta))


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

    def test_pretooluse_matcher_translated_to_shell(self, hooks_data: dict) -> None:
        """Cursor's shell tool is 'Shell', not 'Bash' — a 'Bash' matcher never fires."""
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"]
        assert pre[0]["matcher"] == "Shell"

    def test_posttooluse_matcher_translated_and_deduped(
        self, hooks_data: dict
    ) -> None:
        """'Edit|Write' collapses to 'Write' — Cursor has no 'Edit' tool."""
        result = generate_cursor(hooks_data)
        post = result["hooks"]["postToolUse"]
        assert post[0]["matcher"] == "Write"

    def test_stop_event_name(self, hooks_data: dict) -> None:
        """Cursor uses 'stop' directly."""
        result = generate_cursor(hooks_data)
        assert "stop" in result["hooks"]

    def test_script_path_prefix(self, hooks_data: dict) -> None:
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        # Project hooks must be root-relative WITHOUT a leading "./" — the
        # "./.cursor/..." form trips a false-positive warning in the settings UI.
        assert pre["command"].startswith(".cursor/hooks/scripts/")
        assert not pre["command"].startswith("./")

    def test_no_type_field(self, hooks_data: dict) -> None:
        """Cursor format uses 'command' not 'type' + 'bash'."""
        result = generate_cursor(hooks_data)
        pre = result["hooks"]["preToolUse"][0]
        assert "type" not in pre
        assert "command" in pre


class TestCursorDedicatedEvents:
    """Cursor migration: per-hook `cursor: event:` overrides onto dedicated events."""

    def test_event_override_remaps_to_before_shell_execution(
        self, cursor_dedicated_data: dict
    ) -> None:
        result = generate_cursor(cursor_dedicated_data)
        assert "beforeShellExecution" in result["hooks"]
        # The canonical preToolUse bucket must NOT be emitted for migrated hooks.
        assert "preToolUse" not in result["hooks"]

    def test_event_override_remaps_to_after_file_edit(
        self, cursor_dedicated_data: dict
    ) -> None:
        result = generate_cursor(cursor_dedicated_data)
        assert "afterFileEdit" in result["hooks"]
        assert "postToolUse" not in result["hooks"]

    def test_before_shell_matcher_is_command_regex_not_translated(
        self, cursor_dedicated_data: dict
    ) -> None:
        """On beforeShellExecution the matcher is a command regex — verbatim, never Bash->Shell."""
        result = generate_cursor(cursor_dedicated_data)
        cq = next(
            e
            for e in result["hooks"]["beforeShellExecution"]
            if "commit-quality.sh" in e["command"]
        )
        assert cq["matcher"] == "git commit"

    def test_empty_cursor_matcher_emits_no_matcher(
        self, cursor_dedicated_data: dict
    ) -> None:
        """A blank cursor matcher (block-no-verify) yields an entry with no matcher key."""
        result = generate_cursor(cursor_dedicated_data)
        bnv = next(
            e
            for e in result["hooks"]["beforeShellExecution"]
            if "block-no-verify.sh" in e["command"]
        )
        assert "matcher" not in bnv

    def test_after_file_edit_matcher_passthrough_keeps_tabwrite(
        self, cursor_dedicated_data: dict
    ) -> None:
        """afterFileEdit matcher 'Write|TabWrite' passes through (no token translation)."""
        result = generate_cursor(cursor_dedicated_data)
        qg = next(
            e
            for e in result["hooks"]["afterFileEdit"]
            if "quality-gate.sh" in e["command"]
        )
        assert qg["matcher"] == "Write|TabWrite"

    def test_claude_unaffected_by_cursor_override(
        self, cursor_dedicated_data: dict
    ) -> None:
        """The cursor override must not leak into Claude generation."""
        result = generate_claude(cursor_dedicated_data)
        assert "PreToolUse" in result
        assert "PostToolUse" in result
        # No dedicated Cursor event names appear in the Claude config.
        assert "beforeShellExecution" not in result
        assert "afterFileEdit" not in result

    def test_copilot_unaffected_by_cursor_override(
        self, cursor_dedicated_data: dict
    ) -> None:
        result = generate_copilot(cursor_dedicated_data)
        assert "preToolUse" in result["hooks"]
        assert "postToolUse" in result["hooks"]
        assert "beforeShellExecution" not in result["hooks"]


class TestCursorMCPEvents:
    """Cursor MCP events for the review-stamp guard (beforeMCPExecution/afterMCPExecution).

    NOTE: generate_cursor falls back to ``CURSOR_EVENT_MAP.get(event, event)``,
    so an unknown event ACCIDENTALLY passes through into a bucket. Deliberate
    support means map membership (and dedicated-event membership, which drives
    the matcher passthrough) — assert both, not just the bucket side effect.
    """

    def test_before_mcp_execution_is_supported_event(self, cursor_mcp_data: dict) -> None:
        import hooks_generator

        result = generate_cursor(cursor_mcp_data)

        assert "beforeMCPExecution" in hooks_generator.CURSOR_EVENT_MAP
        assert "beforeMCPExecution" in hooks_generator.CURSOR_DEDICATED_EVENTS
        assert "beforeMCPExecution" in result["hooks"]
        assert "preToolUse" not in result["hooks"]

    def test_after_mcp_execution_is_supported_event(self, cursor_mcp_data: dict) -> None:
        import hooks_generator

        result = generate_cursor(cursor_mcp_data)

        assert "afterMCPExecution" in hooks_generator.CURSOR_EVENT_MAP
        assert "afterMCPExecution" in hooks_generator.CURSOR_DEDICATED_EVENTS
        assert "afterMCPExecution" in result["hooks"]
        assert "postToolUse" not in result["hooks"]

    def test_fail_closed_emitted_only_when_metadata_sets_it(
        self, cursor_mcp_data: dict
    ) -> None:
        """failClosed: "true" in metadata -> boolean true in the entry; absent otherwise."""
        result = generate_cursor(cursor_mcp_data)

        guard = next(
            e
            for e in result["hooks"]["beforeMCPExecution"]
            if "review-stamp-guard.sh" in e["command"]
        )
        audit = next(
            e
            for e in result["hooks"]["afterMCPExecution"]
            if "mcp-audit.sh" in e["command"]
        )
        assert guard["failClosed"] is True
        assert "failClosed" not in audit

    def test_before_mcp_matcher_passes_through_untranslated(
        self, cursor_mcp_data: dict
    ) -> None:
        """MCP-event matchers are tool-name filters — never Bash->Shell translated."""
        result = generate_cursor(cursor_mcp_data)

        guard = next(
            e
            for e in result["hooks"]["beforeMCPExecution"]
            if "review-stamp-guard.sh" in e["command"]
        )
        audit = next(
            e
            for e in result["hooks"]["afterMCPExecution"]
            if "mcp-audit.sh" in e["command"]
        )
        assert guard["matcher"] == "approve_review"
        assert audit["matcher"] == "Bash|approve_review"


class TestCursorMatcherTranslation:
    """Pin the metadata→Cursor tool-name translation for matchers."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Bash", "Shell"),
            ("Edit", "Write"),
            ("Write", "Write"),
            ("Read", "Read"),
            ("Write|Edit", "Write"),
            ("Edit|Write", "Write"),
            ("Bash|Shell", "Shell"),
            ("Read|Write", "Read|Write"),
        ],
    )
    def test_translation(self, raw: str, expected: str) -> None:
        assert _cursor_matcher(raw) == expected

    def test_unknown_token_passes_through(self) -> None:
        assert _cursor_matcher("CustomTool") == "CustomTool"

    def test_order_preserved_after_dedup(self) -> None:
        assert _cursor_matcher("Write|Read|Edit") == "Write|Read"


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
