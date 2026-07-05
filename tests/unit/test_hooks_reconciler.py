"""Unit tests for scripts/hooks_reconciler.py.

The reconciler treats ai-toolkit-emitted hooks as a managed/owned set: existing
owned entries are removed and replaced with the fresh set exactly once, while
user-authored hooks are preserved. These tests pin the convergence (idempotent),
self-healing (de-bloat), and preservation guarantees.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from hooks_reconciler import (
    _is_owned,
    reconcile_bucket,
    reconcile_claude,
    reconcile_cursor,
)

# ── Sample entries ────────────────────────────────────────

OWNED_CURSOR = {"command": "./.cursor/hooks/scripts/block-no-verify.sh", "matcher": "Bash"}
OWNED_CURSOR_2 = {"command": "./.cursor/hooks/scripts/secrets-scan.sh", "matcher": "Write|Edit"}
USER_CURSOR = {"command": "./scripts/my-precommit.sh", "matcher": "Bash"}

OWNED_CLAUDE_GROUP = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/scripts/block-no-verify.sh',
        }
    ],
}
USER_CLAUDE_GROUP = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "./my/user-hook.sh"}],
}


# ── _is_owned() ───────────────────────────────────────────


class TestIsOwned:
    def test_cursor_owned_entry(self) -> None:
        assert _is_owned(OWNED_CURSOR) is True

    def test_cursor_user_entry(self) -> None:
        assert _is_owned(USER_CURSOR) is False

    def test_copilot_bash_field_owned(self) -> None:
        entry = {"type": "command", "bash": "./.github/hooks/scripts/secrets-scan.sh"}
        assert _is_owned(entry) is True

    def test_claude_group_owned_via_nested_handler(self) -> None:
        assert _is_owned(OWNED_CLAUDE_GROUP) is True

    def test_claude_user_group_not_owned(self) -> None:
        assert _is_owned(USER_CLAUDE_GROUP) is False

    def test_entry_with_no_command_not_owned(self) -> None:
        assert _is_owned({"matcher": "Bash"}) is False


# ── reconcile_bucket() ────────────────────────────────────


class TestReconcileBucket:
    def test_empty_existing_appends_generated(self) -> None:
        result = reconcile_bucket([], [OWNED_CURSOR, OWNED_CURSOR_2])
        assert result == [OWNED_CURSOR, OWNED_CURSOR_2]

    def test_removes_preexisting_owned(self) -> None:
        existing = [OWNED_CURSOR, OWNED_CURSOR]  # bloated clones
        result = reconcile_bucket(existing, [OWNED_CURSOR])
        assert result == [OWNED_CURSOR]

    def test_preserves_user_entries_in_place(self) -> None:
        existing = [USER_CURSOR, OWNED_CURSOR]
        result = reconcile_bucket(existing, [OWNED_CURSOR_2])
        # user entry stays first, owned replaced by freshly generated set
        assert result == [USER_CURSOR, OWNED_CURSOR_2]

    def test_dedup_safety_net(self) -> None:
        """Identical generated entries collapse to one."""
        result = reconcile_bucket([], [OWNED_CURSOR, dict(OWNED_CURSOR)])
        assert result == [OWNED_CURSOR]

    def test_idempotent_on_already_reconciled(self) -> None:
        first = reconcile_bucket([USER_CURSOR], [OWNED_CURSOR])
        second = reconcile_bucket(first, [OWNED_CURSOR])
        assert first == second

    def test_drops_owned_hook_removed_from_generated(self) -> None:
        """A hook deleted from shared/ disappears downstream on next sync."""
        existing = [OWNED_CURSOR, OWNED_CURSOR_2]
        result = reconcile_bucket(existing, [OWNED_CURSOR])
        assert OWNED_CURSOR_2 not in result


# ── reconcile_cursor() ────────────────────────────────────


class TestReconcileCursor:
    def test_creates_from_empty(self) -> None:
        generated = {"version": 1, "hooks": {"preToolUse": [OWNED_CURSOR]}}
        result = reconcile_cursor({}, generated)
        assert result["hooks"]["preToolUse"] == [OWNED_CURSOR]
        assert result["version"] == 1

    def test_heals_bloated_file(self) -> None:
        generated = {"version": 1, "hooks": {"preToolUse": [OWNED_CURSOR, OWNED_CURSOR_2]}}
        bloated = {
            "version": 1,
            "hooks": {"preToolUse": [OWNED_CURSOR, OWNED_CURSOR, OWNED_CURSOR_2, USER_CURSOR]},
        }
        result = reconcile_cursor(bloated, generated)
        pre = result["hooks"]["preToolUse"]
        assert pre.count(OWNED_CURSOR) == 1
        assert USER_CURSOR in pre
        assert len(pre) == 3  # 2 owned + 1 user

    def test_idempotent(self) -> None:
        generated = {"version": 1, "hooks": {"preToolUse": [OWNED_CURSOR]}}
        first = reconcile_cursor({}, generated)
        second = reconcile_cursor(first, generated)
        assert first == second

    def test_migration_purges_owned_from_unemitted_bucket(self) -> None:
        """A hook that moved preToolUse -> beforeShellExecution leaves no stale entry.

        The old bucket (preToolUse) is no longer emitted; its owned entry must be
        removed so the migrated hook does not fire on BOTH the generic scratch
        event and the dedicated event.
        """
        existing = {
            "version": 1,
            "hooks": {"preToolUse": [OWNED_CURSOR]},  # pre-migration wiring
        }
        generated = {
            "version": 1,
            "hooks": {"beforeShellExecution": [OWNED_CURSOR]},  # post-migration
        }
        result = reconcile_cursor(existing, generated)
        # Stale preToolUse bucket is gone entirely (no owned entries remain).
        assert "preToolUse" not in result["hooks"]
        assert result["hooks"]["beforeShellExecution"] == [OWNED_CURSOR]

    def test_migration_preserves_user_hook_in_old_bucket(self) -> None:
        """Purging the old bucket must keep user-authored entries in place."""
        existing = {
            "version": 1,
            "hooks": {"preToolUse": [OWNED_CURSOR, USER_CURSOR]},
        }
        generated = {
            "version": 1,
            "hooks": {"beforeShellExecution": [OWNED_CURSOR]},
        }
        result = reconcile_cursor(existing, generated)
        # User hook survives in the old bucket; only the owned entry is purged.
        assert result["hooks"]["preToolUse"] == [USER_CURSOR]
        assert result["hooks"]["beforeShellExecution"] == [OWNED_CURSOR]

    def test_migration_idempotent(self) -> None:
        existing = {"version": 1, "hooks": {"preToolUse": [OWNED_CURSOR]}}
        generated = {"version": 1, "hooks": {"beforeShellExecution": [OWNED_CURSOR]}}
        first = reconcile_cursor(existing, generated)
        second = reconcile_cursor(first, generated)
        assert first == second


# ── reconcile_claude() ────────────────────────────────────


class TestReconcileClaude:
    def test_preserves_other_settings_keys(self) -> None:
        existing = {"model": "x", "hooks": {"PreToolUse": []}}
        generated = {"PreToolUse": [OWNED_CLAUDE_GROUP]}
        result = reconcile_claude(existing, generated)
        assert result["model"] == "x"

    def test_replaces_owned_groups_not_user(self) -> None:
        existing = {
            "hooks": {"PreToolUse": [OWNED_CLAUDE_GROUP, OWNED_CLAUDE_GROUP, USER_CLAUDE_GROUP]}
        }
        generated = {"PreToolUse": [OWNED_CLAUDE_GROUP]}
        result = reconcile_claude(existing, generated)
        pre = result["hooks"]["PreToolUse"]
        assert USER_CLAUDE_GROUP in pre
        assert pre.count(OWNED_CLAUDE_GROUP) == 1
        assert len(pre) == 2

    def test_idempotent(self) -> None:
        generated = {"PreToolUse": [OWNED_CLAUDE_GROUP]}
        first = reconcile_claude({}, generated)
        second = reconcile_claude(first, generated)
        assert first == second

    def test_creates_hooks_key_from_empty(self) -> None:
        generated = {"PreToolUse": [OWNED_CLAUDE_GROUP]}
        result = reconcile_claude({}, generated)
        assert result["hooks"]["PreToolUse"] == [OWNED_CLAUDE_GROUP]

    # Notification silence (issue #146): every synced checkout — hub and spoke
    # alike — silences Claude Code's own idle/turn notification channel so the
    # hub is the single notifier (hub-notify.sh). notifications_disabled is the
    # only setting that suppresses the default ping (a Notification hook fires
    # additively and cannot).
    def test_disables_notifications_from_empty(self) -> None:
        result = reconcile_claude({}, {})
        assert result["preferredNotifChannel"] == "notifications_disabled"

    def test_forces_silence_over_existing_channel(self) -> None:
        result = reconcile_claude({"preferredNotifChannel": "iterm2"}, {})
        assert result["preferredNotifChannel"] == "notifications_disabled"

    def test_silence_added_alongside_reconciled_hooks(self) -> None:
        generated = {"PreToolUse": [OWNED_CLAUDE_GROUP]}
        result = reconcile_claude({"model": "x"}, generated)
        assert result["preferredNotifChannel"] == "notifications_disabled"
        assert result["model"] == "x"
        assert result["hooks"]["PreToolUse"] == [OWNED_CLAUDE_GROUP]
