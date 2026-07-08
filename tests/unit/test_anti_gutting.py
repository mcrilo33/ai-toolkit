"""Unit tests for shared/hooks/anti-gutting-scan.sh.

The adversarial code-review gate is unenforceable policy (a spoke can narrate a
review it never ran — the #43 failure mode), and on the native Claude pre-push
path reviewer-sep is advisory (it does not block). So the one MECHANICAL signal
against the dangerous cheat — an implementation that guts the tests to go green —
is a deterministic diff scan wired into the pre-push path.

Enforcement is split by CHANNEL (issue #193). Attended, the scan is advisory: it
warns on stderr and exits 0, so a human's ordinary test refactor is never gated.
Under an unattended /afk drain (armed by a truthy ``UNATTENDED`` env or the
supervisor's ``ai-toolkit-afk/unattended`` marker under the shared git common dir)
it fails CLOSED on the SHIP paths — branch refs and ``refs/tags/ready/*`` — because
no human is watching for a test-gutting diff. Every other tag is exempt from
blocking (findings still print for the log); the point is the escalation markers
``refs/tags/blocked/*`` and ``refs/tags/gate/*``: a spoke whose diff trips the scan
must always be able to announce "I need a human", or the tripwire deadlocks the very
channel that reports it (the #103 liveness invariant). Classification keys on the
REMOTE ref of git's pre-push stdin — the ref the push actually updates — so a
refspec push cannot smuggle a gutting diff onto a branch under a marker local name.

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

# Strip any ambient arming signal so attended-mode assertions cannot be flipped by
# the calling session's environment (the #169 env-leak class); tests that want the
# unattended path set UNATTENDED explicitly via _scan(env=...).
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if k != "UNATTENDED"},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

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
    repo: Path,
    base: str,
    head: str,
    *,
    ref: str = "refs/heads/feature",
    local_ref: str = "refs/heads/local",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the scan with a synthesized pre-push stdin line (range base..head).

    ``ref`` is the REMOTE ref the push updates — the one classification must key
    on — so the local ref deliberately differs (a refspec push shape).
    """
    stdin = f"{local_ref} {head} {ref} {base}\n"
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


# ── unattended fails CLOSED on the ship paths (issues #74 / #193) ─────────────
# Under an unattended drain no human is watching for a test-gutting diff, so a
# finding on a SHIP ref — a branch or refs/tags/ready/* — blocks the push (exit 1).
# Attended, the same finding stays advisory. The fail-closed path is armed by a
# truthy UNATTENDED env or by the supervisor's `ai-toolkit-afk/unattended` marker
# (#74) under the git common dir, which every spoke worktree shares. Deliberately
# NOT an arming signal: hub-afk's `.afk-state` window file — a live (or stale,
# #107) drain window must not hard-block the hub operator's own attended pushes.

_GUT = {"tests/test_taut.py": "def test_taut():\n    assert True\n"}


def test_unattended_env_blocks_gutting_branch_push(repo: Path) -> None:
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head, env={"UNATTENDED": "1"})

    assert result.returncode == 1, "UNATTENDED must fail closed on a gutting branch push"
    assert "weakens tests" in result.stderr, "the block must name the findings"


def test_unattended_marker_blocks_gutting_branch_push(repo: Path) -> None:
    marker_dir = repo / ".git" / "ai-toolkit-afk"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "unattended").write_text("")
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head)

    assert result.returncode == 1, "the #74 unattended marker must fail closed"
    assert "weakens tests" in result.stderr, "the block must name the findings"


def test_falsy_unattended_env_stays_advisory(repo: Path) -> None:
    # UNATTENDED=0 (or false/empty) means ATTENDED — a falsy-but-set value from a
    # wrapper shell must not arm the fail-closed path.
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head, env={"UNATTENDED": "0"})

    assert result.returncode == 0, "a falsy UNATTENDED must not arm the fail-closed path"
    assert "weakens tests" in result.stderr, "advisory mode must still warn"


def test_armed_afk_state_alone_stays_advisory(repo: Path) -> None:
    # A non-empty .afk-state says "a drain window is armed", not "THIS push is
    # unattended": the hub operator's own attended pushes (and a stale state file
    # from a crashed supervisor, #107) must never be hard-blocked by it.
    (repo / ".git" / ".afk-state").write_text("drain\n")
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head)

    assert result.returncode == 0, ".afk-state alone must not arm the fail-closed path"
    assert "weakens tests" in result.stderr, "advisory mode must still warn"


def test_unattended_ready_tag_push_is_gated(repo: Path) -> None:
    # ready/<N> is auto_land's trust basis — a ship path, not an escalation marker —
    # so it stays gated exactly like a branch push.
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head, ref="refs/tags/ready/5", env={"UNATTENDED": "1"})

    assert result.returncode == 1, "a ready/<N> tag push is a ship path and must stay gated"
    assert "weakens tests" in result.stderr, "the block must name the findings"


def test_unattended_clean_diff_is_not_blocked(repo: Path) -> None:
    base, head = _commit(
        repo,
        {"tests/test_more.py": "def test_more():\n    assert (1 + 1) == 2\n    assert [1] != []\n"},
    )

    result = _scan(repo, base, head, env={"UNATTENDED": "1"})

    assert result.returncode == 0, "fail-closed arms on FINDINGS, not on every unattended push"
    assert "weakens tests" not in result.stderr, "a clean diff must not warn"


# ── non-ready tags are exempt from the fail-closed path (issue #193) ──────────
# blocked/<N> and gate/<N> are the spoke's ONLY way to announce "I need a human".
# If the tripwire gated them, the exact spoke whose diff needs a human decision
# could not report it — the drain would see a silent, stuck spoke instead of a
# blocked/ ping (the same liveness invariant as spoke-ready's blocked/+gate/
# exemption from the upstream guard, #103). accept/<N> parks finished work for a
# human EYEBALL (the human is the gate) and a foreign tag (a consumer repo's
# v1.2.3) carries no hub semantics; no tag ships code — the code only moves on the
# gated branch push. Findings still print to stderr for the log, but the tag push
# always goes through. Only ready/<N>, auto_land's trust basis, stays gated.


@pytest.mark.parametrize(
    "ref",
    ["refs/tags/blocked/5", "refs/tags/gate/5", "refs/tags/accept/5", "refs/tags/v1.2.3"],
)
def test_unattended_non_ready_tag_push_succeeds(repo: Path, ref: str) -> None:
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head, ref=ref, env={"UNATTENDED": "1"})

    assert result.returncode == 0, f"a non-ready tag push must never be blocked ({ref})"
    assert "weakens tests" in result.stderr, f"findings must still print for the log ({ref})"


def test_unattended_refspec_push_to_branch_under_marker_local_name_is_gated(repo: Path) -> None:
    # Classification must key on the REMOTE ref: `git push origin blocked/5:feature`
    # updates a BRANCH, and naming the local side after an escalation marker must
    # not smuggle the gutting diff past the gate.
    base, head = _commit(repo, _GUT)

    result = _scan(
        repo,
        base,
        head,
        ref="refs/heads/feature",
        local_ref="refs/tags/blocked/5",
        env={"UNATTENDED": "1"},
    )

    assert result.returncode == 1, "the remote ref (a branch) decides the channel, not the local"
    assert "weakens tests" in result.stderr, "the block must name the findings"


def test_unattended_gutting_blocks_branch_but_not_blocked_marker(repo: Path) -> None:
    # The #193 acceptance criterion, from ONE repo state: the unattended flag is armed
    # and the branch carries a gutting diff — the branch push is blocked, while the
    # blocked/<N> marker push from that same state succeeds.
    base, head = _commit(repo, _GUT)
    env = {"UNATTENDED": "1"}

    branch = _scan(repo, base, head, env=env)
    marker = _scan(repo, base, head, ref="refs/tags/blocked/5", env=env)

    assert branch.returncode == 1, "the ship path must fail closed on the gutting diff"
    assert marker.returncode == 0, "the escalation channel must stay open from the same state"


def test_unattended_mixed_push_with_gated_ref_is_blocked(repo: Path) -> None:
    # A push carrying BOTH a branch ref and an escalation tag (separate stdin lines)
    # must still block — the exemption is per-ref, not "any escalation tag ⇒ let the
    # push through" (which would let a spoke smuggle a gutting branch past the gate by
    # tacking a blocked/ tag onto the push). Escalation markers are pushed alone.
    base, head = _commit(repo, _GUT)
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
        env={**_GIT_ENV, "UNATTENDED": "1"},
    )

    assert result.returncode == 1, "a gated ref in a mixed push must still fail closed"
    assert "weakens tests" in result.stderr, "the branch ref in a mixed push must still be scanned"


def test_attended_escalation_marker_push_warns_but_passes(repo: Path) -> None:
    # Attended, the marker push behaves like every other attended push: advisory.
    base, head = _commit(repo, _GUT)

    result = _scan(repo, base, head, ref="refs/tags/gate/5")

    assert result.returncode == 0, "attended pushes are advisory on every channel"
    assert "weakens tests" in result.stderr, "findings must still print for the log"
