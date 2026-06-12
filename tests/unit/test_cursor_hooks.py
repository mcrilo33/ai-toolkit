"""Unit tests for the Cursor dedicated-event hook migration.

Covers the new payload accessors, the agent-tools scratch guard, commit-time
secrets/config DENY (beforeShellExecution), the afterFileEdit secret revert, and
the advisory -> hard-DENY promotion at the shipping gate — while asserting the
Claude/Copilot (tool_input) shape still works unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
UTILS = HOOKS_DIR / "lib" / "utils.sh"
WINDOW_OPEN = HOOKS_DIR / "review-window-open.sh"
SECRETS_SCAN = HOOKS_DIR / "secrets-scan.sh"
SECRETS_SCAN_REVERT = HOOKS_DIR / "secrets-scan-revert.sh"
CONFIG_PROTECTION = HOOKS_DIR / "config-protection.sh"
RED_PROOF = HOOKS_DIR / "red-proof-warn.sh"
REVIEWER_SEP = HOOKS_DIR / "reviewer-sep-warn.sh"
GIT_PUSH_REVIEW = HOOKS_DIR / "git-push-review.sh"
DELEGATION = HOOKS_DIR / "delegation-gate-warn.sh"
POST_EDIT_FORMAT = HOOKS_DIR / "post-edit-format.sh"
CONSOLE_LOG_WARN = HOOKS_DIR / "console-log-warn.sh"

BLOCK = 2
ALLOW = 0

# A fake AWS Access Key ID — matches the AKIA[0-9A-Z]{16} pattern but is not a
# real credential (synthetic test fixture, never used against any service).
FAKE_SECRET = "AKIA" + "1234567890ABCDEF"


def _run(script: Path, payload: str, *, cwd: Path | None = None):
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def cursor_shell(command: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": "",
            "workspace_roots": [str(root)],
        }
    )


def cursor_edit(file_path: Path, new_string: str, root: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "afterFileEdit",
            "file_path": str(file_path),
            "edits": [{"old_string": "", "new_string": new_string}],
            "workspace_roots": [str(root)],
        }
    )


def claude_bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def claude_write(file_path: str, content: str) -> str:
    return json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(tmp_path), check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "test@test.test")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-qm", "chore: seed", "-m", "Refs #0")
    return tmp_path


def _stage(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True, capture_output=True)


def _accessor(func_call: str, payload: str) -> subprocess.CompletedProcess:
    """Source utils.sh and run a single accessor against a payload on stdin.

    The script reads stdin into IN, then evaluates the given expression, e.g.
    'get_shell_command "$IN"'. Returns the completed process (stdout/returncode).
    """
    script = f'source "{UTILS}"\nIN=$(cat)\n{func_call}\n'
    return subprocess.run(["bash", "-c", script], input=payload, capture_output=True, text=True)


# ── accessors ─────────────────────────────────────────────


class TestAccessors:
    def test_get_shell_command_top_level(self, tmp_path: Path) -> None:
        out = _accessor('echo "$(get_shell_command "$IN")"', cursor_shell("git status", tmp_path))
        assert out.stdout.strip() == "git status"

    def test_get_shell_command_tool_input_fallback(self) -> None:
        out = _accessor('echo "$(get_shell_command "$IN")"', claude_bash("ls -la"))
        assert out.stdout.strip() == "ls -la"

    def test_get_shell_command_normalizes_escaped_quotes(self, tmp_path: Path) -> None:
        payload = cursor_shell(r"git commit -m \"feat: x\"", tmp_path)
        out = _accessor('echo "$(get_shell_command "$IN")"', payload)
        assert out.stdout.strip() == 'git commit -m "feat: x"'

    def test_get_edit_file_path_top_level(self, tmp_path: Path) -> None:
        out = _accessor(
            'echo "$(get_edit_file_path "$IN")"', cursor_edit(tmp_path / "a.py", "x=1\n", tmp_path)
        )
        assert out.stdout.strip() == str(tmp_path / "a.py")

    def test_get_edit_file_path_tool_input_fallback(self) -> None:
        out = _accessor('echo "$(get_edit_file_path "$IN")"', claude_write("/x/y.py", "z=1\n"))
        assert out.stdout.strip() == "/x/y.py"

    def test_get_edit_new_content_concats_edits(self, tmp_path: Path) -> None:
        payload = json.dumps(
            {
                "hook_event_name": "afterFileEdit",
                "file_path": str(tmp_path / "a.py"),
                "edits": [
                    {"old_string": "", "new_string": "a\n"},
                    {"old_string": "", "new_string": "b\n"},
                ],
            }
        )
        out = _accessor('printf "%s" "$(get_edit_new_content "$IN")"', payload)
        assert out.stdout == "a\nb"

    def test_get_edit_new_content_tool_input_fallback(self) -> None:
        out = _accessor(
            'printf "%s" "$(get_edit_new_content "$IN")"', claude_write("/x.py", "hello\n")
        )
        assert out.stdout == "hello"

    def test_get_hook_event_empty_on_claude(self) -> None:
        out = _accessor('printf "[%s]" "$(get_hook_event "$IN")"', claude_bash("git status"))
        assert out.stdout == "[]"

    def test_on_cursor_dedicated_event_true(self, tmp_path: Path) -> None:
        out = _accessor(
            'on_cursor_dedicated_event "$IN" && echo yes || echo no',
            cursor_shell("git status", tmp_path),
        )
        assert out.stdout.strip() == "yes"

    def test_on_cursor_dedicated_event_false_on_claude(self) -> None:
        out = _accessor(
            'on_cursor_dedicated_event "$IN" && echo yes || echo no', claude_bash("git status")
        )
        assert out.stdout.strip() == "no"

    def test_project_root_from_workspace_roots(self, tmp_path: Path) -> None:
        out = _accessor(
            'echo "$(project_root_from_payload "$IN")"', cursor_shell("git status", tmp_path)
        )
        assert out.stdout.strip() == str(tmp_path)

    def test_project_root_prefers_cursor_project_dir(self, tmp_path: Path) -> None:
        payload = cursor_shell("git status", tmp_path)
        script = f'source "{UTILS}"\nIN=$(cat)\necho "$(project_root_from_payload "$IN")"\n'
        out = subprocess.run(
            ["bash", "-c", script],
            input=payload,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "CURSOR_PROJECT_DIR": "/explicit/root"},
        )
        assert out.stdout.strip() == "/explicit/root"

    def test_find_project_root_stops_at_worktree_gitlink(self, tmp_path: Path) -> None:
        # A linked worktree's .git is a FILE (gitlink), not a directory. The
        # root walk must stop there and not climb past it to an ancestor repo
        # (e.g. a dotfiles repo at $HOME) — otherwise every hook resolves the
        # wrong root inside a worktree.
        wt = tmp_path / "outer" / "wt"
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
        out = _accessor(f'echo "$(find_project_root "{wt}")"', claude_bash("git status"))
        assert out.stdout.strip() == str(wt)


class TestAgentToolsGuard:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/Users/me/.cursor/projects/p/agent-tools/abc.txt", "yes"),
            ("/home/x/.cursor/foo/agent-tools/x", "yes"),
            ("/Users/me/repo/real.py", "no"),
            ("/tmp/agent-tools/x", "no"),
        ],
    )
    def test_is_agent_tools_path(self, path: str, expected: str) -> None:
        script = f'source "{UTILS}"\nis_agent_tools_path "{path}" && echo yes || echo no\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert out.stdout.strip() == expected


# ── secrets-scan: commit-time staged-content DENY ─────────


class TestSecretsScanCommitTime:
    def test_blocks_commit_with_staged_secret(self, git_repo: Path) -> None:
        _stage(git_repo, "bad.py", f'KEY = "{FAKE_SECRET}"\n')
        rc = _run(SECRETS_SCAN, cursor_shell("git commit -m 'x'", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_blocks_git_add_with_staged_secret(self, git_repo: Path) -> None:
        _stage(git_repo, "bad.py", f'KEY = "{FAKE_SECRET}"\n')
        rc = _run(SECRETS_SCAN, cursor_shell("git add bad.py", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_allows_clean_staged_commit(self, git_repo: Path) -> None:
        _stage(git_repo, "good.py", "x = 1\n")
        rc = _run(SECRETS_SCAN, cursor_shell("git commit -m 'feat: x'", git_repo), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_ignores_non_git_command(self, git_repo: Path) -> None:
        _stage(git_repo, "bad.py", f'KEY = "{FAKE_SECRET}"\n')
        rc = _run(SECRETS_SCAN, cursor_shell("ls -la", git_repo), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_blocks_chained_git_add(self, git_repo: Path) -> None:
        # `cd … && git add` must not bypass the staged-content scan.
        _stage(git_repo, "bad.py", f'KEY = "{FAKE_SECRET}"\n')
        rc = _run(SECRETS_SCAN, cursor_shell("cd . && git add bad.py", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_blocks_git_dash_c_commit(self, git_repo: Path) -> None:
        # `git -C path commit` must not bypass the staged-content scan.
        _stage(git_repo, "bad.py", f'KEY = "{FAKE_SECRET}"\n')
        rc = _run(
            SECRETS_SCAN, cursor_shell(f"git -C {git_repo} commit -m x", git_repo), cwd=git_repo
        )
        assert rc.returncode == BLOCK

    def test_claude_pre_write_secret_blocks(self) -> None:
        rc = _run(SECRETS_SCAN, claude_write("/x/c.py", f'tok = "{FAKE_SECRET}"\n'))
        assert rc.returncode == BLOCK

    def test_claude_pre_write_clean_allows(self) -> None:
        rc = _run(SECRETS_SCAN, claude_write("/x/c.py", "tok = 1\n"))
        assert rc.returncode == ALLOW


# ── secrets-scan-revert: afterFileEdit containment ────────


class TestSecretsScanRevert:
    def test_surgically_redacts_secret_line(self, git_repo: Path) -> None:
        (git_repo / "app.py").write_text(f'line1\nKEY = "{FAKE_SECRET}"\nline3\n')
        payload = cursor_edit(git_repo / "app.py", f'KEY = "{FAKE_SECRET}"\n', git_repo)
        rc = _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        content = (git_repo / "app.py").read_text()
        # Only the secret-bearing line is removed; unrelated lines survive.
        assert FAKE_SECRET not in content
        assert "line1" in content
        assert "line3" in content

    def test_backs_up_before_mutation(self, git_repo: Path) -> None:
        (git_repo / "app.py").write_text(f'a = 1\nKEY = "{FAKE_SECRET}"\n')
        payload = cursor_edit(git_repo / "app.py", f'KEY = "{FAKE_SECRET}"\n', git_repo)
        _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        backups = list(git_repo.glob("app.py.secret-revert.*.bak"))
        assert backups, "a backup must exist before any destructive mutation"
        # The backup preserves the original (secret-bearing) content for recovery.
        assert FAKE_SECRET in backups[0].read_text()

    def test_does_not_corrupt_identical_legit_lines(self, git_repo: Path) -> None:
        # A legit line whose text repeats around the secret must be preserved —
        # surgical line redaction removes only the secret line.
        (git_repo / "b.py").write_text(f'token = legit\nAPIKEY = "{FAKE_SECRET}"\ntoken = legit\n')
        payload = cursor_edit(git_repo / "b.py", f'APIKEY = "{FAKE_SECRET}"\n', git_repo)
        rc = _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        content = (git_repo / "b.py").read_text()
        assert content.count("token = legit") == 2
        assert FAKE_SECRET not in content

    def test_preserves_no_trailing_newline(self, git_repo: Path) -> None:
        (git_repo / "c.py").write_text(f'x = 1\nBAD = "{FAKE_SECRET}"')  # no trailing newline
        payload = cursor_edit(git_repo / "c.py", f'BAD = "{FAKE_SECRET}"\n', git_repo)
        rc = _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        content = (git_repo / "c.py").read_text()
        assert content == "x = 1"  # secret line gone, no spurious trailing newline

    def test_clean_edit_is_noop(self, git_repo: Path) -> None:
        (git_repo / "clean.py").write_text("y = 2\n")
        payload = cursor_edit(git_repo / "clean.py", "y = 2\n", git_repo)
        rc = _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        assert (git_repo / "clean.py").read_text() == "y = 2\n"
        assert not list(git_repo.glob("clean.py.secret-revert.*.bak"))

    def test_noop_on_non_after_file_edit_event(self, git_repo: Path) -> None:
        (git_repo / "z.py").write_text(f'KEY = "{FAKE_SECRET}"\n')
        # Claude postToolUse shape: revert must NOT fire (wrong event).
        rc = _run(SECRETS_SCAN_REVERT, claude_write(str(git_repo / "z.py"), "x"), cwd=git_repo)
        assert rc.returncode == ALLOW
        # File is left untouched (revert is Cursor-afterFileEdit-only).
        assert (git_repo / "z.py").read_text() == f'KEY = "{FAKE_SECRET}"\n'

    def test_noop_on_agent_tools_scratch_path(self, git_repo: Path) -> None:
        scratch = "/Users/me/.cursor/projects/p/agent-tools/x.txt"
        payload = cursor_edit(Path(scratch), f'KEY = "{FAKE_SECRET}"\n', git_repo)
        rc = _run(SECRETS_SCAN_REVERT, payload, cwd=git_repo)
        assert rc.returncode == ALLOW


# ── config-protection: commit-time staged-file DENY ───────


class TestConfigProtectionCommitTime:
    def test_blocks_commit_staging_protected_config(self, git_repo: Path) -> None:
        _stage(git_repo, "pyproject.toml", "[tool.x]\n")
        rc = _run(CONFIG_PROTECTION, cursor_shell("git commit -m 'x'", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_blocks_git_add_staging_lockfile(self, git_repo: Path) -> None:
        _stage(git_repo, "uv.lock", "lock\n")
        rc = _run(CONFIG_PROTECTION, cursor_shell("git add uv.lock", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_allows_clean_commit(self, git_repo: Path) -> None:
        _stage(git_repo, "main.py", "x = 1\n")
        rc = _run(
            CONFIG_PROTECTION, cursor_shell("git commit -m 'feat: x'", git_repo), cwd=git_repo
        )
        assert rc.returncode == ALLOW

    def test_claude_pre_write_protected_blocks(self) -> None:
        rc = _run(CONFIG_PROTECTION, claude_write("/repo/pyproject.toml", "x"))
        assert rc.returncode == BLOCK

    def test_claude_pre_write_normal_allows(self) -> None:
        rc = _run(CONFIG_PROTECTION, claude_write("/repo/main.py", "x"))
        assert rc.returncode == ALLOW


# ── advisory hooks: DENY on Cursor, advisory on Claude ────


def _commit_source_no_red(repo: Path) -> None:
    _stage(repo, "src.py", "def f():\n    return 1\n")
    subprocess.run(
        ["git", "commit", "-qm", "feat: add", "-m", "Refs #1"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _add_upstream_with_change(repo: Path) -> None:
    """Give the seeded repo a tracked upstream plus one unpushed source commit.

    reviewer-sep needs a resolvable merge-base to compute the diff hash; without
    an upstream it degrades to allow. This sets origin/main as the base and adds
    a change that has no review-evidence artifact.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)

    git("init", "-q", "--bare", str(repo / "remote.git"))
    git("remote", "add", "origin", str(repo / "remote.git"))
    git("push", "-q", "-u", "origin", "HEAD:main")
    git("checkout", "-q", "-b", "feature/1-x")
    git("branch", "--set-upstream-to=origin/main", "feature/1-x")
    _stage(repo, "src.py", "def f():\n    return 1\n")
    git("commit", "-qm", "feat: add", "-m", "Refs #1")


class TestShippingGatePromotion:
    def test_red_proof_denies_on_cursor_push(self, git_repo: Path) -> None:
        _commit_source_no_red(git_repo)
        rc = _run(RED_PROOF, cursor_shell("git push", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_red_proof_denies_on_cursor_gh_pr(self, git_repo: Path) -> None:
        _commit_source_no_red(git_repo)
        rc = _run(RED_PROOF, cursor_shell("gh pr create --title x", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_red_proof_advisory_on_claude_push(self, git_repo: Path) -> None:
        _commit_source_no_red(git_repo)
        rc = _run(RED_PROOF, claude_bash("git push"), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_reviewer_sep_denies_on_cursor_push(self, git_repo: Path) -> None:
        # With a resolvable base and no APPROVE artifact for the pushed diff,
        # the Cursor shape hard-blocks.
        _add_upstream_with_change(git_repo)
        rc = _run(REVIEWER_SEP, cursor_shell("git push", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_reviewer_sep_advisory_on_claude_push(self, git_repo: Path) -> None:
        # Same missing-artifact condition, but the Claude shape only warns.
        _add_upstream_with_change(git_repo)
        rc = _run(REVIEWER_SEP, claude_bash("git push"), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_delegation_denies_multifile_on_cursor_push(self, git_repo: Path) -> None:
        for n in ("a.py", "b.py", "c.py"):
            _stage(git_repo, n, "x = 1\n")
        rc = _run(DELEGATION, cursor_shell("git push", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_delegation_advisory_on_claude_push(self, git_repo: Path) -> None:
        for n in ("a.py", "b.py", "c.py"):
            _stage(git_repo, n, "x = 1\n")
        rc = _run(DELEGATION, claude_bash("git push"), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_delegation_denies_chained_push_on_cursor(self, git_repo: Path) -> None:
        # A push reached through a command chain must not bypass the ship gate
        # (boundary-aware is_git_push_or_pr, not a start-anchored match).
        for n in ("a.py", "b.py", "c.py"):
            _stage(git_repo, n, "x = 1\n")
        rc = _run(DELEGATION, cursor_shell("cd . && git push", git_repo), cwd=git_repo)
        assert rc.returncode == BLOCK

    def test_git_push_review_denies_force_no_lease_on_cursor(self, git_repo: Path) -> None:
        force = "--for" + "ce"
        rc = _run(
            GIT_PUSH_REVIEW, cursor_shell(f"git push {force} origin main", git_repo), cwd=git_repo
        )
        assert rc.returncode == BLOCK

    def test_git_push_review_allows_force_with_lease_on_cursor(self, git_repo: Path) -> None:
        lease = "--for" + "ce-with-lease"
        rc = _run(
            GIT_PUSH_REVIEW, cursor_shell(f"git push {lease} origin main", git_repo), cwd=git_repo
        )
        assert rc.returncode == ALLOW

    def test_git_push_review_plain_push_allows_on_cursor(self, git_repo: Path) -> None:
        rc = _run(GIT_PUSH_REVIEW, cursor_shell("git push", git_repo), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_git_push_review_advisory_force_on_claude(self, git_repo: Path) -> None:
        force = "--for" + "ce"
        rc = _run(GIT_PUSH_REVIEW, claude_bash(f"git push {force} origin main"), cwd=git_repo)
        assert rc.returncode == ALLOW


# ── afterFileEdit hooks: real-payload side effects ────────


class TestAfterFileEditHooks:
    def test_post_edit_format_formats_real_file(self, git_repo: Path) -> None:
        if subprocess.run(["bash", "-c", "command -v ruff"], capture_output=True).returncode != 0:
            pytest.skip("ruff not installed")
        f = git_repo / "t.py"
        f.write_text("x=1\n")
        rc = _run(POST_EDIT_FORMAT, cursor_edit(f, "x=1\n", git_repo), cwd=git_repo)
        assert rc.returncode == ALLOW
        assert f.read_text() == "x = 1\n"

    def test_post_edit_format_skips_agent_tools_path(self, git_repo: Path) -> None:
        scratch = Path("/Users/me/.cursor/projects/p/agent-tools/x.py")
        rc = _run(POST_EDIT_FORMAT, cursor_edit(scratch, "x=1\n", git_repo), cwd=git_repo)
        assert rc.returncode == ALLOW

    def test_console_log_warn_detects_print_in_edits(self, git_repo: Path) -> None:
        f = git_repo / "d.py"
        payload = cursor_edit(f, 'print("hi")\n', git_repo)
        rc = _run(CONSOLE_LOG_WARN, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        assert "print()" in rc.stderr

    def test_console_log_warn_skips_agent_tools_path(self, git_repo: Path) -> None:
        scratch = Path("/Users/me/.cursor/projects/p/agent-tools/x.py")
        payload = cursor_edit(scratch, 'print("hi")\n', git_repo)
        rc = _run(CONSOLE_LOG_WARN, payload, cwd=git_repo)
        assert rc.returncode == ALLOW
        assert "print()" not in rc.stderr


# ── review-window-open: exact subagent-identity match ─────


class TestReviewWindowOpenIdentity:
    """The window must open on the subagent's IDENTITY, not on the substring
    "code-review" appearing anywhere in the payload (e.g. inside a planner
    prompt that says "then spawn code-review")."""

    @staticmethod
    def _subagent_payload(root: Path, **fields: str) -> str:
        return json.dumps(
            {
                "hook_event_name": "subagentStart",
                "workspace_roots": [str(root)],
                **fields,
            }
        )

    @staticmethod
    def _window(root: Path) -> Path:
        return root / ".review" / ".window"

    def test_code_review_identity_opens_window(self, git_repo: Path) -> None:
        payload = self._subagent_payload(git_repo, subagent_type="code-review")

        rc = _run(WINDOW_OPEN, payload, cwd=git_repo)

        assert rc.returncode == ALLOW
        assert self._window(git_repo).is_file()

    def test_planner_prompt_mentioning_code_review_does_not_open(self, git_repo: Path) -> None:
        # Identity says planner; the prompt merely MENTIONS code-review.
        payload = self._subagent_payload(
            git_repo,
            subagent_type="planner",
            prompt="plan the feature, then spawn code-review on the diff",
        )

        rc = _run(WINDOW_OPEN, payload, cwd=git_repo)

        assert rc.returncode == ALLOW
        assert not self._window(git_repo).exists()

    def test_agent_field_identity_opens_window(self, git_repo: Path) -> None:
        payload = self._subagent_payload(git_repo, agent="code-review")

        rc = _run(WINDOW_OPEN, payload, cwd=git_repo)

        assert rc.returncode == ALLOW
        assert self._window(git_repo).is_file()

    def test_no_identity_field_falls_back_to_substring(self, git_repo: Path) -> None:
        # Unknown payload shape with no identity field: the historical
        # substring fallback keeps the hook working.
        payload = self._subagent_payload(git_repo, description="code-review run")

        rc = _run(WINDOW_OPEN, payload, cwd=git_repo)

        assert rc.returncode == ALLOW
        assert self._window(git_repo).is_file()

    def test_broken_jq_falls_back_to_substring(self, git_repo: Path, tmp_path: Path) -> None:
        # When jq cannot parse the identity (broken/unavailable jq, simulated
        # by shadowing it with an always-failing stub), the substring grep is
        # the documented fallback so unknown environments keep working.
        stub_bin = tmp_path / "stub-bin"
        stub_bin.mkdir()
        jq_stub = stub_bin / "jq"
        jq_stub.write_text("#!/bin/sh\nexit 127\n")
        jq_stub.chmod(0o755)
        payload = self._subagent_payload(git_repo, subagent_type="code-review")
        env = {k: v for k, v in os.environ.items() if k != "CURSOR_PROJECT_DIR"}
        env["PATH"] = f"{stub_bin}:{env.get('PATH', '/usr/bin:/bin')}"

        rc = subprocess.run(
            ["bash", str(WINDOW_OPEN)],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(git_repo),
            env=env,
        )

        assert rc.returncode == ALLOW
        assert self._window(git_repo).is_file()


# ── Cursor matcher regexes must survive `git -C` / global-option forms ──
#
# Regression guard for a live-tested bypass: Cursor gates a beforeShellExecution
# hook with the `matcher` regex BEFORE invoking the script. A literal matcher
# like "git add|git commit" never fires on the very common `git -C <path> add`
# or `git --no-pager commit` forms, silently disabling secrets-scan /
# config-protection / commit-quality. The production matchers must tolerate
# intervening git global options.


class TestCursorMatchersResistBypass:
    @staticmethod
    def _cursor_matchers() -> dict[str, str]:
        import re as _re
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from hooks_generator import generate_cursor, parse_hooks_metadata

        meta = parse_hooks_metadata(str(HOOKS_DIR / "metadata.yml"))
        cfg = generate_cursor(meta)
        out: dict[str, str] = {}
        for items in cfg["hooks"].values():
            for it in items:
                name = it["command"].split("/")[-1]
                if it.get("matcher"):
                    out[name] = it["matcher"]
        # sanity: ensure the regexes compile
        for pat in out.values():
            _re.compile(pat)
        return out

    @pytest.mark.parametrize(
        ("hook", "command"),
        [
            ("secrets-scan.sh", "git add bad.py"),
            ("secrets-scan.sh", "git -C /tmp/x add bad.py"),
            ("secrets-scan.sh", "git commit -m wip"),
            ("secrets-scan.sh", "git -C /tmp/x commit -m wip"),
            ("secrets-scan.sh", "git --no-pager commit"),
            ("config-protection.sh", "git -C /tmp/x add pyproject.toml"),
            ("commit-quality.sh", "git -C /tmp/x commit -m x"),
            ("commit-gauntlet.sh", "git --no-pager commit -m x"),
            ("git-push-review.sh", "git -C /tmp/x push --force"),
            ("red-proof-warn.sh", "git push origin main"),
            ("reviewer-sep-warn.sh", "gh pr create --title x"),
        ],
    )
    def test_matcher_fires_on_real_command(self, hook: str, command: str) -> None:
        import re

        matchers = self._cursor_matchers()
        assert hook in matchers, f"{hook} has no Cursor matcher"
        assert re.search(matchers[hook], command), (
            f"matcher {matchers[hook]!r} for {hook} fails to match {command!r}"
        )

    @pytest.mark.parametrize(
        ("hook", "command"),
        [
            ("secrets-scan.sh", "git status"),
            ("secrets-scan.sh", "git log --grep=add"),
            ("commit-quality.sh", "git log"),
            ("git-push-review.sh", "git status"),
        ],
    )
    def test_matcher_ignores_unrelated_command(self, hook: str, command: str) -> None:
        import re

        matchers = self._cursor_matchers()
        assert not re.search(matchers[hook], command), (
            f"matcher {matchers[hook]!r} for {hook} should NOT match {command!r}"
        )
