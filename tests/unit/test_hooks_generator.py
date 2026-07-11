"""Unit tests for scripts/hooks_generator.py."""

from __future__ import annotations

import json
import os
import re
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

    def test_posttooluse_matcher_translated_and_deduped(self, hooks_data: dict) -> None:
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

    def test_event_override_remaps_to_after_file_edit(self, cursor_dedicated_data: dict) -> None:
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

    def test_empty_cursor_matcher_emits_no_matcher(self, cursor_dedicated_data: dict) -> None:
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
        qg = next(e for e in result["hooks"]["afterFileEdit"] if "quality-gate.sh" in e["command"])
        assert qg["matcher"] == "Write|TabWrite"

    def test_claude_unaffected_by_cursor_override(self, cursor_dedicated_data: dict) -> None:
        """The cursor override must not leak into Claude generation."""
        result = generate_claude(cursor_dedicated_data)
        assert "PreToolUse" in result
        assert "PostToolUse" in result
        # No dedicated Cursor event names appear in the Claude config.
        assert "beforeShellExecution" not in result
        assert "afterFileEdit" not in result

    def test_copilot_unaffected_by_cursor_override(self, cursor_dedicated_data: dict) -> None:
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

    def test_fail_closed_emitted_only_when_metadata_sets_it(self, cursor_mcp_data: dict) -> None:
        """failClosed: "true" in metadata -> boolean true in the entry; absent otherwise."""
        result = generate_cursor(cursor_mcp_data)

        guard = next(
            e
            for e in result["hooks"]["beforeMCPExecution"]
            if "review-stamp-guard.sh" in e["command"]
        )
        audit = next(
            e for e in result["hooks"]["afterMCPExecution"] if "mcp-audit.sh" in e["command"]
        )
        assert guard["failClosed"] is True
        assert "failClosed" not in audit

    def test_before_mcp_matcher_passes_through_untranslated(self, cursor_mcp_data: dict) -> None:
        """MCP-event matchers are tool-name filters — never Bash->Shell translated."""
        result = generate_cursor(cursor_mcp_data)

        guard = next(
            e
            for e in result["hooks"]["beforeMCPExecution"]
            if "review-stamp-guard.sh" in e["command"]
        )
        audit = next(
            e for e in result["hooks"]["afterMCPExecution"] if "mcp-audit.sh" in e["command"]
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


# ── todo-ledger registration against the REAL shared/hooks/metadata.yml ──
# todo-ledger-warn must be wired exactly like reviewer-sep-warn (tier 2, push/PR
# shipping gate on both platforms); todo-ledger-nudge is a SessionStart advisory.

REAL_META = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "metadata.yml"


def _claude_handler(cfg: dict, event: str, script: str) -> dict | None:
    for group in cfg.get(event, []):
        for handler in group.get("hooks", []):
            if handler.get("command", "").endswith(script):
                return handler
    return None


def _cursor_entry(cfg: dict, event: str, script: str) -> dict | None:
    for entry in cfg.get("hooks", {}).get(event, []):
        if entry.get("command", "").endswith(script):
            return entry
    return None


class TestTodoLedgerRegistration:
    def test_warn_claude_wiring_matches_reviewer_sep(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        ledger = _claude_handler(cfg, "PreToolUse", "todo-ledger-warn.sh")
        reviewer = _claude_handler(cfg, "PreToolUse", "reviewer-sep-warn.sh")
        assert ledger is not None, "todo-ledger-warn not registered for Claude PreToolUse"
        assert reviewer is not None, "reviewer-sep-warn baseline missing"
        assert ledger.get("if") == reviewer.get("if") == "Bash(git push *)"

    def test_warn_is_tier_2(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["todo-ledger-warn"]["__defaults"]["tier"] == "2"

    def test_warn_cursor_matcher_matches_reviewer_sep(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        ledger = _cursor_entry(cfg, "beforeShellExecution", "todo-ledger-warn.sh")
        reviewer = _cursor_entry(cfg, "beforeShellExecution", "reviewer-sep-warn.sh")
        assert ledger is not None, "todo-ledger-warn not wired to Cursor beforeShellExecution"
        assert reviewer is not None, "reviewer-sep-warn baseline missing"
        assert ledger["matcher"] == reviewer["matcher"]

    def test_nudge_registered_as_session_start(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        assert _claude_handler(cfg, "SessionStart", "todo-ledger-nudge.sh") is not None


# ── rm-scope-guard registration against the REAL shared/hooks/metadata.yml ──
# rm-scope-guard is a PreToolUse Bash guard like hub-guard: Claude gates it with
# the same `Bash(rm *)` rule the user's ask backstop uses, and Cursor remaps it
# onto beforeShellExecution with a command-regex matcher that fires on rm.


# ── ledger-schema-guard registration against the REAL shared/hooks/metadata.yml ──
# ledger-schema-guard is a Claude-only PreToolUse guard on the TaskCreate/TaskUpdate
# ledger tools (tier 1, deny-with-format). Like cycle-step-mark / afk-notify-wake the
# event lives in the claude block only, so Cursor/Copilot stay unwired.


def _claude_group(cfg: dict, event: str, script: str) -> dict | None:
    for group in cfg.get(event, []):
        if any(h.get("command", "").endswith(script) for h in group.get("hooks", [])):
            return group
    return None


class TestLedgerSchemaGuardRegistration:
    SCRIPT = "ledger-schema-guard.sh"

    def test_registered_for_claude_pretooluse(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        assert _claude_handler(cfg, "PreToolUse", self.SCRIPT) is not None, (
            "ledger-schema-guard not registered for Claude PreToolUse"
        )

    def test_grouped_under_the_task_tool_matcher_not_an_if(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        group = _claude_group(cfg, "PreToolUse", self.SCRIPT)
        assert group is not None
        assert group.get("matcher") == "TaskCreate|TaskUpdate"
        handler = _claude_handler(cfg, "PreToolUse", self.SCRIPT)
        assert handler is not None and "if" not in handler, (
            "a tool-name matcher guard carries no Bash if-clause"
        )

    def test_tier_1(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["ledger-schema-guard"]["__defaults"]["tier"] == "1"

    def test_claude_only_unwired_on_cursor_and_copilot(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        cursor = generate_cursor(meta)
        wired = any(
            self.SCRIPT in entry.get("command", "")
            for entries in cursor.get("hooks", {}).values()
            for entry in entries
        )
        assert not wired, "a Claude-only ledger guard must not wire into Cursor"
        assert self.SCRIPT not in json.dumps(generate_copilot(meta)), (
            "a Claude-only ledger guard must not wire into Copilot"
        )


class TestRmScopeGuardRegistration:
    SCRIPT = "rm-scope-guard.sh"

    def test_claude_if_is_bash_rm(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        guard = _claude_handler(cfg, "PreToolUse", self.SCRIPT)
        assert guard is not None, "rm-scope-guard not registered for Claude PreToolUse"
        assert guard.get("if") == "Bash(rm *)"

    def test_tier_1(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["rm-scope-guard"]["__defaults"]["tier"] == "1"

    def test_cursor_wired_to_before_shell_execution(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", self.SCRIPT)
        assert guard is not None, "rm-scope-guard not wired to Cursor beforeShellExecution"

    def test_cursor_matcher_fires_on_rm_not_on_plain_git(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", self.SCRIPT)
        assert guard is not None
        pattern = re.compile(guard["matcher"])
        assert pattern.search("rm -rf /tmp/x")
        assert pattern.search("git status && rm /tmp/x")
        assert not pattern.search("git status --short")


# ── push-scope-guard registration against the REAL shared/hooks/metadata.yml ──
# push-scope-guard is a tier-1 enforcement gate (like hub-guard, not advisory).
# On Claude it shares git-push-review's if-clause; on Cursor it matches push
# ONLY — it judges git refspecs, so `gh pr` is out of its scope.


class TestPushScopeGuardRegistration:
    def test_claude_wiring_matches_git_push_review(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        guard = _claude_handler(cfg, "PreToolUse", "push-scope-guard.sh")
        review = _claude_handler(cfg, "PreToolUse", "git-push-review.sh")
        assert guard is not None, "push-scope-guard not registered for Claude PreToolUse"
        assert review is not None, "git-push-review baseline missing"
        assert guard.get("if") == review.get("if") == "Bash(git push *)"

    def test_is_tier_1(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["push-scope-guard"]["__defaults"]["tier"] == "1"

    def test_cursor_wiring_is_push_only(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", "push-scope-guard.sh")
        review = _cursor_entry(cfg, "beforeShellExecution", "git-push-review.sh")
        assert guard is not None, "push-scope-guard not wired to Cursor beforeShellExecution"
        assert review is not None, "git-push-review cursor baseline missing"
        assert guard["matcher"] == "git( +-[^ ]+| +-C +[^ ]+)* +push( |$)"
        assert "gh +pr" in review["matcher"], "git-push-review baseline should cover gh pr"
        assert guard["matcher"] != review["matcher"]


# ── spoke-main-guard registration against the REAL shared/hooks/metadata.yml ──
# spoke-main-guard (issue #32) is a tier-1 DENY guard. Like hub-guard it spans
# many git subcommands, so on Claude it carries NO `if` clause (matcher Bash,
# the script self-filters); on Cursor it remaps onto beforeShellExecution with a
# command regex firing on the ref-touching verbs and the land script.


class TestSpokeMainGuardRegistration:
    SCRIPT = "spoke-main-guard.sh"

    def test_claude_registered_on_bash_without_if(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        guard = _claude_handler(cfg, "PreToolUse", self.SCRIPT)
        assert guard is not None, "spoke-main-guard not registered for Claude PreToolUse"
        # Spans checkout/switch/merge/branch/reset/update-ref/push — no single
        # `if` rule fits, so it fires on all Bash and the script self-filters.
        assert guard.get("if") is None

    def test_claude_grouped_under_bash_matcher(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        for group in cfg.get("PreToolUse", []):
            for handler in group.get("hooks", []):
                if handler.get("command", "").endswith(self.SCRIPT):
                    assert group.get("matcher") == "Bash"
                    return
        raise AssertionError("spoke-main-guard not found under a Bash matcher group")

    def test_is_tier_1(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["spoke-main-guard"]["__defaults"]["tier"] == "1"

    def test_cursor_wired_to_before_shell_execution(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", self.SCRIPT)
        assert guard is not None, "spoke-main-guard not wired to Cursor beforeShellExecution"

    def test_cursor_matcher_fires_on_ref_verbs_and_land_script(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", self.SCRIPT)
        assert guard is not None
        pattern = re.compile(guard["matcher"])
        assert pattern.search("git checkout main")
        assert pattern.search("git switch main")
        assert pattern.search("git merge origin/main")
        assert pattern.search("git branch -f main")
        assert pattern.search("git reset --hard origin/main")
        assert pattern.search("git update-ref refs/heads/main HEAD")
        assert pattern.search("git push . HEAD:main")
        # `-c key=val` value must not break the chain to the verb.
        assert pattern.search("git -c core.pager=cat checkout main")
        assert pattern.search("scripts/worktree-land.sh 32")
        # Read-only git commands must not fire.
        assert not pattern.search("git status --short")
        assert not pattern.search("git log --oneline")

    def test_script_exists_and_executable(self) -> None:
        script = REAL_META.parent / self.SCRIPT
        assert script.is_file(), "spoke-main-guard.sh missing from shared/hooks/"
        assert os.access(script, os.X_OK), (
            "spoke-main-guard.sh must be executable for sync to install"
        )


# ── plan-gate-guard registration against the REAL shared/hooks/metadata.yml ──
# plan-gate-guard (issue #173) is a tier-1 DENY guard that blocks writes while a
# spoke is parked at its PLAN gate. Like hub-guard it spans both Bash (git commit)
# and the file-edit tools, so on Claude it carries NO `if` clause (matcher
# "Edit|Write|NotebookEdit|Bash", the script self-filters). Cursor has no
# pre-file-edit dedicated event, so — mirroring config-protection — it remaps
# onto beforeShellExecution and enforces the park at commit time.


class TestPlanGateGuardRegistration:
    SCRIPT = "plan-gate-guard.sh"

    def test_claude_registered_on_bash_and_edit_without_if(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        guard = _claude_handler(cfg, "PreToolUse", self.SCRIPT)
        assert guard is not None, "plan-gate-guard not registered for Claude PreToolUse"
        # Spans git commit AND file writes — no single `if` rule fits, so it
        # fires on the whole matcher group and the script self-filters.
        assert guard.get("if") is None

    def test_claude_matcher_covers_edit_write_notebook_and_bash(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        for group in cfg.get("PreToolUse", []):
            for handler in group.get("hooks", []):
                if handler.get("command", "").endswith(self.SCRIPT):
                    tools = set(group.get("matcher", "").split("|"))
                    assert {"Edit", "Write", "NotebookEdit", "Bash"} <= tools
                    return
        raise AssertionError("plan-gate-guard not found under a matcher group")

    def test_is_tier_1(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["plan-gate-guard"]["__defaults"]["tier"] == "1"

    def test_cursor_wired_to_before_shell_execution_on_commit(self) -> None:
        cfg = generate_cursor(parse_hooks_metadata(str(REAL_META)))
        guard = _cursor_entry(cfg, "beforeShellExecution", self.SCRIPT)
        assert guard is not None, "plan-gate-guard not wired to Cursor beforeShellExecution"
        pattern = re.compile(guard["matcher"])
        assert pattern.search("git commit -m x")
        # A `-c key=val` value must not break the chain to the verb (mirrors the
        # spoke-main-guard matcher; is_git_commit's plainer form misses this).
        assert pattern.search("git -c core.pager=cat commit -m x")
        assert not pattern.search("git status --short")

    def test_script_exists_and_executable(self) -> None:
        script = REAL_META.parent / self.SCRIPT
        assert script.is_file(), "plan-gate-guard.sh missing from shared/hooks/"
        assert os.access(script, os.X_OK), (
            "plan-gate-guard.sh must be executable for sync to install"
        )


# ── afk-notify-wake: the Notification event, Claude-only (issue #176) ──────────
# The event-driven wake's permission/question announcer is a Notification hook. Claude Code
# is the only platform that fires Notification, so it maps for Claude and the other two
# generators must SKIP it (an unmapped canonical event must not leak a bogus bucket).


@pytest.fixture()
def notification_meta(tmp_path: Path) -> Path:
    content = textwrap.dedent("""\
        afk-notify-wake:
          event: notification
          description: "announce a parked spoke"
          tier: 2
    """)
    p = tmp_path / "metadata.yml"
    p.write_text(content)
    return p


def test_notification_maps_to_capitalized_event_for_claude(notification_meta: Path) -> None:
    cfg = generate_claude(parse_hooks_metadata(str(notification_meta)))

    assert "Notification" in cfg, "the canonical `notification` event must map to Notification"
    handler = _claude_handler(cfg, "Notification", "afk-notify-wake.sh")
    assert handler is not None, "the hook must be registered under the Notification event"


def test_notification_has_no_matcher_group_for_claude(notification_meta: Path) -> None:
    # Notification carries no tool matcher, so its group is matcher-less (like SessionStart).
    cfg = generate_claude(parse_hooks_metadata(str(notification_meta)))

    assert "matcher" not in cfg["Notification"][0], "a Notification group takes no matcher"


def test_notification_is_skipped_for_copilot(notification_meta: Path) -> None:
    cfg = generate_copilot(parse_hooks_metadata(str(notification_meta)))

    hooks = cfg["hooks"]
    assert "notification" not in hooks and "Notification" not in hooks, (
        "a Claude-only event must not emit a bucket for Copilot"
    )


def test_notification_is_skipped_for_cursor(notification_meta: Path) -> None:
    cfg = generate_cursor(parse_hooks_metadata(str(notification_meta)))

    hooks = cfg["hooks"]
    assert "notification" not in hooks and "Notification" not in hooks, (
        "a Claude-only event must not emit a bucket for Cursor"
    )


class TestAfkNotifyWakeRegistration:
    """Registration through the sync pipeline is an explicit #176 acceptance criterion."""

    SCRIPT = "afk-notify-wake.sh"

    def test_registered_for_claude_notification(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        handler = _claude_handler(cfg, "Notification", self.SCRIPT)
        assert handler is not None, "afk-notify-wake not registered for Claude Notification"

    def test_not_registered_for_copilot_or_cursor(self) -> None:
        cop = generate_copilot(parse_hooks_metadata(str(REAL_META)))["hooks"]
        cur = generate_cursor(parse_hooks_metadata(str(REAL_META)))["hooks"]
        assert not any(self.SCRIPT in json_dump(v) for v in cop.values()), "leaked into Copilot"
        assert not any(self.SCRIPT in json_dump(v) for v in cur.values()), "leaked into Cursor"

    def test_hook_script_present_and_executable(self) -> None:
        script = REAL_META.parent / self.SCRIPT
        assert script.is_file(), "afk-notify-wake.sh missing from shared/hooks/"
        assert os.access(script, os.X_OK), "afk-notify-wake.sh must be executable for sync"


# ── cycle-step-mark registration against the REAL shared/hooks/metadata.yml ──
# cycle-step-mark (issue #178) is a Claude-only tier-3 telemetry hook: it derives
# solo-cycle step markers from commit/push/review witnesses on PostToolUse. Like
# spoke-main-guard it spans two tool types (Bash for commit/push, Write for the
# .review artifact), so it carries NO `if` clause and the script self-filters. It
# is telemetry-only and Claude-centric (the spokecycle trace), so — like
# afk-notify-wake — it must not leak into Copilot or Cursor.


class TestCycleStepMarkRegistration:
    SCRIPT = "cycle-step-mark.sh"

    def test_registered_for_claude_post_tool_use_without_if(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        handler = _claude_handler(cfg, "PostToolUse", self.SCRIPT)
        assert handler is not None, "cycle-step-mark not registered for Claude PostToolUse"
        # Spans Bash (commit/push) and Write (.review artifact) — no single `if`
        # rule fits, so it fires on all matched tools and the script self-filters.
        assert handler.get("if") is None

    def test_claude_grouped_under_bash_write_matcher(self) -> None:
        cfg = generate_claude(parse_hooks_metadata(str(REAL_META)))
        for group in cfg.get("PostToolUse", []):
            for handler in group.get("hooks", []):
                if handler.get("command", "").endswith(self.SCRIPT):
                    assert group.get("matcher") == "Bash|Write"
                    return
        raise AssertionError("cycle-step-mark not found under a Bash|Write matcher group")

    def test_is_tier_3(self) -> None:
        meta = parse_hooks_metadata(str(REAL_META))
        assert meta["cycle-step-mark"]["__defaults"]["tier"] == "3"

    def test_not_registered_for_copilot_or_cursor(self) -> None:
        cop = generate_copilot(parse_hooks_metadata(str(REAL_META)))["hooks"]
        cur = generate_cursor(parse_hooks_metadata(str(REAL_META)))["hooks"]
        assert not any(self.SCRIPT in json_dump(v) for v in cop.values()), "leaked into Copilot"
        assert not any(self.SCRIPT in json_dump(v) for v in cur.values()), "leaked into Cursor"

    def test_hook_script_present_and_executable(self) -> None:
        script = REAL_META.parent / self.SCRIPT
        assert script.is_file(), "cycle-step-mark.sh missing from shared/hooks/"
        assert os.access(script, os.X_OK), "cycle-step-mark.sh must be executable for sync"


def json_dump(obj: object) -> str:
    import json

    return json.dumps(obj)
