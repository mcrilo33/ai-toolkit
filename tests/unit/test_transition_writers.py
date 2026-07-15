"""Actor writers for the #300 lifecycle transition log (migration step 2).

Each lifecycle ACTOR records the transition it causes, at the instant it causes it.
These pin the contract the detector conversions will later depend on:

  worktree-new.sh   -> dispatched
  spoke-ready.sh    -> parked-gate | ready | accepted | blocked
  spoke-push.sh     -> pushing (INTENT-FIRST, before the multi-minute gate) | pushed
  worktree-land.sh  -> landing (INTENT-FIRST, before the merge) | landed | land_failed

Shadow-only: nothing reads the log for decisions yet, so these assert the RECORD,
not any behavior change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WT_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"


def _run(snippet: str, state_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{WT_LIB}" 2>/dev/null; {snippet}'],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "AFK_STATE_DIR": str(state_dir),
        },
    )


def _log(state_dir: Path, issue: int) -> str:
    p = state_dir / "transitions" / f"{issue}.jsonl"
    return p.read_text() if p.is_file() else ""


# --- the lib seam every actor inherits ---


def test_worktree_lib_exposes_the_log_to_actors(tmp_path: Path) -> None:
    # All four actors source worktree-lib.sh; the locator lives there ONCE.
    result = _run("command -v afk_tlog_transition >/dev/null && echo found", tmp_path)

    assert "found" in result.stdout, "worktree-lib.sh must locate transition-log.sh for the actors"


def test_wrapper_records_a_transition(tmp_path: Path) -> None:
    _run('wt_tlog_transition 42 landing worktree-land.sh "merge" \'{"branch":"b"}\'', tmp_path)

    line = _log(tmp_path, 42)
    assert '"to":"landing"' in line
    assert '"actor":"worktree-land.sh"' in line


def test_wrapper_no_ops_on_an_adhoc_slug(tmp_path: Path) -> None:
    # A /quick branch has no issue number — the wrappers must skip, not crash or
    # scatter a stray log file.
    result = _run('wt_tlog_transition "quick-slug" pushing spoke-push.sh x; echo rc=$?', tmp_path)

    assert "rc=0" in result.stdout
    assert not (tmp_path / "transitions").exists()


def test_wrapper_never_fails_the_actor_without_the_lib(tmp_path: Path) -> None:
    # Best-effort contract: if transition-log.sh is missing, an actor must still
    # succeed. Simulate by calling the wrapper with the function undefined.
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'. "{WT_LIB}" 2>/dev/null; unset -f afk_tlog_transition; '
            "wt_tlog_transition 42 landing a b; echo rc=$?",
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AFK_STATE_DIR": str(tmp_path)},
    )

    assert "rc=0" in result.stdout


# --- intent-first: the property that kills the #290 class ---


def test_land_records_landing_before_the_merge(tmp_path: Path) -> None:
    # The #290 root cause: a land consumes the ready tag and kills the spoke window
    # BEFORE removing the worktree, so mid-land the watchdog saw "dead pane, no
    # marker" and false-fired. `landing` must therefore be recorded BEFORE the merge
    # runs — not after it completes, which would leave the same blind window.
    # Proven by source order: the write precedes the `git merge` call.
    land = (REPO_ROOT / "scripts" / "worktree-land.sh").read_text()
    write_at = land.find('wt_tlog_transition "$ISSUE" landing')
    merge_at = land.find('if ! git merge --no-edit "$WT_BRANCH"')

    assert write_at != -1, "worktree-land.sh must record the landing transition"
    assert merge_at != -1
    assert write_at < merge_at, "landing must be recorded BEFORE the merge starts (#290)"


def test_push_records_pushing_before_the_push(tmp_path: Path) -> None:
    # `pushing` is unlearnable today: the pre-push gate runs the suite for minutes and
    # looks identical to a stall. Recording it BEFORE wt_git_push is what makes the
    # phase visible; recording it after would defeat the purpose.
    push = (REPO_ROOT / "scripts" / "spoke-push.sh").read_text()
    write_at = push.find('wt_tlog_transition "$_SP_ISSUE" pushing')
    push_at = push.find("wt_git_push -u origin")

    assert write_at != -1, "spoke-push.sh must record the pushing transition"
    assert push_at != -1
    assert write_at < push_at, "pushing must be recorded BEFORE the push/gate starts"


# --- the marker-kind -> state map (the #292 conflation) ---


def test_spoke_ready_maps_accept_to_a_distinct_state(tmp_path: Path) -> None:
    # #292: slot_state conflates accept/ with ready/ (both read `done`), so a
    # human-sign-off close escalates as an un-landed mergeable branch. The log's
    # vocabulary keeps them distinct.
    ready = (REPO_ROOT / "scripts" / "spoke-ready.sh").read_text()

    assert "accept)  printf 'accepted" in ready
    assert "ready)   printf 'ready" in ready
    assert "gate)    printf 'parked-gate" in ready
    assert "blocked) printf 'blocked" in ready
