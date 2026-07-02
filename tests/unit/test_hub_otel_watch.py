"""Unit tests for shared/skills/hub/scripts/hub-otel-watch.sh.

The hub-side OTel watchdog (issue #115): when ≥1 spoke pane is live it ensures the
otelcol collector (:4317) and the Langfuse message bridge (:4319) are up —
recycling a dead/stale one via the worktree-lib ensure paths — and is a silent
no-op when no spoke runs. Meant to be run on a loop from the hub (main checkout).

These tests source the script (a source-guard keeps ``main`` from running on
import) and drive its layers directly with the tmux/worktree probes and the
docker-touching preflights stubbed, so no real tmux, git, or docker is invoked:

  * ``main`` orchestration — ensure exactly when a spoke pane is live, else silent;
  * ``spoke_pane_live`` — the pane-vs-spoke-worktree correlation predicate.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_OTEL_WATCH = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-otel-watch.sh"


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    """Source hub-otel-watch.sh and invoke a shell expression against its functions."""
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_OTEL_WATCH}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


# Stub the two docker-touching preflights so a run prints markers instead of
# spawning anything, force MAIN_ROOT so the ensure target is deterministic, and
# opt into native OTel (AI_TOOLKIT_OTEL=1) so main() takes the ensure path rather
# than the opted-out notice.
_ENSURE_STUBS = "; ".join(
    [
        "MAIN_ROOT=/repo",
        "export AI_TOOLKIT_OTEL=1",
        'wt_otel_collector_preflight() { echo "COLLECTOR $1"; }',
        'wt_otel_bridge_preflight() { echo "BRIDGE $1"; }',
    ]
)


# ── main orchestration ────────────────────────────────────────────────────────


def test_main_ensures_stack_when_spoke_pane_live() -> None:
    # A spoke pane is live → ensure BOTH collector and bridge against MAIN_ROOT.
    result = _call(f"spoke_pane_live() {{ return 0; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR /repo" in result.stdout
    assert "BRIDGE /repo" in result.stdout


def test_main_ensures_collector_before_bridge() -> None:
    # Ordering matters: the collector forks LLM I/O + audit events to the bridge,
    # so it must be ensured first — mirror wt_otel_collector_preflight's "run BEFORE
    # the bridge preflight" contract.
    result = _call(f"spoke_pane_live() {{ return 0; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert result.stdout.index("COLLECTOR") < result.stdout.index("BRIDGE")


def test_main_is_silent_noop_when_no_spoke_pane() -> None:
    # No spoke pane live → touch nothing (a quiet no-op; the stack need not run when
    # no spoke does). Never fail.
    result = _call(f"spoke_pane_live() {{ return 1; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR" not in result.stdout
    assert "BRIDGE" not in result.stdout


def test_main_warns_and_skips_when_otel_opted_out() -> None:
    # A spoke IS live but AI_TOOLKIT_OTEL != 1 → the preflights would silently
    # no-op and that spoke's traces are lost (the #115 footgun). main() must NOT
    # ensure, and must surface a one-line stderr notice rather than fail.
    parts = [
        "spoke_pane_live() { return 0; }",
        "MAIN_ROOT=/repo",
        "unset AI_TOOLKIT_OTEL",
        'wt_otel_collector_preflight() { echo "COLLECTOR $1"; }',
        'wt_otel_bridge_preflight() { echo "BRIDGE $1"; }',
        "main",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR" not in result.stdout
    assert "BRIDGE" not in result.stdout
    assert "AI_TOOLKIT_OTEL" in result.stderr


# ── spoke_pane_live predicate ─────────────────────────────────────────────────


def test_spoke_pane_live_true_when_pane_sits_in_spoke_worktree() -> None:
    # A tmux pane's path equals a spoke worktree path → live.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf "%s\\n" /repo/wt-115; }',
        '_pane_paths() { printf "%s\\n" /repo/wt-115; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LIVE"


def test_spoke_pane_live_false_when_only_hub_pane() -> None:
    # The only pane sits in the hub (not a spoke worktree) → idle, no ensure.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf "%s\\n" /repo/wt-115; }',
        '_pane_paths() { printf "%s\\n" /repo; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IDLE"


def test_spoke_pane_live_false_when_no_spoke_worktrees() -> None:
    # No linked spoke worktrees at all → idle regardless of panes.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf ""; }',
        '_pane_paths() { printf "%s\\n" /repo/wt-115; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IDLE"


# ── real-worktree coverage (porcelain parse, hub-exclusion, symlink resolve) ──


@pytest.fixture()
def hub_with_spoke(tmp_path: Path) -> tuple[Path, Path]:
    """A real git hub checkout with one linked spoke worktree.

    Returns (hub, spoke) as canonical paths so callers can assert against them
    without the tmp_path symlink (/var → /private/var on macOS) getting in the way.
    """
    hub = tmp_path / "hub"
    hub.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(hub), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    (hub / "f").write_text("x")
    git("add", "f")
    git("commit", "-qm", "init")
    spoke = tmp_path / "spoke"
    git("worktree", "add", "-q", "-b", "spoke", str(spoke))
    return Path(os.path.realpath(hub)), Path(os.path.realpath(spoke))


def test_spoke_worktree_paths_lists_spoke_excludes_hub(hub_with_spoke: tuple[Path, Path]) -> None:
    # The real porcelain parse + hub self-exclusion: only the spoke worktree is
    # listed, never the hub main checkout itself.
    hub, spoke = hub_with_spoke
    result = _call(f"MAIN_ROOT={hub}; _spoke_worktree_paths")

    assert result.returncode == 0, result.stderr
    resolved = [os.path.realpath(ln) for ln in result.stdout.splitlines() if ln.strip()]
    assert str(spoke) in resolved
    assert str(hub) not in resolved


def test_spoke_pane_live_resolves_symlinked_pane_path(
    hub_with_spoke: tuple[Path, Path], tmp_path: Path
) -> None:
    # A pane whose path reaches the spoke through a symlinked root (the /tmp →
    # /private/tmp trap) still correlates: spoke_pane_live canonicalizes both the
    # real spoke worktree path and the symlinked pane path. Would read IDLE if
    # wt_realpath resolution were dropped, so this genuinely exercises it.
    hub, spoke = hub_with_spoke
    link = tmp_path / "link"
    link.symlink_to(spoke)
    parts = [
        f"MAIN_ROOT={hub}",
        f'_pane_paths() {{ printf "%s\\n" {link}; }}',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LIVE"
