"""Unit tests for shared/hooks/lib/identity.sh — the one spoke-identity reader (#360, S2a).

Identity is RECORDED data (`<wt>/.ai-toolkit/identity`, key=value lines written by
provision-worktree.sh); the env marker, the git-dir pattern and the branch slug are
per-key fallbacks, never the primary source.

Contract under test:
  * `ai_toolkit_identity_get <key> [root]` — value + rc 0, or empty + rc 1 (no file, no
    key, empty value). Strict parse: blanks, `#` comments and `=`-less lines skipped,
    first occurrence wins, the file is never sourced or eval'd.
  * `ai_toolkit_identity_issue [root]` — record `issue`, else the branch leaf's leading digits.
  * `ai_toolkit_identity_is_spoke [root] [any|env|gitdir]` — a non-empty record `issue`
    always wins; otherwise the selected fallback (`any`: WT_SPOKE or linked-worktree
    git-dir; `env`: WT_SPOKE only; `gitdir`: git-dir only).
  * `ai_toolkit_identity_has_issue_anchor [root]` — record `issue`, else the branch
    carries a tracker key or a bare number right after the type prefix.
  * `ai_toolkit_identity_issue_at <root>` — the hub-side by-path reader (#361): the record's
    numeric `issue` ONLY (no branch fallback), empty + rc 1 otherwise, never an error.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib" / "identity.sh"

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
for _leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "WT_SPOKE"):
    _GIT_ENV.pop(_leak, None)

_LIB_LOAD_FAILED = 97


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _call(
    cwd: Path, expr: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = f'source "{LIB}" || exit {_LIB_LOAD_FAILED}; {expr}'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, **(env or {})},
        timeout=30,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    assert result.returncode not in (_LIB_LOAD_FAILED, 127), result.stderr
    return result


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    r = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True, env=_GIT_ENV)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _linked(hub: Path, tmp_path: Path, branch: str) -> Path:
    path = tmp_path / "linked"
    _git(hub, "worktree", "add", "-q", str(path), "-b", branch, "main")
    return path


def _record(root: Path, text: str) -> None:
    (root / ".ai-toolkit").mkdir(exist_ok=True)
    (root / ".ai-toolkit" / "identity").write_text(text)


def test_lib_exists_and_parses() -> None:
    assert LIB.is_file(), "shared/hooks/lib/identity.sh missing"
    assert subprocess.run(["bash", "-n", str(LIB)]).returncode == 0


# --- get ----------------------------------------------------------------------


def test_get_reads_a_recorded_value(tmp_path: Path) -> None:
    _record(tmp_path, "issue=360\nlane=spoke\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get lane "{tmp_path}"'))

    assert (result.returncode, result.stdout) == (0, "spoke")


@pytest.mark.parametrize("text", [None, "lane=spoke\n", "issue=\nlane=spoke\n"])
def test_get_is_empty_rc1_for_absent_file_key_or_empty_value(
    tmp_path: Path, text: str | None
) -> None:
    if text is not None:
        _record(tmp_path, text)

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get issue "{tmp_path}"'))

    assert (result.returncode, result.stdout) == (1, "")


def test_get_skips_comments_blanks_and_malformed_lines_and_first_occurrence_wins(
    tmp_path: Path,
) -> None:
    _record(tmp_path, "# issue=1\n\nnot a pair\n  \nissue=360\nissue=999\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get issue "{tmp_path}"'))

    assert (result.returncode, result.stdout) == (0, "360")


def test_get_keeps_equals_signs_in_the_value(tmp_path: Path) -> None:
    _record(tmp_path, "slug=a=b\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get slug "{tmp_path}"'))

    assert result.stdout == "a=b"


def test_get_never_executes_the_file(tmp_path: Path) -> None:
    canary = tmp_path / "canary"
    _record(tmp_path, f"issue=$(touch {canary})\n`touch {canary}`\n; touch {canary}\nlane=spoke\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get lane "{tmp_path}"'))

    assert result.stdout == "spoke"
    assert not canary.exists()


def test_get_does_not_match_a_key_prefix(tmp_path: Path) -> None:
    _record(tmp_path, "issue_extra=1\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get issue "{tmp_path}"'))

    assert (result.returncode, result.stdout) == (1, "")


# --- issue --------------------------------------------------------------------


def test_issue_prefers_the_record_over_the_branch(hub: Path) -> None:
    _git(hub, "checkout", "-q", "-b", "orca-migration")
    _record(hub, "issue=360\n")

    result = _ok(_call(hub, f'ai_toolkit_identity_issue "{hub}"'))

    assert (result.returncode, result.stdout) == (0, "360")


@pytest.mark.parametrize("record", [None, "issue=\n"])
def test_issue_falls_back_to_the_branch_leaf_digits(hub: Path, record: str | None) -> None:
    _git(hub, "checkout", "-q", "-b", "feature/173-foo")
    if record is not None:
        _record(hub, record)

    result = _ok(_call(hub, f'ai_toolkit_identity_issue "{hub}"'))

    assert (result.returncode, result.stdout) == (0, "173")


def test_issue_is_empty_rc1_when_neither_record_nor_slug_has_one(hub: Path) -> None:
    _git(hub, "checkout", "-q", "-b", "orca-migration")

    result = _ok(_call(hub, f'ai_toolkit_identity_issue "{hub}"'))

    assert (result.returncode, result.stdout) == (1, "")


# --- is_spoke -----------------------------------------------------------------


def test_is_spoke_record_wins_in_a_plain_checkout_with_no_env(hub: Path) -> None:
    _record(hub, "issue=360\n")

    for mode in ("any", "env", "gitdir"):
        result = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" {mode}'))
        assert result.returncode == 0, mode


def test_is_spoke_an_empty_record_issue_does_not_count(hub: Path) -> None:
    _record(hub, "issue=\nlane=spoke\n")

    result = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" any'))

    assert result.returncode == 1


def test_is_spoke_any_is_the_default_and_accepts_env_or_gitdir(hub: Path, tmp_path: Path) -> None:
    linked = _linked(hub, tmp_path, "orca-migration")

    by_env = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}"', {"WT_SPOKE": "1"}))
    by_gitdir = _ok(_call(linked, f'ai_toolkit_identity_is_spoke "{linked}"'))
    neither = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}"'))

    assert (by_env.returncode, by_gitdir.returncode, neither.returncode) == (0, 0, 1)


def test_is_spoke_env_selector_ignores_the_gitdir(hub: Path, tmp_path: Path) -> None:
    linked = _linked(hub, tmp_path, "orca-migration")

    no_env = _ok(_call(linked, f'ai_toolkit_identity_is_spoke "{linked}" env'))
    with_env = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" env', {"WT_SPOKE": "1"}))

    assert (no_env.returncode, with_env.returncode) == (1, 0)


def test_is_spoke_gitdir_selector_ignores_the_env(hub: Path, tmp_path: Path) -> None:
    linked = _linked(hub, tmp_path, "orca-migration")

    env_only = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" gitdir', {"WT_SPOKE": "1"}))
    in_linked = _ok(_call(linked, f'ai_toolkit_identity_is_spoke "{linked}" gitdir'))

    assert (env_only.returncode, in_linked.returncode) == (1, 0)


# --- has_issue_anchor ---------------------------------------------------------


def test_anchor_record_wins_on_an_unanchored_branch(hub: Path) -> None:
    _git(hub, "checkout", "-q", "-b", "orca-migration")
    _record(hub, "issue=360\n")

    result = _ok(_call(hub, f'ai_toolkit_identity_has_issue_anchor "{hub}"'))

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("branch", "anchored"),
    [
        ("feature/142-x", True),
        ("fix/142", True),
        ("142-x", True),
        ("feat/PROJ-12-thing", True),
        ("PROJ-12/thing", True),
        ("feature/oauth-2-factor", False),
        ("release-2024", False),
        ("orca-migration", False),
    ],
)
def test_anchor_falls_back_to_the_existing_branch_regexes(
    hub: Path, branch: str, anchored: bool
) -> None:
    _git(hub, "checkout", "-q", "-b", branch)

    result = _ok(_call(hub, f'ai_toolkit_identity_has_issue_anchor "{hub}"'))

    assert result.returncode == (0 if anchored else 1)


# --- hardening ------------------------------------------------------------------


def test_get_reads_a_last_line_without_a_trailing_newline(tmp_path: Path) -> None:
    _record(tmp_path, "issue=360")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get issue "{tmp_path}"'))

    assert result.stdout == "360"


def test_get_strips_a_carriage_return(tmp_path: Path) -> None:
    _record(tmp_path, "issue=360\r\nlane=spoke\r\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get issue "{tmp_path}"'))

    assert result.stdout == "360"


@pytest.mark.parametrize("key", ["#issue", "a=b", "", "Issue"])
def test_get_rejects_an_invalid_key(tmp_path: Path, key: str) -> None:
    _record(tmp_path, "#issue=1\na=b=2\n=3\nIssue=4\n")

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_get "{key}" "{tmp_path}"'))

    assert (result.returncode, result.stdout) == (1, "")


def test_a_non_numeric_recorded_issue_is_unknown_and_falls_back(hub: Path) -> None:
    _git(hub, "checkout", "-q", "-b", "feature/fix-typo")
    _record(hub, "issue=fix-typo\n")

    issue = _ok(_call(hub, f'ai_toolkit_identity_issue "{hub}"'))
    anchor = _ok(_call(hub, f'ai_toolkit_identity_has_issue_anchor "{hub}"'))
    spoke = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" gitdir'))

    assert (issue.returncode, anchor.returncode, spoke.returncode) == (1, 1, 1)


def test_an_unknown_is_spoke_selector_fails_loudly(hub: Path) -> None:
    result = _ok(_call(hub, f'ai_toolkit_identity_is_spoke "{hub}" gitdirs'))

    assert result.returncode == 2
    assert "unknown is_spoke fallback" in result.stderr


def test_every_function_is_safe_under_set_euo_pipefail(hub: Path, tmp_path: Path) -> None:
    linked = _linked(hub, tmp_path, "feature/5-x")
    expr = (
        "set -euo pipefail; "
        f'ai_toolkit_identity_get issue "{linked}" || true; '
        f'ai_toolkit_identity_issue "{linked}" || true; '
        f'ai_toolkit_identity_is_spoke "{linked}" || true; '
        f'ai_toolkit_identity_has_issue_anchor "{linked}" || true; echo done'
    )

    result = _ok(_call(linked, expr))

    assert result.stdout.strip().endswith("done"), result.stderr
    assert "unbound" not in result.stderr


# --- issue_at (by-path, record-only; #361 S2b) --------------------------------


def test_issue_at_reads_another_worktrees_recorded_issue(hub: Path, tmp_path: Path) -> None:
    linked = _linked(hub, tmp_path, "orca-migration")
    _record(linked, "issue=361\nlane=spoke\n")

    result = _ok(_call(hub, f'ai_toolkit_identity_issue_at "{linked}"'))

    assert (result.returncode, result.stdout) == (0, "361")


@pytest.mark.parametrize(
    "text", [None, "lane=spoke\n", "issue=\n", "issue=fix-typo\n", "issue=12x\n", "issue=-3\n"]
)
def test_issue_at_is_empty_rc1_when_the_record_names_no_numeric_issue(
    tmp_path: Path, text: str | None
) -> None:
    if text is not None:
        _record(tmp_path, text)

    result = _ok(_call(tmp_path, f'ai_toolkit_identity_issue_at "{tmp_path}"'))

    assert (result.returncode, result.stdout, result.stderr) == (1, "", "")


def test_issue_at_tolerates_a_path_that_does_not_exist(tmp_path: Path) -> None:
    result = _ok(_call(tmp_path, f'ai_toolkit_identity_issue_at "{tmp_path}/nope/at/all"'))

    assert (result.returncode, result.stdout, result.stderr) == (1, "", "")


def test_issue_at_never_falls_back_to_the_branch_name(hub: Path, tmp_path: Path) -> None:
    # Record-only: the hub sites own their branch-slug fallback, so an issue-shaped branch
    # with no record must read as unknown here, not as 5.
    linked = _linked(hub, tmp_path, "feature/5-x")

    result = _ok(_call(hub, f'ai_toolkit_identity_issue_at "{linked}"'))

    assert (result.returncode, result.stdout) == (1, "")


def test_issue_at_is_safe_under_set_euo_pipefail(tmp_path: Path) -> None:
    result = _ok(
        _call(
            tmp_path,
            f'set -euo pipefail; ai_toolkit_identity_issue_at "{tmp_path}" || true; echo done',
        )
    )

    assert result.stdout.strip() == "done", result.stderr


def test_get_key_check_spells_the_alphabet_out_not_a_range() -> None:
    """A bracket range collates by locale: under fr_FR.UTF-8 on macOS `a-z` also matches
    uppercase, so `Issue` was accepted as a key (#384). The check must not use ranges."""
    source = LIB.read_text()
    body = source.split("ai_toolkit_identity_get() {", 1)[1].split("\n}\n", 1)[0]

    assert "a-z" not in body
