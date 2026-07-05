"""Unit tests for shared/hooks/anti-gutting-scan.sh.

The adversarial code-review gate is unenforceable policy (a spoke can narrate a
review it never ran — the #43 failure mode), and on the native Claude pre-push
path reviewer-sep is advisory (it does not block). So the one MECHANICAL signal
against the dangerous cheat — an implementation that guts the tests to go green —
is a deterministic diff scan wired into the pre-push path. It is ADVISORY: on any
test-gutting signature it prints a warning to stderr and exits 0, so it surfaces
the smell before landing without gating a human's ordinary test refactor.

Gutting signatures (in the pushed range's diff):
  * added ``sys.exit(0)`` / ``sys.exit()`` / ``os._exit(...)`` in ANY .py — a hard
    short-circuit that can make a test process exit green;
  * added ``@pytest.mark.skip`` / ``xfail`` or ``pytest.skip(`` in a TEST file —
    silencing a test;
  * added a tautological ``assert True`` / ``assert 1`` in a TEST file;
  * a NET DECREASE in ``assert`` statements in TEST files (more removed than added)
    — deleted / weakened assertions.

Hermetic: a throwaway repo on the default branch with a real test, then a feature
commit carrying the change under test. The scan reads git's pre-push stdin
(``<lref> <lsha> <rref> <rsha>``) exactly like test-select.sh, so the test feeds
the same line shape.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCAN = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "anti-gutting-scan.sh"

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

_REAL_TEST = "def test_adds_up():\n    total = 2 + 2\n    assert total == 4\n    assert total > 0\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo on main with a committed real test file (the pre-push base)."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "tests").mkdir()
    (r / "tests" / "test_thing.py").write_text(_REAL_TEST)
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "chore: seed test", "-m", "Refs #0")
    return r


def _commit(repo: Path, files: dict[str, str], msg: str = "feat: change") -> tuple[str, str]:
    """Record base, write/overwrite files, commit; return (base_sha, head_sha)."""
    base = _git(repo, "rev-parse", "HEAD").strip()
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, "-m", "Refs #72")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return base, head


def _scan(
    repo: Path, base: str, head: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run the scan with a synthesized pre-push stdin line (range base..head)."""
    stdin = f"refs/heads/feature {head} refs/heads/feature {base}\n"
    return subprocess.run(
        ["bash", str(SCAN)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env={**_GIT_ENV, **(env or {})},
    )


# ── gutting signatures warn but never block (advisory) ───────────────────────


@pytest.mark.parametrize(
    "rel,content",
    [
        # sys.exit short-circuit in implementation
        ("app.py", "import sys\n\n\ndef run():\n    sys.exit(0)\n"),
        # skip marker silencing a test
        (
            "tests/test_thing.py",
            "import pytest\n\n\n@pytest.mark.skip\ndef test_adds_up():\n    assert 2 + 2 == 4\n",
        ),
        # tautological assert
        ("tests/test_taut.py", "def test_taut():\n    assert True\n"),
    ],
)
def test_gutting_signature_warns_but_is_advisory(repo: Path, rel: str, content: str) -> None:
    base, head = _commit(repo, {rel: content})

    result = _scan(repo, base, head)

    assert result.returncode == 0, f"advisory scan must not block a gutting diff ({rel})"
    assert "weakens tests" in result.stderr, f"advisory mode must still warn ({rel})"


def test_deleted_assertions_warn_but_are_advisory(repo: Path) -> None:
    # Rewrite the seeded test so its two asserts become one trivial body -> a net
    # decrease in assert statements (deleted/weakened assertions).
    base, head = _commit(repo, {"tests/test_thing.py": "def test_adds_up():\n    x = 2 + 2\n"})

    result = _scan(repo, base, head)

    assert result.returncode == 0, "advisory scan must not block a net decrease in assertions"
    assert "weakens tests" in result.stderr, "advisory mode must still warn"


# ── a clean diff passes silently ─────────────────────────────────────────────


def test_clean_diff_passes_without_warning(repo: Path) -> None:
    # Adds a genuine test with real assertions and no gutting signatures.
    base, head = _commit(
        repo,
        {"tests/test_more.py": "def test_more():\n    assert (1 + 1) == 2\n    assert [1] != []\n"},
    )

    result = _scan(repo, base, head)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "weakens tests" not in result.stderr, "a clean diff must not warn"


def test_new_branch_zero_remote_sha_uses_merge_base(repo: Path) -> None:
    # A first push has an all-zero remote sha; the scan must fall back to the
    # merge-base with the default branch rather than erroring, and still warn on a
    # gutting signature in the new commits. The commits must live on a branch
    # AHEAD of the default so the merge-base is the fork point, not the tip.
    _git(repo, "checkout", "-q", "-b", "feature")
    _, head = _commit(repo, {"tests/test_taut.py": "def test_t():\n    assert True\n"})
    zero = "0" * 40
    stdin = f"refs/heads/feature {head} refs/heads/feature {zero}\n"

    result = subprocess.run(
        ["bash", str(SCAN)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    )

    assert result.returncode == 0, "advisory scan must not block a new-branch push"
    assert "weakens tests" in result.stderr, "a new-branch push must still scan the new commits"


# ── advisory everywhere: UNATTENDED no longer fails closed (issue #143) ───────
# The reasoning code-review verdict is now the /afk gatekeeper (hub-afk.sh auto_land
# escalates to blocked on a non-clean verdict), so the mechanical scan is advisory in
# EVERY context — including under an unattended drain. The old fail-closed path (armed by
# the UNATTENDED env or the supervisor's `unattended` marker) is gone: a gutting diff
# still warns, but never blocks, regardless of the marker.


def test_unattended_env_is_now_advisory(repo: Path) -> None:
    base, head = _commit(repo, {"tests/test_taut.py": "def test_taut():\n    assert True\n"})

    result = _scan(repo, base, head, env={"UNATTENDED": "1"})

    assert result.returncode == 0, "UNATTENDED must no longer block — the scan is advisory"
    assert "weakens tests" in result.stderr, "advisory mode must still warn under UNATTENDED"


def test_unattended_marker_is_now_advisory(repo: Path) -> None:
    marker_dir = repo / ".git" / "ai-toolkit-afk"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "unattended").write_text("")
    base, head = _commit(repo, {"tests/test_taut.py": "def test_taut():\n    assert True\n"})

    result = _scan(repo, base, head)

    assert result.returncode == 0, "the unattended marker must no longer block — advisory only"
    assert "weakens tests" in result.stderr, "advisory mode must still warn with the marker present"


# ── tag-only / marker pushes are exempt (issue #143, false positive 2) ────────
# A blocked/<issue> (or any tag) push carries no reviewable code, so anti-gutting must
# not scan it at all — a spoke escalating its state must always be able to push its
# marker. A push whose refs are ALL tags contributes no range: nothing is scanned and
# nothing is warned, even when the underlying commit range has a gutting signature.


def test_tag_only_push_is_exempt(repo: Path) -> None:
    base, head = _commit(repo, {"tests/test_taut.py": "def test_taut():\n    assert True\n"})
    stdin = f"refs/tags/blocked/5 {head} refs/tags/blocked/5 {base}\n"

    result = subprocess.run(
        ["bash", str(SCAN)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    )

    assert result.returncode == 0, "a tag-only push must never be blocked"
    assert "weakens tests" not in result.stderr, "a tag-only push must not even be scanned"


def test_mixed_branch_and_tag_push_still_scans_the_branch(repo: Path) -> None:
    # A push carrying BOTH a branch ref and a tag ref (separate stdin lines) must still
    # scan the branch range — the exemption is per-ref, not "any tag ⇒ skip the push"
    # (which would let anyone bypass the scan by tacking a tag onto a branch push).
    base, head = _commit(repo, {"tests/test_taut.py": "def test_taut():\n    assert True\n"})
    stdin = (
        f"refs/heads/feature {head} refs/heads/feature {base}\n"
        f"refs/tags/blocked/5 {head} refs/tags/blocked/5 {base}\n"
    )

    result = subprocess.run(
        ["bash", str(SCAN)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    )

    assert result.returncode == 0, "advisory scan never blocks"
    assert "weakens tests" in result.stderr, "the branch ref in a mixed push must still be scanned"
