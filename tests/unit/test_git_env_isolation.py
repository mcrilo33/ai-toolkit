"""Regression test for issue #30 — the pre-push gate's GIT_DIR leak.

Git exports ``GIT_DIR``/``GIT_WORK_TREE``/etc. into the environment of its native
hooks. The pre-push test gate runs ``pytest`` from inside such a hook, so without
``tests/conftest.py`` stripping those vars, any test that shells out to ``git``
inherits an absolute pointer to the REAL repository and operates on it instead of
its own tmpdir — exactly how issue #24's push moved the hub's ``main`` to a bogus
``chore: seed`` commit and flipped ``core.bare``.

This test reproduces that leak against a THROWAWAY DECOY repo (never the real
one): it launches a child ``pytest`` whose environment points ``GIT_DIR``/
``GIT_WORK_TREE`` at the decoy, and the child test git-inits its own tmpdir and
commits ``chore: seed`` — the exact incident. With the conftest strip in place the
child's git lands in its tmpdir and the decoy is untouched; without it, the commit
leaks onto the decoy and the assertions below fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Vars git exports into native-hook environments; any of them can redirect a
# child git process away from its intended repo (mirrors the conftest list).
_GIT_HOOK_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


def _clean_env() -> dict[str, str]:
    """A copy of os.environ with every git-hook var removed.

    The harness's OWN git calls (decoy setup/verification) must hit the path they
    name, so they never run under a leaked GIT_DIR — only the simulated child does.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_HOOK_VARS and not k.startswith("GIT_CONFIG_")
    }


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return proc.stdout.strip()


def test_child_pytest_does_not_corrupt_repo_when_git_dir_leaks(tmp_path: Path) -> None:
    # ── A decoy standing in for the real repo (NEVER point at the actual repo) ──
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q", "-b", "main")
    _git(decoy, "config", "user.email", "decoy@example.com")
    _git(decoy, "config", "user.name", "Decoy")
    (decoy / "README.md").write_text("untouched decoy content\n")
    _git(decoy, "add", "README.md")
    _git(decoy, "commit", "-q", "-m", "decoy initial")
    head_before = _git(decoy, "rev-parse", "HEAD")

    # ── A child test that mimics the suite: git init a tmpdir + commit ──────────
    childdir = tmp_path / "child"
    childdir.mkdir()
    # The child must load the same conftest to be protected; copy it when present.
    # During RED (conftest absent) the child has no strip and the leak corrupts
    # the decoy — that is the failing assertion that drives the fix.
    if _CONFTEST.exists():
        shutil.copy(_CONFTEST, childdir / "conftest.py")
    # cwd-based git, inheriting the process env — the exact shape of the suite's
    # own helpers (see test_review_stamp.py). A leaked GIT_DIR overrides cwd, so
    # these operations target the decoy unless the conftest strip removes it.
    (childdir / "test_inner.py").write_text(
        "import subprocess\n"
        "\n"
        "def test_inner(tmp_path):\n"
        "    repo = tmp_path / 'work'\n"
        "    repo.mkdir()\n"
        "    def g(*a):\n"
        "        subprocess.run(['git', *a], cwd=str(repo), check=True,\n"
        "                       capture_output=True)\n"
        "    g('init', '-q', '-b', 'main')\n"
        "    (repo / 'f.txt').write_text('x')\n"
        "    g('add', 'f.txt')\n"
        "    g('-c', 'user.email=a@b.c', '-c', 'user.name=A',\n"
        "      'commit', '-q', '-m', 'chore: seed')\n"
    )

    # ── Simulate git's native-hook env: GIT_DIR points at the decoy ─────────────
    # GIT_WORK_TREE is left unset so the worktree defaults to the child's cwd —
    # the staged tree comes from the child's tmpdir but the COMMIT lands on the
    # decoy's HEAD, reproducing issue #24's bogus 'chore: seed' on main.
    polluted = {
        **_clean_env(),
        "GIT_DIR": str(decoy / ".git"),
    }
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(childdir)],
        env=polluted,
        capture_output=True,
        text=True,
    )

    # The child must run its commit to completion — otherwise a clean decoy would
    # prove nothing (a crashed child never reaches the commit either way).
    assert child.returncode == 0, f"child pytest did not pass:\n{child.stdout}\n{child.stderr}"

    # ── The decoy must be exactly as we left it ─────────────────────────────────
    assert _git(decoy, "rev-parse", "HEAD") == head_before, (
        "child pytest leaked a commit onto the decoy via GIT_DIR — "
        "the conftest git-hook env strip is missing or ineffective"
    )
    assert (decoy / "README.md").read_text() == "untouched decoy content\n"
    assert _git(decoy, "config", "--get", "core.bare") != "true"
