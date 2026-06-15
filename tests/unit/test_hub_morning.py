"""Unit tests for shared/skills/hub/scripts/hub-morning.sh (issue #40 ST6, Phase 4).

The morning report assembles the three terminal markers + a pre-computed land-triage
into a worklist sorted fastest -> slowest human effort:
  LAND     ready/N, merges clean, agent-approved -> rubber-stamp /land
  EYEBALL  accept/N, built + pushed + agent-reviewed -> glance then land/send back
  THINK    blocked/N -> read the parked blocker, answer + re-queue
  CONFLICTS ready/N whose throwaway merge hit a conflict -> hand-resolution
A gate/N (still parked at the PLAN gate) is a footer, not a worklist tier.

Land-triage merges each branch onto the default in a HERMETIC detached temp worktree
and probes ONLY for a merge conflict (NO pytest — the real gate fires at /land's push,
keeping triage off the GIT_DIR-leak/tripwire path). The pure tiering/next-command
helpers and the merge probe are sourced and called directly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HUB_MORNING = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-morning.sh"
)
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_MORNING}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


# ── tier_for_marker: marker namespace + mergeability -> effort tier ───────────


@pytest.mark.parametrize(
    "kind,mergeable,expected",
    [
        ("ready", "clean", "LAND"),
        ("ready", "conflict", "CONFLICTS"),
        ("accept", "clean", "EYEBALL"),
        ("blocked", "clean", "THINK"),
        ("gate", "clean", ""),  # still parked at PLAN — a footer, not a worklist tier
    ],
)
def test_tier_for_marker(kind: str, mergeable: str, expected: str) -> None:
    result = _call(f"tier_for_marker {kind} {mergeable}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_next_command_land_is_rubber_stamp() -> None:
    result = _call("next_command LAND 101")

    assert "/land 101" in result.stdout


def test_next_command_think_is_answer_and_requeue() -> None:
    result = _call("next_command THINK 101")

    out = result.stdout.lower()
    assert "re-queue" in out or "requeue" in out or "answer" in out


# ── probe_merge: hermetic land-triage (detached temp worktree, conflict-only) ──


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "base.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    return r


def test_probe_merge_clean(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feature/1-add")
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: add", "-m", "Refs #1")
    _git(repo, "checkout", "-q", "main")

    result = _call(f"probe_merge {repo} feature/1-add main")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


def test_probe_merge_conflict_and_no_leftover_worktree(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feature/2-edit")
    (repo / "base.txt").write_text("branch change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: edit", "-m", "Refs #2")
    _git(repo, "checkout", "-q", "main")
    (repo / "base.txt").write_text("main change\n")  # diverge the same line
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix: edit", "-m", "Refs #0")

    result = _call(f"probe_merge {repo} feature/2-edit main")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "conflict"
    # The throwaway worktree must be torn down — never strand a probe worktree.
    assert "probe" not in _git(repo, "worktree", "list")


# ── end-to-end: the report tiers the markers and prints the next command ──────


def _seed_spoke(hub: Path, tmp_path: Path, issue: int, kind: str, slug: str = "wip") -> Path:
    """A feature worktree one commit ahead of main, tagged <kind>/<issue> at its tip."""
    wt = tmp_path / f"wt-{issue}"
    _git(hub, "worktree", "add", "-q", "-b", f"feature/{issue}-{slug}", str(wt))
    (wt / f"f{issue}.txt").write_text("work\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"feat: {issue}", "-m", f"Refs #{issue}")
    _git(hub, "tag", "-a", f"{kind}/{issue}", "-m", kind, f"feature/{issue}-{slug}")
    return wt


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    h = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(h)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(h, "config", k, v)
    (h / "README.md").write_text("seed\n")
    _git(h, "add", "-A")
    _git(h, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    return h


def _run_report(hub: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HUB_MORNING)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "NIGHT_STATE_DIR": str(tmp_path / "night-state")},
    )


def test_report_tiers_each_terminal_marker(hub: Path, tmp_path: Path) -> None:
    _seed_spoke(hub, tmp_path, 101, "ready")
    _seed_spoke(hub, tmp_path, 102, "accept")
    _seed_spoke(hub, tmp_path, 103, "blocked")

    result = _run_report(hub, tmp_path)

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "LAND" in out and "/land 101" in out
    assert "EYEBALL" in out and "#102" in out
    assert "THINK" in out and "#103" in out


def test_report_shows_blocker_reason_for_blocked(hub: Path, tmp_path: Path) -> None:
    wt = tmp_path / "wt-103"
    _git(hub, "worktree", "add", "-q", "-b", "feature/103-wip", str(wt))
    (wt / "f.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "feat: x", "-m", "Refs #103")
    _git(hub, "tag", "-a", "blocked/103", "-m", "ambiguous acceptance criteria", "feature/103-wip")

    result = _run_report(hub, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ambiguous acceptance criteria" in result.stdout, (
        "the blocker reason (tag body) is shown"
    )
