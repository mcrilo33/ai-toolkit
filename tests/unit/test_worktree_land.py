"""Unit tests for scripts/worktree-land.sh — the hub-side landing sequence.

Landing is hub-owned: verify the spoke pushed → merge into the default branch →
push main (the pre-push hook is the single test gate, issue #19) → tear down the
worktree (worktree-done.sh) → close the issue → kill the stranded tmux window.
Every guard must abort with a precise reason BEFORE the merge; landing no longer
runs the suite itself, and a pre-push rejection (the gate failing) must roll main
back.

Hermetic like test_worktree_done.py: git runs against a local bare `origin`, and
`gh`, `tmux`, `code`, and `pytest` are logging stubs on PATH — no network, no
real issue closes, no real tmux server, and no land-side pytest (a stub proves
landing never invokes it). A stub hub pre-push hook stands in for the real gate
when a test needs to assert env threading or rollback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

WORKTREE_LAND = Path(__file__).resolve().parents[2] / "scripts" / "worktree-land.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)
# A host GIT_SSH_COMMAND must not prefix the keepalive assertion (#119).
_GIT_ENV.pop("GIT_SSH_COMMAND", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    return hub


def _issue_of(branch: str) -> str:
    """Leading digits of the branch slug (feature/1-foo → '1'); '' when ad-hoc."""
    slug = branch.rsplit("/", 1)[-1]
    num = slug.split("-", 1)[0]
    return num if num.isdigit() else ""


def _make_spoke(hub: Path, tmp_path: Path, branch: str, *, push: bool, ready: bool = True) -> Path:
    """Add a worktree on `branch` with one commit; optionally push it to origin.

    A normally-completed spoke also carries the issue #16 completion marker: when
    `push` and `ready` and the slug has an issue number, tag `ready/<issue>` at the
    branch tip and push it. Tests that exercise the marker guard pass `ready=False`
    (or mutate the tag afterwards) to model a mid-task or stale push.
    """
    wt = tmp_path / branch.replace("/", "-")
    _git(hub, "worktree", "add", "-q", "-b", branch, str(wt))
    fname = f"{branch.replace('/', '-')}.txt"
    (wt / fname).write_text(f"work on {branch}\n")
    _git(wt, "add", fname)
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #1")
    if push:
        _git(wt, "push", "-q", "-u", "origin", branch)
        issue = _issue_of(branch)
        if ready and issue:
            _git(wt, "tag", f"ready/{issue}")
            _git(wt, "push", "-q", "origin", f"ready/{issue}")
    return wt


def _run_land(
    hub: Path,
    tmp_path: Path,
    *args: str,
    gh_exit: int = 0,
    tmux_windows: str = "",
    spoke_marker: str | None = None,
    extra_env: dict[str, str] | None = None,
    stub_python312: bool = False,
    stub_curl: bool = False,
) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    """Run worktree-land.sh from the hub with logging stubs on PATH.

    Stubs `gh`, `tmux`, and `code` (one log line per invocation each), plus a
    `pytest` stub logging every call — landing must NOT run pytest itself anymore
    (issue #19): the suite runs once via the pre-push hook on the main push.
    `tmux_windows` is the line(s) the tmux stub prints for `list-windows`.
    Returns the completed process and the stub logs by name.

    Telemetry isolation (issue #127): the land script now resolves Langfuse auth
    ITSELF from ${AFK_TELEMETRY_CONF:-~/.afk-telemetry}, so the harness always
    pins AFK_TELEMETRY_CONF to a nonexistent sandbox path and strips the
    LANGFUSE_* / span-endpoint env — otherwise every test here would read the
    operator's REAL conf and POST fixture spans to a live collector (the #49
    fixture-leak class). A test that wants auth opts in via `extra_env` with its
    own tmp conf. `stub_python312` stubs the ingest interpreter (logging its
    LANGFUSE_* env per call); `stub_curl` stubs curl (logging argv, then stdin —
    the OTLP span payload) so span POSTs are captured, never sent."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    logs = {
        name: tmp_path / f"{name}-calls.log"
        for name in ("gh", "tmux", "code", "pytest", "python3.12", "curl")
    }
    for name, exit_code in (("gh", gh_exit), ("code", 0)):
        stub = bindir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs[name]}"\nexit {exit_code}\n')
        stub.chmod(0o755)
    tmux = bindir / "tmux"
    tmux.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs["tmux"]}"\n'
        f'case "$1" in list-windows) printf "%s\\n" "{tmux_windows}" ;; esac\nexit 0\n'
    )
    tmux.chmod(0o755)
    pytest_stub = bindir / "pytest"
    pytest_stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs["pytest"]}"\nexit 0\n')
    pytest_stub.chmod(0o755)
    if stub_python312:
        py_stub = bindir / "python3.12"
        py_stub.write_text(
            "#!/bin/sh\n"
            f'printf "CALL %s\\n" "$*" >> "{logs["python3.12"]}"\n'
            f'env | grep -E "^LANGFUSE_" >> "{logs["python3.12"]}" || true\n'
            "exit 0\n"
        )
        py_stub.chmod(0o755)
    if stub_curl:
        curl_stub = bindir / "curl"
        curl_stub.write_text(
            "#!/bin/sh\n"
            f'printf "ARGV %s\\n" "$*" >> "{logs["curl"]}"\n'
            f'cat >> "{logs["curl"]}"\nprintf "\\n" >> "{logs["curl"]}"\n'
            "exit 0\n"
        )
        curl_stub.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    for var in ("LANGFUSE_BASIC_AUTH", "LANGFUSE_HOST", "AI_TOOLKIT_OTEL_SPAN_ENDPOINT"):
        env.pop(var, None)
    env["AFK_TELEMETRY_CONF"] = str(tmp_path / "no-such-conf")
    # The host's own spoke marker must never steer the guard; set it explicitly
    # only when a test means to model a spoke session (issue #26).
    env.pop("WT_SPOKE", None)
    if spoke_marker is not None:
        env["WT_SPOKE"] = spoke_marker
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(WORKTREE_LAND), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, logs


def _install_prepush_stub(hub: Path, *, exit_code: int = 0, env_log: Path | None = None) -> None:
    """Install a hub pre-push hook standing in for the real test gate.

    It records the threaded TEST_SELECT_* environment (when `env_log` is given)
    and exits `exit_code`, so a test can assert what landing delegates to the
    hook and that a rejection (non-zero) rolls the merge back."""
    hook = hub / ".git" / "hooks" / "pre-push"
    body = "#!/bin/sh\n"
    if env_log is not None:
        body += f'env | grep -E "^TEST_SELECT_" >> "{env_log}" || true\n'
    body += f"exit {exit_code}\n"
    hook.write_text(body)
    hook.chmod(0o755)


def _local_branches(hub: Path) -> list[str]:
    out = _git(hub, "branch", "--format=%(refname:short)")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_sha(hub: Path, branch: str) -> str:
    out = _git(hub, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out.strip() else ""


def _local_tags(hub: Path) -> list[str]:
    out = _git(hub, "tag", "--list")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _remote_tag_sha(hub: Path, tag: str) -> str:
    out = _git(hub, "ls-remote", "--tags", "origin", tag)
    return out.split()[0] if out.strip() else ""


def _log_text(log: Path) -> str:
    return log.read_text() if log.exists() else ""


# --- happy path ----------------------------------------------------------------


def test_lands_pushed_branch_into_main(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-done.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_fast_forwards_when_possible(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ff", push=True)
    spoke_sha = _git(wt, "rev-parse", "HEAD").strip()

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _git(hub, "rev-parse", "HEAD").strip() == spoke_sha


def test_merge_commit_when_main_advanced(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-diverged", push=True)
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    parents = _git(hub, "rev-list", "--parents", "-n1", "HEAD").split()
    assert len(parents) == 3  # merge commit: self + two parents


def test_worktree_removed_and_branch_pruned(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-pruned", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert not wt.exists()
    assert "feature/1-pruned" not in _local_branches(hub)
    assert _remote_sha(hub, "feature/1-pruned") == ""


def test_keep_branch_flag_keeps_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-kept", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", "--keep-branch")

    assert proc.returncode == 0, proc.stderr
    assert "feature/1-kept" in _local_branches(hub)


# --- spoke-session guard (issue #26) --------------------------------------------
# Landing is hub-owned. A spoke's claude carries WT_SPOKE in its environment, so
# even if it cd's into the main checkout (where the directory guard would pass),
# the role marker must abort the land before any merge. The hub session is
# started directly by the user, never carries WT_SPOKE, and lands freely.


def test_refuses_when_run_as_spoke_session(hub: Path, tmp_path: Path) -> None:
    # A fully-ready spoke (pushed + matching marker) would otherwise land — the
    # WT_SPOKE marker alone must stop it before the merge.
    _make_spoke(hub, tmp_path, "feature/1-spoke", push=True, ready=True)
    pre_main = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "1", spoke_marker="1")

    assert proc.returncode != 0
    assert "spoke" in proc.stderr.lower()
    assert "hub" in proc.stderr.lower()
    assert not (hub / "feature-1-spoke.txt").exists()  # nothing merged
    assert _remote_sha(hub, "main") == pre_main


def test_lands_when_not_a_spoke_session(hub: Path, tmp_path: Path) -> None:
    # The mirror: with WT_SPOKE unset (the hub), landing proceeds as normal.
    _make_spoke(hub, tmp_path, "feature/1-hub", push=True, ready=True)

    proc, _ = _run_land(hub, tmp_path, "1", spoke_marker=None)

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-hub.txt").exists()


# --- guards (all must abort BEFORE the merge) -----------------------------------


def test_refuses_off_default_branch(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-guard", push=True)
    _git(hub, "checkout", "-qb", "side")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "main" in proc.stderr
    assert not (hub / "feature-1-guard.txt").exists()


def test_refuses_dirty_hub(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-guard", push=True)
    (hub / "README.md").write_text("dirty\n")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert not (hub / "feature-1-guard.txt").exists()


def test_refuses_never_pushed_spoke(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-local", push=False)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "push" in proc.stderr.lower()
    assert not (hub / "feature-1-local.txt").exists()


def test_refuses_spoke_ahead_of_upstream(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ahead", push=True)
    (wt / "extra.txt").write_text("not pushed\n")
    _git(wt, "add", "extra.txt")
    _git(wt, "commit", "-qm", "feat: extra", "-m", "Refs #1")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "push" in proc.stderr.lower()


def test_refuses_spoke_behind_upstream(hub: Path, tmp_path: Path) -> None:
    # Push two commits, then drop one locally (an ordinary post-push "undo").
    # Landing the reduced branch would prune the remote ref and silently lose
    # the dropped commit — the guard must refuse instead.
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-behind", push=False)
    (wt / "second.txt").write_text("pushed then dropped\n")
    _git(wt, "add", "second.txt")
    _git(wt, "commit", "-qm", "feat: two", "-m", "Refs #1")
    _git(wt, "push", "-q", "-u", "origin", "feature/1-behind")
    _git(wt, "reset", "-q", "--hard", "HEAD~1")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "behind" in proc.stderr.lower()
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha
    assert _remote_sha(hub, "feature/1-behind") != ""  # remote ref untouched


def test_merge_conflict_aborts_cleanly(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-conflict", push=False)
    (wt / "README.md").write_text("spoke version\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "feat: spoke readme", "-m", "Refs #1")
    _git(wt, "push", "-q", "-u", "origin", "feature/1-conflict")
    _git(wt, "tag", "ready/1")  # marked complete, so landing reaches the merge step
    _git(wt, "push", "-q", "origin", "ready/1")
    (hub / "README.md").write_text("hub version\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: hub readme", "-m", "Refs #0")
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha
    assert _git(hub, "status", "--porcelain") == ""  # no MERGE_HEAD left behind
    assert wt.exists()


def test_refuses_dirty_spoke_worktree(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-dirty", push=True)
    (wt / "wip.txt").write_text("uncommitted\n")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert not (hub / "feature-1-dirty.txt").exists()


def test_refuses_unknown_target(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-real", push=True)

    proc, _ = _run_land(hub, tmp_path, "99")

    assert proc.returncode != 0
    assert "feature/1-real" in proc.stderr  # candidates are listed for recovery


# --- ready-to-land marker guard (issue #16) -------------------------------------
# A per-subtask push is indistinguishable from task completion. Landing a
# numbered branch therefore requires an explicit ready/<issue> marker at the
# branch tip; --force-land overrides, and ad-hoc/express branches are exempt.


def test_refuses_pushed_branch_without_marker(hub: Path, tmp_path: Path) -> None:
    # Mid-task: pushed and clean, but no completion marker → must refuse so the
    # hub never lands a half-finished issue and tears down its worktree.
    _make_spoke(hub, tmp_path, "feature/1-midtask", push=True, ready=False)
    pre_main = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "ready/1" in proc.stderr
    assert not (hub / "feature-1-midtask.txt").exists()  # nothing merged
    assert _remote_sha(hub, "main") == pre_main


def test_refuses_stale_marker(hub: Path, tmp_path: Path) -> None:
    # Marker sha != branch tip: the spoke pushed more work after tagging, so the
    # completion claim no longer covers the tip — refuse like a missing marker.
    wt = _make_spoke(hub, tmp_path, "feature/1-stale", push=True, ready=True)
    (wt / "more.txt").write_text("pushed after tagging\n")
    _git(wt, "add", "more.txt")
    _git(wt, "commit", "-qm", "feat: more", "-m", "Refs #1")
    _git(wt, "push", "-q", "origin", "feature/1-stale")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "ready/1" in proc.stderr
    assert not (hub / "more.txt").exists()  # nothing merged


def test_lands_with_matching_marker(hub: Path, tmp_path: Path) -> None:
    # The marker points at the tip → land normally.
    _make_spoke(hub, tmp_path, "feature/1-ready", push=True, ready=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-ready.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_force_land_overrides_missing_marker(hub: Path, tmp_path: Path) -> None:
    # --force-land is the escape hatch for branches that never carry a marker.
    _make_spoke(hub, tmp_path, "feature/1-forced", push=True, ready=False)

    proc, _ = _run_land(hub, tmp_path, "1", "--force-land")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-forced.txt").exists()


def test_adhoc_branch_lands_without_marker(hub: Path, tmp_path: Path) -> None:
    # Non-numeric slug = no issue to anchor a marker to; the single push IS
    # completion, so the guard is exempt without --force-land.
    _make_spoke(hub, tmp_path, "chore/adhoc-marker", push=True)

    proc, _ = _run_land(hub, tmp_path, "adhoc-marker")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "chore-adhoc-marker.txt").exists()


def test_marker_tag_deleted_after_landing(hub: Path, tmp_path: Path) -> None:
    # Landing consumes the marker: the local and remote ready/<issue> tags are
    # cleaned up so a stale tag can never re-flag a future branch as mergeable.
    _make_spoke(hub, tmp_path, "feature/1-consumed", push=True, ready=True)
    assert "ready/1" in _local_tags(hub)
    assert _remote_tag_sha(hub, "ready/1") != ""

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert "ready/1" not in _local_tags(hub)
    assert _remote_tag_sha(hub, "ready/1") == ""


def test_local_micro_spoke_exempt_from_marker(hub: Path, tmp_path: Path) -> None:
    # --local micro-spokes never push and carry no marker; the hub's diff review
    # is their gate, so the marker guard must not apply.
    _make_spoke(hub, tmp_path, "feature/2-micro", push=False, ready=False)

    proc, _ = _run_land(hub, tmp_path, "2", "--local")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-2-micro.txt").exists()


# --- the pre-push hook is the single test gate (issue #19) -----------------------


def test_default_land_runs_no_land_side_pytest(hub: Path, tmp_path: Path) -> None:
    # Landing no longer runs the suite itself; the pre-push hook tests once on the
    # main push. A diverged merge takes the gate path (a clean-FF land instead
    # auto-skips it — see test_clean_ff_land_skips_redundant_gate), and with no
    # hook installed here the land honestly warns that the gate did not run.
    _make_spoke(hub, tmp_path, "feature/1-nopytest", push=True)
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _log_text(logs["pytest"]) == ""  # land never invoked pytest itself
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    assert "test gate will NOT run" in proc.stderr  # honest about the absent hook


# --- skip the redundant gate on a clean fast-forward land (issue #96) ------------
# A clean-FF land of an already-gated branch re-tests an identical tree: the spoke
# already ran the gate on its push (ready/N marker == tip == upstream). Thread
# TEST_SELECT_SKIP=1 in that case only; any diverged/merge-commit land — whose
# combined tree was never tested as a unit — still runs the full gate.


def test_clean_ff_land_skips_redundant_gate(hub: Path, tmp_path: Path) -> None:
    # Clean fast-forward (nothing landed since the branch's base) of a branch whose
    # ready/1 marker sits at the tip → the merged tree is identical to the already-
    # gated tip, so the gate is skipped by threading TEST_SELECT_SKIP=1 to the push.
    _make_spoke(hub, tmp_path, "feature/1-ffskip", push=True, ready=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP=1" in _log_text(env_log)
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_diverged_merge_still_runs_gate(hub: Path, tmp_path: Path) -> None:
    # main gained a commit since the branch's base, so landing creates a merge
    # commit whose combined tree was never tested as a unit — the gate MUST still
    # run (no TEST_SELECT_SKIP threaded), even though the branch carries a marker.
    _make_spoke(hub, tmp_path, "feature/1-divgate", push=True, ready=True)
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP" not in _log_text(env_log)


# --- SSH keepalive + retry-once-after-green on the ship push (issue #119) --------
# The ship push runs the ~6-minute gate INSIDE `git push`; GitHub reaps the idle
# SSH connection mid-gate and the post-gate transfer dies on a fully green tree.
# Two lines of defense: the push routes through wt_git_push (keepalive), and when
# the gate demonstrably passed but the transport still died, worktree-land retries
# exactly once with TEST_SELECT_SKIP=1 — loudly, and never after a failed gate.


def _install_counting_gate(hub: Path, log: Path, *, exit_code: int = 0, stderr: str = "") -> None:
    """A hub pre-push hook logging one `INVOKED skip=[…]` line per invocation.

    `stderr` simulates gate (pytest) output; `exit_code` non-zero models a
    failing gate. The skip value records the threaded TEST_SELECT_SKIP so a
    test can tell a first attempt from a skip-retry.
    """
    hook = hub / ".git" / "hooks" / "pre-push"
    body = f'#!/bin/sh\necho "INVOKED skip=[${{TEST_SELECT_SKIP:-}}]" >> "{log}"\n'
    if stderr:
        body += f"cat >&2 <<'GATEEOF'\n{stderr}\nGATEEOF\n"
    body += f"exit {exit_code}\n"
    hook.write_text(body)
    hook.chmod(0o755)


def _install_pre_receive(
    tmp_path: Path, log: Path, *, fail_first: bool = False, stderr_line: str = ""
) -> None:
    """A pre-receive hook on the bare origin logging each pushed ref.

    With `fail_first`, the FIRST invocation emits `stderr_line` and rejects —
    modeling a transport-death/rejection after the local gate ran — and every
    later one succeeds, so a retry can land.
    """
    marker = tmp_path / "pre-receive-failed-once"
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    body = f'#!/bin/sh\nwhile read -r _o _n ref; do echo "$ref" >> "{log}"; done\n'
    if fail_first:
        body += (
            f'if [ ! -e "{marker}" ]; then\n'
            f'  touch "{marker}"\n'
            f'  echo "{stderr_line}" >&2\n'
            "  exit 1\n"
            "fi\n"
        )
    body += "exit 0\n"
    hook.write_text(body)
    hook.chmod(0o755)


def _diverge_hub(hub: Path) -> None:
    """Advance the hub's main so the land is a real merge and the gate runs."""
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")


def test_green_gate_transport_death_retries_once_with_skip(hub: Path, tmp_path: Path) -> None:
    # Gate green, then the transfer dies with a transport signature → retry
    # EXACTLY once with TEST_SELECT_SKIP=1 (the gate already ran green), loudly,
    # and the land completes.
    _make_spoke(hub, tmp_path, "feature/1-transport", push=True, ready=True)
    _diverge_hub(hub)
    gate_log = tmp_path / "gate-calls.log"
    _install_counting_gate(hub, gate_log)
    ref_log = tmp_path / "pre-receive-refs.log"
    _install_pre_receive(
        tmp_path,
        ref_log,
        fail_first=True,
        stderr_line="Connection to ssh.github.com closed by remote host.",
    )

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    # Ship attempts: gated first try, then exactly one skip-retry. Later entries
    # are the post-land maintenance pushes (ready-tag delete, branch delete),
    # which fire the same hub hook gate-free — pre-existing behavior.
    gate_calls = _log_text(gate_log).splitlines()
    assert gate_calls[:2] == ["INVOKED skip=[]", "INVOKED skip=[1]"], gate_calls
    assert gate_calls.count("INVOKED skip=[1]") == 1, gate_calls
    # Exactly two transfers reached the remote for main: the dying one + the retry.
    main_refs = [ln for ln in _log_text(ref_log).splitlines() if ln == "refs/heads/main"]
    assert len(main_refs) == 2, _log_text(ref_log)
    # The retry is loud about what it is doing and why it may skip the gate.
    assert "retry" in proc.stderr.lower()
    assert "TEST_SELECT_SKIP" in proc.stderr


def test_failed_gate_rolls_back_without_retry(hub: Path, tmp_path: Path) -> None:
    # The gate itself fails → exactly ONE attempt, rollback as today, no retry.
    _make_spoke(hub, tmp_path, "feature/1-redgate", push=True, ready=True)
    _diverge_hub(hub)
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    gate_log = tmp_path / "gate-calls.log"
    _install_counting_gate(hub, gate_log, exit_code=1)
    ref_log = tmp_path / "pre-receive-refs.log"
    _install_pre_receive(tmp_path, ref_log)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _log_text(gate_log).splitlines() == ["INVOKED skip=[]"]
    assert "refs/heads/main" not in _log_text(ref_log)  # push aborted locally
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back
    assert _remote_sha(hub, "main") != ""  # origin/main untouched


def test_failed_gate_with_transport_prose_never_retries(hub: Path, tmp_path: Path) -> None:
    # A FAILING gate whose pytest output quotes a transport signature (this very
    # repo's tests embed those literals) must still read as a failed gate: the
    # pytest failure summary is the tiebreaker, and no skip-retry may ship the
    # red tree.
    _make_spoke(hub, tmp_path, "feature/1-prose", push=True, ready=True)
    _diverge_hub(hub)
    gate_log = tmp_path / "gate-calls.log"
    gate_output = (
        "FAILED tests/unit/test_x.py::test_y - assert 'Connection to "
        "ssh.github.com closed by remote host.' in caplog.text\n"
        "=== 1 failed, 12 passed in 340.12s ==="
    )
    _install_counting_gate(hub, gate_log, exit_code=1, stderr=gate_output)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _log_text(gate_log).splitlines() == ["INVOKED skip=[]"]


def test_failed_collection_with_transport_prose_never_retries(hub: Path, tmp_path: Path) -> None:
    # A gate failing at COLLECTION time prints "Interrupted: N errors during
    # collection" and never a "N failed" summary — with a transport literal in
    # the traceback (importing a literal-bearing test file that broke) it must
    # STILL read as a failed gate: no skip-retry may ship a non-importing tree.
    _make_spoke(hub, tmp_path, "feature/1-collect", push=True, ready=True)
    _diverge_hub(hub)
    gate_log = tmp_path / "gate-calls.log"
    gate_output = (
        "ERROR tests/unit/test_worktree_lib.py - ImportError while importing; source "
        "quotes 'Connection to ssh.github.com closed by remote host.'\n"
        "!!!!!!!! Interrupted: 1 error during collection !!!!!!!!\n"
        "=== 1 error in 2.31s ==="
    )
    _install_counting_gate(hub, gate_log, exit_code=1, stderr=gate_output)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _log_text(gate_log).splitlines() == ["INVOKED skip=[]"]


def test_remote_rejection_after_green_gate_rolls_back_without_retry(
    hub: Path, tmp_path: Path
) -> None:
    # Green gate but the remote rejects for a POLICY reason (no transport
    # signature) → a retry would just fail again: roll back exactly as today.
    _make_spoke(hub, tmp_path, "feature/1-policy", push=True, ready=True)
    _diverge_hub(hub)
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    gate_log = tmp_path / "gate-calls.log"
    _install_counting_gate(hub, gate_log)
    ref_log = tmp_path / "pre-receive-refs.log"
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'while read -r _o _n ref; do echo "$ref" >> "{ref_log}"; done\n'
        'echo "protected branch hook declined" >&2\n'
        "exit 1\n"
    )
    hook.chmod(0o755)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _log_text(gate_log).splitlines() == ["INVOKED skip=[]"]
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back


def test_ship_push_carries_keepalive(hub: Path, tmp_path: Path) -> None:
    # The ship push routes through wt_git_push: a PATH-front git shim (in the
    # same bindir _run_land populates) records GIT_SSH_COMMAND per push.
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    push_log = tmp_path / "push-invocations.log"
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = push ]; then echo "GIT_SSH_COMMAND=[$GIT_SSH_COMMAND] $*" >> "{push_log}"; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)
    _make_spoke(hub, tmp_path, "feature/1-shipkeep", push=True, ready=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    keepalive = "-o ServerAliveInterval=15 -o ServerAliveCountMax=40"
    main_pushes = [ln for ln in _log_text(push_log).splitlines() if ln.endswith("push origin main")]
    assert main_pushes, f"no ship push recorded: {_log_text(push_log)!r}"
    assert f"GIT_SSH_COMMAND=[ssh {keepalive}]" in main_pushes[0]


def test_force_gate_env_overrides_ff_skip(hub: Path, tmp_path: Path) -> None:
    # LAND_FORCE_GATE=1 is the escape hatch: run the full gate even on a clean-FF
    # already-gated land, for when the redundant run is wanted anyway.
    _make_spoke(hub, tmp_path, "feature/1-forcegate", push=True, ready=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1", extra_env={"LAND_FORCE_GATE": "1"})

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP" not in _log_text(env_log)


def test_force_land_without_marker_still_runs_ff_gate(hub: Path, tmp_path: Path) -> None:
    # --force-land lands a markerless branch: without a marker we cannot prove the
    # tip was gated, so even a clean FF must still run the gate (no auto-skip).
    _make_spoke(hub, tmp_path, "feature/1-forcedff", push=True, ready=False)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1", "--force-land")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP" not in _log_text(env_log)


def test_push_gate_failure_rolls_back(hub: Path, tmp_path: Path) -> None:
    # A pre-push rejection (the test gate failing) must roll the merged hub back
    # and ship nothing — the clean-hub invariant the old land-side gate held.
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-broken", push=True)
    _install_prepush_stub(hub, exit_code=1)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back
    assert _remote_sha(hub, "main") == pre_sha  # nothing was pushed
    assert wt.exists()  # teardown never ran
    assert "feature/1-broken" in _local_branches(hub)


def test_skip_tests_threads_skip_env(hub: Path, tmp_path: Path) -> None:
    # --skip-tests bypasses the suite by threading TEST_SELECT_SKIP to the push,
    # not by a land-side run — the hook stays the single executor.
    _make_spoke(hub, tmp_path, "feature/1-untested", push=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, logs = _run_land(hub, tmp_path, "1", "--skip-tests")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP=1" in _log_text(env_log)
    assert _log_text(logs["pytest"]) == ""  # still no land-side pytest


def test_test_cmd_threads_cmd_env(hub: Path, tmp_path: Path) -> None:
    # --test-cmd overrides the suite by threading TEST_SELECT_CMD to the push, so
    # the hook runs the custom command instead of the tiered selection.
    _make_spoke(hub, tmp_path, "feature/1-custom", push=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1", "--test-cmd", "my-suite --fast")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_CMD=my-suite --fast" in _log_text(env_log)
    assert "test gate will NOT run" not in proc.stderr  # hook present → no warning


# --- ship and teardown -----------------------------------------------------------


def test_push_failure_aborts_before_teardown(hub: Path, tmp_path: Path) -> None:
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-stuck", push=True)
    _git(hub, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # merge rolled back
    assert wt.exists()  # worktree survives a failed ship
    assert _log_text(logs["gh"]) == ""  # issue was not closed


def test_issue_closed_via_gh(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-shipped", push=True)

    proc, logs = _run_land(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    assert "issue close 7" in _log_text(logs["gh"])


def test_gh_failure_is_non_fatal(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-ghdown", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", gh_exit=1)

    assert proc.returncode == 0, proc.stderr


def test_adhoc_branch_skips_issue_close(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "chore/adhoc-task", push=True)

    proc, logs = _run_land(hub, tmp_path, "adhoc-task")

    assert proc.returncode == 0, proc.stderr
    assert "issue close" not in _log_text(logs["gh"])


# --- tmux window cleanup (per-project session, issue #39) -------------------------


def test_cleanup_lists_windows_in_project_session(hub: Path, tmp_path: Path) -> None:
    # The land-side cleanup must enumerate the project session that
    # worktree-new.sh spawned spokes into — not the retired hardcoded session 0,
    # or stranded windows would never be found and would accumulate.
    _make_spoke(hub, tmp_path, "feature/1-stranded", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows="@3\t1-stranded\t/gone/path")

    assert proc.returncode == 0, proc.stderr
    lw = next(ln for ln in _log_text(logs["tmux"]).splitlines() if ln.startswith("list-windows"))
    assert "-t 0" not in lw, "cleanup still targets the retired session 0"
    assert "-t =" in lw, "the '=' exact-match guard must be preserved"
    assert "hub" in lw, "the session must be named after the project (repo basename)"


def test_stranded_tmux_window_is_killed(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-stranded", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows="@3\t1-stranded\t/gone/path")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" in _log_text(logs["tmux"])


def test_live_tmux_window_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-alive", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows=f"@3\t1-alive\t{tmp_path}")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" not in _log_text(logs["tmux"])


def test_unrelated_tmux_window_is_kept(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-mine", push=True)

    proc, logs = _run_land(hub, tmp_path, "1", tmux_windows="@4\t2-other\t/gone/path")

    assert proc.returncode == 0, proc.stderr
    assert "kill-window" not in _log_text(logs["tmux"])


# --- --local: micro-spoke landing (issue #10) ---


def test_local_lands_never_pushed_worktree_branch(hub: Path, tmp_path: Path) -> None:
    # A micro-spoke commits locally and never pushes its branch; --local skips
    # the upstream guards and lands it like any other worktree.
    wt = _make_spoke(hub, tmp_path, "feature/micro-tweak", push=False)

    proc, _ = _run_land(hub, tmp_path, "micro-tweak", "--local")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-micro-tweak.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    assert not wt.exists()
    assert "feature/micro-tweak" not in _local_branches(hub)


def test_local_lands_bare_branch_without_worktree(hub: Path, tmp_path: Path) -> None:
    # A hub-dispatched subagent's temp worktree may already be gone; --local
    # must land the bare local branch all the same.
    wt = _make_spoke(hub, tmp_path, "claude/micro-docs", push=False)
    _git(hub, "worktree", "remove", str(wt))  # clean post-commit, removal succeeds

    proc, logs = _run_land(hub, tmp_path, "claude/micro-docs", "--local")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "claude-micro-docs.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    assert "claude/micro-docs" not in _local_branches(hub)
    assert "issue close" not in _log_text(logs["gh"])  # non-numeric slug


def test_bare_branch_without_local_flag_refused(hub: Path, tmp_path: Path) -> None:
    # Without --local a branch with no registered worktree must not match —
    # bare-branch landing is opt-in, not the default resolution path.
    wt = _make_spoke(hub, tmp_path, "claude/micro-docs", push=False)
    _git(hub, "worktree", "remove", str(wt))
    pre_main = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "claude/micro-docs")

    assert proc.returncode != 0
    assert _remote_sha(hub, "main") == pre_main


def test_local_still_refuses_dirty_worktree(hub: Path, tmp_path: Path) -> None:
    # --local only waives the upstream guards; uncommitted work in the spoke
    # would be silently dropped by teardown, so that guard must hold.
    wt = _make_spoke(hub, tmp_path, "feature/micro-dirty", push=False)
    (wt / "wip.txt").write_text("uncommitted\n")

    proc, _ = _run_land(hub, tmp_path, "micro-dirty", "--local")

    assert proc.returncode != 0
    assert "uncommitted" in proc.stderr
    assert wt.exists()


def test_local_push_gate_failure_rolls_back_bare_branch(hub: Path, tmp_path: Path) -> None:
    # A pre-push rejection must roll main back and keep the bare branch around so
    # the work survives for a retry — same rollback contract as worktree lands.
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "claude/micro-docs", push=False)
    _git(hub, "worktree", "remove", str(wt))
    _install_prepush_stub(hub, exit_code=1)

    proc, _ = _run_land(hub, tmp_path, "claude/micro-docs", "--local")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha
    assert _remote_sha(hub, "main") == pre_sha  # nothing was pushed
    assert "claude/micro-docs" in _local_branches(hub)


def test_local_never_pushed_guard_stays_without_flag(hub: Path, tmp_path: Path) -> None:
    # Pins that adding --local does not weaken the default path: a never-pushed
    # branch without the flag still hits the upstream guard verbatim.
    _make_spoke(hub, tmp_path, "feature/2-nopush", push=False)

    proc, _ = _run_land(hub, tmp_path, "2")

    assert proc.returncode != 0
    assert "never been pushed" in proc.stderr


def test_local_refuses_branch_with_upstream(hub: Path, tmp_path: Path) -> None:
    # --local is for micro-spokes, which never push. A branch WITH an upstream
    # is not a micro-spoke: skipping the behind guard could merge a reduced
    # local tip and later prune the remote ref, losing remote-only commits.
    _make_spoke(hub, tmp_path, "feature/9-pushed", push=True)

    proc, _ = _run_land(hub, tmp_path, "9", "--local")

    assert proc.returncode != 0
    assert "upstream" in proc.stderr.lower()
    assert not (hub / "feature-9-pushed.txt").exists()  # nothing was merged


def test_local_refuses_default_branch(hub: Path, tmp_path: Path) -> None:
    # A typo'd target equal to the default branch must not "land" as a no-op
    # self-merge that exits 0 and advises deleting main by hand.
    pre = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "main", "--local")

    assert proc.returncode != 0
    assert "default branch" in proc.stderr  # the dedicated guard, not the upstream one
    assert _remote_sha(hub, "main") == pre


def test_local_keep_branch_keeps_bare_branch(hub: Path, tmp_path: Path) -> None:
    # --keep-branch must survive bare-branch mode: the merged local branch is
    # kept for follow-up work instead of being deleted after the push.
    wt = _make_spoke(hub, tmp_path, "claude/micro-keep", push=False)
    _git(hub, "worktree", "remove", str(wt))

    proc, _ = _run_land(hub, tmp_path, "claude/micro-keep", "--local", "--keep-branch")

    assert proc.returncode == 0, proc.stderr
    assert "claude/micro-keep" in _local_branches(hub)


def test_local_refuses_bare_branch_with_upstream(hub: Path, tmp_path: Path) -> None:
    # Same guard as above, but in BARE-BRANCH mode (worktree already gone).
    # The guard is mode-independent today; this pins it so a refactor folding
    # it into the resolution arms can't silently reopen the bare-branch hole.
    wt = _make_spoke(hub, tmp_path, "feature/9-bare-pushed", push=True)
    _git(hub, "worktree", "remove", str(wt))

    proc, _ = _run_land(hub, tmp_path, "feature/9-bare-pushed", "--local")

    assert proc.returncode != 0
    assert "upstream" in proc.stderr.lower()
    assert not (hub / "feature-9-bare-pushed.txt").exists()  # nothing was merged


# --- configurable base branch (issue #117) --------------------------------------
# Landing merges into the RESOLVED base branch (git config ai-toolkit.base-branch
# > AI_TOOLKIT_BASE_BRANCH > origin/HEAD > main/master), not literal main.


def _add_develop(hub: Path) -> str:
    """Create `develop` one commit ahead of main, push it, return its tip sha.

    Leaves the hub checked out on develop (the configured integration branch).
    """
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "push", "-q", "-u", "origin", "develop")
    return _git(hub, "rev-parse", "HEAD").strip()


def test_land_refuses_hub_not_on_configured_base(hub: Path, tmp_path: Path) -> None:
    # config says develop; the hub sits on main → refuse BEFORE any merge,
    # naming the configured base (proves resolution honors the config tier).
    _add_develop(hub)
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")
    main_before = _git(hub, "rev-parse", "main").strip()
    _make_spoke(hub, tmp_path, "feature/1-work", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "develop" in proc.stderr
    assert _git(hub, "rev-parse", "main").strip() == main_before


def test_land_merges_into_configured_base(hub: Path, tmp_path: Path) -> None:
    # hub on develop (the configured base): landing merges the spoke into
    # develop and leaves main untouched.
    _add_develop(hub)
    _git(hub, "config", "ai-toolkit.base-branch", "develop")
    main_before = _git(hub, "rev-parse", "main").strip()
    _make_spoke(hub, tmp_path, "feature/1-work", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "feat: work" in _git(hub, "log", "--oneline", "develop")
    assert _git(hub, "rev-parse", "main").strip() == main_before


# --- hub-side Langfuse auth resolution (issue #127) ------------------------------
# The land script resolves Langfuse auth itself (wt_resolve_langfuse_auth: env
# wins, then ${AFK_TELEMETRY_CONF:-~/.afk-telemetry}) just before its telemetry
# section, so (a) telemetry-ingest-spoke.sh inherits working credentials from a
# fresh hub shell and builds the spoke tree + backfill, and (b) the existing
# lifecycle/script spans get AI_TOOLKIT_OTEL_SPAN_ENDPOINT and fan out to the
# collector. Resolution is best-effort: no conf + no env keeps the existing
# skip-WARN and the land still succeeds. No credential ever reaches an argv.


def _seed_otel_spoke(hub: Path, wt: Path, *, raw_bodies: bool) -> None:
    """Give a fixture worktree the durable OTel-spoke state a real spawn mints.

    .ai-toolkit/ is git-excluded exactly as worktree-new.sh does it — without the
    exclude the seeded files count as dirty and the land refuses before merge.
    """
    exclude = Path(_git(wt, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = wt / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as f:
        f.write(".ai-toolkit/\n")
    ait = wt / ".ai-toolkit"
    ait.mkdir()
    (ait / "spoke-run-id").write_text("feature/1-otel+1234\n")
    if raw_bodies:
        (ait / "raw-bodies").mkdir()


def _wait_for_content(log: Path, needle: str, tries: int = 40) -> str:
    """Poll a detached-writer log until `needle` appears (or ~4s elapse).

    The OTLP span sink runs curl backgrounded and disowned, so its stub may
    still be writing when the land process has already exited.
    """
    for _ in range(tries):
        text = _log_text(log)
        if needle in text:
            return text
        time.sleep(0.1)
    return _log_text(log)


def test_land_resolves_auth_from_conf_for_ingest(hub: Path, tmp_path: Path) -> None:
    # Fresh hub shell (no LANGFUSE_* in env) + conf present ⇒ the ingest step's
    # interpreter must inherit the resolved auth + defaulted host, so the spoke
    # tree (#87) and backfill (#92) actually build. Both steps run.
    wt = _make_spoke(hub, tmp_path, "feature/1-otel", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, logs = _run_land(
        hub,
        tmp_path,
        "1",
        stub_python312=True,
        stub_curl=True,  # auth resolves here, so the span sink fires — capture, never send
        extra_env={
            "AFK_TELEMETRY_CONF": str(conf),
            "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    ingest_log = _log_text(logs["python3.12"])
    assert "langfuse_spoke_tree.py" in ingest_log, "loaded-context itemization must run"
    assert "langfuse_backfill.py" in ingest_log, "transcript backfill must run"
    assert "LANGFUSE_BASIC_AUTH=Basic-test-127" in ingest_log, (
        "ingest must inherit the conf-resolved auth without operator hand-export"
    )
    assert "LANGFUSE_HOST=http://localhost:3000" in ingest_log, "host defaults to the local stack"


def test_land_ingest_skips_with_warn_when_no_conf_no_env(hub: Path, tmp_path: Path) -> None:
    # Conf absent + env absent ⇒ the existing auth-gate skip notice fires and the
    # land still succeeds — resolution must stay best-effort, never a guard.
    wt = _make_spoke(hub, tmp_path, "feature/1-noauth", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)

    proc, logs = _run_land(hub, tmp_path, "1", stub_python312=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "LANGFUSE_BASIC_AUTH unset — skipping post-run Langfuse ingestion" in (
        proc.stdout + proc.stderr
    )
    assert _log_text(logs["python3.12"]) == "", "no ingester may run without auth"


def test_land_script_span_posted_when_conf_present(hub: Path, tmp_path: Path) -> None:
    # With auth resolvable, the land's existing lifecycle/script span emission
    # must reach the OTLP span endpoint (defaulted to the local collector) — the
    # land-duration signal for #122/#123/#124. The credential itself must never
    # appear on the curl argv (the collector holds auth).
    wt = _make_spoke(hub, tmp_path, "feature/1-span", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=False)
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, logs = _run_land(
        hub,
        tmp_path,
        "1",
        stub_curl=True,
        extra_env={"AFK_TELEMETRY_CONF": str(conf)},
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    curl_log = _wait_for_content(logs["curl"], "worktree-land")
    assert "/v1/traces" in curl_log, "span must POST to the OTLP traces endpoint"
    assert "http://localhost:4318" in curl_log, "endpoint defaults to the local collector"
    assert "worktree-land" in curl_log, "the land script span carries its script name"
    assert "Basic-test-127" not in curl_log, "credential must never reach the curl argv/payload"


def test_land_emits_no_span_when_auth_unresolvable(hub: Path, tmp_path: Path) -> None:
    # No conf + no env ⇒ the resolver exports nothing, so the span sink stays
    # dark (no endpoint) — no blind POSTs from an unconfigured hub.
    wt = _make_spoke(hub, tmp_path, "feature/1-dark", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=False)

    proc, logs = _run_land(hub, tmp_path, "1", stub_curl=True)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert _log_text(logs["curl"]) == "", "no span POST may fire without resolved auth"


# --- conditional post-land background sweep (issue #124) --------------------------
# After a successful land + teardown, the land script calls gate-sweep.sh --spawn
# with the merged commit: a PRUNED green-tree stamp (testmon/selected, issue #122)
# on the landed tree launches exactly one detached full-suite sweep; a `full`
# stamp or no stamp launches none. Best-effort like the rest of the land tail:
# the land's exit code and duration are unaffected in all cases, and a sweep
# that fails to launch warns without failing the land.


def _mint_stamp(hub: Path, ref: str, tier: str) -> None:
    """Write a #122 green-tree stamp for `ref`'s tree into the hub's stamp store."""
    tree = _git(hub, "rev-parse", f"{ref}^{{tree}}").strip()
    stamps = hub / ".git" / ".gate-stamps"
    stamps.mkdir(parents=True, exist_ok=True)
    (stamps / tree).write_text(f"tier={tier}\nenv=test\n")


def _wait_for_file(path: Path, timeout: float = 10.0) -> bool:
    """Poll for a file the detached sweep worker writes; True when it appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def test_land_launches_one_sweep_for_pruned_gated_tree(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-sweep", push=True)
    _mint_stamp(hub, "feature/1-sweep", "testmon")
    runner_log = tmp_path / "sweep-runner.log"

    proc, _ = _run_land(
        hub, tmp_path, "1", extra_env={"GATE_SWEEP_CMD": f'echo RUN >> "{runner_log}"'}
    )

    assert proc.returncode == 0, proc.stderr
    assert "launching background full-suite sweep" in proc.stdout
    assert _wait_for_file(runner_log), "the detached sweep worker never ran"
    time.sleep(0.5)  # grace: a wrongly-spawned second worker would land by now
    assert runner_log.read_text().count("RUN") == 1  # exactly one sweep


def test_land_launches_no_sweep_for_full_gated_tree(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-fullswp", push=True)
    _mint_stamp(hub, "feature/1-fullswp", "full")
    runner_log = tmp_path / "sweep-runner.log"

    proc, _ = _run_land(
        hub, tmp_path, "1", extra_env={"GATE_SWEEP_CMD": f'echo RUN >> "{runner_log}"'}
    )

    assert proc.returncode == 0, proc.stderr
    assert "no sweep needed" in proc.stdout  # a full pass needs no safety net
    time.sleep(0.8)  # grace: a wrongly-spawned worker would have written by now
    assert not runner_log.exists()


def test_land_launches_no_sweep_without_stamp(hub: Path, tmp_path: Path) -> None:
    # No stamp: docs-only skip or --skip-tests — the gate certified nothing
    # pruned, so there is no selection miss to backstop.
    _make_spoke(hub, tmp_path, "feature/1-nostamp", push=True)
    runner_log = tmp_path / "sweep-runner.log"

    proc, _ = _run_land(
        hub, tmp_path, "1", extra_env={"GATE_SWEEP_CMD": f'echo RUN >> "{runner_log}"'}
    )

    assert proc.returncode == 0, proc.stderr
    assert "no gate stamp" in proc.stdout
    time.sleep(0.8)
    assert not runner_log.exists()


def test_land_returns_while_slow_sweep_still_runs(hub: Path, tmp_path: Path) -> None:
    # The land's duration is unaffected: it returns while the (slow) suite is
    # still running in the detached worker.
    _make_spoke(hub, tmp_path, "feature/1-slowswp", push=True)
    _mint_stamp(hub, "feature/1-slowswp", "testmon")
    start_log = tmp_path / "sweep-start.log"

    t0 = time.monotonic()
    proc, _ = _run_land(
        hub,
        tmp_path,
        "1",
        extra_env={"GATE_SWEEP_CMD": f'echo START >> "{start_log}"; sleep 30'},
    )
    elapsed = time.monotonic() - t0

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 20, f"land blocked on the sweep ({elapsed:.1f}s)"
    assert _wait_for_file(start_log)  # the worker is alive past the land's return


def test_land_warns_but_succeeds_when_sweep_launch_fails(hub: Path, tmp_path: Path) -> None:
    # Best-effort like the rest of the land tail: an unlaunchable sweep script
    # (e.g. a synced repo missing it) warns and never fails the land.
    _make_spoke(hub, tmp_path, "feature/1-noswp", push=True)
    _mint_stamp(hub, "feature/1-noswp", "testmon")

    proc, _ = _run_land(
        hub, tmp_path, "1", extra_env={"GATE_SWEEP_BIN": str(tmp_path / "no-such-sweep.sh")}
    )

    assert proc.returncode == 0, proc.stderr
    assert "post-land sweep failed to launch" in proc.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()  # still landed
