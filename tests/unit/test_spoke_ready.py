"""Unit tests for scripts/spoke-ready.sh (issue #45).

Marker emission is a workflow control signal. Today the spoke emits ``ready/N``
and ``gate/N`` by having the LLM hand-run a ``git tag … && git push …`` chain —
unreliable (narrated-not-run), re-prompting (the compound command never matches a
bare allow rule), and wasteful (pushing the tag fires the full pre-push suite for
a tag that carries no code). ``spoke-ready.sh`` collapses marker emission into ONE
allowlistable command so the model launches a single canonical process:

    spoke-ready.sh <N>          # emit ready/N at HEAD (annotated, force) and push it
    spoke-ready.sh --gate <N>   # emit gate/N (the PLAN-gate park marker)

The script must:

* create an ANNOTATED tag at HEAD and push it to ``origin`` in one invocation;
* be IDEMPOTENT — a re-run force-moves the tag and re-pushes without error;
* default the ``ready/N`` message and stamp ``gate/N`` with the park state ``plan``;
* refuse when no issue number is given.

Hermetic setup mirrors test_spoke_push.py: a local bare ``origin`` (no network)
and a feature-branch checkout one commit ahead. Git config is pinned to nothing so
a host's global/system config never reaches the fixture repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SPOKE_READY = Path(__file__).resolve().parents[2] / "scripts" / "spoke-ready.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir) must not reach the fixture repo's commits or pushes.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

OWN = "fix/45-spoke-ready"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _run(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SPOKE_READY), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env or _GIT_ENV,
    )


def _stamp_review(
    repo: Path, *, age_offset: int = 0, verdict: str = "APPROVE", name: str = "review.json"
) -> Path:
    """Write a ``.review/*.json`` artifact and set its mtime relative to the tip.

    The ready gate's precondition 3 (issue #172) accepts a review only when a
    ``.review/*.json`` with an ``APPROVE`` verdict is at least as new as the tip
    commit. ``age_offset`` shifts the artifact's mtime off the HEAD commit time:
    ``0`` sits it exactly on the ``>=`` boundary, a negative value makes it stale.
    ``verdict`` and ``name`` let a test forge a non-approving or extra artifact.
    """
    review_dir = repo / ".review"
    review_dir.mkdir(exist_ok=True)
    artifact = review_dir / name
    artifact.write_text(f'{{"verdict": "{verdict}"}}\n')
    tip = int(_git(repo, "log", "-1", "--format=%ct", "HEAD").strip())
    stamp = tip + age_offset
    os.utime(artifact, (stamp, stamp))
    return artifact


@pytest.fixture()
def remote(tmp_path: Path) -> Path:
    """A bare ``origin`` to push into — no network needed."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    return remote


@pytest.fixture()
def main_checkout(tmp_path: Path, remote: Path) -> Path:
    """A checkout sitting on the default branch ``main``."""
    repo = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "README.md").write_text("seed\n")
    # Mirror the real repo: .review/ is gitignored, so a review artifact never
    # reads as a dirty working tree in the ready gate's clean-tree precondition.
    (repo / ".gitignore").write_text(".review/\n")
    _git(repo, "add", "README.md", ".gitignore")
    _git(repo, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@pytest.fixture()
def spoke(main_checkout: Path) -> Path:
    """A feature-branch checkout one commit ahead, branch pushed to origin."""
    _git(main_checkout, "checkout", "-q", "-b", OWN)
    (main_checkout / "work.txt").write_text("spoke work\n")
    _git(main_checkout, "add", "work.txt")
    _git(main_checkout, "commit", "-qm", "feat: work", "-m", "Refs #45")
    _git(main_checkout, "push", "-q", "-u", "origin", OWN)
    # A ready-eligible spoke is clean, pushed AND carries a review of the tip —
    # stamp one so the #172 gate's happy path is the fixture's default state.
    _stamp_review(main_checkout)
    return main_checkout


def _remote_has_ref(remote: Path, ref: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", str(remote), ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout
    return bool(out.strip())


def _remote_sha(remote: Path, ref: str) -> str:
    """First field of the ls-remote line for `ref` (``^{}`` peels annotated tags)."""
    out = subprocess.run(
        ["git", "ls-remote", str(remote), ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    ).stdout
    return out.split()[0] if out.strip() else ""


def _tag_type(repo: Path, tag: str) -> str:
    return _git(repo, "cat-file", "-t", tag).strip()


# ── ready/N: create + push the completion marker ─────────────────────────────


def test_ready_creates_tag(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", "ready/45").strip() == "ready/45", "local tag not created"


def test_ready_tag_is_annotated(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tag_type(spoke, "ready/45") == "tag", "marker must be an annotated tag"


def test_ready_pushes_tag(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45"), "ready/45 not pushed to origin"


def test_ready_points_at_head(spoke: Path) -> None:
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    tagged = _git(spoke, "rev-list", "-n", "1", "ready/45").strip()
    head = _git(spoke, "rev-parse", "HEAD").strip()
    assert tagged == head, "marker must point at HEAD"


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_ready_idempotent_rerun(spoke: Path, remote: Path) -> None:
    first = _run(spoke, "45")
    second = _run(spoke, "45")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, "re-run must not error — emission is idempotent"
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_retags_at_new_head(spoke: Path) -> None:
    _run(spoke, "45")
    (spoke / "more.txt").write_text("more work\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")
    # A terminal marker requires the tip to be on origin (durability, issue #40),
    # so push the new commit before re-emitting at the new tip.
    _git(spoke, "push", "-q", "origin", OWN)
    _stamp_review(spoke)  # the ready gate (#172) needs a review of the NEW tip

    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    tagged = _git(spoke, "rev-list", "-n", "1", "ready/45").strip()
    head = _git(spoke, "rev-parse", "HEAD").strip()
    assert tagged == head, "re-run must force-move the marker to the new tip"


def test_ready_force_moves_the_remote_tag(spoke: Path, remote: Path) -> None:
    # The push uses `-f`: when the marker moves to a new tip a re-run must
    # force-update the REMOTE tag, not be rejected as a non-fast-forward.
    _run(spoke, "45")
    first_remote = _remote_sha(remote, "refs/tags/ready/45")
    (spoke / "more.txt").write_text("more work\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")
    # Push the new commit first — a terminal marker is refused over un-pushed
    # work (durability, issue #40), so the force-move re-emits at a pushed tip.
    _git(spoke, "push", "-q", "origin", OWN)
    _stamp_review(spoke)  # the ready gate (#172) needs a review of the NEW tip

    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_sha(remote, "refs/tags/ready/45") != first_remote, (
        "remote marker must force-update to the new tag object"
    )
    peeled = _remote_sha(remote, "refs/tags/ready/45^{}")
    assert peeled == _git(spoke, "rev-parse", "HEAD").strip(), (
        "remote marker must point at the new tip"
    )


# ── --gate N: the PLAN-gate park marker ──────────────────────────────────────


def test_gate_creates_and_pushes_tag(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", "gate/45").strip() == "gate/45", "local gate tag not created"
    assert _remote_has_ref(remote, "refs/tags/gate/45"), "gate/45 not pushed to origin"


def test_gate_tag_message_is_plan(spoke: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    message = _git(spoke, "tag", "-l", "--format=%(contents)", "gate/45")
    assert "plan" in message, "gate marker must carry the 'plan' park state"


def test_gate_does_not_emit_ready(spoke: Path, remote: Path) -> None:
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _git(spoke, "tag", "-l", "ready/45").strip(), "--gate must not emit ready/N"
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


# ── --gate N: the structured plan artifact (issue #175) ──────────────────────
# The gate park hands its plan to the broker through a SCRIPTED channel instead of
# the transcript heuristic: `--gate N` accepts the plan (`-m <text>` or `--plan-file
# <path>`) and writes it to <wt>/.ai-toolkit/gate-<N>.md (and into the tag body)
# before pushing the tag, so a script reads what a script wrote.


def test_gate_plan_written_to_artifact(spoke: Path) -> None:
    result = _run(spoke, "--gate", "45", "-m", "Plan: add a helper, then wire it in.")

    assert result.returncode == 0, result.stdout + result.stderr
    artifact = spoke / ".ai-toolkit" / "gate-45.md"
    assert artifact.is_file(), "the plan artifact must be written for a --gate park"
    assert "add a helper, then wire it in" in artifact.read_text()


def test_gate_plan_also_lands_in_tag_body(spoke: Path) -> None:
    result = _run(spoke, "--gate", "45", "-m", "Plan: add a helper, then wire it in.")

    assert result.returncode == 0, result.stdout + result.stderr
    body = _git(spoke, "tag", "-l", "--format=%(contents:body)", "gate/45")
    assert "add a helper, then wire it in" in body, "the plan must also ride the tag annotation"


def test_gate_plan_file_read_into_artifact_and_tag(spoke: Path, tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\nStep 1: RED\nStep 2: GREEN\n")

    result = _run(spoke, "--gate", "45", "--plan-file", str(plan_file))

    assert result.returncode == 0, result.stdout + result.stderr
    artifact = spoke / ".ai-toolkit" / "gate-45.md"
    assert artifact.is_file(), "the plan artifact must be written from --plan-file"
    assert "Step 1: RED" in artifact.read_text()
    body = _git(spoke, "tag", "-l", "--format=%(contents:body)", "gate/45")
    assert "Step 1: RED" in body, "the --plan-file content must ride the tag annotation"


def test_gate_without_plan_writes_no_artifact(spoke: Path) -> None:
    # A bare --gate (no plan given) stays back-compatible: it emits the marker but
    # writes no artifact, so the broker falls back to the transcript.
    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (spoke / ".ai-toolkit" / "gate-45.md").exists(), (
        "no plan given → no artifact (transcript fallback)"
    )


def test_gate_plan_file_missing_is_an_error(spoke: Path) -> None:
    result = _run(spoke, "--gate", "45", "--plan-file", str(spoke / "nope.md"))

    assert result.returncode != 0, "a missing --plan-file path must error, not emit a blank plan"


def test_message_and_plan_file_conflict_is_a_usage_error(spoke: Path, tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("plan\n")

    result = _run(spoke, "--gate", "45", "-m", "inline", "--plan-file", str(plan_file))

    assert result.returncode == 2, "passing both -m and --plan-file is a usage error (exit 2)"


def test_plan_artifact_only_for_gate(spoke: Path) -> None:
    # The plan artifact is a PLAN-gate concept: --blocked -m <reason> stamps the tag
    # body but must not spill a gate-<N>.md artifact.
    result = _run(spoke, "--blocked", "45", "-m", "stuck on ambiguity")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (spoke / ".ai-toolkit" / "gate-45.md").exists(), (
        "only --gate writes the plan artifact"
    )


def test_gate_bare_repark_clears_stale_artifact(spoke: Path) -> None:
    # #175 review: the write site owns the artifact's freshness. A plan-bearing park writes
    # gate-N.md; a later BARE --gate (a re-park that hands over no plan — after an escalation
    # or a human in-pane answer) must CLEAR it, so the broker never prefers a stale prior plan
    # over the current transcript.
    _run(spoke, "--gate", "45", "-m", "stale plan A")
    assert (spoke / ".ai-toolkit" / "gate-45.md").is_file(), "precondition: the artifact exists"

    result = _run(spoke, "--gate", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (spoke / ".ai-toolkit" / "gate-45.md").exists(), (
        "a bare re-park must clear the stale plan artifact so the broker falls back to the transcript"
    )


# ── accept/N and blocked/N: the terminal markers ─────────────────────────────
# An unattended drain (`/afk`) adds two more terminal markers beside ready/N, each
# frees a supervisor slot:
#   accept/N  — built + pushed + agent-reviewed, final sign-off inherently human
#               (surfaced on the dashboard for a human glance).
#   blocked/N — stuck (ambiguity, suspected cheating, budget) → answer + re-queue
#               (surfaced on the dashboard).
# They are annotated, force-moved, pushed tags exactly like ready/N, selected by a
# NAMED flag mirroring --gate (so they ride the existing `spoke-ready.sh:*`
# wildcard allowlist with no new spoke permission). Payload schema: the tag
# SUBJECT is the state word; the tag BODY is the optional -m reason — the trust
# summary / blocker text the morning report renders.


@pytest.mark.parametrize("flag,kind", [("--accept", "accept"), ("--blocked", "blocked")])
def test_terminal_marker_creates_annotated_pushed_tag(
    spoke: Path, remote: Path, flag: str, kind: str
) -> None:
    result = _run(spoke, flag, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(spoke, "tag", "-l", f"{kind}/45").strip() == f"{kind}/45", "local tag not created"
    assert _tag_type(spoke, f"{kind}/45") == "tag", "marker must be an annotated tag"
    assert _remote_has_ref(remote, f"refs/tags/{kind}/45"), f"{kind}/45 not pushed to origin"


@pytest.mark.parametrize("flag,kind", [("--accept", "accept"), ("--blocked", "blocked")])
def test_terminal_marker_subject_defaults_to_state_word(spoke: Path, flag: str, kind: str) -> None:
    result = _run(spoke, flag, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    subject = _git(spoke, "tag", "-l", "--format=%(contents:subject)", f"{kind}/45").strip()
    assert subject == kind, "the tag subject must default to the state word"


def test_terminal_marker_reason_lands_in_tag_body(spoke: Path) -> None:
    result = _run(spoke, "--blocked", "45", "-m", "ambiguous acceptance criteria")

    assert result.returncode == 0, result.stdout + result.stderr
    subject = _git(spoke, "tag", "-l", "--format=%(contents:subject)", "blocked/45").strip()
    body = _git(spoke, "tag", "-l", "--format=%(contents:body)", "blocked/45")
    assert subject == "blocked", "the subject stays the state word even with a reason"
    assert "ambiguous acceptance criteria" in body, "the -m reason must be the tag body"


@pytest.mark.parametrize("flag", ["--accept", "--blocked"])
def test_terminal_marker_does_not_emit_ready(spoke: Path, remote: Path, flag: str) -> None:
    result = _run(spoke, flag, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not _git(spoke, "tag", "-l", "ready/45").strip(), f"{flag} must not also emit ready/N"
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


# ── Durability: a terminal marker must not be emitted over un-pushed work ─────
# A terminal marker claims the issue's work is finished and landable. If the
# branch commits never reached origin (the #43 narrated-push failure), the hub
# would free the slot and the morning report would show a LAND row for work that
# is not durable. So spoke-ready.sh refuses a terminal marker when HEAD is not
# contained in the branch's pushed upstream. The non-terminal gate/N park is
# EXEMPT — it legitimately precedes any branch push.


def _commit_without_pushing(repo: Path, name: str = "more.txt") -> None:
    (repo / name).write_text("unpushed work\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "feat: unpushed", "-m", "Refs #45")


# ready/N and accept/N CLAIM landable/reviewable work, so they require the tip on
# origin. blocked/N and gate/N are STOP signals that make no such claim (the work
# is incomplete by definition), so they are durability-EXEMPT — the hub also emits
# blocked/N when it reaps a hung/idle spoke whose work never landed (issue #40 ST2).
@pytest.mark.parametrize("args,kind", [(("45",), "ready"), (("--accept", "45"), "accept")])
def test_terminal_marker_refused_over_unpushed_work(
    spoke: Path, remote: Path, args: tuple[str, ...], kind: str
) -> None:
    _commit_without_pushing(spoke)  # HEAD now ahead of @{upstream}

    result = _run(spoke, *args)

    assert result.returncode != 0, "ready/accept over un-pushed work must be refused"
    assert not _remote_has_ref(remote, f"refs/tags/{kind}/45"), (
        "no landable marker may reach origin"
    )


@pytest.mark.parametrize("flag,kind", [("--gate", "gate"), ("--blocked", "blocked")])
def test_stop_marker_allowed_over_unpushed_work(
    spoke: Path, remote: Path, flag: str, kind: str
) -> None:
    # gate/N (PLAN park) and blocked/N (stuck) are STOP signals — emitted over
    # incomplete, possibly un-pushed work — so durability never refuses them.
    _commit_without_pushing(spoke)

    result = _run(spoke, flag, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, f"refs/tags/{kind}/45")


# ── Misuse ───────────────────────────────────────────────────────────────────


def test_missing_issue_number_errors(spoke: Path) -> None:
    result = _run(spoke)

    assert result.returncode == 2, "a missing issue number is a usage error (exit 2)"
    assert "issue number is required" in result.stderr


def test_conflicting_state_flags_error(spoke: Path) -> None:
    result = _run(spoke, "--gate", "--accept", "45")

    assert result.returncode == 2, "two mutually-exclusive state flags is a usage error (exit 2)"


# ── ready/N precondition gate (issue #172) ───────────────────────────────────
# ready/<N> is auto_land's entire trust basis — it lands with --skip-tests — so
# emission must be MECHANICALLY verified, not asserted. spoke-ready refuses ready/N
# unless (1) the working tree is clean, (2) HEAD is exactly the pushed tip
# (@{upstream}), and (3) a review artifact is at least as new as the tip commit.
# On refusal it names the unmet condition and the fix command. The escape hatch
# AI_TOOLKIT_READY_FORCE=1 skips the gate, logged loudly. The other markers
# (--gate/--accept/--blocked) keep their prior behavior and stay ungated.


def test_ready_emitted_when_all_preconditions_met(spoke: Path, remote: Path) -> None:
    # The `spoke` fixture is clean, pushed (HEAD==@{upstream}) and carries a fresh
    # review artifact — the gate must let ready/45 through.
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refused_dirty_working_tree(spoke: Path, remote: Path) -> None:
    (spoke / "work.txt").write_text("uncommitted edit\n")  # tracked file now dirty

    result = _run(spoke, "45")

    assert result.returncode != 0, "a dirty tree must block ready/N"
    assert "working tree" in (result.stdout + result.stderr).lower(), (
        "refusal must name the failing precondition"
    )
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refused_untracked_file(spoke: Path, remote: Path) -> None:
    (spoke / "stray.txt").write_text("untracked\n")  # not ignored → tree not clean

    result = _run(spoke, "45")

    assert result.returncode != 0, "an untracked file leaves the tree unclean"
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_review_artifact_does_not_dirty_the_tree(spoke: Path, remote: Path) -> None:
    # .review/ is gitignored, so the present review artifact must NOT read as a
    # dirty tree — the gate has to emit ready/N with the artifact in place.
    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refused_head_ahead_of_upstream(spoke: Path, remote: Path) -> None:
    (spoke / "more.txt").write_text("unpushed\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")  # HEAD now ahead
    _stamp_review(spoke)  # fresh review, so only precondition 2 is unmet

    result = _run(spoke, "45")

    assert result.returncode != 0, "un-pushed HEAD must block ready/N"
    assert "pushed tip" in (result.stdout + result.stderr).lower()
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refused_head_behind_upstream(spoke: Path) -> None:
    # Strict equality (#172): HEAD *behind* the pushed tip is refused too — the
    # old ancestor-based durability check let a behind-HEAD through.
    (spoke / "more.txt").write_text("pushed\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")
    _git(spoke, "push", "-q", "origin", OWN)  # upstream now at C2
    _git(spoke, "reset", "--hard", "HEAD~1")  # HEAD back at C1, upstream ahead
    _stamp_review(spoke)

    result = _run(spoke, "45")

    assert result.returncode != 0, "HEAD behind the pushed tip must block ready/N"
    assert "pushed tip" in (result.stdout + result.stderr).lower()


def test_ready_refused_no_upstream(main_checkout: Path) -> None:
    # A branch that was never pushed has no @{upstream} — refuse and say so.
    _git(main_checkout, "checkout", "-q", "-b", "fix/45-unpushed")
    (main_checkout / "w.txt").write_text("w\n")
    _git(main_checkout, "add", "w.txt")
    _git(main_checkout, "commit", "-qm", "feat: w", "-m", "Refs #45")
    _stamp_review(main_checkout)

    result = _run(main_checkout, "45")

    assert result.returncode != 0, "no upstream must block ready/N"
    assert "upstream" in (result.stdout + result.stderr).lower()


def test_ready_refused_without_review_artifact(spoke: Path, remote: Path) -> None:
    shutil.rmtree(spoke / ".review")  # no review evidence at all

    result = _run(spoke, "45")

    assert result.returncode != 0, "a missing review artifact must block ready/N"
    assert "review" in (result.stdout + result.stderr).lower()
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refused_stale_review_artifact(spoke: Path, remote: Path) -> None:
    # A review recorded BEFORE the tip commit does not cover it.
    (spoke / "more.txt").write_text("later work\n")
    _git(spoke, "add", "more.txt")
    _git(spoke, "commit", "-qm", "feat: more", "-m", "Refs #45")
    _git(spoke, "push", "-q", "origin", OWN)
    _stamp_review(spoke, age_offset=-100)  # artifact predates the new tip

    result = _run(spoke, "45")

    assert result.returncode != 0, "a stale review artifact must block ready/N"
    assert "review" in (result.stdout + result.stderr).lower()


def test_ready_refused_on_request_changes_review(spoke: Path, remote: Path) -> None:
    # A review artifact is trusted only when it APPROVES. ready/<N> is auto_land's
    # basis (--skip-tests), so a fresh REQUEST_CHANGES must not satisfy the gate.
    shutil.rmtree(spoke / ".review")
    _stamp_review(spoke, verdict="REQUEST_CHANGES")

    result = _run(spoke, "45")

    assert result.returncode != 0, "a REQUEST_CHANGES review must block ready/N"
    assert "review" in (result.stdout + result.stderr).lower()
    assert not _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_emitted_when_a_fresh_approve_exists_beside_request_changes(
    spoke: Path, remote: Path
) -> None:
    # A stale/other REQUEST_CHANGES must not veto a fresh APPROVE of the tip.
    _stamp_review(spoke, verdict="REQUEST_CHANGES", name="old.json", age_offset=-100)
    _stamp_review(spoke, verdict="APPROVE", name="new.json")

    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_refusal_reports_the_first_unmet_precondition(spoke: Path) -> None:
    # Dirty tree AND no review: the clean-tree precondition is checked first, so
    # its message is the one surfaced.
    shutil.rmtree(spoke / ".review")
    (spoke / "work.txt").write_text("dirty\n")

    result = _run(spoke, "45")

    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "working tree" in combined and "review" not in combined


def test_ready_accepts_review_artifact_on_tip_boundary(spoke: Path, remote: Path) -> None:
    # mtime exactly equal to the tip commit time counts — the check is `>=`.
    _stamp_review(spoke, age_offset=0)

    result = _run(spoke, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45")


def test_ready_force_bypasses_preconditions(spoke: Path, remote: Path) -> None:
    shutil.rmtree(spoke / ".review")  # precondition 3 would refuse
    (spoke / "work.txt").write_text("dirty\n")  # precondition 1 would refuse too

    result = _run(spoke, "45", env={**_GIT_ENV, "AI_TOOLKIT_READY_FORCE": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45"), "force must still emit the marker"


def test_ready_force_is_logged_loudly(spoke: Path) -> None:
    shutil.rmtree(spoke / ".review")

    result = _run(spoke, "45", env={**_GIT_ENV, "AI_TOOLKIT_READY_FORCE": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORCE" in (result.stdout + result.stderr), "the force bypass must be logged loudly"


def test_accept_is_not_subject_to_the_ready_gate(spoke: Path, remote: Path) -> None:
    # --accept is unchanged by #172: it keeps only its durability check, so a
    # missing review artifact / dirty tree must NOT block it.
    shutil.rmtree(spoke / ".review")
    (spoke / "work.txt").write_text("dirty\n")

    result = _run(spoke, "--accept", "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/accept/45")


# ── event-driven wake: announce the marker to a live /afk supervisor (issue #176) ──
# After the marker reaches origin, spoke-ready drops a <epoch>-<issue>-<kind> file in the
# event spool and SIGUSR1s the heartbeat pid, so a parked answer/land fires without waiting
# the backstop tick. Gated on a LIVE supervisor: attended runs leave no spool artifact.


def _live_pid(tmp_path: Path) -> subprocess.Popen:
    """A real background process to stand in for a running supervisor's heartbeat pid."""
    return subprocess.Popen(["sleep", "30"])


def _wake_env(heartbeat: Path, state_dir: Path) -> dict[str, str]:
    return {**_GIT_ENV, "AFK_HEARTBEAT": str(heartbeat), "AFK_STATE_DIR": str(state_dir)}


def test_gate_emits_spool_file_when_supervisor_live(spoke: Path, tmp_path: Path) -> None:
    proc = _live_pid(tmp_path)
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        result = _run(spoke, "--gate", "45", env=_wake_env(hb, state))
    finally:
        proc.terminate()

    assert result.returncode == 0, result.stdout + result.stderr
    events = list((state / "events").glob("*-45-gate"))
    assert len(events) == 1, f"a live supervisor must get one gate spool file, saw {events}"


@pytest.mark.parametrize("flag,kind", [("--gate", "gate"), ("--blocked", "blocked")])
def test_marker_kinds_spool_their_own_type(
    spoke: Path, tmp_path: Path, flag: str, kind: str
) -> None:
    proc = _live_pid(tmp_path)
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        result = _run(spoke, flag, "45", env=_wake_env(hb, state))
    finally:
        proc.terminate()

    assert result.returncode == 0, result.stdout + result.stderr
    assert list((state / "events").glob(f"*-45-{kind}")), f"{flag} must spool a {kind} event"


def test_wake_signals_the_heartbeat_pid(spoke: Path, tmp_path: Path) -> None:
    # The signal itself, not just the spool file: a process trapping USR1 must be woken.
    flag_file = tmp_path / "signalled"
    # `sleep & wait` (not a bare `sleep`) so USR1 interrupts the wait and the trap fires at
    # once — the same reason the real supervisor's afk_interruptible_sleep backgrounds it.
    proc = subprocess.Popen(
        ["bash", "-c", f'trap "touch {flag_file}; exit 0" USR1; sleep 30 & wait']
    )
    hb = tmp_path / "hb"
    hb.write_text(f"{proc.pid} 1000000\n")
    state = tmp_path / "afk-state"
    try:
        _run(spoke, "--gate", "45", env=_wake_env(hb, state))
        proc.wait(timeout=5)  # the trap calls exit on USR1
    finally:
        if proc.poll() is None:
            proc.terminate()

    assert flag_file.exists(), "the heartbeat pid must receive SIGUSR1"


def test_no_spool_without_a_heartbeat_file(spoke: Path, tmp_path: Path) -> None:
    # Attended run (no supervisor): no heartbeat -> no spool artifact, emission still succeeds.
    hb = tmp_path / "hb"  # never created
    state = tmp_path / "afk-state"

    result = _run(spoke, "--gate", "45", env=_wake_env(hb, state))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (state / "events").exists(), "no live supervisor => no spool dir/file"


def test_no_spool_when_heartbeat_pid_is_dead(spoke: Path, tmp_path: Path) -> None:
    proc = subprocess.Popen(["sleep", "30"])
    dead_pid = proc.pid
    proc.terminate()
    proc.wait()
    hb = tmp_path / "hb"
    hb.write_text(f"{dead_pid} 1000000\n")
    state = tmp_path / "afk-state"

    result = _run(spoke, "--gate", "45", env=_wake_env(hb, state))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (state / "events").exists(), "a dead heartbeat pid => nothing to wake, no spool"


# ── The marker push: SSH keepalive + retry-once on transport death (issue #184) ──
# On the --ready path the tag push runs seconds after the branch push's ~6-minute
# in-push gate staled the SSH connection (issue #119): a bare `git push -f` died in
# the transfer phase (exit 141 / SIGPIPE), so the branch landed but the spoke never
# announced itself and the drain stalled. The ONE push line all four marker kinds
# share must (a) route through wt_git_push so GIT_SSH_COMMAND carries ServerAlive*
# keepalive options, and (b) mirror worktree-land's second line of defense: retry
# EXACTLY once when a failed attempt is demonstrably a TRANSPORT death — a
# non-transport failure (hook/policy rejection) must NOT be retried.

KEEPALIVE_OPTS = "-o ServerAliveInterval=15 -o ServerAliveCountMax=40"


def _install_git_shim(tmp_path: Path, *, fail_first_push: str | None = None) -> tuple[Path, Path]:
    """A PATH-front ``git`` logging GIT_SSH_COMMAND + argv per push, then delegating.

    With ``fail_first_push``, the FIRST ``git push`` invocation fails instead of
    delegating: ``"transport"`` simulates a stale-SSH transport death (exit 141,
    the post-gate SIGPIPE shape of issue #119); ``"rejected"`` a hook/policy
    rejection (exit 1, no transport signature). Every later push delegates to the
    real git, so a retried push still lands on the hermetic bare origin.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    log = tmp_path / "push-invocations.log"
    marker = tmp_path / "first-push-done"
    fail_snippet = ""
    if fail_first_push == "transport":
        fail_snippet = (
            f'  if [ ! -f "{marker}" ]; then\n'
            f'    touch "{marker}"\n'
            '    echo "client_loop: send disconnect: Broken pipe" >&2\n'
            "    exit 141\n"
            "  fi\n"
        )
    elif fail_first_push == "rejected":
        fail_snippet = (
            f'  if [ ! -f "{marker}" ]; then\n'
            f'    touch "{marker}"\n'
            '    echo "remote: rejected by policy" >&2\n'
            "    exit 1\n"
            "  fi\n"
        )
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = push ]; then\n'
        f'  echo "GIT_SSH_COMMAND=[$GIT_SSH_COMMAND] $*" >> "{log}"\n'
        f"{fail_snippet}"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    return log, bindir


def _run_with_shim(repo: Path, bindir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("GIT_SSH_COMMAND", None)
    return subprocess.run(
        ["bash", str(SPOKE_READY), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "args,tag",
    [
        pytest.param(("45",), "ready/45", id="ready"),
        pytest.param(("--blocked", "45"), "blocked/45", id="blocked"),
    ],
)
def test_marker_push_carries_keepalive_ssh_command(
    spoke: Path, remote: Path, tmp_path: Path, args: tuple[str, ...], tag: str
) -> None:
    log, bindir = _install_git_shim(tmp_path)

    result = _run_with_shim(spoke, bindir, *args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, f"refs/tags/{tag}")
    recorded = log.read_text()
    assert f"GIT_SSH_COMMAND=[ssh {KEEPALIVE_OPTS}] push -f origin {tag}" in recorded


def test_marker_push_retries_once_on_transport_death(
    spoke: Path, remote: Path, tmp_path: Path
) -> None:
    log, bindir = _install_git_shim(tmp_path, fail_first_push="transport")

    result = _run_with_shim(spoke, bindir, "45")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_has_ref(remote, "refs/tags/ready/45"), "the retried push must land the marker"
    pushes = log.read_text().splitlines()  # the shim logs one line per `git push`
    assert len(pushes) == 2, f"a transport death must be retried exactly once, saw {pushes}"


def test_marker_push_not_retried_on_rejection(spoke: Path, remote: Path, tmp_path: Path) -> None:
    # Only a demonstrable transport death earns the retry: a hook/policy rejection
    # must fail the emission on the first attempt, not be blindly re-pushed.
    log, bindir = _install_git_shim(tmp_path, fail_first_push="rejected")

    result = _run_with_shim(spoke, bindir, "45")

    assert result.returncode != 0, "a rejected push must fail the emission"
    assert not _remote_has_ref(remote, "refs/tags/ready/45")
    pushes = log.read_text().splitlines()
    assert len(pushes) == 1, f"a non-transport failure must NOT be retried, saw {pushes}"
