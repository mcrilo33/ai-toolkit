"""Unit tests for the rm-scope-guard PreToolUse hook (issue #13).

The hook reads a JSON payload (``{"tool_name": "Bash", "tool_input":
{"command": "..."}, "cwd": "..."}``) on stdin and either:

* prints ``hookSpecificOutput.permissionDecision: "allow"`` JSON (exit 0) when
  EVERY rm target provably resolves inside the project root or /tmp//private/tmp
  and hits no protected pattern, or
* stays SILENT (empty stdout, exit 0) so the normal permission prompt fires.

It NEVER denies — the user's ``Bash(rm *)`` ask rule stays the backstop. These
tests subprocess the real script and assert the full decision matrix from the
issue, including the JSON output shape.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
RM_SCOPE_GUARD = HOOKS_DIR / "rm-scope-guard.sh"

ALLOW = "allow"


def _payload(command: str, cwd: Path | str) -> str:
    """Claude/Copilot generic shape: command under tool_input, top-level cwd."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})


def _cursor_payload(command: str, *, root: Path | None = None) -> str:
    """Cursor beforeShellExecution shape: top-level command, EMPTY cwd."""
    payload: dict = {
        "hook_event_name": "beforeShellExecution",
        "command": command,
        "cwd": "",
    }
    if root is not None:
        payload["workspace_roots"] = [str(root)]
    return json.dumps(payload)


def _run(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(RM_SCOPE_GUARD)],
        input=payload,
        capture_output=True,
        text=True,
    )


def decision(command: str, cwd: Path | str) -> str | None:
    """Run the guard; return the permissionDecision or None when silent.

    Asserts the design invariant on every call: the hook never blocks
    (exit 0 always), never writes stderr, and silence means an empty stdout.
    """
    proc = _run(_payload(command, cwd))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like a worktree with deletable content."""
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "scratch.txt").write_text("x\n")
    (root / "build").mkdir()
    (root / "dist").mkdir()
    (root / "sub").mkdir()
    (root / ".env").write_text("KEY=1\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}\n")
    (root / ".review").mkdir()
    return root


class TestAutoAllow:
    """Targets provably scoped to the worktree or /tmp → allow."""

    @pytest.mark.parametrize(
        "command",
        [
            "rm -f scratch.txt",
            "rm -rf build/ dist/",
            "rm /tmp/foo",
            # Verbatim must-auto-allow case from issue #13 (comment 2).
            "rm -rf /tmp/wtland-rev",
            "rm scratch.txt build/old.txt",
            "rm -- scratch.txt",
            "rm sub/../scratch.txt",
            "rm 'name with spaces.txt'",
        ],
    )
    def test_allows_scoped_targets(self, repo: Path, command: str) -> None:
        assert decision(command, repo) == ALLOW

    def test_allows_absolute_target_inside_repo(self, repo: Path) -> None:
        assert decision(f"rm {repo}/scratch.txt", repo) == ALLOW

    def test_allows_dotdot_staying_inside_repo(self, repo: Path) -> None:
        assert decision("rm ../scratch.txt", repo / "sub") == ALLOW

    def test_allows_tmp_target_outside_any_repo(self, tmp_path: Path) -> None:
        assert decision("rm /tmp/foo", tmp_path.resolve()) == ALLOW


class TestFallThrough:
    """Out-of-scope, protected, or unparseable → silent (normal prompt)."""

    @pytest.mark.parametrize(
        "command",
        [
            # Out of scope.
            "rm -rf ~/x",
            "rm /etc/hosts",
            "rm ../other-repo/file",
            "rm -rf /",
            # Protected patterns inside the repo.
            "rm .env",
            "rm .env.local",
            "rm -rf .git",
            "rm .git/config",
            "rm .claude/settings.json",
            "rm .claude/settings.local.json",
            "rm -rf .review",
            "rm .review/abc.json",
            # The repo root itself.
            "rm -rf .",
            # Case variants must match protected patterns too: macOS APFS is
            # case-insensitive, so .GIT IS .git on disk.
            "rm -rf .GIT",
            "rm .ENV",
            "rm .Claude/settings.json",
            "rm -rf .Review",
            # Deleting .claude itself would take settings with it.
            "rm -rf .claude",
            "rm -rf .claude/",
            # zsh expands =name to the PATH binary — never auto-allow.
            "rm =python3",
            # Never auto-allow sudo / --no-preserve-root.
            "sudo rm -rf /tmp/x",
            "rm --no-preserve-root -rf /tmp/x",
            # Dynamic / unparseable targets.
            "rm $FOO",
            "rm $(ls)",
            "rm `ls`",
            "rm *.txt",
            # No operands to prove anything about.
            "rm",
            "rm -rf",
        ],
    )
    def test_falls_through(self, repo: Path, command: str) -> None:
        assert decision(command, repo) is None

    def test_falls_through_on_repo_root_absolute(self, repo: Path) -> None:
        assert decision(f"rm -rf {repo}", repo) is None

    def test_relative_target_with_unknown_cwd_falls_through(self, repo: Path) -> None:
        """Cursor reports an empty cwd: relative targets are unresolvable."""
        proc = _run(_cursor_payload("rm -f scratch.txt", root=repo))
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    def test_falls_through_when_dir_contains_env_file(self, repo: Path) -> None:
        """The .env* rule must not be bypassable by deleting the parent dir."""
        (repo / "build" / ".env").write_text("KEY=1\n")
        assert decision("rm -rf build/", repo) is None

    def test_symlink_to_outside_falls_through(self, repo: Path) -> None:
        """realpath resolution is the load-bearing property: a repo-internal
        symlink whose destination is outside scope must not auto-allow."""
        (repo / "escape").symlink_to("/etc")
        assert decision("rm escape/hosts", repo) is None
        assert decision("rm -rf escape", repo) is None

    def test_cwdless_payload_never_anchors_to_process_cwd(self, tmp_path: Path) -> None:
        """With no payload cwd, ROOT must come only from explicit anchors
        (workspace_roots / CURSOR_PROJECT_DIR) — never from walking up the
        hook process's own working directory into an unrelated repo."""
        other = tmp_path.resolve() / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        (other / "f.txt").write_text("x\n")
        env = {k: v for k, v in os.environ.items() if k != "CURSOR_PROJECT_DIR"}
        proc = subprocess.run(
            ["bash", str(RM_SCOPE_GUARD)],
            input=_cursor_payload(f"rm {other}/f.txt"),
            capture_output=True,
            text=True,
            cwd=str(other),
            env=env,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""


class TestNeverBreaks:
    """Malformed input must never produce an exit code or output."""

    @pytest.mark.parametrize("payload", ["", "not json", "{}", '{"tool_input": 3}'])
    def test_malformed_payload_is_silent(self, payload: str) -> None:
        proc = _run(payload)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""


def test_allow_payload_shape(repo: Path) -> None:
    """The allow decision carries the full Claude hookSpecificOutput shape."""
    proc = _run(_payload("rm -f scratch.txt", repo))
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    out = data["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"]


def test_silent_fall_through_is_truly_silent(repo: Path) -> None:
    """No decision means NO output at all — the prompt must fire untouched."""
    proc = _run(_payload("rm /etc/hosts", repo))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr == ""
