"""Unit tests for the deterministic-cage commit/push hooks.

Each hook is an agent PreToolUse script that reads a JSON payload of the form
``{"tool_input": {"command": "..."}}`` on stdin and signals its decision via
exit code (2 = block, 0 = allow). These tests subprocess the real scripts and
assert the decision, covering the bugs found in live stress-testing:

* escaped-quote normalization in the subject parser (Issue #1)
* the ``Tested-RED:`` typecheck carve-out (Issue #2a)
* changed-line lint scoping (Issue #2b)
* advisory hooks never blocking
* the strictness spec: configured-but-missing tools, the time budget, and a
  BOOTSTRAP pytest runner now DENY instead of degrading to allow (see
  ``TestStrictGauntlet`` / ``TestStrictRedProof``)
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
COMMIT_QUALITY = HOOKS_DIR / "commit-quality.sh"
COMMIT_GAUNTLET = HOOKS_DIR / "commit-gauntlet.sh"
RED_PROOF = HOOKS_DIR / "red-proof-warn.sh"
RED_PROOF_VERIFY = HOOKS_DIR / "red-proof-verify.sh"
REVIEWER_SEP = HOOKS_DIR / "reviewer-sep-warn.sh"
BLOCK_NO_VERIFY = HOOKS_DIR / "block-no-verify.sh"
SECRETS_SCAN = HOOKS_DIR / "secrets-scan.sh"
SECRETS_SCAN_REVERT = HOOKS_DIR / "secrets-scan-revert.sh"
CONFIG_PROTECTION = HOOKS_DIR / "config-protection.sh"
GIT_PUSH_REVIEW = HOOKS_DIR / "git-push-review.sh"
DELEGATION = HOOKS_DIR / "delegation-gate-warn.sh"
HUB_GUARD = HOOKS_DIR / "hub-guard.sh"
TODO_LEDGER = HOOKS_DIR / "todo-ledger-warn.sh"
LEDGER_GUARD = HOOKS_DIR / "ledger-schema-guard.sh"
UTILS = HOOKS_DIR / "lib" / "utils.sh"

BLOCK = 2
ALLOW = 0


def _payload(command: str) -> str:
    """Claude/Copilot generic shape: command under tool_input."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _cursor_shell_payload(command: str, *, root: Path | None = None) -> str:
    """Cursor beforeShellExecution shape: top-level command + workspace_roots."""
    payload: dict = {
        "hook_event_name": "beforeShellExecution",
        "command": command,
        "cwd": "",
    }
    if root is not None:
        payload["workspace_roots"] = [str(root)]
    return json.dumps(payload)


def _cursor_edit_payload(file_path: Path, new_string: str, *, root: Path | None = None) -> str:
    """Cursor afterFileEdit shape: top-level file_path + edits[]."""
    payload: dict = {
        "hook_event_name": "afterFileEdit",
        "file_path": str(file_path),
        "edits": [{"old_string": "", "new_string": new_string}],
    }
    if root is not None:
        payload["workspace_roots"] = [str(root)]
    return json.dumps(payload)


def _run(script: Path, payload: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )


def run_hook(script: Path, command: str, *, cwd: Path | None = None) -> int:
    """Run a hook with a synthesized (Claude/Copilot) command payload; return exit code."""
    return _run(script, _payload(command), cwd=cwd).returncode


def run_hook_cursor_shell(script: Path, command: str, *, cwd: Path | None = None) -> int:
    """Run a hook with a Cursor beforeShellExecution payload; return exit code."""
    return _run(script, _cursor_shell_payload(command, root=cwd), cwd=cwd).returncode


def _no_stamp_key_env(home: Path) -> dict[str, str]:
    """Controlled hook env (mirrors _hook_env in test_review_stamp.py): strip
    REVIEW_STAMP_KEY so a developer machine's real key never leaks in, and
    redirect HOME so the macOS Keychain lookup cannot resolve one either.
    Without this, reviewer-sep's signature-verification branch activates and
    DENIES the unsigned artifacts these tests write."""
    env = {k: v for k, v in os.environ.items() if k != "REVIEW_STAMP_KEY"}
    env.pop("CURSOR_PROJECT_DIR", None)
    env["HOME"] = str(home)
    return env


def run_reviewer_sep(command: str, repo: Path, *, cursor: bool = True) -> int:
    """Run reviewer-sep-warn in the controlled (key-free) env; return exit code."""
    payload = _cursor_shell_payload(command, root=repo) if cursor else _payload(command)
    return subprocess.run(
        ["bash", str(REVIEWER_SEP)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=_no_stamp_key_env(home=repo),
    ).returncode


def _run_restricted(
    script: Path, command: str, repo: Path, *, cursor: bool = False
) -> subprocess.CompletedProcess:
    """Run a hook with PATH=/usr/bin:/bin — git/jq/bash resolve, but dev tools
    (ruff, eslint, pyright, mypy, tsc, pytest) do not. Simulates the
    'configured but not installed' / 'pytest cannot start' environments."""
    payload = _cursor_shell_payload(command, root=repo) if cursor else _payload(command)
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )


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
    command = r"git commit -m \"feat(core): add helper\" -m \"Refs #1\""
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == ALLOW


@pytest.mark.parametrize(
    "command,branch",
    [
        pytest.param("git commit -m '\"add helper'", "feature/1-x", id="leading-double-anchored"),
        pytest.param('git commit -m "\'add helper"', "feature/1-x", id="leading-single-anchored"),
        pytest.param("git commit -m '\"feat: x'", "wip-no-anchor", id="leading-double-unanchored"),
    ],
)
def test_commit_quality_leading_quote_subject_blocks(
    on_branch: Callable[[str], Path], command: str, branch: str
) -> None:
    # Issue #227: when the subject's first char is a quote, the quote-agnostic
    # extractor captured the EMPTY span between the opening shell quote and the
    # leading content quote, so MSG came out empty and the `-z MSG` guard exited 0 —
    # bypassing BOTH the conventional-format and issue-anchor gates (fail-OPEN). A
    # leading-quote subject is never conventional, so it must BLOCK on both an
    # anchored branch (format gate catches it) and an unanchored one.
    repo = on_branch(branch)
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            "git commit -m \"Merge branch 'feature/188-push-gate-tripwire-scope'\"",
            id="merge-branch",
        ),
        pytest.param('git commit -m "Merge pull request #12 from x/y"', id="merge-pr"),
        pytest.param(
            "git commit -m \"Merge remote-tracking branch 'origin/main'\"",
            id="merge-remote",
        ),
        pytest.param('git commit -m "Revert \\"feat(core): add helper\\""', id="native-revert"),
    ],
)
def test_commit_quality_exempts_git_generated_messages(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # worktree-land's merge into main carries git's own "Merge branch '…'"
    # message; denying it wedges every diverged land (2026-07-08 drain outage).
    repo = on_branch("landing")  # no issue anchor — the exemption alone must allow
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == ALLOW


# ── commit-quality: issue-anchor gate ─────────────────────


def test_commit_quality_chained_commit_not_bypassed(on_branch: Callable[[str], Path]) -> None:
    # Boundary-aware gate: a commit chained after another command must still
    # hit the conventional-commit deny (was bypassed by the ^-anchored grep).
    repo = on_branch("feature/1-x")
    assert run_hook(COMMIT_QUALITY, 'true; git commit -m "bad message"', cwd=repo) == BLOCK


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


# ── commit-quality: docs/chore doc-only exemption (issue #10) ─────────
# Three-lane triage: a sanctioned no-issue path for micro/express spokes. The
# anchor gate must EXEMPT a commit when (1) the subject type is docs or chore
# AND (2) every staged path is a non-executable documentation file (.md,
# .markdown, .txt, .rst) outside top-level scripts/, shared/hooks/, tests/,
# and outside any */scripts/ directory. Everything else stays anchored.


def test_docs_only_md_staged_passes_without_anchor(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/guide.md", "# guide\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: fix wording"', cwd=repo) == ALLOW


def test_chore_only_md_staged_passes_without_anchor(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("claude/micro-docs")
    _stage(repo, "shared/rules/workflow.md", "# workflow\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "chore: tidy rule wording"', cwd=repo) == ALLOW


def test_feat_with_only_md_staged_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # Only docs/chore types are exempt — feat must stay anchored even when the
    # staged set is documentation-only.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/guide.md", "# guide\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "feat: add guide"', cwd=repo) == BLOCK


def test_docs_with_script_staged_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # A staged executable path (wrong extension, top-level scripts/) breaks the
    # allowlist — the whole commit stays anchored.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "scripts/helper.sh", "#!/bin/sh\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: fix wording"', cwd=repo) == BLOCK


def test_docs_with_md_under_tests_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # Right extension, excluded directory: tests/ is never a doc-only path.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "tests/README.md", "# tests\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: explain test layout"', cwd=repo) == BLOCK


def test_docs_with_md_in_skill_scripts_dir_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # Any */scripts/ directory (skill scripts) is excluded even for .md files.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "shared/skills/foo/scripts/README.md", "# scripts\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: describe scripts"', cwd=repo) == BLOCK


def test_docs_with_mixed_staged_files_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # EVERY staged path must be on the allowlist — one stray .py poisons the lot.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")
    _stage(repo, "pkg/b.py", "x = 1\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: update a"', cwd=repo) == BLOCK


def test_docs_with_nothing_staged_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # Fail closed: the hook reads the index at PreToolUse time, so a
    # `git commit -am` style command sees an EMPTY index here — an empty staged
    # set must not count as "all documentation".
    repo = on_branch("claude/micro-docs")

    assert run_hook(COMMIT_QUALITY, 'git commit -am "docs: fix wording"', cwd=repo) == BLOCK


def test_docs_dash_a_with_dirty_script_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # `git commit -am` commits the index PLUS every dirty tracked file — the
    # staged set the hook inspects is NOT what this command commits. A dirty
    # tracked script must poison the exemption even when only a doc is staged.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "tools/deploy.sh", "#!/bin/sh\n")
    subprocess.run(
        ["git", "commit", "-qm", "chore: tool", "-m", "Refs #1"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "tools" / "deploy.sh").write_text("#!/bin/sh\necho changed\n")  # dirty, NOT staged
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -am "docs: fix wording"', cwd=repo) == BLOCK


def test_docs_pathspec_form_still_requires_anchor(on_branch: Callable[[str], Path]) -> None:
    # `git commit -m ... <pathspec>` bypasses the index entirely and commits
    # the named worktree paths — the staged doc-only set is irrelevant.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    command = 'git commit -m "docs: fix wording" tools/deploy.sh'
    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


def test_docs_amend_still_requires_anchor(on_branch: Callable[[str], Path]) -> None:
    # `--amend -m "..."` HAS a parseable subject, so it reaches the gate (the
    # no-message early-allow does not apply). An amend rewrites the previous
    # commit — the staged doc is not what it commits — so no exemption.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, 'git commit --amend -m "docs: x"', cwd=repo) == BLOCK


def test_docs_executable_md_still_requires_anchor(on_branch: Callable[[str], Path]) -> None:
    # "Non-executable documentation" must be literal: a .md staged with mode
    # 100755 is executable and must not ride the exemption.
    repo = on_branch("claude/micro-docs")
    doc = repo / "docs" / "a.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# a\n")
    os.chmod(doc, 0o755)
    subprocess.run(["git", "add", "docs/a.md"], cwd=str(repo), check=True, capture_output=True)

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: x"', cwd=repo) == BLOCK


def test_docs_symlink_md_still_requires_anchor(on_branch: Callable[[str], Path]) -> None:
    # A symlink staged as docs/link.md (mode 120000) is not a documentation
    # FILE — it can point anywhere, so it must not ride the exemption.
    repo = on_branch("claude/micro-docs")
    link = repo / "docs" / "link.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink("../README.md", link)
    subprocess.run(["git", "add", "docs/link.md"], cwd=str(repo), check=True, capture_output=True)

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: x"', cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("docs/n.markdown", id="markdown"),
        pytest.param("docs/n.txt", id="txt"),
        pytest.param("docs/n.rst", id="rst"),
    ],
)
def test_docs_only_alternate_doc_extensions_pass_without_anchor(
    on_branch: Callable[[str], Path], rel: str
) -> None:
    # Pins the extension alternation: .markdown/.txt/.rst are doc-only too.
    repo = on_branch("claude/micro-docs")
    _stage(repo, rel, "notes\n")

    assert run_hook(COMMIT_QUALITY, 'git commit -m "docs: x"', cwd=repo) == ALLOW


def test_docs_only_cursor_shape_passes_without_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # Pins the PROJECT_ROOT-based staged lookup: Cursor's beforeShellExecution
    # payload has an empty cwd, so the index must resolve via workspace_roots.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook_cursor_shell(COMMIT_QUALITY, 'git commit -m "docs: x"', cwd=repo) == ALLOW


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'git commit --pathspec-from-file=paths.txt -m "docs: tidy"',
            id="pathspec-from-file",
        ),
        pytest.param(
            'git commit --pathspec-from-file=paths.txt --pathspec-file-nul -m "docs: tidy"',
            id="pathspec-from-file-nul",
        ),
    ],
)
def test_docs_pathspec_from_file_still_requires_anchor(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # `--pathspec-from-file` delivers the pathspec via a FILE, so no bare
    # trailing token appears in the command — the trailing-pathspec detector
    # never fires and the exact long-option denylist does not list it. Like an
    # inline pathspec, it commits the named worktree paths and bypasses the
    # staged doc-only set entirely, so it must stay anchored.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")
    (repo / "README.md").write_text("seed\nchanged\n")  # dirty, NOT staged
    (repo / "paths.txt").write_text("README.md\n")

    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git commit --amen -m "docs: x"', id="amen"),
        pytest.param('git commit --am -m "docs: x"', id="am"),
        pytest.param('git commit --patc -m "docs: x"', id="patc"),
    ],
)
def test_docs_long_option_abbreviation_still_requires_anchor(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # Git accepts unambiguous long-option PREFIXES: --amen/--am mean --amend
    # and --patc means --patch. The denylist matches only the exact spellings,
    # and the short-cluster regex requires whitespace before a single dash, so
    # these abbreviations slip past both — yet they commit something other than
    # the staged doc-only set and must stay anchored.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


def test_docs_pathspec_named_commit_still_requires_anchor(
    on_branch: Callable[[str], Path],
) -> None:
    # A pathspec literally named "commit" must not read as a second subcommand
    # token: only the FIRST `commit` is the subcommand; a later one is a bare
    # pathspec and disqualifies the exemption like any other.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "commit", "tracked file named commit\n")
    subprocess.run(
        ["git", "commit", "-qm", "chore: seed file", "-m", "Refs #1"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "commit").write_text("dirty, NOT staged\n")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, 'git commit commit -m "docs: x"', cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git commit "-a" -m "docs: x"', id="quoted-a"),
        pytest.param('git commit "--amend" -m "docs: x"', id="quoted-amend"),
        pytest.param('git commit -m "docs: x" "evil.sh"', id="quoted-pathspec"),
    ],
)
def test_docs_quoted_flag_still_requires_anchor(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # The hook strips quoted strings before scanning for dangerous flags and
    # bare pathspecs, so quoting a token ("-a", "--amend", "evil.sh") erases it
    # from the scan — but the shell unquotes and git honors it, committing
    # something other than the staged doc-only set. Quoting must not launder a
    # disqualifying token.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('git add -A && git commit -m "docs: x"', id="add-all"),
        pytest.param('git add app.py; git commit -m "docs: x"', id="add-file"),
        pytest.param('git stash pop; git commit -m "docs: x"', id="stash-pop"),
        pytest.param('echo hi && git commit -m "docs: x"', id="echo"),
    ],
)
def test_docs_pre_commit_chain_still_requires_anchor(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # Tokens BEFORE `commit` are never inspected, yet a chained prefix command
    # (git add, stash pop, anything) runs first and mutates the index AFTER the
    # hook reads it — the doc-only staged set the hook approved is not what the
    # commit captures. Any chained prefix must disqualify the exemption.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'git commit -m "docs: x" -m "second paragraph"',
            id="multi-m",
        ),
        pytest.param('git commit --message="docs: x"', id="message-eq"),
        pytest.param(
            'git commit -m "docs: x" --author="A B <a@b.c>"',
            id="author",
        ),
    ],
)
def test_docs_multi_m_and_metadata_flags_keep_exemption(
    on_branch: Callable[[str], Path], command: str
) -> None:
    # Retention pins for the upcoming stricter fix: legitimate message-only
    # shapes (multi -m body, --message=, --author=) commit exactly the staged
    # doc-only set and must KEEP the exemption.
    repo = on_branch("claude/micro-docs")
    _stage(repo, "docs/a.md", "# a\n")

    assert run_hook(COMMIT_QUALITY, command, cwd=repo) == ALLOW


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


def test_gauntlet_unconfigured_repo_allows_when_tools_absent(
    on_branch: Callable[[str], Path],
) -> None:
    # UNCONFIGURED side of the configured-vs-unconfigured split: this repo has
    # NO linter/typechecker config (no ruff.toml, no pyproject [tool.ruff], no
    # eslint/pyright/tsconfig) and a PATH on which no tool resolves. It never
    # opted in to any check → nothing to enforce → allow. Contrast with
    # TestStrictGauntlet, where a CONFIGURED-but-missing tool must DENY.
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
    assert run_reviewer_sep("git push", git_repo, cursor=False) == ALLOW


# ── Cursor beforeShellExecution shape: commit hooks parity ─
# The same blocking decisions must hold when the command arrives at the TOP
# LEVEL (Cursor's dedicated event) rather than under tool_input (Claude/Copilot).


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param('git commit -m "feat: y" -m "Refs #1"', ALLOW, id="valid-feat"),
        pytest.param('git commit -m "add helper" -m "Refs #1"', BLOCK, id="no-type"),
        pytest.param("ls -la", ALLOW, id="non-git"),
    ],
)
def test_commit_quality_cursor_shape(
    on_branch: Callable[[str], Path], command: str, expected: int
) -> None:
    repo = on_branch("feature/1-x")
    assert run_hook_cursor_shell(COMMIT_QUALITY, command, cwd=repo) == expected


def test_commit_quality_cursor_escaped_quotes(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    command = r"git commit -m \"feat(core): add helper\" -m \"Refs #1\""
    assert run_hook_cursor_shell(COMMIT_QUALITY, command, cwd=repo) == ALLOW


@ruff
def test_gauntlet_cursor_shape_blocks_lint_error(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')
    _stage(repo, "pkg/bad.py", "import os\n")  # F401 unused import
    assert (
        run_hook_cursor_shell(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', cwd=repo)
        == BLOCK
    )


def test_block_no_verify_cursor_shape(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    flag = "--no-" + "verify"
    assert run_hook_cursor_shell(BLOCK_NO_VERIFY, f"git commit {flag}", cwd=repo) == BLOCK


# ── red-proof-verify: execute the Tested-RED node at commit time ──────
# A declared RED node must FAIL at the RED commit (allow). If it PASSES, the
# test drives no new code → block. If pytest cannot run (BOOTSTRAP) → block:
# declaring a pytest node claims pytest works, so a broken runner must be
# fixed, not skipped (see TestStrictRedProof).

pytest_runner = pytest.mark.skipif(
    subprocess.run(["bash", "-c", "command -v pytest"], capture_output=True).returncode != 0,
    reason="pytest not on PATH",
)


@pytest_runner
def test_red_verify_allows_when_node_fails(on_branch: Callable[[str], Path]) -> None:
    # A genuine RED test: imports a symbol that does not exist yet → pytest
    # reports the node as failing (collection error, exit 1) → RED proven → allow.
    repo = on_branch("feature/1-x")
    _stage(
        repo,
        "tests/test_feature.py",
        "from pkg.feature import compute\n\n\ndef test_compute():\n    assert compute() == 42\n",
    )
    cmd = (
        'git commit -m "test: add failing test" -m "Refs #1" '
        '-m "Tested-RED: tests/test_feature.py::test_compute"'
    )
    assert run_hook(RED_PROOF_VERIFY, cmd, cwd=repo) == ALLOW


@pytest_runner
def test_red_verify_blocks_when_node_passes(on_branch: Callable[[str], Path]) -> None:
    # A "RED" test that actually passes asserts already-existing behavior — it
    # cannot be driving new code, so the commit must be blocked.
    repo = on_branch("feature/1-x")
    _stage(
        repo,
        "tests/test_trivial.py",
        "def test_trivial():\n    assert True\n",
    )
    cmd = (
        'git commit -m "test: add test" -m "Refs #1" '
        '-m "Tested-RED: tests/test_trivial.py::test_trivial"'
    )
    assert run_hook(RED_PROOF_VERIFY, cmd, cwd=repo) == BLOCK


def test_red_verify_no_trailer_allows(on_branch: Callable[[str], Path]) -> None:
    # No Tested-RED trailer → nothing to verify → allow (no pytest needed).
    repo = on_branch("feature/1-x")
    _stage(repo, "tests/test_x.py", "def test_x():\n    assert True\n")
    cmd = 'git commit -m "test: add" -m "Refs #1"'
    assert run_hook(RED_PROOF_VERIFY, cmd, cwd=repo) == ALLOW


def test_red_verify_blocks_when_pytest_absent(on_branch: Callable[[str], Path]) -> None:
    # STRICTNESS change (was: degrade to allow). With no pytest resolvable
    # (restricted PATH, no importable module), the node cannot run → BOOTSTRAP.
    # Declaring a Tested-RED pytest node claims pytest works; a broken runner
    # must be fixed, not skipped → DENY, with a message saying to fix the env.
    repo = on_branch("feature/1-x")
    _stage(repo, "tests/test_trivial.py", "def test_trivial():\n    assert True\n")
    cmd = (
        'git commit -m "test: add" -m "Refs #1" '
        '-m "Tested-RED: tests/test_trivial.py::test_trivial"'
    )

    result = _run_restricted(RED_PROOF_VERIFY, cmd, repo)

    assert result.returncode == BLOCK
    assert "fix" in result.stderr.lower()


def test_red_verify_non_commit_allows(on_branch: Callable[[str], Path]) -> None:
    repo = on_branch("feature/1-x")
    assert run_hook(RED_PROOF_VERIFY, "ls -la", cwd=repo) == ALLOW


@pytest_runner
def test_red_verify_breach_blocks_and_restores(on_branch: Callable[[str], Path]) -> None:
    # Issue #31 backstop tripwire: a Tested-RED node that escapes isolation and
    # mutates THIS repo (flips core.bare) must trip the tripwire — the commit is
    # blocked with a breach message and the repo is restored, rather than the
    # corruption persisting silently as in the #29/#30 incident.
    repo = on_branch("feature/1-x")
    _stage(
        repo,
        "tests/test_escape.py",
        "import subprocess\n\n\n"
        "def test_escape():\n"
        "    subprocess.run(['git', 'config', 'core.bare', 'true'], check=False)\n",
    )
    cmd = (
        'git commit -m "test: add" -m "Refs #1" -m "Tested-RED: tests/test_escape.py::test_escape"'
    )

    result = _run(RED_PROOF_VERIFY, _payload(cmd), cwd=repo)

    assert result.returncode == BLOCK
    assert "breach" in result.stderr.lower()
    bare = subprocess.run(
        ["git", "config", "--get", "core.bare"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    assert bare == "false"  # the leaked flip was rolled back


# ── red-proof-warn: GREEN backstop (Tested-RED node must pass now) ────


@pytest_runner
def test_red_proof_green_backstop_blocks_failing_node(git_repo: Path) -> None:
    # A committed Tested-RED node that still FAILS at push time means the shipped
    # code does not satisfy its test. On the Cursor shell shape this hard-blocks.
    _stage(
        git_repo,
        "tests/test_unsatisfied.py",
        "from pkg.thing import go\n\n\ndef test_go():\n    assert go() == 1\n",
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-qm",
            "test: add",
            "-m",
            "Refs #1",
            "-m",
            "Tested-RED: tests/test_unsatisfied.py::test_go",
            "--no-verify",
        ],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    assert run_hook_cursor_shell(RED_PROOF, "git push", cwd=git_repo) == BLOCK


@pytest_runner
def test_red_proof_green_backstop_allows_passing_node(git_repo: Path) -> None:
    # The implementation exists and the Tested-RED node passes → backstop clears.
    # The same commit adds source with the trailer, so the presence check also
    # passes; the push is allowed.
    _stage(git_repo, "pkg/__init__.py", "")
    _stage(git_repo, "pkg/thing.py", "def go():\n    return 1\n")
    _stage(
        git_repo,
        "tests/test_satisfied.py",
        "from pkg.thing import go\n\n\ndef test_go():\n    assert go() == 1\n",
    )
    subprocess.run(
        [
            "git",
            "commit",
            "-qm",
            "feat: add thing",
            "-m",
            "Refs #1",
            "-m",
            "Tested-RED: tests/test_satisfied.py::test_go",
            "--no-verify",
        ],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    assert run_hook_cursor_shell(RED_PROOF, "git push", cwd=git_repo) == ALLOW


# ── reviewer-sep-warn: diff-bound APPROVE artifact gate ───────────────
# The hook recomputes the hash of the pushed range (BASE..HEAD) and ships only
# if .review/<hash>.json carries an APPROVE verdict. No upstream → degrade allow.


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def repo_with_upstream(tmp_path: Path) -> Path:
    """A repo whose feature branch tracks an upstream, with one unpushed commit.

    Mirrors the real workflow: seed is on the upstream; the feature branch adds
    one commit that the reviewer-sep hook must adjudicate against the merge-base.
    """
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    for k, v in (
        ("user.email", "t@t.t"),
        ("user.name", "t"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        _git(work, "config", k, v)
    (work / "README.md").write_text("seed\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "-u", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "feature/1-x")
    # One unpushed change on the feature branch.
    (work / "app.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-qm", "feat: add", "-m", "Refs #1", "--no-verify")
    _git(work, "branch", "--set-upstream-to=origin/main", "feature/1-x")
    return work


def _range_hash(repo: Path) -> str:
    """Compute the push-time range hash the hook will compute (BASE..HEAD)."""
    base = _git(repo, "merge-base", "@{upstream}", "HEAD").strip()
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "-M",
            f"{base}..HEAD",
            "--",
            ".",
            ":(exclude).review/",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    normalized = diff.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _write_artifact(repo: Path, diff_hash: str, verdict: str) -> None:
    review = repo / ".review"
    review.mkdir(exist_ok=True)
    (review / f"{diff_hash}.json").write_text(
        json.dumps({"schema_version": 1, "diff_hash": diff_hash, "verdict": verdict})
    )


def test_reviewer_sep_blocks_when_no_artifact(repo_with_upstream: Path) -> None:
    # No .review/ artifact for the pushed diff → blocked on the Cursor shape.
    assert run_reviewer_sep("git push", repo_with_upstream) == BLOCK


def test_reviewer_sep_allows_with_matching_approve(repo_with_upstream: Path) -> None:
    _write_artifact(repo_with_upstream, _range_hash(repo_with_upstream), "APPROVE")
    assert run_reviewer_sep("git push", repo_with_upstream) == ALLOW


def test_reviewer_sep_blocks_request_changes(repo_with_upstream: Path) -> None:
    _write_artifact(repo_with_upstream, _range_hash(repo_with_upstream), "REQUEST_CHANGES")
    assert run_reviewer_sep("git push", repo_with_upstream) == BLOCK


def test_reviewer_sep_blocks_stale_artifact_for_other_diff(repo_with_upstream: Path) -> None:
    # An APPROVE bound to a DIFFERENT hash does not satisfy this diff.
    _write_artifact(repo_with_upstream, "0" * 64, "APPROVE")
    assert run_reviewer_sep("git push", repo_with_upstream) == BLOCK


def test_reviewer_sep_no_upstream_degrades_allow(git_repo: Path) -> None:
    # No tracked upstream → cannot compute base → degrade to allow (no block).
    _stage(git_repo, "app.py", "x = 1\n")
    subprocess.run(
        ["git", "commit", "-qm", "feat: x", "-m", "Refs #1", "--no-verify"],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    assert run_reviewer_sep("git push", git_repo) == ALLOW


def test_reviewer_sep_non_push_allows(repo_with_upstream: Path) -> None:
    assert run_reviewer_sep("ls -la", repo_with_upstream) == ALLOW


def test_reviewer_sep_chained_push_not_bypassed(repo_with_upstream: Path) -> None:
    # Boundary-aware gate: a push chained after another command must still be
    # adjudicated (was bypassed by the ^-anchored grep). No artifact → block.
    assert run_reviewer_sep("cd /tmp && git push", repo_with_upstream) == BLOCK


# ── STRICTNESS SPEC: commit-gauntlet must fail closed ─────────────────
# A repo that CONFIGURES a linter/typechecker (config file present) has opted
# in to that gate. If the binary is then missing from PATH while staged files
# match its extensions, silently skipping the check is fail-open — the hook
# must DENY and name the tool. A repo with NO config never opted in and still
# allows (test_gauntlet_unconfigured_repo_allows_when_tools_absent above).


def _fake_tsc(repo: Path, output: str) -> None:
    """Install a fake node_modules/.bin/tsc that prints `output` and exits 1.

    The gauntlet resolves tsc through the project-local node_modules/.bin
    before PATH, so this works under the restricted-PATH harness too."""
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "tsc"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(output)}\nexit 1\n")
    fake.chmod(0o755)


class TestStrictGauntlet:
    def test_configured_linter_missing_blocks_matching_staged_files(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # ruff is CONFIGURED (ruff.toml on disk) but not on the restricted
        # PATH, and a .py file is staged → deny, naming the tool and saying it
        # must be installed.
        repo = on_branch("feature/1-x")
        (repo / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
        _stage(repo, "pkg/mod.py", "x = 1\n")

        result = _run_restricted(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', repo)

        assert result.returncode == BLOCK
        assert "ruff" in result.stderr.lower()
        assert "install" in result.stderr.lower()

    def test_configured_linter_missing_allows_non_matching_staged_files(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # Same configured-but-missing ruff, but only a .md file is staged —
        # ruff would never check it, so there is nothing to enforce → allow.
        repo = on_branch("feature/1-x")
        (repo / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
        _stage(repo, "docs/notes.md", "# notes\n")

        result = _run_restricted(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', repo)

        assert result.returncode == ALLOW

    def test_configured_typechecker_missing_blocks_matching_staged_files(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # pyright is CONFIGURED (pyrightconfig.json) but not on the restricted
        # PATH, and a .py file is staged → deny, naming the tool.
        repo = on_branch("feature/1-x")
        (repo / "pyrightconfig.json").write_text("{}\n")
        _stage(repo, "pkg/mod.py", "x = 1\n")

        result = _run_restricted(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', repo)

        assert result.returncode == BLOCK
        assert "pyright" in result.stderr.lower()
        assert "install" in result.stderr.lower()

    def test_red_commit_still_skips_missing_typechecker(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # The RED carve-out stands: a Tested-RED commit skips typecheck
        # entirely, so a configured-but-missing typechecker cannot block it.
        repo = on_branch("feature/1-x")
        (repo / "pyrightconfig.json").write_text("{}\n")
        _stage(repo, "tests/test_x.py", "def test_x():\n    assert True\n")
        cmd = 'git commit -m "test: add" -m "Refs #1" -m "Tested-RED: tests/test_x.py::test_x"'

        result = _run_restricted(COMMIT_GAUNTLET, cmd, repo)

        assert result.returncode == ALLOW

    def test_red_commit_still_blocked_by_missing_configured_linter(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # Lint strictness applies even to RED commits — only typecheck is
        # carved out. Configured-but-missing ruff + staged .py → deny.
        repo = on_branch("feature/1-x")
        (repo / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
        _stage(repo, "tests/test_x.py", "def test_x():\n    assert True\n")
        cmd = 'git commit -m "test: add" -m "Refs #1" -m "Tested-RED: tests/test_x.py::test_x"'

        result = _run_restricted(COMMIT_GAUNTLET, cmd, repo)

        assert result.returncode == BLOCK
        assert "ruff" in result.stderr.lower()

    def test_type_error_with_cannot_find_wording_blocks(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # Regression (fail-open): tsc's `error TS2304: Cannot find name 'x'`
        # contains "cannot find", which the bootstrap classifier used to match
        # — misclassifying a GENUINE type error as a bootstrap failure and
        # degrading to warn + SKIP. A real type error must DENY.
        repo = on_branch("feature/1-x")
        (repo / "tsconfig.json").write_text("{}\n")
        _fake_tsc(repo, "src/app.ts(1,1): error TS2304: Cannot find name 'x'.")
        _stage(repo, "src/app.ts", "x;\n")

        result = _run_restricted(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', repo)

        assert result.returncode == BLOCK
        assert "TS2304" in result.stderr

    def test_true_bootstrap_failure_still_skips_typecheck(
        self, on_branch: Callable[[str], Path]
    ) -> None:
        # The bootstrap carve-out survives the tightened classifier: a
        # typechecker that cannot START (ENOENT on its cache, not a type
        # error) still degrades to warn + SKIP → allow.
        repo = on_branch("feature/1-x")
        (repo / "tsconfig.json").write_text("{}\n")
        _fake_tsc(repo, "Error: ENOENT: no such file or directory, open '/x/.cache/tsc'")
        _stage(repo, "src/app.ts", "const a = 1;\n")

        result = _run_restricted(COMMIT_GAUNTLET, 'git commit -m "feat: x" -m "Refs #1"', repo)

        assert result.returncode == ALLOW
        assert "skip" in result.stderr.lower()

    @ruff
    def test_budget_trip_blocks_with_split_hint(self, on_branch: Callable[[str], Path]) -> None:
        # AI_TOOLKIT_GAUNTLET_BUDGET (default 55) exists for testability and is
        # NOT a bypass vector: raising it only INCREASES how much gets checked
        # before the trip; lowering it only causes MORE blocking — neither
        # direction fails open. Budget 0 trips instantly: with 2+ lintable
        # files staged and a configured+installed linter, the trip must DENY
        # (was: warn + skip remaining + allow) and advise splitting the commit.
        repo = on_branch("feature/1-x")
        _stage(repo, "ruff.toml", '[lint]\nselect = ["F"]\n')
        _stage(repo, "pkg/a.py", "x = 1\n")
        _stage(repo, "pkg/b.py", "y = 2\n")
        env = {**os.environ, "AI_TOOLKIT_GAUNTLET_BUDGET": "0"}
        env.pop("CURSOR_PROJECT_DIR", None)  # must not override temp-repo resolution

        result = subprocess.run(
            ["bash", str(COMMIT_GAUNTLET)],
            input=_payload('git commit -m "feat: x" -m "Refs #1"'),
            capture_output=True,
            text=True,
            cwd=str(repo),
            env=env,
        )

        assert result.returncode == BLOCK
        assert "split" in result.stderr.lower()


# ── STRICTNESS SPEC: red-proof-warn GREEN backstop on BOOTSTRAP ───────
# At push, a Tested-RED node that cannot run (BOOTSTRAP) now goes through
# ship_gate_enforce instead of warn+skip: hard deny on Cursor payloads,
# advisory elsewhere. PASS/FAIL backstop behavior is unchanged (pinned by
# test_red_proof_green_backstop_* above).


def _commit_with_red_trailer(repo: Path) -> None:
    _stage(repo, "tests/test_pending.py", "def test_pending():\n    assert True\n")
    subprocess.run(
        [
            "git",
            "commit",
            "-qm",
            "test: add",
            "-m",
            "Refs #1",
            "-m",
            "Tested-RED: tests/test_pending.py::test_pending",
            "--no-verify",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


class TestStrictRedProof:
    def test_green_backstop_bootstrap_blocks_on_cursor_push(self, git_repo: Path) -> None:
        # STRICTNESS change (was: warn + skip). The restricted PATH has no
        # pytest, so the declared node returns BOOTSTRAP — on a Cursor
        # beforeShellExecution payload ship_gate_enforce hard-denies.
        _commit_with_red_trailer(git_repo)

        result = _run_restricted(RED_PROOF, "git push", git_repo, cursor=True)

        assert result.returncode == BLOCK

    def test_green_backstop_bootstrap_advisory_on_claude_push(self, git_repo: Path) -> None:
        # On non-Cursor payloads ship_gate_enforce stays advisory: the
        # BOOTSTRAP node is reported on stderr but the push is allowed.
        _commit_with_red_trailer(git_repo)

        result = _run_restricted(RED_PROOF, "git push", git_repo, cursor=False)

        assert result.returncode == ALLOW
        assert result.stderr != ""


# ── hub-guard: enforce the planning-hub invariant ─────────────────────
# The main checkout (the planning hub) stays on the default branch and never
# holds task work. hub-guard DENIES edits/commits/branch-creation there, and is
# a no-op inside any linked worktree or on any non-default branch.


def _write_payload(file_path: Path, content: str = "x\n") -> str:
    """Claude Write tool shape: file_path + content under tool_input."""
    return json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(file_path), "content": content}}
    )


def _tool_payload(tool_name: str, tool_input: dict) -> str:
    """Generic Claude tool-call shape for an arbitrary tool."""
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


def _hub_env() -> dict[str, str]:
    """Hook env with CURSOR_PROJECT_DIR stripped so the project-root resolution
    falls to the payload/cwd. Without this, a Cursor-driven test run would point
    every hub-guard probe at the IDE's project dir and flip the verdict (cf.
    _no_stamp_key_env)."""
    # AI_TOOLKIT_BASE_BRANCH is also stripped: the host's base-branch override
    # (#117) must never steer the guard under test.
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("CURSOR_PROJECT_DIR", "AI_TOOLKIT_BASE_BRANCH")
    }


def run_hub_guard(payload: str, *, cwd: Path) -> subprocess.CompletedProcess:
    """Run hub-guard with an explicit payload in a CURSOR_PROJECT_DIR-free env."""
    return subprocess.run(
        ["bash", str(HUB_GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_hub_env(),
    )


def hub_guard_edit_rc(file_path: Path, *, cwd: Path) -> int:
    return run_hub_guard(_write_payload(file_path), cwd=cwd).returncode


def hub_guard_cmd_rc(command: str, *, cwd: Path) -> int:
    return run_hub_guard(_payload(command), cwd=cwd).returncode


@pytest.fixture()
def linked_worktree(git_repo: Path, tmp_path: Path) -> Path:
    """A linked git worktree of git_repo, on its own task branch."""
    wt = tmp_path / "spoke"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature/9-spoke", str(wt)],
        cwd=str(git_repo),
        check=True,
        capture_output=True,
    )
    return wt


@pytest.fixture()
def hub_on_master(git_repo: Path) -> Path:
    """The hub repo with its default branch renamed to master and no origin —
    reproducing a CI ubuntu runner (bare `git init` → master). Pins that the
    guard resolves the real default rather than assuming a literal "main"."""
    subprocess.run(
        ["git", "branch", "-m", "master"], cwd=str(git_repo), check=True, capture_output=True
    )
    return git_repo


class TestHubGuard:
    # ── On the hub (main checkout, default branch): deny ──────────────

    def test_blocks_edit_on_hub(self, git_repo: Path) -> None:
        assert hub_guard_edit_rc(git_repo / "notes.md", cwd=git_repo) == BLOCK

    def test_blocks_commit_on_hub(self, git_repo: Path) -> None:
        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=git_repo) == BLOCK

    def test_blocks_notebook_edit_on_hub(self, git_repo: Path) -> None:
        # NotebookEdit carries notebook_path (no file_path) — name-based dispatch
        # must still catch it.
        payload = _tool_payload(
            "NotebookEdit", {"notebook_path": str(git_repo / "nb.ipynb"), "new_source": "x"}
        )
        assert run_hub_guard(payload, cwd=git_repo).returncode == BLOCK

    @pytest.mark.parametrize(
        "command",
        [
            "git checkout -b feature/1-x",
            "git switch -c feature/1-x",
            "git checkout -B feature/1-x",
            "git switch --create feature/1-x",
            "git switch --force-create feature/1-x",
            "git checkout --orphan feature/1-x",
            "git branch feature/1-x",  # bare create
            "true && git checkout -b feature/1-x",  # chained must not bypass
        ],
    )
    def test_blocks_branch_create_on_hub(self, git_repo: Path, command: str) -> None:
        assert hub_guard_cmd_rc(command, cwd=git_repo) == BLOCK

    def test_blocks_create_file_tool_on_hub(self, git_repo: Path) -> None:
        # A "create"-named tool that writes a file (carries file_path) is denied.
        payload = _tool_payload("create_file", {"file_path": str(git_repo / "x.py")})
        assert run_hub_guard(payload, cwd=git_repo).returncode == BLOCK

    def test_blocks_edit_on_master_default_without_origin(self, hub_on_master: Path) -> None:
        # The guard must resolve master as the default, not assume "main".
        assert hub_guard_edit_rc(hub_on_master / "notes.md", cwd=hub_on_master) == BLOCK

    def test_deny_message_points_to_start_task(self, git_repo: Path) -> None:
        result = run_hub_guard(_write_payload(git_repo / "notes.md"), cwd=git_repo)
        assert result.returncode == BLOCK
        assert "start-task" in result.stderr.lower()

    # ── Still allowed on the hub: non-mutating / sanctioned actions ───

    def test_allows_plain_command_on_hub(self, git_repo: Path) -> None:
        assert hub_guard_cmd_rc("ls -la", cwd=git_repo) == ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            "git checkout main",  # switch to an existing branch (no -b/-c)
            "git branch",  # list branches
            "git branch -a",  # list all
            "git branch -d old-branch",  # delete
            "git branch --list 'feature/*'",  # list with pattern
        ],
    )
    def test_allows_non_creating_branch_ops_on_hub(self, git_repo: Path, command: str) -> None:
        assert hub_guard_cmd_rc(command, cwd=git_repo) == ALLOW

    def test_allows_read_tool_on_hub(self, git_repo: Path) -> None:
        # A read-only tool (no command) must not be denied — matters under
        # Copilot, where this hook carries no matcher and sees every tool.
        payload = _tool_payload("Read", {"file_path": str(git_repo / "README.md")})
        assert run_hub_guard(payload, cwd=git_repo).returncode == ALLOW

    def test_allows_create_issue_tool_on_hub(self, git_repo: Path) -> None:
        # A "create"-named tool with NO file_path (create_issue, create_pull_request)
        # is hub planning work, not a file write — must not be collateral-blocked.
        payload = _tool_payload("create_issue", {"title": "x", "body": "y"})
        assert run_hub_guard(payload, cwd=git_repo).returncode == ALLOW

    def test_allows_write_outside_repo_on_hub(
        self, git_repo: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        # Writing a scratch file outside the repo (e.g. a /tmp issue body) is not
        # hub task work on the repo, so it is allowed. The dir is a sibling of
        # git_repo (which is itself the test's tmp_path), so it is truly outside.
        outside = tmp_path_factory.mktemp("elsewhere") / "body.md"
        assert hub_guard_edit_rc(outside, cwd=git_repo) == ALLOW

    def test_allows_worktree_add_on_hub(self, git_repo: Path, tmp_path: Path) -> None:
        # Spawning a worktree is the sanctioned dispatch path — never blocked.
        dest = tmp_path / "wt"
        assert hub_guard_cmd_rc(f"git worktree add -b feature/2-x {dest}", cwd=git_repo) == ALLOW

    def test_allows_edit_during_merge_on_hub(self, git_repo: Path) -> None:
        # A merge in progress is sanctioned hub work; resolution needs edits.
        (git_repo / ".git" / "MERGE_HEAD").write_text(f"{'0' * 40}\n")
        assert hub_guard_edit_rc(git_repo / "notes.md", cwd=git_repo) == ALLOW

    # ── The hub-guard-allow escape-hatch marker (issue #89) ───────────
    # A `hub-guard-allow` file in the git-dir is the conscious, user-granted
    # override the /quick lane uses to let the hub session commit into a
    # worktree. While present it bypasses EVERY guard check (incl. on the hub's
    # default branch); removing it restores the default deny — nothing else
    # changes.

    def test_marker_allows_commit_on_hub(self, git_repo: Path) -> None:
        (git_repo / ".git" / "hub-guard-allow").write_text("")
        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=git_repo) == ALLOW

    def test_marker_allows_branch_create_on_hub(self, git_repo: Path) -> None:
        (git_repo / ".git" / "hub-guard-allow").write_text("")
        assert hub_guard_cmd_rc("git checkout -b feature/1-x", cwd=git_repo) == ALLOW

    def test_marker_allows_edit_on_hub(self, git_repo: Path) -> None:
        (git_repo / ".git" / "hub-guard-allow").write_text("")
        assert hub_guard_edit_rc(git_repo / "notes.md", cwd=git_repo) == ALLOW

    def test_absent_marker_still_blocks_commit(self, git_repo: Path) -> None:
        # Create then remove the marker: the default deny must be restored, with
        # no residual exemption — the marker is the only thing that grants.
        marker = git_repo / ".git" / "hub-guard-allow"
        marker.write_text("")
        marker.unlink()
        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=git_repo) == BLOCK

    # ── On a task branch in the main checkout: no-op ──────────────────

    def test_allows_edit_on_task_branch(self, on_branch: Callable[[str], Path]) -> None:
        repo = on_branch("feature/1-x")
        assert hub_guard_edit_rc(repo / "notes.md", cwd=repo) == ALLOW

    def test_allows_commit_on_task_branch(self, on_branch: Callable[[str], Path]) -> None:
        repo = on_branch("feature/1-x")
        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=repo) == ALLOW

    # ── Inside a linked worktree (a spoke): no-op ─────────────────────

    def test_allows_edit_in_worktree(self, linked_worktree: Path) -> None:
        assert hub_guard_edit_rc(linked_worktree / "notes.md", cwd=linked_worktree) == ALLOW

    def test_allows_commit_in_worktree(self, linked_worktree: Path) -> None:
        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=linked_worktree) == ALLOW

    # ── Outside any git repo: no-op ───────────────────────────────────

    def test_allows_outside_git_repo(self, tmp_path: Path) -> None:
        outside = tmp_path / "plain"
        outside.mkdir()
        assert hub_guard_edit_rc(outside / "notes.md", cwd=outside) == ALLOW


# ── todo-ledger-warn: TodoWrite-use ship gate ─────────────────────────
# Mirrors reviewer-sep-warn: warn on Claude / hard-deny on Cursor when the
# session transcript shows NO TodoWrite tool call. A `No-Ledger:` trailer on
# the pushed range bypasses the gate (single-step escape hatch). Any
# unadjudicable state (no transcript_path, unreadable transcript) degrades to
# allow — an advisory hook must never false-block.


def _transcript_payload(
    command: str, transcript_path: Path | None, *, cursor: bool, root: Path | None = None
) -> str:
    """A push payload carrying a top-level transcript_path (both platforms)."""
    if cursor:
        payload: dict = {"hook_event_name": "beforeShellExecution", "command": command, "cwd": ""}
        if root is not None:
            payload["workspace_roots"] = [str(root)]
    else:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    return json.dumps(payload)


def _write_transcript(path: Path, *, with_todowrite: bool) -> None:
    """Write a minimal Claude-style JSONL transcript with/without a TodoWrite call."""
    tool = "TodoWrite" if with_todowrite else "Bash"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": tool, "input": {}}]},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")


def run_todo_ledger(
    command: str, repo: Path, transcript: Path | None, *, cursor: bool = True
) -> subprocess.CompletedProcess:
    """Run todo-ledger-warn with CURSOR_PROJECT_DIR stripped so the root resolves
    from the payload/cwd (cf. _hub_env)."""
    payload = _transcript_payload(command, transcript, cursor=cursor, root=repo)
    env = {k: v for k, v in os.environ.items() if k != "CURSOR_PROJECT_DIR"}
    return subprocess.run(
        ["bash", str(TODO_LEDGER)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )


def test_todo_ledger_allows_when_todowrite_present(git_repo: Path, tmp_path: Path) -> None:
    # The session transcript shows a TodoWrite call → ledger exists → allow.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=True)
    assert run_todo_ledger("git push", git_repo, transcript).returncode == ALLOW


def _write_tasks_transcript(path: Path, *, update_only: bool = False) -> None:
    """A Tasks-system ledger transcript in the real claude ≥2.1.175 shape: the
    TaskCreate input carries no id — it arrives in the tool_result line's
    toolUseResult. With update_only, only a TaskUpdate appears (a resumed
    session driving tasks created in an earlier one)."""
    create_use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "TaskCreate",
                    "input": {"subject": "Subtask 1 · RED", "description": "failing test"},
                }
            ]
        },
    }
    create_result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu_01",
                    "type": "tool_result",
                    "content": "Task #1 created successfully: Subtask 1 · RED",
                }
            ],
        },
        "toolUseResult": {"task": {"id": "1", "subject": "Subtask 1 · RED"}},
    }
    update_use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "TaskUpdate",
                    "input": {"taskId": "1", "status": "in_progress"},
                }
            ]
        },
    }
    update_result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": "toolu_02", "type": "tool_result", "content": "Updated task #1"}
            ],
        },
        "toolUseResult": {
            "success": True,
            "taskId": "1",
            "updatedFields": ["status"],
            "statusChange": {"from": "pending", "to": "in_progress"},
        },
    }
    events = [update_use, update_result] if update_only else [create_use, create_result]
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "go"}})]
    lines += [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")


def test_todo_ledger_allows_when_tasks_ledger_present(
    repo_with_upstream: Path, tmp_path: Path
) -> None:
    # A Tasks-system ledger (TaskCreate in the transcript) is as valid as
    # TodoWrite — the gate must not fire for current-runtime spokes.
    transcript = tmp_path / "t.jsonl"
    _write_tasks_transcript(transcript)
    assert run_todo_ledger("git push", repo_with_upstream, transcript).returncode == ALLOW


def test_todo_ledger_allows_on_resumed_tasks_session(
    repo_with_upstream: Path, tmp_path: Path
) -> None:
    # Tasks persist across sessions: a resumed spoke may only ever call
    # TaskUpdate on tasks created in a dead session — still a live ledger.
    transcript = tmp_path / "t.jsonl"
    _write_tasks_transcript(transcript, update_only=True)
    assert run_todo_ledger("git push", repo_with_upstream, transcript).returncode == ALLOW


def test_todo_ledger_blocks_on_cursor_when_no_todowrite(
    repo_with_upstream: Path, tmp_path: Path
) -> None:
    # No TodoWrite in the transcript, no escape hatch → hard-deny on the Cursor
    # beforeShellExecution shape.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert run_todo_ledger("git push", repo_with_upstream, transcript).returncode == BLOCK


def test_todo_ledger_warns_but_allows_on_claude(repo_with_upstream: Path, tmp_path: Path) -> None:
    # Same missing-ledger state on the Claude shape → advisory warn, never block.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    result = run_todo_ledger("git push", repo_with_upstream, transcript, cursor=False)
    assert result.returncode == ALLOW
    assert result.stderr != ""


def test_todo_ledger_no_ledger_trailer_bypasses(repo_with_upstream: Path, tmp_path: Path) -> None:
    # A `No-Ledger:` trailer on a commit in the pushed range is the single-step
    # escape hatch — the gate is skipped even with no TodoWrite in the transcript.
    _stage(repo_with_upstream, "notes.md", "tweak\n")
    subprocess.run(
        ["git", "commit", "-qm", "docs: tweak", "-m", "Refs #1", "-m", "No-Ledger: one-liner"],
        cwd=str(repo_with_upstream),
        check=True,
        capture_output=True,
    )
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert run_todo_ledger("git push", repo_with_upstream, transcript).returncode == ALLOW


def test_todo_ledger_no_base_degrades_allow(git_repo: Path, tmp_path: Path) -> None:
    # A repo with no resolvable base (no upstream, no origin) cannot adjudicate
    # the escape-hatch range → degrade to allow even with no TodoWrite present.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert run_todo_ledger("git push", git_repo, transcript).returncode == ALLOW


def test_todo_ledger_empty_range_degrades_allow(repo_with_upstream: Path, tmp_path: Path) -> None:
    # HEAD == base (nothing ahead of upstream, e.g. `gh pr create` after the
    # push): nothing is being shipped → allow, even on the Cursor shape with no
    # TodoWrite. The No-Ledger: escape hatch is impossible on an empty range, so
    # enforcing here would be an unfixable false-block.
    _git(repo_with_upstream, "reset", "--hard", "origin/main")
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert run_todo_ledger("gh pr create", repo_with_upstream, transcript).returncode == ALLOW


def test_todo_ledger_missing_transcript_degrades_allow(git_repo: Path) -> None:
    # No transcript_path in the payload → cannot adjudicate → degrade to allow.
    assert run_todo_ledger("git push", git_repo, None).returncode == ALLOW


def test_todo_ledger_unreadable_transcript_degrades_allow(git_repo: Path, tmp_path: Path) -> None:
    # transcript_path points at a nonexistent file → degrade to allow.
    assert run_todo_ledger("git push", git_repo, tmp_path / "missing.jsonl").returncode == ALLOW


def test_todo_ledger_non_push_allows(git_repo: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert run_todo_ledger("ls -la", git_repo, transcript).returncode == ALLOW


def test_todo_ledger_chained_push_not_bypassed(repo_with_upstream: Path, tmp_path: Path) -> None:
    # Boundary-aware gate: a push chained after another command is still gated.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, with_todowrite=False)
    assert (
        run_todo_ledger("cd /tmp && git push", repo_with_upstream, transcript).returncode == BLOCK
    )


class TestHubGuardConfigurableBase:
    """hub-guard follows the resolved base branch (issue #117)."""

    def test_commit_on_configured_base_is_denied(self, git_repo: Path) -> None:
        # git config ai-toolkit.base-branch develop: the hub invariant guards
        # the RESOLVED base, so a main checkout parked on develop is the hub.
        subprocess.run(
            ["git", "checkout", "-q", "-b", "develop"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "ai-toolkit.base-branch", "develop"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )

        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=git_repo) == BLOCK

    def test_commit_on_main_allowed_when_base_configured(self, git_repo: Path) -> None:
        # With develop as the configured base, literal main is an ordinary
        # (spoke-able) branch — the guard must not deny it out of habit.
        subprocess.run(
            ["git", "branch", "develop"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "ai-toolkit.base-branch", "develop"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )

        assert hub_guard_cmd_rc('git commit -m "feat: x"', cwd=git_repo) == ALLOW


# --- commit-gauntlet advisory coverage nudge (#123) --------------------------------


_NUDGE_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _nudge_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env=_NUDGE_GIT_ENV,
    )


@pytest.fixture()
def nudge_repo(tmp_path: Path) -> Path:
    """A git repo with a seed commit — no linter/typechecker configured, so the
    gauntlet's only possible output is the #123 coverage nudge."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)],
        check=True,
        capture_output=True,
        env=_NUDGE_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _nudge_git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _nudge_git(r, "add", "README.md")
    _nudge_git(r, "commit", "-qm", "chore: seed")
    return r


def _stage_nudge(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _nudge_git(repo, "add", rel)


def _run_gauntlet(repo: Path) -> subprocess.CompletedProcess:
    return _run(COMMIT_GAUNTLET, _payload("git commit -m 'x'"), cwd=repo)


class TestGauntletCoverageNudge:
    def test_new_unreferenced_control_plane_script_warns_and_allows(self, nudge_repo: Path) -> None:
        _stage_nudge(nudge_repo, "scripts/new-tool.sh", "#!/bin/sh\necho hi\n")

        proc = _run_gauntlet(nudge_repo)

        assert proc.returncode == ALLOW  # advisory: never blocks
        assert "new-tool.sh" in proc.stderr
        assert "referencing" in proc.stderr

    def test_new_script_with_staged_referencing_test_stays_silent(self, nudge_repo: Path) -> None:
        _stage_nudge(nudge_repo, "scripts/new-tool.sh", "#!/bin/sh\necho hi\n")
        _stage_nudge(nudge_repo, "tests/unit/test_new_tool.py", '"""Covers new-tool.sh."""\n')

        proc = _run_gauntlet(nudge_repo)

        assert proc.returncode == ALLOW
        assert "new-tool.sh" not in proc.stderr  # the staged test satisfies it

    def test_exempt_new_script_stays_silent(self, nudge_repo: Path) -> None:
        (nudge_repo / ".test-select-exempt").write_text("scripts/vendored.sh\n")
        _nudge_git(nudge_repo, "add", ".test-select-exempt")
        _nudge_git(nudge_repo, "commit", "-qm", "chore: exempt")
        _stage_nudge(nudge_repo, "scripts/vendored.sh", "#!/bin/sh\necho hi\n")

        proc = _run_gauntlet(nudge_repo)

        assert proc.returncode == ALLOW
        assert "vendored.sh" not in proc.stderr

    def test_modified_unreferenced_script_stays_silent(self, nudge_repo: Path) -> None:
        # The nudge is for NEW scripts only: modifying an existing unreferenced
        # one nags at push time (escalation), not on every commit.
        _stage_nudge(nudge_repo, "scripts/old.sh", "#!/bin/sh\necho v1\n")
        _nudge_git(nudge_repo, "commit", "-qm", "chore: add old.sh")
        _stage_nudge(nudge_repo, "scripts/old.sh", "#!/bin/sh\necho v2\n")

        proc = _run_gauntlet(nudge_repo)

        assert proc.returncode == ALLOW
        assert "old.sh" not in proc.stderr

    def test_new_script_outside_control_plane_stays_silent(self, nudge_repo: Path) -> None:
        _stage_nudge(nudge_repo, "tools/helper.sh", "#!/bin/sh\necho hi\n")

        proc = _run_gauntlet(nudge_repo)

        assert proc.returncode == ALLOW
        assert "helper.sh" not in proc.stderr


@ruff
def test_corrupt_reverse_index_lib_does_not_fail_open(nudge_repo: Path, tmp_path: Path) -> None:
    # E-review finding: a syntax-corrupt lib killed the gauntlet at `source`
    # with exit 0 — silently disabling the blocking lint gate. Sourcing is now
    # gated on a child-process `bash -n`: corruption skips the nudge, and the
    # deny path below still fires.
    hookdir = tmp_path / "installed"
    (hookdir / "lib").mkdir(parents=True)
    (hookdir / "commit-gauntlet.sh").write_text(COMMIT_GAUNTLET.read_text())
    (hookdir / "lib" / "utils.sh").write_text(UTILS.read_text())
    (hookdir / "lib" / "telemetry.sh").write_text((UTILS.parent / "telemetry.sh").read_text())
    (hookdir / "lib" / "test-reverse-index.sh").write_text("if [ broken\n")  # syntax error
    (nudge_repo / "ruff.toml").write_text('[lint]\nselect = ["F"]\n')
    _stage_nudge(nudge_repo, "pkg/bad.py", "import os\n")  # F401 on a new line

    proc = _run(hookdir / "commit-gauntlet.sh", _payload("git commit -m 'x'"), cwd=nudge_repo)

    assert proc.returncode == BLOCK, proc.stderr  # the lint gate still fired


# ── ledger-schema-guard (issue #235): enforce the todo-ledger entry schema ──
#
# Every ledger entry subject must read `<subtask-id> · <STEP|type> — <label>` so
# cycle steps and ad-hoc todos stay aggregable. STEP is one of ANCHOR/RED/GREEN/
# REVIEW/PUSH; an ad-hoc entry instead carries a type from investigate|fix|test|
# docs|chore|recover. A non-conforming TaskCreate / subject-changing TaskUpdate is
# DENIED with the expected format; a status-only TaskUpdate (no subject) passes.
# Gated on WT_SPOKE so the hub / quick lanes are never blocked.

_DOT = "\N{MIDDLE DOT}"
_DASH = "\N{EM DASH}"


def _ledger_payload(subject: str | None, *, tool: str = "TaskCreate") -> str:
    tool_input: dict = {"status": "in_progress"} if tool == "TaskUpdate" else {}
    if subject is not None:
        tool_input["subject"] = subject
    if tool == "TaskUpdate":
        tool_input["taskId"] = "1"
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


def run_ledger(subject: str | None, *, tool: str = "TaskCreate", spoke: bool = True) -> int:
    env = {**os.environ}
    if spoke:
        env["WT_SPOKE"] = "1"
    else:
        env.pop("WT_SPOKE", None)
    return subprocess.run(
        ["bash", str(LEDGER_GUARD)],
        input=_ledger_payload(subject, tool=tool),
        capture_output=True,
        text=True,
        env=env,
    ).returncode


def _entry(keyword: str, *, sub: str = "#235.main", label: str = "pin the failing test") -> str:
    return f"{sub} {_DOT} {keyword} {_DASH} {label}"


class TestLedgerSchemaGuard:
    def test_allows_a_conforming_step_entry(self) -> None:
        assert run_ledger(_entry("RED")) == ALLOW

    def test_allows_every_solo_cycle_step_keyword(self) -> None:
        for step in ("ANCHOR", "RED", "GREEN", "REVIEW", "PUSH"):
            assert run_ledger(_entry(step)) == ALLOW, step

    def test_allows_a_conforming_adhoc_type_entry(self) -> None:
        assert run_ledger(_entry("fix", label="patch the off-by-one")) == ALLOW

    def test_blocks_a_free_form_entry(self) -> None:
        assert run_ledger("implement the marker spine") == BLOCK

    def test_blocks_a_merged_step_entry(self) -> None:
        # The #229 regression: "REVIEW + sync + PUSH" merged into one entry.
        assert run_ledger(_entry("REVIEW + sync + PUSH")) == BLOCK

    def test_blocks_an_ascii_separator_entry(self) -> None:
        # A plain hyphen/pipe instead of the ` · `/` — ` separators is rejected.
        assert run_ledger("#235.main - RED - label") == BLOCK

    def test_blocks_an_unknown_keyword(self) -> None:
        assert run_ledger(_entry("frobnicate")) == BLOCK

    def test_blocks_a_missing_subtask_id(self) -> None:
        assert run_ledger(f"{_DOT} RED {_DASH} no id") == BLOCK

    def test_blocks_an_empty_label(self) -> None:
        assert run_ledger(f"#235.main {_DOT} RED {_DASH} ") == BLOCK

    def test_status_only_update_without_a_subject_passes(self) -> None:
        assert run_ledger(None, tool="TaskUpdate") == ALLOW

    def test_conforming_subject_update_passes(self) -> None:
        assert run_ledger(_entry("GREEN"), tool="TaskUpdate") == ALLOW

    def test_no_op_outside_a_spoke(self) -> None:
        # The hub / quick lanes (no WT_SPOKE) are never blocked, even on a bad entry.
        assert run_ledger("free form nonsense", spoke=False) == ALLOW

    def test_ignores_other_tools(self) -> None:
        env = {**os.environ, "WT_SPOKE": "1"}
        rc = subprocess.run(
            ["bash", str(LEDGER_GUARD)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
            capture_output=True,
            text=True,
            env=env,
        ).returncode
        assert rc == ALLOW
