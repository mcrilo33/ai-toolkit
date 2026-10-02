"""
Unit tests for shared/hooks/lib/gate-stamp.sh — the green-tree stamp storage.

Issue #122 introduced content-addressed stamps keyed on the proven tree hash
(`git rev-parse HEAD^{tree}`), written under `<git-common-dir>/.gate-stamps/`
(shared by hub + all spoke worktrees, never in-tree). Since #378 CI is the gate and
the pre-push hook no longer consults or mints stamps, so only the storage half that
gate-sweep.sh still uses remains:
  * `gate_stamp_tree`  — prints HEAD^{tree} only for a CLEAN working tree; a
    dirty tree (tracked mods or untracked files) prints nothing and fails.
  * `gate_stamp_mint <tree> <tier> <env>` — writes the stamp file atomically,
    overwrites unconditionally, and prunes stamps older than ~14 days.

Hermetic: a throwaway git repo per test; the lib is driven via
`bash -c 'source …; <fn>'`.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

GATE_STAMP = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib" / "gate-stamp.sh"

# Pin git config to nothing so a host's global config can't reach these repos.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


# Sentinel exit for "the lib itself failed to load": refusal tests assert the
# returncode is neither 0 nor this, so a missing/broken lib can't pass them
# vacuously.
_LIB_LOAD_FAILED = 97


def _lib(cwd: Path, expr: str) -> subprocess.CompletedProcess[str]:
    """Source gate-stamp.sh in `cwd` and run a shell expression against it."""
    return subprocess.run(
        ["bash", "-c", f'source "{GATE_STAMP}" || exit {_LIB_LOAD_FAILED}; {expr}'],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}").strip()


def _stamps_dir(repo: Path) -> Path:
    return repo / ".git" / ".gate-stamps"


def _commit_change(repo: Path, rel: str, body: str) -> None:
    (repo / rel).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


# --- gate_stamp_tree: the key, gated on a clean working tree ----------------------


def test_tree_prints_head_tree_when_clean(repo: Path) -> None:
    proc = _lib(repo, "gate_stamp_tree")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _tree(repo)


def test_tree_changes_when_a_tracked_file_changes(repo: Path) -> None:
    before = _tree(repo)

    _commit_change(repo, "README.md", "seed\nmore\n")
    proc = _lib(repo, "gate_stamp_tree")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != before  # structural invalidation: new tree, new key


def test_tree_refuses_dirty_tracked_file(repo: Path) -> None:
    (repo / "README.md").write_text("uncommitted\n")

    proc = _lib(repo, "gate_stamp_tree")

    assert proc.returncode not in (0, _LIB_LOAD_FAILED)
    assert proc.stdout.strip() == ""  # no key ⇒ neither mint nor consume


def test_tree_refuses_untracked_file(repo: Path) -> None:
    (repo / "stray.py").write_text("x = 1\n")

    proc = _lib(repo, "gate_stamp_tree")

    assert proc.returncode not in (0, _LIB_LOAD_FAILED)
    assert proc.stdout.strip() == ""


# --- gate_stamp_mint: key, content, placement -------------------------------------


def test_mint_writes_stamp_keyed_on_tree_hash_in_common_dir(repo: Path) -> None:
    tree = _tree(repo)

    proc = _lib(repo, f'gate_stamp_mint "{tree}" full "py3.12/pytest-9"')

    assert proc.returncode == 0, proc.stderr
    stamp = _stamps_dir(repo) / tree
    assert stamp.is_file()
    content = stamp.read_text()
    assert "tier=full\n" in content
    assert "env=py3.12/pytest-9\n" in content


def test_mint_from_linked_worktree_lands_in_shared_common_dir(repo: Path, tmp_path: Path) -> None:
    # Stamps are shared hub↔spokes: minting from a linked worktree must write
    # under the MAIN repo's common dir, not the worktree's private git dir.
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    tree = _git(wt, "rev-parse", "HEAD^{tree}").strip()

    proc = _lib(wt, f'gate_stamp_mint "{tree}" testmon "py3.12"')

    assert proc.returncode == 0, proc.stderr
    assert (_stamps_dir(repo) / tree).is_file()


def test_mint_overwrites_with_the_latest_run(repo: Path) -> None:
    tree = _tree(repo)

    _lib(repo, f'gate_stamp_mint "{tree}" testmon "py3.12"')
    proc = _lib(repo, f'gate_stamp_mint "{tree}" full "py3.12"')

    assert proc.returncode == 0, proc.stderr
    assert "tier=full\n" in (_stamps_dir(repo) / tree).read_text()  # upgraded


# --- GC on mint --------------------------------------------------------------------


def test_mint_prunes_stamps_older_than_fourteen_days(repo: Path) -> None:
    tree = _tree(repo)
    stale = _stamps_dir(repo) / ("0" * 40)
    fresh = _stamps_dir(repo) / ("1" * 40)
    _stamps_dir(repo).mkdir(parents=True, exist_ok=True)
    stale.write_text("tier=full\nenv=old\n")
    fresh.write_text("tier=full\nenv=recent\n")
    old = time.time() - 16 * 86400
    os.utime(stale, (old, old))

    proc = _lib(repo, f'gate_stamp_mint "{tree}" full "py3.12"')

    assert proc.returncode == 0, proc.stderr
    assert not stale.exists()  # pruned
    assert fresh.exists()  # recent stamps survive
    assert (_stamps_dir(repo) / tree).is_file()
