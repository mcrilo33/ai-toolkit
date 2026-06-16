"""Unit tests for shared/skills/hub/scripts/hub-return.sh (issue #40 ST6, Phase 4).

The return report assembles the three terminal markers + a pre-computed land-triage
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

HUB_RETURN = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-return.sh"
)
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_RETURN}"; {fn_call}'],
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


# ── _repo_is_real: the #55 fixture-leak guard (mirror of queries' real-spoke filter) ──


def test_repo_is_real_accepts_the_toolkit_checkout() -> None:
    # The real hub (and its worktrees) basename starts with the toolkit name.
    real = _call("MAIN_ROOT=/Users/me/ai-toolkit _repo_is_real && echo REAL || echo FAKE")
    worktree = _call("MAIN_ROOT=/Users/me/ai-toolkit-55 _repo_is_real && echo REAL || echo FAKE")

    assert real.stdout.strip() == "REAL", real.stderr
    assert worktree.stdout.strip() == "REAL", worktree.stderr


def test_repo_is_real_rejects_a_sandbox_basename() -> None:
    fake = _call("MAIN_ROOT=/tmp/pytest-xyz/proj _repo_is_real && echo REAL || echo FAKE")

    assert fake.stdout.strip() == "FAKE", fake.stderr


def test_repo_is_real_honours_an_explicit_prefix() -> None:
    # The override seam the test harness uses to run report() over a sandbox repo.
    matched = _call(
        "HUB_REAL_REPO_PREFIX=proj MAIN_ROOT=/tmp/x/proj-8 _repo_is_real && echo REAL || echo FAKE"
    )

    assert matched.stdout.strip() == "REAL", matched.stderr


def test_repo_is_real_empty_prefix_disables_the_guard() -> None:
    disabled = _call(
        "HUB_REAL_REPO_PREFIX= MAIN_ROOT=/tmp/anything _repo_is_real && echo REAL || echo FAKE"
    )

    assert disabled.stdout.strip() == "REAL", disabled.stderr


def _run_report_guarded(hub: Path, tmp_path: Path, prefix: str) -> subprocess.CompletedProcess[str]:
    """Run the report with the #55 repo-real guard active at ``prefix``."""
    return subprocess.run(
        ["bash", str(HUB_RETURN)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env={
            **_GIT_ENV,
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "HUB_REAL_REPO_PREFIX": prefix,
        },
    )


def test_report_refuses_to_render_when_repo_is_not_real(hub: Path, tmp_path: Path) -> None:
    # The sandbox hub basename ('hub') is not 'ai-toolkit*': the worklist must not
    # render markers as a fake worklist; a one-line notice is emitted instead.
    _seed_spoke(hub, tmp_path, 101, "ready")

    result = _run_report_guarded(hub, tmp_path, prefix="ai-toolkit")

    assert result.returncode == 0, result.stderr
    assert "/land 101" not in result.stdout
    assert "LAND — rubber-stamp" not in result.stdout
    assert "not a real" in result.stdout.lower() or "skipping" in result.stdout.lower()


def test_report_renders_when_repo_matches_prefix(hub: Path, tmp_path: Path) -> None:
    _seed_spoke(hub, tmp_path, 101, "ready")

    result = _run_report_guarded(hub, tmp_path, prefix="hub")  # matches the sandbox basename

    assert result.returncode == 0, result.stderr
    assert "/land 101" in result.stdout


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
    # The throwaway worktree must be torn down — only the main checkout remains
    # (count entries; the repo's own tmp path happens to contain "probe").
    entries = [ln for ln in _git(repo, "worktree", "list").splitlines() if ln.strip()]
    assert len(entries) == 1, f"a probe worktree was stranded: {entries}"


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
        ["bash", str(HUB_RETURN)],
        cwd=str(hub),
        capture_output=True,
        text=True,
        # Empty prefix disables the #55 repo-real guard so the worklist renders over
        # the temp sandbox hub; the guard itself is covered by its own tests.
        env={
            **_GIT_ENV,
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
            "HUB_REAL_REPO_PREFIX": "",
        },
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
    # spoke-ready.sh emits the marker as subject=state word, body=reason
    # (git tag -a -m "<state>" -m "<reason>"); the report reads the body as trust.
    _git(
        hub,
        "tag",
        "-a",
        "blocked/103",
        "-m",
        "blocked",
        "-m",
        "ambiguous acceptance criteria",
        "feature/103-wip",
    )

    result = _run_report(hub, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "ambiguous acceptance criteria" in result.stdout, (
        "the blocker reason (tag body) is shown"
    )


def _run_triage(hub: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HUB_RETURN), "--triage"],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "AFK_STATE_DIR": str(tmp_path / "afk-state")},
    )


def test_triage_then_report_routes_a_conflicting_ready_to_conflicts(
    hub: Path, tmp_path: Path
) -> None:
    # End-to-end of the load-bearing path: land_triage_all merge-probes a ready
    # branch that conflicts with the (diverged) default, caches "conflict", and the
    # report then routes that ready/N into CONFLICTS rather than LAND.
    wt = tmp_path / "wt-104"
    _git(hub, "worktree", "add", "-q", "-b", "feature/104-edit", str(wt))
    (wt / "README.md").write_text("branch change\n")  # same file the hub seeds
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "feat: edit", "-m", "Refs #104")
    _git(hub, "tag", "-a", "ready/104", "-m", "ready", "feature/104-edit")
    # Diverge main on the same file so the merge conflicts.
    (hub / "README.md").write_text("main change\n")
    _git(hub, "add", "-A")
    _git(hub, "commit", "-qm", "fix: edit", "-m", "Refs #0")

    triage = _run_triage(hub, tmp_path)
    assert triage.returncode == 0, triage.stderr
    cache = tmp_path / "afk-state" / "land-triage"
    assert cache.is_file() and "104 conflict" in cache.read_text(), "triage must cache the verdict"

    result = _run_report(hub, tmp_path)

    assert result.returncode == 0, result.stderr
    out = result.stdout
    conflicts_section = out.split("CONFLICTS")[1] if "CONFLICTS" in out else ""
    assert "#104" in conflicts_section, "a conflicting ready/N must land in the CONFLICTS tier"
    land_section = out.split("LAND")[1].split("EYEBALL")[0]
    assert "#104" not in land_section, "a conflicting ready/N must NOT be in LAND"


# ── --comments: echo each terminal marker as a gh issue comment (AC#3) ────────
# AC#3 wants the three terminal markers emitted as tags AND issue comments. The
# spoke stays gh-READ-ONLY (it only writes the annotated tag); the HUB echoes the
# tag body as a `gh issue comment`, idempotently (one per issue+kind) and
# best-effort (no gh / no remote -> skip, never crash).


def _seed_marker(hub: Path, tmp_path: Path, issue: int, kind: str, reason: str) -> None:
    """A feature worktree tagged <kind>/<issue> whose tag body is <reason>."""
    wt = tmp_path / f"wt-{issue}"
    _git(hub, "worktree", "add", "-q", "-b", f"feature/{issue}-wip", str(wt))
    (wt / f"f{issue}.txt").write_text("work\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"feat: {issue}", "-m", f"Refs #{issue}")
    _git(hub, "tag", "-a", f"{kind}/{issue}", "-m", kind, "-m", reason, f"feature/{issue}-wip")


def _run_comments(hub: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run `hub-return.sh --comments` with a gh stub that logs each comment."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh_log = tmp_path / "gh-comments.log"
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "issue" ] && [ "$2" = "comment" ]; then\n'
        '  printf "comment %s :: %s\\n" "$3" "$5" >> "$GH_LOG"\n'
        "fi\n"
        "exit 0\n"
    )
    gh.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(HUB_RETURN), "--comments"],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env={
            **_GIT_ENV,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GH_LOG": str(gh_log),
            "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        },
    )
    return proc, gh_log


def test_comments_echo_each_terminal_marker(hub: Path, tmp_path: Path) -> None:
    _seed_marker(hub, tmp_path, 101, "ready", "all gates passed")
    _seed_marker(hub, tmp_path, 102, "accept", "needs a human glance")
    _seed_marker(hub, tmp_path, 103, "blocked", "ambiguous acceptance criteria")

    proc, gh_log = _run_comments(hub, tmp_path)

    assert proc.returncode == 0, proc.stderr
    log = gh_log.read_text() if gh_log.exists() else ""
    assert "comment 101" in log and "comment 102" in log and "comment 103" in log
    assert "needs a human glance" in log, "the comment body carries the marker's reason"


def test_comments_skip_the_gate_park(hub: Path, tmp_path: Path) -> None:
    # gate/N is a non-terminal PLAN park, not a completion marker — no comment.
    _seed_marker(hub, tmp_path, 104, "gate", "plan")

    proc, gh_log = _run_comments(hub, tmp_path)

    assert proc.returncode == 0, proc.stderr
    log = gh_log.read_text() if gh_log.exists() else ""
    assert "comment 104" not in log, "the gate park must not be echoed as a terminal comment"


def test_comments_are_idempotent(hub: Path, tmp_path: Path) -> None:
    _seed_marker(hub, tmp_path, 101, "accept", "needs a human glance")

    _run_comments(hub, tmp_path)
    _proc, gh_log = _run_comments(hub, tmp_path)

    log = gh_log.read_text() if gh_log.exists() else ""
    assert log.count("comment 101") == 1, "a marker must be echoed once, not on every run"
