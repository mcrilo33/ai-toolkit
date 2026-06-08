"""Unit tests for the deterministic-cage commit/push hooks.

Each hook is an agent PreToolUse script that reads a JSON payload of the form
``{"tool_input": {"command": "..."}}`` on stdin and signals its decision via
exit code (2 = block, 0 = allow). These tests subprocess the real scripts and
assert the decision, covering the bugs found in live stress-testing:

* escaped-quote normalization in the subject parser (Issue #1)
* the ``Tested-RED:`` typecheck carve-out (Issue #2a)
* changed-line lint scoping (Issue #2b)
* advisory hooks never blocking
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
COMMIT_QUALITY = HOOKS_DIR / "commit-quality.sh"
COMMIT_GAUNTLET = HOOKS_DIR / "commit-gauntlet.sh"
RED_PROOF = HOOKS_DIR / "red-proof-warn.sh"
REVIEWER_SEP = HOOKS_DIR / "reviewer-sep-warn.sh"

BLOCK = 2
ALLOW = 0


def _payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def run_hook(script: Path, command: str, *, cwd: Path | None = None) -> int:
    """Run a hook with a synthesized command payload; return its exit code."""
    result = subprocess.run(
        ["bash", str(script)],
        input=_payload(command),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    return result.returncode


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A standalone git repo, isolated from any parent repo via GIT_CEILING."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "test@test.test")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("seed\n")
    git("add", "README.md")
    git("commit", "-qm", "chore: seed", "-m", "Refs #0")
    return tmp_path


@pytest.fixture()
def on_branch(git_repo: Path) -> Callable[[str], Path]:
    """Factory: check out a named branch in the repo and return the repo path."""

    def _switch(branch: str) -> Path:
        subprocess.run(
            ["git", "checkout", "-q", "-b", branch],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
            text=True,
        )
        return git_repo

    return _switch


# ── commit-quality: conventional format ───────────────────


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param('git commit -m "feat: y" -m "Refs #1"', ALLOW, id="valid-feat"),
        pytest.param('git commit -m "add helper" -m "Refs #1"', BLOCK, id="no-type"),
        pytest.param('git commit -m "feat add helper" -m "Refs #1"', BLOCK, id="missing-colon"),
        pytest.param("git commit -m 'feat: y' -m 'Refs #1'", ALLOW, id="single-quotes"),
        pytest.param('git commit --message="feat: y" -m "Refs #1"', ALLOW, id="long-message-eq"),
        pytest.param('git commit -am "feat: y" -m "Refs #1"', ALLOW, id="combined-am"),
        pytest.param("git commit --amend", ALLOW, id="amend-no-message"),
        pytest.param("git commit -F msg.txt", ALLOW, id="file-message"),
        pytest.param("ls -la", ALLOW, id="non-git"),
    ],
)
def test_commit_quality_format(
    on_branch: Callable[[str], Path], command: str, expected: int
) -> None:
    repo = on_branch("feature/1-x")  # branch carries an anchor; isolates format check
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == expected


def test_commit_quality_escaped_double_quotes_pass(on_branch: Callable[[str], Path]) -> None:
    # Issue #1: a runtime that serializes inner quotes as \"...\" must still parse.
    repo = on_branch("feature/1-x")
    command = r'git commit -m \"feat(core): add helper\" -m \"Refs #1\"'
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == ALLOW


# ── commit-quality: issue-anchor gate ─────────────────────


def test_anchor_missing_blocks(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("no-issue-here")
    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: y"', cwd=repo) == BLOCK


def test_anchor_in_message_passes(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("no-issue-here")
    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: y" -m "Closes #5"', cwd=repo) == ALLOW


def test_anchor_in_branch_passes(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/142-add-login")
    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: y"', cwd=repo) == ALLOW


def test_anchor_substring_keyword_does_not_count(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("no-issue-here")
    # "prefix #5" must not satisfy the "ref" keyword as a substring.
    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: add prefix #5"', cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "branch,expected",
    [
        pytest.param("feature/142-x", ALLOW, id="number-segment"),
        pytest.param("fix/PROJ-12-bug", ALLOW, id="tracker-key"),
        pytest.param("release-2024", BLOCK, id="year-not-issue"),
        pytest.param("feature/oauth-2-factor", BLOCK, id="incidental-number"),
    ],
)
def test_branch_issue_detection(
    on_branch: Callable[[str], Path], branch: str, expected: int
) -> None:
    repo = on_branch(branch)
    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: y"', cwd=repo) == expected


# ── commit-gauntlet: lint scoping + RED carve-out ─────────

ruff = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v ruff"], capture_output=True).returncode != 0,
    reason="ruff not installed",
)


def _stage(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    subprocess.run(["git", "add", rel], cwd=str(repo), check=True, capture_output=True)


@ruff
def test_gauntlet_blocks_lint_error_on_changed_line(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    (repo / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')
    _stage(repo, "pkg/bad.py", "import os\n")  # F401 unused import on a changed line
    assert run_hook(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', cwd=repo) == BLOCK


@ruff
def test_gauntlet_allows_clean_addition(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')
    _stage(repo, "pkg/good.py", "x = 1\n")
    assert run_hook(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', cwd=repo) == ALLOW


def test_gauntlet_degrades_when_tool_absent(on_branch: Callable[[str], Path]) -> None:
    # Graceful degradation means "tool not on PATH", not "no project config":
    # detect_linter falls back to `command -v ruff`, so absence of config alone
    # still lints. Simulate a toolless environment by emptying PATH so no
    # linter/typechecker resolves → nothing to enforce → allow.
    repo = on_branch("feature/1-x")
    _stage(repo, "pkg/whatever.py", "import os\n")  # would be F401 if ruff ran
    result = subprocess.run(
        ["bash", str(COMMIT_GAUNTLET)],
        input=_payload('git commit -m "feat: x" -m "Refs #1"'),
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert result.returncode == ALLOW


def test_gauntlet_no_staged_files_allows(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    assert run_hook(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', cwd=repo) == ALLOW


@ruff
def test_gauntlet_ignores_preexisting_lint_debt_on_unchanged_lines(
    on_branch: Callable[[str], Path],
) -> None:
    # Issue #2b: a file with a pre-existing F401 on line 1; the commit only adds
    # a clean line at the end. Lint is scoped to changed lines → no block.
    repo = on_branch("feature/1-x")
    _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')
    # Commit 1: file already has the unused import (pre-existing debt).
    _stage(repo, "pkg/legacy.py", "import os\n")
    subprocess.run(
        ["git", "commit", "-qm", "chore: legacy", "-m", "Refs #1", "--no-verify"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    # Commit 2: append a clean line; the pre-existing F401 is on an unchanged line.
    (repo / "pkg/legacy.py").write_text("import os\n\n\nCLEAN = 1\n")
    subprocess.run(["git", "add", "pkg/legacy.py"], cwd=str(repo), check=True, capture_output=True)
    assert run_hook(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', cwd=repo) == ALLOW


@ruff
def test_gauntlet_red_commit_skips_typecheck_but_keeps_lint(
    on_branch: Callable[[str], Path],
) -> None:
    # Issue #2a: a RED test commit (Tested-RED trailer) with a clean test file
    # passes — the carve-out means an unresolved import would not block. But a
    # real lint violation on a changed line still blocks even on a RED commit.
    repo = on_branch("feature/1-x")
    _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')

    # Clean RED test → PASS (typecheck would have flagged a missing import, but
    # it is skipped; lint is clean).
    _stage(repo, "tests/test_x.py", "def test_behavior():\n    assert True\n")
    red_cmd = 'git commit -m "test: add failing test" -m "Refs #1" -m "Tested-RED: tests/test_x.py::test_behavior"'
    assert run_hook(COMMIT_GAUNTLET, red_cmd, cwd=repo) == ALLOW

    # Now a RED commit that ALSO has a lint error on a changed line → still BLOCK.
    _stage(repo, "tests/test_y.py", "import os\n\n\ndef test_behavior():\n    assert True\n")
    red_lint_cmd = 'git commit -m "test: add failing test" -m "Refs #1" -m "Tested-RED: tests/test_y.py::test_behavior"'
    assert run_hook(COMMIT_GAUNTLET, red_lint_cmd, cwd=repo) == BLOCK


# ── advisory push hooks never block ───────────────────────


def test_red_proof_never_blocks(git_repo: Path) -> None:
    _stage(git_repo, "pkg/new.py", "def f():\n    return 1\n")
    subprocess.run(
        ["git", "commit", "-qm", "feat: add", "-m", "Refs #1"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    # Commit adds source with no Tested-RED trailer → warns, but must exit 0.
    assert run_hook(RED_PROOF, "git push", cwd=git_repo) == ALLOW


def test_reviewer_sep_never_blocks(git_repo: Path) -> None:
    _stage(git_repo, "pkg/new.py", "x = 1\n")
    subprocess.run(
        ["git", "commit", "-qm", "feat: add", "-m", "Refs #1"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    assert run_hook(REVIEWER_SEP, "git push", cwd=git_repo) == ALLOW
