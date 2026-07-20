"""Unit tests for the pre-push repo-integrity tripwire (issue #31).

The #29/#30 GIT_DIR leak corrupted the real repo SILENTLY: a test fixture's
``git`` call moved ``main`` and flipped ``core.bare`` during the pre-push gate,
and nothing noticed. #21 + #30 fixed the known vector (env stripping); this
tripwire is the safety net for the whole CLASS of isolation breaches.

Before the gate runs pytest it snapshots the real repo's integrity markers —
``HEAD`` + every local ref tip, ``core.bare``, ``core.worktree`` — and re-reads
them after. A genuine change means a test escaped isolation and mutated THIS
repo: the push is aborted (non-zero), the snapshot restored, and the changed
marker named. A hermetic test that creates/deletes its OWN tmpdir repo must NOT
trip it.

Two #135 refinements: worktrees share one ref store, so a fast-forward advance
(or creation) of a branch checked out in a live sibling worktree is legitimate
concurrent spoke work, not an escape — it neither trips the check nor gets
rewound. And restore never orphans commits: a ref is never rewound to a strict
ancestor of its current tip (warn + abort instead), and an appeared ref checked
out in a registered worktree is never deleted.

Issue #188 narrows the gate further: the pre-push stdin names exactly the refs
the push updates, and the gate's tripwire snapshot covers ONLY those (plus HEAD
and the config markers) — any other ref moving mid-gate is concurrent-spoke
behavior, not a breach.

Two layers are covered:

* the ``tripwire_*`` library in ``lib/utils.sh`` (capture / check / restore),
  exercised directly against a throwaway repo, and
* the ``test-select.sh`` pre-push gate, exercised with a ``pytest`` stub that
  mutates (or does not mutate) the real repo, asserting trip+restore+abort vs a
  clean pass.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "shared" / "hooks"
UTILS = HOOKS / "lib" / "utils.sh"
TEST_SELECT = HOOKS / "test-select.sh"
ZERO_SHA = "0" * 40
BREACH_RC = 97

# The tripwire family (issue #328): these tests exercise the repo-integrity tripwire by
# writing refs (update-ref, reset --hard, commit) and are the canonical "escape isolation
# and rewrite real refs" set that forces the suite's conservative full-run behaviour. Run
# them single-process in the serial phase, never under xdist workers (AC#5 — the tripwire
# family still runs serially and still guards). See docs/test-gate.md.
pytestmark = pytest.mark.serial

# Pin git config to nothing so a host's global config can't reach these commits.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).strip()


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


def _commit(repo: Path, files: dict[str, str], msg: str = "change") -> str:
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _rev(repo)


def _stdin(local_sha: str, remote_sha: str, ref: str = "refs/heads/main") -> str:
    return f"{ref} {local_sha} {ref} {remote_sha}\n"


# --- library-level: tripwire_capture / tripwire_check / tripwire_restore ---------


def _lib(repo: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet with lib/utils.sh sourced and cwd at `repo`."""
    script = f'set -euo pipefail\nsource "{UTILS}"\ncd "{repo}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=_GIT_ENV)


def test_check_clean_returns_zero(repo: Path) -> None:
    proc = _lib(repo, 'b="$(tripwire_capture)"; tripwire_check "$b" && echo CLEAN')

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_detects_ref_move(repo: Path) -> None:
    # Snapshot, then move main onto a fresh empty commit; check must report it.
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        "git commit --allow-empty -q -m sneak\n"
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/main" in proc.stdout


def test_restore_refuses_rewind_to_strict_ancestor(repo: Path) -> None:
    # A ref that only GAINED commits (snapshot tip is a strict ancestor of the
    # current tip) must not be rewound — that orphans commits (issue #135's
    # data loss). Restore warns and leaves the ref alone; the abort, not the
    # rewind, is the protection.
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit commit --allow-empty -q -m sneak\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    tip = _rev(repo, "main")
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"  # not rewound
    assert "NOT rewinding" in proc.stderr
    assert "refs/heads/main" in proc.stderr
    assert tip == _rev(repo, "main")


def test_restore_still_recovers_rewound_ref(repo: Path) -> None:
    # The inverse move — the ref LOST commits during the run (snapshot tip is
    # ahead of the current tip) — is genuine corruption; restore must still
    # bring the ref forward to the snapshot.
    tip = _commit(repo, {"src/a.py": "x = 1\n"})
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit reset -q --hard HEAD~1\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "main") == tip  # the lost commit is back


def test_restore_writes_labeled_reflog_entry(repo: Path) -> None:
    # Issue #188: a killed mid-gate push once rolled a branch back with NO
    # reflog trace, leaving committed work unreachable. Every restore must be
    # reflog-visible: a labeled entry whose previous position (`ref@{1}`) is
    # the pre-restore tip, so the rollback is recoverable from `git reflog`.
    tip = _commit(repo, {"src/a.py": "x = 1\n"})
    rewound = _rev(repo, "main~1")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit reset -q --hard HEAD~1\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "main") == tip  # restored to the snapshot
    assert "tripwire: restore after aborted gate" in _git(repo, "reflog", "show", "main")
    assert _rev(repo, "main@{1}") == rewound  # the entry points back at the pre-restore tip


def test_restore_creates_reflog_where_none_exists(repo: Path) -> None:
    # The #188 incident shape: reflog logging is OFF for the ref (a bare hub
    # ref store / core.logAllRefUpdates=false), so without --create-reflog the
    # rollback leaves NO trace at all — `-m` alone writes nothing there. The
    # restore must create the reflog and leave the labeled entry.
    tip = _commit(repo, {"src/a.py": "x = 1\n"})
    _git(repo, "config", "core.logAllRefUpdates", "false")
    _git(repo, "branch", "side")  # created with logging off — no reflog exists
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        "git update-ref refs/heads/side refs/heads/side~1\n"  # lost a commit mid-run
        'tripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "side") == tip  # restored to the snapshot
    assert "tripwire: restore after aborted gate" in _git(repo, "reflog", "show", "side")


def test_restore_skips_deleting_ref_checked_out_in_worktree(repo: Path, tmp_path: Path) -> None:
    # A ref that appeared during the run but is checked out in a registered
    # worktree is a live spoke's branch — deleting it destroys the spoke's
    # anchor. Restore must leave it in place.
    wt = tmp_path / "spawned"
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git worktree add -q -b feature/spawned "{wt}"\n'
        'tripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _rev(repo, "refs/heads/feature/spawned")  # still exists
    assert "NOT deleting" in proc.stderr  # and the skip is named


def test_check_detects_bare_flip(repo: Path) -> None:
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        "git config core.bare true\n"
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "core.bare" in proc.stdout


def test_restore_resets_bare_flip(repo: Path) -> None:
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\ngit config core.bare true\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"


def test_restore_preserves_worktree_path_with_spaces(repo: Path, tmp_path: Path) -> None:
    # core.worktree can be a path containing spaces; restore must round-trip it
    # verbatim, not truncate at the first space. Use a real dir (git validates it).
    spaced_dir = tmp_path / "work dir"
    spaced_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _git(repo, "config", "core.worktree", str(spaced_dir))
    proc = _lib(
        repo,
        f'b="$(tripwire_capture)"\ngit config core.worktree "{other_dir}"\ntripwire_restore "$b"',
    )

    assert proc.returncode == 0, proc.stderr
    assert _git(repo, "config", "--get", "core.worktree").strip() == str(spaced_dir)


# --- live sibling worktrees (issue #135) ------------------------------------------
# Worktrees share one ref store: a live sibling spoke committing during a long
# gate advances its own branch by fast-forward. That is legitimate concurrent
# work, not a test escape — the tripwire must not trip on it (and must not
# rewind it). Genuine escapes still trip: any move of the CURRENT branch, a
# non-FF move/rewind of a sibling ref, and moves of refs no worktree has
# checked out.


@pytest.fixture()
def spoke(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree of `repo` on its own branch — a live sibling spoke."""
    wt = tmp_path / "spoke"
    _git(repo, "worktree", "add", "-q", "-b", "feature/spoke", str(wt))
    return wt


def test_check_ignores_sibling_worktree_ff_advance(repo: Path, spoke: Path) -> None:
    # A live spoke committing mid-gate fast-forwards its checked-out branch;
    # the check must stay CLEAN (issue #135's false breach).
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git -C "{spoke}" commit --allow-empty -q -m spoke-work\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_ignores_branch_created_with_new_worktree(repo: Path, tmp_path: Path) -> None:
    # The hub spawning a new spoke mid-gate creates a branch + worktree; the
    # new ref belongs to a registered worktree and must not trip the check.
    wt = tmp_path / "spawned"
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git worktree add -q -b feature/spawned "{wt}"\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_check_trips_on_sibling_worktree_rewind(repo: Path, spoke: Path) -> None:
    # A sibling ref moving BACKWARD is not spoke work — something destroyed
    # commits in the shared ref store. Must still trip.
    _git(spoke, "commit", "--allow-empty", "-qm", "spoke-work")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        f'git -C "{spoke}" reset -q --hard HEAD~1\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/feature/spoke" in proc.stdout


def test_check_trips_on_ff_advance_of_unregistered_branch(repo: Path) -> None:
    # An FF advance of a branch NO worktree has checked out has no live-spoke
    # explanation — that is exactly what an escaped test looks like. Must trip.
    _git(repo, "branch", "side")
    proc = _lib(
        repo,
        'b="$(tripwire_capture)"\n'
        'sha="$(git commit-tree -m sneak -p refs/heads/side "$(git rev-parse side^{tree})")"\n'
        'git update-ref refs/heads/side "$sha"\n'
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/side" in proc.stdout


# --- scoped tripwire: only the land's own refs (issue #205) -----------------------
# worktree-land's merge-sanity check runs pytest INSIDE the shared hub ref store,
# where sibling spokes legitimately move their own refs (a committed head, a pushed
# branch's remote-tracking ref, an /afk drain's ready/<N> tag). run_under_tripwire_scoped
# narrows the snapshot to the refs the land owns (refs/heads/<default>), so a concurrent
# sibling ref move is neither a breach nor rolled back by the restore — while a real
# escape onto the owned ref is still caught.


def test_scoped_check_ignores_out_of_scope_ref_change(repo: Path) -> None:
    # With the scope pinned to refs/heads/main, creating an out-of-scope tag (a
    # sibling's ready/<N> push) leaves the check CLEAN.
    proc = _lib(
        repo,
        '_TRIPWIRE_SCOPE="refs/heads/main"\n'
        'b="$(tripwire_capture)"\n'
        "git tag ready/99 HEAD\n"
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLEAN" in proc.stdout


def test_scoped_check_multi_ref_list(repo: Path, tmp_path: Path) -> None:
    # The scope is documented as a newline-separated list; with two in-scope refs a
    # move of EITHER trips the check while an out-of-scope tag stays clean. Guards
    # the BSD/macOS awk trap where a newline in an `-v` assignment aborts awk and
    # silently empties the snapshot (disabling the tripwire).
    _git(repo, "branch", "release")
    proc = _lib(
        repo,
        "_TRIPWIRE_SCOPE=$"
        "'"
        "refs/heads/main\nrefs/heads/release"
        "'"
        "\n"
        'b="$(tripwire_capture)"\n'
        "git update-ref refs/heads/release refs/heads/main\n"  # not an ancestor move
        "git commit --allow-empty -q -m sneak\n"  # advances main too
        "git tag ready/99 HEAD\n"  # out of scope — must be ignored
        'out="$(tripwire_check "$b")" && echo CLEAN || echo "CHANGED $out"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "CHANGED" in proc.stdout
    assert "refs/heads/main" in proc.stdout
    assert "ready/99" not in proc.stdout  # out-of-scope tag never enters the diff


def test_scoped_run_ignores_concurrent_out_of_scope_tag(repo: Path) -> None:
    # A scoped run whose command creates an out-of-scope tag returns the command's
    # own exit code (0), prints no breach, and leaves the tag in place (pre-#205 the
    # whole-repo restore deleted it).
    proc = _lib(
        repo,
        'run_under_tripwire_scoped "refs/heads/main" bash -c "git tag ready/99 HEAD" && echo OK',
    )

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _git(repo, "tag", "--list").strip() == "ready/99"  # not rolled back


def test_scoped_run_still_catches_escape_on_owned_ref(repo: Path) -> None:
    # An escape onto the owned ref (refs/heads/main) is still a breach: the scoped
    # run returns BREACH_RC and names the ref.
    proc = _lib(
        repo,
        'run_under_tripwire_scoped "refs/heads/main" '
        'bash -c "git commit --allow-empty -q -m sneak" || echo "RC=$?"',
    )

    assert proc.returncode == 0, proc.stderr
    assert f"RC={BREACH_RC}" in proc.stdout
    assert "refs/heads/main" in proc.stderr


# --- observe-only sweep tripwire: verdict never overridden, never restores (#267) ---
# The #124 post-land sweep re-runs the full suite on the live hub/main checkout as a
# NON-PUSHING diagnostic. A concurrent /afk drain legitimately FF-advances
# main/origin/*/sibling refs, stamps needs-human-land/* tags, and moves HEAD the whole
# time — so run_under_tripwire_observe must (a) return the wrapped command's OWN exit
# code, never BREACH_RC, (b) never restore any ref, and (c) only LOG a note when a
# change looks like a genuine escape the drain never produces (a config flip, a ref
# deletion, or a non-fast-forward move). The wrapped command's stdout+stderr are
# captured to the <capfile> first arg (the sweep reads failing ids from there).


def test_observe_returns_command_exit_despite_ff_ref_move(repo: Path, tmp_path: Path) -> None:
    # The drain FF-advances main/HEAD mid-sweep; observe returns the suite's own exit
    # (0), never BREACH_RC, and never rewinds the ref.
    cap = tmp_path / "cap.log"
    proc = _lib(
        repo,
        f'run_under_tripwire_observe "{cap}" bash -c "git commit --allow-empty -q -m drain-lands"'
        ' && echo "RC=0"',
    )

    assert proc.returncode == 0, proc.stderr
    assert "RC=0" in proc.stdout
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "drain-lands"  # not rewound


def test_observe_propagates_suite_failure_exit(repo: Path, tmp_path: Path) -> None:
    # A genuinely failing suite surfaces its OWN exit code (1), not the tripwire's 97.
    cap = tmp_path / "cap.log"
    proc = _lib(repo, f'run_under_tripwire_observe "{cap}" bash -c "exit 1" || echo "RC=$?"')

    assert proc.returncode == 0, proc.stderr
    assert "RC=1" in proc.stdout
    assert f"RC={BREACH_RC}" not in proc.stdout


def test_observe_tolerates_concurrent_tag_and_remote_ref(repo: Path, tmp_path: Path) -> None:
    # needs-human-land/* tag creation + a remote-tracking ref FF are legitimate drain
    # movement: the run stays green, prints no breach, and deletes nothing.
    cap = tmp_path / "cap.log"
    proc = _lib(
        repo,
        f'run_under_tripwire_observe "{cap}" bash -c "'
        "git commit --allow-empty -q -m land; "
        "git update-ref refs/remotes/origin/main HEAD; "
        'git tag needs-human-land/261 HEAD" && echo OK',
    )

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _git(repo, "tag", "--list").strip() == "needs-human-land/261"  # not deleted


def test_observe_logs_note_on_config_flip_without_restoring(repo: Path, tmp_path: Path) -> None:
    # A config-marker flip IS the #29/#30 escape class the drain never produces:
    # observe LOGS a note, still returns the suite's exit, and does NOT restore it
    # (the non-pushing sweep restores nothing).
    cap = tmp_path / "cap.log"
    proc = _lib(
        repo,
        f'run_under_tripwire_observe "{cap}" bash -c "git config core.bare true" && echo OK',
    )

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "NOTE" in proc.stderr
    assert "core.bare" in proc.stderr
    assert _git(repo, "config", "--get", "core.bare").strip() == "true"  # not restored


def test_observe_logs_note_on_non_ff_ref_move_without_restoring(repo: Path, tmp_path: Path) -> None:
    # A ref REWIND (non-fast-forward) is garbage the drain never produces: observed
    # and logged, but never a red verdict and never rewound back.
    _git(repo, "commit", "--allow-empty", "-qm", "c1")
    tip = _rev(repo, "main")
    cap = tmp_path / "cap.log"
    proc = _lib(
        repo,
        f'run_under_tripwire_observe "{cap}" bash -c "git reset -q --hard HEAD~1" && echo OK',
    )

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "NOTE" in proc.stderr
    assert "refs/heads/main" in proc.stderr
    assert _rev(repo, "main") != tip  # left rewound — observe restores nothing


def test_observe_tolerates_concurrent_ref_deletion(repo: Path, tmp_path: Path) -> None:
    # A concurrent /afk drain legitimately DELETES refs at land teardown (the landed
    # feature branch, its remote-tracking ref, a needs-human-land/* tag). A vanished ref
    # is indistinguishable from an escape here, so observe tolerates it silently — no
    # NOTE — rather than raise the false alarm #267 removes.
    _git(repo, "branch", "feature/251-landed", "main")
    cap = tmp_path / "cap.log"
    proc = _lib(
        repo,
        f'run_under_tripwire_observe "{cap}" bash -c "git branch -D feature/251-landed" && echo OK',
    )

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
    assert "NOTE" not in proc.stderr  # a deletion is legit drain teardown, not flagged


def test_observe_captures_command_output_to_capfile(repo: Path, tmp_path: Path) -> None:
    # The wrapped command's stdout+stderr land in <capfile> (the sweep reads the
    # failing ids from there), not on the observe caller's streams.
    cap = tmp_path / "cap.log"
    proc = _lib(repo, f'run_under_tripwire_observe "{cap}" bash -c "echo hello-suite"')

    assert proc.returncode == 0, proc.stderr
    assert "hello-suite" in cap.read_text()


# --- integration: the test-select.sh pre-push gate -------------------------------


def _make_pytest_stub(bindir: Path, body: str) -> None:
    """Install a `pytest` stub: answers `--help`, else runs `body` then exits 0."""
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "pytest").write_text(
        f'#!/bin/sh\ncase "$1" in --help|-h) echo "usage: pytest"; exit 0 ;; esac\n{body}\nexit 0\n'
    )
    (bindir / "pytest").chmod(0o755)


def _run_select(
    repo: Path, stdin: str, bindir: Path, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def test_breach_ref_move_aborts_and_restores(repo: Path, tmp_path: Path) -> None:
    # A FULL-tier diff (.yml) so the stub runs as the suite; the stub mutates the
    # real repo (moves main) the way an escaped test would.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    before = _rev(repo, "main")
    _make_pytest_stub(tmp_path / "bin", "git commit --allow-empty -q -m sneak")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == BREACH_RC, proc.stderr  # push aborted, not 0
    assert "refs/heads/main" in proc.stderr  # names the changed marker
    # The sneak commit FF-advanced main, so restore refuses the rewind (data
    # loss) and warns — the abort above is the protection (issue #135).
    assert "NOT rewinding" in proc.stderr
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"
    assert before in _git(repo, "log", "--format=%H", "main")  # ancestor intact


def test_breach_bare_flip_aborts_and_restores(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", "git config core.bare true")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == BREACH_RC, proc.stderr
    assert "core.bare" in proc.stderr
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"  # restored


def test_clean_run_passes(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", ":")  # touches nothing

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # no trip on a clean run


def test_hermetic_tmpdir_does_not_trip(repo: Path, tmp_path: Path) -> None:
    # A well-behaved hermetic test creates and deletes its OWN tmpdir repo; that
    # must NOT count as mutating THIS repo (no false positive).
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(
        tmp_path / "bin",
        'd="$(mktemp -d)"; git init -q "$d"; '
        'git -C "$d" -c user.email=a@b.c -c user.name=x commit --allow-empty -q -m own; '
        'rm -rf "$d"',
    )

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr


def test_known_gitdir_scenario_passes_through(repo: Path, tmp_path: Path) -> None:
    # The already-fixed GIT_DIR vector: git exports GIT_DIR into the hook. The
    # pytest child runs with it stripped (issue #30), so a hermetic test reaches
    # only its own tmpdir — the tripwire sees an intact repo and lets the push by.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(
        tmp_path / "bin",
        'd="$(mktemp -d)"; git init -q "$d"; rm -rf "$d"',
    )

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"GIT_DIR": str(repo / ".git")}
    )

    assert proc.returncode == 0, proc.stderr


def test_live_spoke_commit_mid_gate_passes_and_survives(
    repo: Path, spoke: Path, tmp_path: Path
) -> None:
    # THE #135 regression, end to end: a stub "spoke" advances its own branch
    # while the gate runs. The push must NOT abort and the spoke's commit must
    # NOT be rewound.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", f'git -C "{spoke}" commit --allow-empty -q -m spoke-work')

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # push not aborted
    assert (
        _git(repo, "log", "-1", "--format=%s", "refs/heads/feature/spoke").strip() == "spoke-work"
    )  # the spoke's commit survives


# --- gate scoped to the pushed refs (issue #188) ----------------------------------
# The pre-push stdin names exactly the refs this push updates; the gate's tripwire
# guards ONLY those (plus HEAD and the config markers). Any other ref moving during
# the 6-8 min gate window — a sibling spoke's commit, rewind, marker tag, or a
# push completing into the shared store — is normal concurrent-spoke behavior:
# the push proceeds, nothing is reported, and the restore never touches it
# (whole-namespace policing is what orphaned #135's spokes and false-aborted
# sibling pushes).


def test_sibling_rewind_mid_gate_out_of_scope_passes(
    repo: Path, spoke: Path, tmp_path: Path
) -> None:
    # The push updates only refs/heads/main, so a sibling ref moving BACKWARD
    # mid-gate is no longer the gate's business — the push proceeds and the
    # gate neither reports a breach nor "restores" the sibling ref.
    _git(spoke, "commit", "--allow-empty", "-qm", "spoke-work")
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    rewind_target = _rev(repo, "refs/heads/feature/spoke~1")
    _make_pytest_stub(tmp_path / "bin", f'git -C "{spoke}" reset -q --hard HEAD~1')

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # push proceeds
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _rev(repo, "refs/heads/feature/spoke") == rewind_target  # left alone


def test_concurrent_marker_tag_mid_gate_passes_and_survives(repo: Path, tmp_path: Path) -> None:
    # A sibling's ready/<N> marker landing in the shared store mid-gate is an
    # out-of-scope appeared ref: no breach, and the restore must not delete it
    # (the pre-#188 whole-namespace restore rolled such tags back).
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", "git tag ready/99 HEAD")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _git(repo, "tag", "--list").strip() == "ready/99"  # survives


def test_concurrent_remote_tracking_update_mid_gate_passes(repo: Path, tmp_path: Path) -> None:
    # A sibling's `git push` completing mid-gate updates a remote-tracking ref
    # in the shared store — the false REPO-INTEGRITY BREACH that aborted
    # legitimate pushes. Out of scope now: the push proceeds.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    _make_pytest_stub(tmp_path / "bin", "git update-ref refs/remotes/origin/feature/other HEAD")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert _rev(repo, "refs/remotes/origin/feature/other")  # survives


# --- the backstop: run_pytest_node under the tripwire (issue #31) -----------------
# The red-proof hooks run individual Tested-RED nodes through run_pytest_node,
# which shells out to pytest just like the gate. The tripwire wraps that run too:
# a node that mutates THIS repo yields the BREACH verdict and the snapshot is
# restored, so the caller can block instead of shipping a corrupted repo.


def _run_node(
    repo: Path, bindir: Path, node: str = "tests/test_x.py::test_x"
) -> subprocess.CompletedProcess[str]:
    """Source utils.sh and call run_pytest_node with `bindir` (the stub) on PATH."""
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    script = (
        f'set -uo pipefail\nsource "{UTILS}"\n'
        f'v="$(run_pytest_node "{repo}" "{node}")"\necho "VERDICT=$v"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_backstop_clean_node_passes_through(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", ":")  # exits 0, mutates nothing

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=PASS" in proc.stdout, proc.stderr  # normal verdict still flows


def test_backstop_node_bare_flip_breaches_and_restores(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", "git config core.bare true")

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # breach beats the PASS verdict
    assert "core.bare" in proc.stderr  # names the marker
    assert _git(repo, "config", "--get", "core.bare").strip() == "false"  # restored


def test_backstop_node_ref_move_breaches_and_refuses_rewind(repo: Path, tmp_path: Path) -> None:
    _make_pytest_stub(tmp_path / "bin", "git commit --allow-empty -q -m sneak")

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # the block still fires
    # FF advance → restore refuses the rewind and warns (issue #135).
    assert "NOT rewinding" in proc.stderr
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == "sneak"


# --- backstop tolerates a concurrent control-plane marker stamp (issue #272) -------
# The red-proof green backstop runs each Tested-RED node through run_pytest_node,
# whose tripwire covers the whole repo (unscoped). While that node runs on a live
# hub checkout, an /afk drain or hub-watchdog legitimately stamps a control-plane
# marker tag — `needs-human-land/<N>` is the one that bit #272 (the watchdog's
# escalate-only land marker), created mid-land while the backstop was armed. That
# creation must NOT read as a #31 escape (a hermetic test writes only to its own
# tmpdir; it never creates these real namespaces), and the marker must survive —
# the pre-#272 restore collateral-deleted it as an "appeared ref", destroying the
# escalation. Only CREATION is tolerated: a MOVE or DELETE of such a ref still
# breaches, so #31 is not weakened.


def test_backstop_tolerates_concurrent_marker_tag_creation(repo: Path, tmp_path: Path) -> None:
    # The pinned repro: a node that stamps needs-human-land/<N> mid-run no longer
    # yields BREACH, and the marker survives (AC1 + AC4).
    _make_pytest_stub(tmp_path / "bin", "git tag needs-human-land/272 HEAD")

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=PASS" in proc.stdout, proc.stderr  # creation tolerated, node verdict flows
    assert "REPO-INTEGRITY BREACH" not in proc.stderr
    assert "needs-human-land/272" in _git(repo, "tag", "--list")  # not collateral-deleted


def test_backstop_real_breach_preserves_concurrent_marker(repo: Path, tmp_path: Path) -> None:
    # A genuine escape (main moved) co-occurring with a concurrent needs-human-land/*
    # stamp: the real escape still BREACHes, but the restore must NOT collateral-delete
    # the innocent marker the wrapped command did not "own" (AC2 under a concurrent
    # breach — the exact compound case #272 consequence #2 targets).
    _make_pytest_stub(
        tmp_path / "bin",
        "git commit --allow-empty -q -m sneak; git tag needs-human-land/272 HEAD",
    )

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # the real escape still fires
    assert "refs/heads/main" in proc.stderr  # the moved ref is named
    assert "needs-human-land/272" in _git(repo, "tag", "--list")  # marker survives the restore


def test_backstop_marker_ns_move_still_breaches(repo: Path, tmp_path: Path) -> None:
    # Only CREATION is tolerated. A pre-existing marker tag MOVED mid-run is not a
    # creation, so it still BREACHes — the #31 protection is not weakened (AC3).
    _git(repo, "commit", "--allow-empty", "-qm", "c1")
    _git(repo, "tag", "needs-human-land/272", "HEAD~1")  # pre-existing, at the parent
    _make_pytest_stub(tmp_path / "bin", "git tag -f needs-human-land/272 HEAD")  # move it forward

    proc = _run_node(repo, tmp_path / "bin")

    assert "VERDICT=BREACH" in proc.stdout, proc.stderr  # a move is not a tolerated creation
    assert "needs-human-land/272" in proc.stderr  # the moved marker is named
