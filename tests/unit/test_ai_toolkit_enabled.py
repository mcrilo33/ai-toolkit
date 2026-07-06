"""Unit tests for the global on/off switch (issue #154).

The toolkit runs enforcement at runtime on every commit / push / tool-call
(the pre-push + commit-msg git hooks and the Claude Code guard/marker/telemetry
hooks). ``shared/hooks/lib/enabled.sh`` is the ONE canonical resolver every hook
sources to decide whether to enforce or pass through, plus the ``on|off|status``
toggle CLI (also exposed as the ``scripts/ai-toolkit`` PATH shim).

Resolver precedence (first-decisive) — mirrors ``base-branch.sh`` but with the
MARKER made decisive over git config, because ``sync-to-repo.sh`` re-materializes
``git config ai-toolkit.enabled`` from the yaml on every sync and would silently
clobber a manual git-config override; the ``<git-common-dir>/ai-toolkit-off``
marker is sync-safe:

    1. ``<git-common-dir>/ai-toolkit-off`` present            ⇒ DISABLED (decisive)
    2. else ``git config --local --get ai-toolkit.enabled``   ⇒ false/0/off ⇒ DISABLED
                                                                  true/1/on   ⇒ ENABLED
    3. else                                                    ⇒ ENABLED (default)

``ai-toolkit off`` drops the marker (survives re-syncs); ``ai-toolkit on`` removes
it. The toggle is scoped to the CLONE — the marker lives at the shared
``git-common-dir`` and git config is read ``--local`` — so every linked worktree
of the clone sees the same state and it is never machine-global.

Hermetic: a throwaway git repo per test with HOME / git config sandboxed so no
real user config leaks in (mirrors test_install_git_hooks.py's _GIT_ENV).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
ENABLED_SH = _REPO_ROOT / "shared" / "hooks" / "lib" / "enabled.sh"
AI_TOOLKIT_CMD = _REPO_ROOT / "scripts" / "ai-toolkit"

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit, HOME/git config sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
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


def _env(repo: Path) -> dict[str, str]:
    return {**_GIT_ENV, "HOME": str(repo.parent / "home")}


def _common_dir(repo: Path) -> Path:
    """Absolute <git-common-dir> for the repo (mirrors the resolver)."""
    raw = _git(repo, "rev-parse", "--git-common-dir").strip()
    p = Path(raw)
    return p if p.is_absolute() else (repo / p).resolve()


def _check(repo: Path, cwd: Path | None = None) -> int:
    """Run ``enabled.sh check`` — exit 0 when ENABLED, non-zero when DISABLED."""
    return subprocess.run(
        [str(ENABLED_SH), "check"],
        cwd=str(cwd or repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    ).returncode


def _cli(repo: Path, *args: str, cmd: Path = ENABLED_SH) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cmd), *args], cwd=str(repo), capture_output=True, text=True, env=_env(repo)
    )


# ── Resolver precedence ─────────────────────────────────────────────


def test_default_enabled_when_unset(repo: Path) -> None:
    """Nothing set (no marker, git config unset) ⇒ ENABLED (today's behavior)."""
    assert _check(repo) == 0


def test_git_config_false_disables(repo: Path) -> None:
    """git config ai-toolkit.enabled=false, no marker ⇒ DISABLED."""
    _git(repo, "config", "--local", "ai-toolkit.enabled", "false")
    assert _check(repo) != 0


def test_git_config_true_enabled(repo: Path) -> None:
    """git config ai-toolkit.enabled=true, no marker ⇒ ENABLED."""
    _git(repo, "config", "--local", "ai-toolkit.enabled", "true")
    assert _check(repo) == 0


def test_marker_disables(repo: Path) -> None:
    """A bare <git-common-dir>/ai-toolkit-off marker ⇒ DISABLED."""
    (_common_dir(repo) / "ai-toolkit-off").write_text("")
    assert _check(repo) != 0


def test_marker_beats_git_config_true(repo: Path) -> None:
    """The marker is DECISIVE: present even over git config=true ⇒ DISABLED.

    This is the #154 correction — the sync-safe marker wins so a re-sync can't
    silently clobber a manual off.
    """
    _git(repo, "config", "--local", "ai-toolkit.enabled", "true")
    (_common_dir(repo) / "ai-toolkit-off").write_text("")
    assert _check(repo) != 0


# ── Toggle CLI ──────────────────────────────────────────────────────


def test_off_creates_marker_and_disables(repo: Path) -> None:
    """``off`` drops the marker at git-common-dir and flips check to DISABLED."""
    result = _cli(repo, "off")
    assert result.returncode == 0, result.stderr
    assert (_common_dir(repo) / "ai-toolkit-off").exists()
    assert _check(repo) != 0


def test_on_removes_marker_and_enables(repo: Path) -> None:
    """``on`` removes the marker and reverts to the default (ENABLED)."""
    _cli(repo, "off")
    result = _cli(repo, "on")
    assert result.returncode == 0, result.stderr
    assert not (_common_dir(repo) / "ai-toolkit-off").exists()
    assert _check(repo) == 0


def test_status_reports_off(repo: Path) -> None:
    """``status`` reports the effective OFF state when disabled."""
    _cli(repo, "off")
    result = _cli(repo, "status")
    assert result.returncode == 0
    assert "OFF" in result.stdout


def test_status_reports_on(repo: Path) -> None:
    """``status`` reports the effective ON state by default."""
    result = _cli(repo, "status")
    assert result.returncode == 0
    assert "ON" in result.stdout


# ── Clone scope (shared across linked worktrees) ────────────────────


def test_marker_shared_across_worktrees(repo: Path, tmp_path: Path) -> None:
    """``off`` in the main checkout disables enforcement in a linked worktree too.

    The marker lives at the shared git-common-dir, so the toggle is clone-scoped,
    not per-worktree and not machine-global (AC: scoped to the clone).
    """
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    _cli(repo, "off")
    assert _check(repo, cwd=wt) != 0


# ── PATH shim ───────────────────────────────────────────────────────


def test_ai_toolkit_shim_toggles(repo: Path) -> None:
    """The ``scripts/ai-toolkit`` PATH command drives the same on/off/check flow."""
    assert _cli(repo, "off", cmd=AI_TOOLKIT_CMD).returncode == 0
    assert _check(repo) != 0
    assert _cli(repo, "on", cmd=AI_TOOLKIT_CMD).returncode == 0
    assert _check(repo) == 0
