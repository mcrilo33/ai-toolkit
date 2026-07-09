"""Fail-closed payload extraction for the security guards (issue #208).

jq-terminal extraction used to crash secrets-scan / config-protection / hub-guard
OPEN on a malformed or shape-mismatched payload: under `set -euo pipefail` jq's
parse failure (exit 5) propagates out of the extraction assignment, and Claude
Code treats any exit other than 2 as a NON-blocking error — so the guarded
Write/tool call proceeded with its content unscanned or its path unchecked.

These tests pin the fix: an unparseable payload must DENY (exit 2), the
fail-closed conversion must NOT leak into spokes (hub-guard stays a no-op in a
linked worktree), and secrets-scan must fail closed when jq is absent (it cannot
extract content to scan, so it must not silently pass).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
SECRETS_SCAN = HOOKS_DIR / "secrets-scan.sh"
CONFIG_PROTECTION = HOOKS_DIR / "config-protection.sh"
HUB_GUARD = HOOKS_DIR / "hub-guard.sh"

BLOCK = 2
ALLOW = 0

# A fake AWS Access Key ID — matches AKIA[0-9A-Z]{16} but is not a real
# credential (synthetic test fixture, never used against any service).
FAKE_SECRET = "AKIA" + "1234567890ABCDEF"

# Malformed / shape-mismatched payloads that used to crash the guards open.
BARE_GARBAGE = "garbage"
STRING_TOOL_INPUT = '{"tool_name":"Write","tool_input":"garbage"}'


def _hook_env() -> dict[str, str]:
    """Hook env with CURSOR_PROJECT_DIR / AI_TOOLKIT_BASE_BRANCH stripped so the
    project-root and default-branch resolution fall to the payload/cwd rather
    than a host override (mirrors test_commit_hooks._hub_env)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("CURSOR_PROJECT_DIR", "AI_TOOLKIT_BASE_BRANCH")
    }


def _run(script: Path, payload: str, *, cwd: Path | None = None, env: dict | None = None):
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env if env is not None else _hook_env(),
    )


def claude_write(file_path: str, content: str) -> str:
    # Real Claude Code PreToolUse payloads carry hook_event_name; include it so
    # get_hook_event extracts cleanly (even via the jq-less grep fallback) and
    # control reaches the pre-write content path rather than crashing earlier.
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": file_path, "content": content},
        }
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A standalone git repo whose HEAD sits on the default branch (the hub)."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@test.test")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("seed\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    return tmp_path


@pytest.fixture()
def linked_worktree(git_repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of git_repo on its own task branch — a spoke, never the
    hub. hub-guard must stay a no-op here even on a malformed payload."""
    wt = tmp_path / "spoke"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature/208-spoke", str(wt)],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    return wt


def _path_without_jq(tmp_path: Path) -> str:
    """A PATH that resolves every real tool EXCEPT jq.

    Mirrors each current PATH directory's entries into one shim dir as symlinks,
    skipping any file named `jq`. Listing real directory entries (not
    `command -v`) sidesteps shell aliases, and dropping jq everywhere makes
    `command -v jq` fail while grep/sed/git/... still resolve.
    """
    shim = tmp_path / "nojq_bin"
    shim.mkdir()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not directory.is_dir():
            continue
        for tool in directory.iterdir():
            if tool.name == "jq":
                continue
            target = shim / tool.name
            if not target.exists():
                target.symlink_to(tool)
    return str(shim)


# ── malformed payload → fail closed (DENY) ────────────────


class TestMalformedPayloadFailsClosed:
    def test_secrets_scan_bare_garbage_denies(self) -> None:
        # Non-JSON stdin crashes get_hook_event (secrets-scan.sh:23); the ERR
        # trap must convert that crash into a deny. (A shape-mismatched payload
        # whose tool_input is a bare string carries no content field, so it
        # legitimately extracts empty and allows — like any no-content tool —
        # and is not a fail-open the guard should block.)
        assert _run(SECRETS_SCAN, BARE_GARBAGE).returncode == BLOCK

    def test_config_protection_bare_garbage_denies(self) -> None:
        assert _run(CONFIG_PROTECTION, BARE_GARBAGE).returncode == BLOCK

    def test_config_protection_string_tool_input_denies(self) -> None:
        assert _run(CONFIG_PROTECTION, STRING_TOOL_INPUT).returncode == BLOCK

    def test_hub_guard_string_tool_input_on_hub_denies(self, git_repo: Path) -> None:
        assert _run(HUB_GUARD, STRING_TOOL_INPUT, cwd=git_repo).returncode == BLOCK


# ── the fix must not over-block well-formed payloads ──────


class TestFailClosedDoesNotOverBlock:
    def test_secrets_scan_clean_write_allows(self) -> None:
        assert _run(SECRETS_SCAN, claude_write("/x/c.py", "clean = 1\n")).returncode == ALLOW

    def test_secrets_scan_secret_write_blocks(self) -> None:
        assert (
            _run(SECRETS_SCAN, claude_write("/x/c.py", f'k = "{FAKE_SECRET}"\n')).returncode
            == BLOCK
        )

    def test_config_protection_normal_write_allows(self) -> None:
        assert _run(CONFIG_PROTECTION, claude_write("/repo/main.py", "x")).returncode == ALLOW

    def test_config_protection_protected_write_blocks(self) -> None:
        assert (
            _run(CONFIG_PROTECTION, claude_write("/repo/pyproject.toml", "x")).returncode == BLOCK
        )

    def test_hub_guard_malformed_in_worktree_stays_noop(self, linked_worktree: Path) -> None:
        # A spoke must never be fail-closed by a malformed payload: hub-guard
        # exits 0 in a linked worktree before any extraction can crash.
        assert _run(HUB_GUARD, STRING_TOOL_INPUT, cwd=linked_worktree).returncode == ALLOW

    def test_hub_guard_bare_garbage_in_worktree_stays_noop(self, linked_worktree: Path) -> None:
        assert _run(HUB_GUARD, BARE_GARBAGE, cwd=linked_worktree).returncode == ALLOW

    def test_hub_guard_bare_garbage_on_hub_allows(self, git_repo: Path) -> None:
        # Intended asymmetry vs. the sibling guards: bare non-JSON carries no
        # command, file path, or tool name, so hub-guard extracts nothing
        # actionable and allows. There is no edit/commit/branch-create to block,
        # and a real Claude payload always arrives as valid JSON (and IS blocked,
        # see test_hub_guard_string_tool_input_on_hub_denies). Only a
        # shape-mismatched payload that crashes the one top-level extraction
        # (get_edit_file_path) fails closed.
        assert _run(HUB_GUARD, BARE_GARBAGE, cwd=git_repo).returncode == ALLOW


# ── secrets-scan: jq absent → blind scanner must fail closed ──


class TestSecretsScanJqAbsent:
    def test_clean_write_denies_without_jq(self, tmp_path: Path) -> None:
        # get_edit_new_content extracts content only via jq; with jq absent the
        # scanner is blind, so even a clean-looking write must be blocked rather
        # than silently passed unscanned. The dedicated jq-absent guard (not the
        # ERR trap) must handle this — bind the assertion to its message so the
        # test fails if the guard is removed and the trap silently takes over.
        env = _hook_env()
        env["PATH"] = _path_without_jq(tmp_path)
        result = _run(SECRETS_SCAN, claude_write("/x/c.py", "clean = 1\n"), env=env)
        assert result.returncode == BLOCK
        assert "jq is unavailable" in result.stderr

    def test_secret_write_denies_without_jq(self, tmp_path: Path) -> None:
        # A secret-bearing write is blocked too — but via the same blind-scanner
        # gate, since without jq the content is never actually inspected.
        env = _hook_env()
        env["PATH"] = _path_without_jq(tmp_path)
        result = _run(SECRETS_SCAN, claude_write("/x/c.py", f'k = "{FAKE_SECRET}"\n'), env=env)
        assert result.returncode == BLOCK
        assert "jq is unavailable" in result.stderr
