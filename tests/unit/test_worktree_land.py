"""Unit tests for scripts/worktree-land.sh — the hub-side landing sequence.

Landing is hub-owned: verify the spoke pushed → merge into the default branch →
push main (the pre-push hook is the single test gate, issue #19) → tear down the
worktree through Orca (worker-release, then worktree-done.sh) → close the issue.
Every guard must abort with a precise reason BEFORE the merge; landing no longer
runs the suite itself, and a pre-push rejection (the gate failing) must roll main
back.

Hermetic like test_worktree_done.py: git runs against a local bare `origin`, and
`gh` and `pytest` are logging stubs and `orca` is the PATH stub (conftest) — no network, no
real issue closes, no real Orca, and no land-side pytest (a stub proves
landing never invokes it). A stub hub pre-push hook stands in for the real gate
when a test needs to assert env threading or rollback.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _ci_gh_support import CI_URL, FAILED_RUNS, GREEN_RUNS, PENDING_RUNS
from _orca_stub import orca_calls, orca_scenario

_REPO_ROOT = Path(__file__).resolve().parents[2]
WORKTREE_LAND = _REPO_ROOT / "scripts" / "worktree-land.sh"
WORKTREE_LIB = _REPO_ROOT / "scripts" / "worktree-lib.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)
# A host GIT_SSH_COMMAND must not prefix the keepalive assertion (#119).
_GIT_ENV.pop("GIT_SSH_COMMAND", None)


# The dedicated exit code (#354) for an INVARIANT upstream precondition the hub cannot clear
# (branch never pushed / ahead / behind / --local-with-upstream). Distinct from 1 (generic
# die), 3 (teardown-incomplete), and 4 (merge conflict) so auto_land can branch on the kind.
_WT_LAND_PRECONDITION_EXIT = 5


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    # Pin the bare remote's HEAD to `main` explicitly (-b main). Without it the
    # bare inherits the runner's compiled init.defaultBranch — `main` on Apple
    # git but `master` on upstream/Ubuntu — leaving HEAD dangling at a nonexistent
    # `master`. A later `git clone` of this bare (the sibling-land helpers) then
    # cannot check out `main`, lands on an unborn `master`, and its `push origin
    # main` fails "src refspec main does not match any" — green locally, red on CI
    # (issue #317).
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
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
    # A real hub has the pre-push test gate installed (issue #19); a required-gate
    # land with no executable hook now ABORTS (issue #196), so model the installed
    # state by default. Tests that exercise the missing-hook or gate-failure paths
    # overwrite this stub (or chmod it -x) via _install_prepush_stub.
    _install_prepush_stub(hub, exit_code=0)
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
    spoke_marker: str | None = None,
    extra_env: dict[str, str] | None = None,
    stub_python312: bool = False,
    stub_curl: bool = False,
    issue_state: str = "OPEN",
    ci_runs: str = GREEN_RUNS,
    ci_green_only_for: str | None = None,
    no_gh: bool = False,
    pytest_exit: int = 0,
    pytest_side_effect: str = "",
) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    """Run worktree-land.sh from the hub with logging stubs on PATH.

    Stubs `gh` (one log line per invocation), plus a
    `pytest` stub logging every call (exiting `pytest_exit`, default 0) — landing
    runs pytest itself ONLY for the diverged --skip-tests merge-sanity check
    (issue #174); otherwise the suite runs once via the pre-push hook on the main
    push (issue #19). `pytest_exit` non-zero models a failing merge-sanity run.
    `pytest_side_effect` is a shell snippet the pytest stub runs before exiting —
    used to model a concurrent sibling ref move (or an escape) DURING the
    merge-sanity tripwire window (issue #205).
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
    logs = {name: tmp_path / f"{name}-calls.log" for name in ("gh", "pytest", "python3.12", "curl")}
    # `gh` logs every call AND answers `issue view --json state` with `issue_state`,
    # so the resume finalize's OPEN-check (issue #151) can be steered per test. `run list`
    # (the CI-gate query, #378) answers `ci_runs` and is unaffected by `gh_exit`, which
    # models a failing issue-close / label call.
    gh_stub = bindir / "gh"
    # `ci_green_only_for=<sha>`: `run list --commit <sha>` is green, any other SHA is pending
    # (models CI that has not caught up with a freshly pushed merge commit).
    only_for = (
        f"case \"$*\" in *\"--commit {ci_green_only_for} \"*) ;; "
        f"*\"run list\"*) printf '%s' '{PENDING_RUNS}'; exit 0 ;; esac\n"
        if ci_green_only_for
        else ""
    )
    gh_stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{logs["gh"]}"\n'
        f"{only_for}"
        f"case \"$*\" in *\"run list\"*) printf '%s' '{ci_runs}'; exit 0 ;; esac\n"
        f'case "$*" in *"issue view"*state*) printf "%s\\n" "{issue_state}" ;; esac\n'
        f"exit {gh_exit}\n"
    )
    gh_stub.chmod(0o755)
    if no_gh:
        gh_stub.unlink()
    pytest_stub = bindir / "pytest"
    pytest_stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs["pytest"]}"\n'
        f"{pytest_side_effect}\n"
        f"exit {pytest_exit}\n"
    )
    pytest_stub.chmod(0o755)
    if stub_python312:
        py_stub = bindir / "python3.12"
        py_stub.write_text(
            "#!/bin/sh\n"
            f'printf "CALL %s\\n" "$*" >> "{logs["python3.12"]}"\n'
            f'env | grep -E "^LANGFUSE_" >> "{logs["python3.12"]}" || true\n'
            # With ORCA_CALLS_LOG set, the ingest also leaves its place in the orca call order.
            '[ -z "$ORCA_CALLS_LOG" ] || printf \'["INGEST"]\\n\' >> "$ORCA_CALLS_LOG"\n'
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
    # CI polls fast and gives up fast: a stubbed `gh` never changes its answer mid-test.
    env.update({"WT_CI_POLL": "1", "WT_CI_NONE_GRACE": "0", "LAND_CI_WAIT_MAX": "2"})
    # The host's own spoke marker must never steer the guard; set it explicitly
    # only when a test means to model a spoke session (issue #26).
    env.pop("WT_SPOKE", None)
    # The host must not steer the issue #236 lifecycle-label mirror on/off.
    env.pop("AI_TOOLKIT_GH_LIFECYCLE_LABELS", None)
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
    # The real hook announces a custom/local gate on stderr; the land trusts that line.
    body += (
        '[ -z "${TEST_SELECT_CMD:-}" ] || '
        'echo "test-select: running custom suite (TEST_SELECT_CMD)" >&2\n'
    )
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


def _fetch_fail_git_shim(tmp_path: Path) -> None:
    """PATH-front `git` shim: every `git fetch` dies (the network-down/stale-SSH
    shape, issue #195); everything else delegates to the real git. Written into
    the same bindir _run_land prepends to PATH."""
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = fetch ]; then echo "fatal: unable to access origin (stubbed)" >&2; exit 128; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)


# --- happy path ----------------------------------------------------------------


def test_lands_pushed_branch_into_main(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-done.txt").exists()
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_land_returns_cleanup_sentinel_when_teardown_fails_after_push(
    hub: Path, tmp_path: Path
) -> None:
    # #202 I / #198: a land that pushed main (main ADVANCED) but then failed a teardown step
    # must exit with a DISTINCT sentinel (3), not the generic 1 — so hub-afk's auto_land can
    # tell "nothing shipped" (escalate blocked) from "shipped, cleanup incomplete" (never
    # stamp blocked over merged code). WT_DONE seams the worktree-done teardown to a failing stub.
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)
    fail_done = tmp_path / "fail-done.sh"
    fail_done.write_text("#!/bin/sh\necho 'teardown boom' >&2\nexit 1\n")
    fail_done.chmod(0o755)

    proc, _ = _run_land(hub, tmp_path, "1", extra_env={"WT_DONE": str(fail_done)})

    assert proc.returncode == 3, (
        "a teardown failure AFTER main advanced must exit the cleanup sentinel (3), not 1: "
        + proc.stdout
        + proc.stderr
    )
    # main really advanced despite the teardown failure — the work IS shipped.
    assert (hub / "feature-1-done.txt").exists(), "the merge landed on main before teardown failed"
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

    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT, proc.stderr
    assert "push" in proc.stderr.lower()
    assert not (hub / "feature-1-local.txt").exists()


def test_refuses_spoke_ahead_of_upstream(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ahead", push=True)
    (wt / "extra.txt").write_text("not pushed\n")
    _git(wt, "add", "extra.txt")
    _git(wt, "commit", "-qm", "feat: extra", "-m", "Refs #1")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT, proc.stderr
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

    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT, proc.stderr
    assert "behind" in proc.stderr.lower()
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha
    assert _remote_sha(hub, "feature/1-behind") != ""  # remote ref untouched


def test_fetch_failure_aborts_land_before_merge(hub: Path, tmp_path: Path) -> None:
    # Issue #195: on a failed fetch the ahead/behind guards run against the
    # LAST-KNOWN origin/<branch> — a spoke push the hub never fetched reads
    # behind=0, the land proceeds, and teardown later deletes the remote ref
    # with its commits. Stale remote state must never feed that destructive
    # chain: the land aborts before any merge instead of warning and going on.
    wt = _make_spoke(hub, tmp_path, "feature/1-fetchdead", push=True, ready=True)
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    _fetch_fail_git_shim(tmp_path)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "fetch" in proc.stderr.lower()
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # nothing merged
    assert wt.exists()  # no teardown
    assert _remote_sha(hub, "feature/1-fetchdead") != ""  # remote ref untouched


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


def test_merge_conflict_exits_distinct_code_and_names_files(hub: Path, tmp_path: Path) -> None:
    # #285: a deterministic merge conflict is NOT the same failure as a push rejection —
    # worktree-land signals it with a dedicated exit code (4) and a machine-readable
    # CONFLICT marker naming the conflicting file(s), so auto_land can branch on the
    # failure kind and route to a resolution lane instead of blind-retrying the identical
    # land. 1 (generic die) and 3 (cleanup-incomplete) are already taken.
    wt = _make_spoke(hub, tmp_path, "feature/1-conflict", push=False)
    (wt / "README.md").write_text("spoke version\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "feat: spoke readme", "-m", "Refs #1")
    _git(wt, "push", "-q", "-u", "origin", "feature/1-conflict")
    _git(wt, "tag", "ready/1")
    _git(wt, "push", "-q", "origin", "ready/1")
    (hub / "README.md").write_text("hub version\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: hub readme", "-m", "Refs #0")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 4, proc.stderr
    assert "CONFLICT" in proc.stderr
    assert "README.md" in proc.stderr


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
    # CI is the gate (#378): landing never runs the suite itself — not even for a diverged
    # land, where the hub's merge used to build an untested tree.
    _make_spoke(hub, tmp_path, "feature/1-nopytest", push=True)
    _diverge_hub(hub)

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _log_text(logs["pytest"]) == ""  # land never invoked pytest itself
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


# --- CI is the gate (issue #378) ------------------------------------------------------
# The full suite runs in CI on every branch push. A pushed spoke lands only on a green CI
# run for the exact SHA it ships: a fast-forward of the CI-green ready SHA lands directly
# (the main push skips the local hook), and when main moved the land merges main INTO the
# spoke on the spoke, pushes it, waits for CI on the new SHA, then fast-forwards main.
# `gh` is stubbed with realistic `gh run list --json` output (see _ci_gh_support).


def _diverge_hub(hub: Path) -> None:
    """Advance the hub's main (locally) so the spoke no longer fast-forwards."""
    (hub / "hub-only.txt").write_text("hub moved on\n")
    _git(hub, "add", "hub-only.txt")
    _git(hub, "commit", "-qm", "chore: hub work", "-m", "Refs #0")


def test_ff_of_a_green_sha_lands_without_the_hook_or_pytest(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-ffskip", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _git(hub, "rev-parse", "HEAD").strip() == tip  # a pure fast-forward
    assert _remote_sha(hub, "main") == tip
    assert "TEST_SELECT_SKIP=1" in _log_text(env_log)  # CI proved this SHA: hook skipped
    assert _log_text(logs["pytest"]) == ""
    assert "CI green" in proc.stdout  # the land log names what gated it
    assert f"--commit {tip}" in _log_text(logs["gh"])  # asked CI about the exact SHA


def test_land_refuses_while_ci_is_pending(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-pending", push=True)
    before = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "1", ci_runs=PENDING_RUNS)

    assert proc.returncode == 1  # transient: the drain retries
    assert f"CI pending ({CI_URL})" in proc.stderr
    assert _remote_sha(hub, "main") == before and wt.exists()


def test_land_refuses_a_red_ci_with_the_precondition_exit(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-red", push=True)
    before = _remote_sha(hub, "main")

    proc, _ = _run_land(hub, tmp_path, "1", ci_runs=FAILED_RUNS)

    # Only the spoke can fix a red run, so auto_land must escalate it, not retry (#354).
    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT
    assert f"CI failed ({CI_URL}" in proc.stderr
    assert _remote_sha(hub, "main") == before and wt.exists()


def test_land_refuses_when_no_ci_run_exists_for_the_sha(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-norun", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", ci_runs="[]")

    assert proc.returncode == 1
    assert "no CI run for this SHA (was it pushed?)" in proc.stderr


def test_land_refuses_without_gh_and_points_at_local_gate(
    hub: Path, tmp_path: Path, orca_bin: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/1-nogh", push=True)
    # `gh` is installed on CI runners and dev hosts alike: hide it explicitly.
    env_path = {"PATH": f"{tmp_path / 'bin'}:{orca_bin}:{_path_without_gh(tmp_path)}"}

    proc, _ = _run_land(hub, tmp_path, "1", no_gh=True, extra_env=env_path)

    assert proc.returncode == 1
    assert "--local-gate" in proc.stderr


def _path_without_gh(tmp_path: Path) -> Path:
    """A dir of symlinks to every system tool in /usr/bin and /bin except `gh`.

    `gh` is preinstalled on CI runners (and on dev hosts), so "gh absent" cannot be modelled by
    trimming PATH to the system dirs — it must be a PATH that has everything else but not gh.
    """
    farm = tmp_path / "nogh-path"
    farm.mkdir(exist_ok=True)
    for d in ("/usr/bin", "/bin"):
        if not Path(d).is_dir():
            continue
        for entry in os.scandir(d):
            link = farm / entry.name
            if entry.name != "gh" and not link.exists():
                with contextlib.suppress(OSError):
                    link.symlink_to(entry.path)
    return farm


def test_skip_tests_does_not_bypass_the_ci_gate(hub: Path, tmp_path: Path) -> None:
    # The drain lands with --skip-tests; CI must still be green (it is the gate now).
    _make_spoke(hub, tmp_path, "feature/1-skipci", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", "--skip-tests", ci_runs=PENDING_RUNS)

    assert proc.returncode == 1
    assert "CI pending" in proc.stderr


def test_force_land_without_marker_still_requires_ci(hub: Path, tmp_path: Path) -> None:
    # --force-land waives the ready marker, not the gate: the SHA must still be CI-green.
    _make_spoke(hub, tmp_path, "feature/1-forcedci", push=True, ready=False)

    red, _ = _run_land(hub, tmp_path, "1", "--force-land", ci_runs=FAILED_RUNS)
    green, logs = _run_land(hub, tmp_path, "1", "--force-land")

    assert red.returncode == _WT_LAND_PRECONDITION_EXIT
    assert green.returncode == 0, green.stderr
    assert _log_text(logs["pytest"]) == ""


def test_non_ff_land_merges_main_into_the_spoke_waits_for_ci_then_fast_forwards(
    hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-diverged", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _diverge_hub(hub)
    hub_tip = _git(hub, "rev-parse", "HEAD").strip()
    ref_log = tmp_path / "pre-receive-refs.log"
    _install_pre_receive(tmp_path, ref_log)

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    final = _git(hub, "rev-parse", "HEAD").strip()
    assert _remote_sha(hub, "main") == final
    # The merge commit was built ON THE SPOKE (parents: the spoke tip + the hub's main) and
    # main fast-forwarded to it — the hub never builds an untested combined tree.
    assert _git(hub, "rev-list", "--parents", "-n1", final).split()[1:] == [tip, hub_tip]
    # The spoke branch reached origin BEFORE main did.
    pushed = [r for r in _log_text(ref_log).splitlines() if r.startswith("refs/heads/")]
    assert pushed.index("refs/heads/feature/1-diverged") < pushed.index("refs/heads/main")
    # CI was consulted for the ready SHA and again for the new merged SHA.
    gh = _log_text(logs["gh"])
    assert f"--commit {tip}" in gh and f"--commit {final}" in gh
    assert _log_text(logs["pytest"]) == ""  # still no local test run


def test_non_ff_land_waits_for_green_on_the_new_sha_before_touching_main(
    hub: Path, tmp_path: Path
) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-newsha", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _diverge_hub(hub)
    before = _remote_sha(hub, "main")

    # CI is green for the ORIGINAL ready SHA only; the merged SHA never goes green.
    proc, _ = _run_land(hub, tmp_path, "1", ci_green_only_for=tip)

    assert proc.returncode == 1
    assert "CI pending" in proc.stderr
    assert _remote_sha(hub, "main") == before  # main is only fast-forwarded to a green SHA
    assert wt.exists()
    assert _remote_sha(hub, "feature/1-newsha") != tip  # the merge was pushed for CI to see


def test_non_ff_spoke_side_conflict_exits_the_conflict_code(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-clash", push=True, ready=True)
    (wt / "shared.txt").write_text("spoke side\n")
    _git(wt, "add", "shared.txt")
    _git(wt, "commit", "-qm", "feat: shared", "-m", "Refs #1")
    _git(wt, "push", "-q", "origin", "feature/1-clash")
    _git(wt, "tag", "-f", "ready/1")
    _git(wt, "push", "-qf", "origin", "ready/1")
    (hub / "shared.txt").write_text("hub side\n")
    _git(hub, "add", "shared.txt")
    _git(hub, "commit", "-qm", "chore: shared", "-m", "Refs #0")
    hub_tip = _git(hub, "rev-parse", "HEAD").strip()

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 4  # WT_LAND_CONFLICT_EXIT: the resolution lane, not a retry
    assert "CONFLICT shared.txt" in proc.stderr
    assert _git(hub, "rev-parse", "HEAD").strip() == hub_tip
    assert _git(wt, "status", "--porcelain").strip() == ""  # the spoke merge was aborted


# --- --local-gate: the offline escape hatch (issue #378) ------------------------------


def test_local_gate_runs_the_full_suite_through_the_hook_and_never_asks_ci(
    hub: Path, tmp_path: Path
) -> None:
    _make_spoke(hub, tmp_path, "feature/1-localgate", push=True, ready=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    # CI is red: the escape hatch must not consult it.
    proc, logs = _run_land(hub, tmp_path, "1", "--local-gate", ci_runs=FAILED_RUNS)

    assert proc.returncode == 0, proc.stderr
    cmd = _log_text(env_log)
    assert '-n auto -m "not serial"' in cmd and "-m serial" in cmd  # the former full suite
    assert "TEST_SELECT_SKIP" not in cmd  # the hook (and so the suite) really runs
    assert "run list" not in _log_text(logs["gh"])
    assert "--local-gate" in proc.stdout  # recorded in the land log ...
    assert "--local-gate" in _log_text(logs["gh"])  # ... and in the issue-close comment


def test_local_gate_works_with_gh_absent(hub: Path, tmp_path: Path, orca_bin: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-offline", push=True, ready=True)
    env_path = {"PATH": f"{tmp_path / 'bin'}:{orca_bin}:{_path_without_gh(tmp_path)}"}

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate", no_gh=True, extra_env=env_path)

    assert proc.returncode == 0, proc.stderr
    assert (hub / "feature-1-offline.txt").exists()
    assert "gh not found" in proc.stderr  # the close is left to the operator, loudly


def test_local_gate_failure_rolls_the_merge_back(hub: Path, tmp_path: Path) -> None:
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-lgred", push=True, ready=True)
    _install_prepush_stub(hub, exit_code=1)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back
    assert _remote_sha(hub, "main") == pre_sha
    assert wt.exists()


def test_local_gate_with_a_diverged_main_merges_on_the_hub_and_gates_locally(
    hub: Path, tmp_path: Path
) -> None:
    # Offline there is no CI SHA to fast-forward to: the former behavior (hub merge commit,
    # then the local gate on the merged tree) is the escape hatch.
    _make_spoke(hub, tmp_path, "feature/1-lgdiv", push=True, ready=True)
    _diverge_hub(hub)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate")

    assert proc.returncode == 0, proc.stderr
    assert len(_git(hub, "rev-list", "--parents", "-n1", "HEAD").split()) == 3  # hub merge commit
    assert "TEST_SELECT_CMD" in _log_text(env_log)


def test_local_gate_conflicts_with_test_cmd(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-both", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate", "--test-cmd", "x")

    assert proc.returncode != 0
    assert "conflict" in proc.stderr


# --- a required local gate with no executable pre-push hook aborts the land (issue #196) -
# --local-gate/--test-cmd/--local lands are gated by the pre-push hook, the single executor
# of local tests. If it is missing the push would run NOTHING — the #187 fail-open shape.
# Landing must ABORT with the install command, never warn-and-push untested code to main.


def test_missing_prepush_hook_aborts_a_local_gate_land(hub: Path, tmp_path: Path) -> None:
    # The hook exists but is not executable (a fresh checkout, a botched install, a
    # chmod -x): the land must roll the merge back and abort before pushing main.
    pre_main = _remote_sha(hub, "main")
    wt = _make_spoke(hub, tmp_path, "feature/1-nohook", push=True)
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    (hub / ".git" / "hooks" / "pre-push").chmod(0o644)  # fixture hook made non-executable

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # merge rolled back
    assert _remote_sha(hub, "main") == pre_main  # nothing was pushed
    assert "install-git-hooks.sh" in proc.stderr  # the fix is in the error
    assert wt.exists()  # teardown never ran


def test_ci_green_land_needs_no_prepush_hook(hub: Path, tmp_path: Path) -> None:
    # CI already proved the SHA, so the hook is irrelevant to a CI-gated land.
    wt = _make_spoke(hub, tmp_path, "feature/1-ciok", push=True)
    (hub / ".git" / "hooks" / "pre-push").chmod(0o644)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    assert not wt.exists()


# --- ship push: keepalive + rollback (issues #119, #19) --------------------------------


def _install_pre_receive(
    tmp_path: Path, log: Path, *, decline: str = "", stderr_line: str = ""
) -> None:
    """A pre-receive hook on the bare origin logging each pushed ref.

    With `decline`, it also rejects every push (printing `stderr_line`) — a server-side
    policy refusal, which a re-fetch can never fix.
    """
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    body = f'#!/bin/sh\nwhile read -r _o _n ref; do echo "$ref" >> "{log}"; done\n'
    if decline:
        body += f'echo "{stderr_line}" >&2\nexit 1\n'
    body += "exit 0\n"
    hook.write_text(body)
    hook.chmod(0o755)


def test_remote_policy_rejection_rolls_back_without_retry(hub: Path, tmp_path: Path) -> None:
    # CI green but the remote declines the main push for a POLICY reason (no non-ff
    # signature) → a retry would just fail again: roll back, never loop.
    _make_spoke(hub, tmp_path, "feature/1-policy", push=True, ready=True)
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
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
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back
    assert _log_text(ref_log).count("refs/heads/main") == 1  # exactly one attempt


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


def test_push_gate_failure_rolls_back(hub: Path, tmp_path: Path) -> None:
    # A pre-push rejection (the local gate failing) must roll the merged hub back
    # and ship nothing — the clean-hub invariant the old land-side gate held.
    pre_sha = _git(hub, "rev-parse", "HEAD").strip()
    wt = _make_spoke(hub, tmp_path, "feature/1-broken", push=True)
    _install_prepush_stub(hub, exit_code=1)

    proc, _ = _run_land(hub, tmp_path, "1", "--test-cmd", "my-suite")

    assert proc.returncode != 0
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_sha  # rolled back
    assert _remote_sha(hub, "main") == pre_sha  # nothing was pushed
    assert wt.exists()  # teardown never ran
    assert "feature/1-broken" in _local_branches(hub)


def test_skip_tests_threads_skip_env(hub: Path, tmp_path: Path) -> None:
    # --skip-tests skips the hook's fast tier on the main push by threading
    # TEST_SELECT_SKIP, not by a land-side run — the hook stays the single executor.
    _make_spoke(hub, tmp_path, "feature/1-untested", push=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, logs = _run_land(hub, tmp_path, "1", "--skip-tests")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_SKIP=1" in _log_text(env_log)
    assert _log_text(logs["pytest"]) == ""  # still no land-side pytest


def test_test_cmd_threads_cmd_env(hub: Path, tmp_path: Path) -> None:
    # --test-cmd gates the land with a custom command, threaded as TEST_SELECT_CMD so the
    # hook runs it on the merged tree instead of CI being consulted.
    _make_spoke(hub, tmp_path, "feature/1-custom", push=True)
    env_log = tmp_path / "prepush-env.log"
    _install_prepush_stub(hub, exit_code=0, env_log=env_log)

    proc, logs = _run_land(hub, tmp_path, "1", "--test-cmd", "my-suite --fast")

    assert proc.returncode == 0, proc.stderr
    assert "TEST_SELECT_CMD=my-suite --fast" in _log_text(env_log)
    assert "run list" not in _log_text(logs["gh"])


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

    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT, proc.stderr
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


def test_local_refuses_bare_branch_with_upstream(hub: Path, tmp_path: Path) -> None:
    # Same guard as above, but in BARE-BRANCH mode (worktree already gone).
    # The guard is mode-independent today; this pins it so a refactor folding
    # it into the resolution arms can't silently reopen the bare-branch hole.
    wt = _make_spoke(hub, tmp_path, "feature/9-bare-pushed", push=True)
    _git(hub, "worktree", "remove", str(wt))

    proc, _ = _run_land(hub, tmp_path, "feature/9-bare-pushed", "--local")

    assert proc.returncode == _WT_LAND_PRECONDITION_EXIT, proc.stderr
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
# fresh hub shell and builds the spoke tree (#140 retired the transcript
# backfill), and (b) the existing
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
    # tree (#87) actually builds — the only telemetry step since #140 retired
    # the transcript backfill.
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
    assert "langfuse_backfill" not in ingest_log, "retired transcript backfill must NOT run (#140)"
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


def _noop_wt_done(tmp_path: Path) -> Path:
    """A no-op worktree-done stub so a land keeps the worktree for post-land inspection."""
    stub = tmp_path / "bin" / "wt-done-noop.sh"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return stub


def test_land_stamps_outcome_landed_pointer(hub: Path, tmp_path: Path) -> None:
    # #231: the land records outcome=landed into the worktree's .ai-toolkit/outcome pointer
    # BEFORE the view build reads it, so the assembled trace carries an outcome:landed tag.
    wt = _make_spoke(hub, tmp_path, "feature/1-outcome", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, _logs = _run_land(
        hub,
        tmp_path,
        "1",
        stub_python312=True,
        stub_curl=True,
        extra_env={
            "AFK_TELEMETRY_CONF": str(conf),
            "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
            "WT_DONE": str(_noop_wt_done(tmp_path)),  # keep the worktree so the pointer survives
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert (wt / ".ai-toolkit" / "outcome").read_text().strip() == "landed"


def test_land_after_prior_block_outcome_passes_rebuild(hub: Path, tmp_path: Path) -> None:
    # #231: a spoke the supervisor already stamped outcome=blocked (a block-time view was posted)
    # must land with --rebuild so the final landed view refreshes that partial snapshot, and the
    # pointer ends at landed.
    wt = _make_spoke(hub, tmp_path, "feature/1-reblock", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)
    (wt / ".ai-toolkit" / "outcome").write_text("blocked\n")
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, logs = _run_land(
        hub,
        tmp_path,
        "1",
        stub_python312=True,
        stub_curl=True,
        extra_env={
            "AFK_TELEMETRY_CONF": str(conf),
            "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
            "WT_DONE": str(_noop_wt_done(tmp_path)),
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "--rebuild" in _log_text(logs["python3.12"]), "a prior block-time view must be rebuilt"
    assert (wt / ".ai-toolkit" / "outcome").read_text().strip() == "landed"


def test_land_threads_pre_merge_tip_to_ingest(hub: Path, tmp_path: Path) -> None:
    # Regression for #344: the land runs the ingest AFTER the merge pushes, so origin/main
    # already contains the spoke's commit (worktrees share remote-tracking refs) and the
    # ingest's origin/main..HEAD range is EMPTY — every commit-derived enrichment is lost.
    # The land must thread the PRE-MERGE default tip so the range predates the merge. Prove it
    # end-to-end: the view builder gets a non-empty --commits dump carrying the spoke commit.
    wt = _make_spoke(hub, tmp_path, "feature/1-commitbase", push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, logs = _run_land(
        hub,
        tmp_path,
        "1",
        stub_python312=True,
        stub_curl=True,
        extra_env={
            "AFK_TELEMETRY_CONF": str(conf),
            "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
            "WT_DONE": str(_noop_wt_done(tmp_path)),  # keep the worktree so commits.dump survives
        },
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    ingest_log = _log_text(logs["python3.12"])
    assert "--commits" in ingest_log, (
        "the pre-merge tip must be threaded so origin/main..HEAD's post-push emptiness "
        "does not drop the commits dump"
    )
    dump = (wt / ".ai-toolkit" / "commits.dump").read_text()
    assert "feat: work" in dump, "the spoke's own commit must be captured in the dump"


def test_land_without_ai_toolkit_dir_writes_no_outcome(hub: Path, tmp_path: Path) -> None:
    # #231: a non-OTel worktree (no .ai-toolkit) must NOT gain an outcome pointer — writing there
    # would only dirty a tree the teardown then refuses to remove. The land still succeeds.
    wt = _make_spoke(hub, tmp_path, "feature/1-plain", push=True)

    proc, _logs = _run_land(hub, tmp_path, "1", extra_env={"WT_DONE": str(_noop_wt_done(tmp_path))})

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert not (wt / ".ai-toolkit").exists()


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


# --- resilient / idempotent re-land (issue #151) --------------------------------
# A land can be killed by a caller timeout AFTER the push succeeded but mid-teardown.
# Re-invoking the land on that partially-landed spoke must COMPLETE it, not abort.
# Two partial states: (a) the worktree is still stranded — the merge is a no-op and
# teardown finishes; (b) the worktree was already removed but the branch/tag/issue
# survive — the land finalizes from the ready/<issue> marker that proves it shipped.
# The finalize is gated on positive proof (a ready marker merged into the base), so it
# can never close an unshipped issue.


def _complete_ship(hub: Path, branch: str) -> None:
    """FF-merge `branch` into main and push — the ship half of a land that then died."""
    _git(hub, "merge", "--ff-only", branch)
    _git(hub, "push", "-q", "origin", "main")


def test_reland_stranded_worktree_completes_idempotently(hub: Path, tmp_path: Path) -> None:
    # State (a): the ship succeeded but the worktree was left stranded. The worktree
    # still resolves, so this exercises the NORMAL land path's already-merged
    # idempotency (a no-op re-merge), NOT land_resume_finalize — re-running finishes
    # the teardown: no second commit, worktree gone, issue closed.
    wt = _make_spoke(hub, tmp_path, "feature/1-stranded-land", push=True, ready=True)
    _complete_ship(hub, "feature/1-stranded-land")
    pre_main = _git(hub, "rev-parse", "HEAD").strip()

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_main, "no second merge commit"
    assert not wt.exists(), "the stranded worktree is torn down on re-land"
    assert "issue close 1" in _log_text(logs["gh"])


def test_reland_after_worktree_removed_finalizes_from_marker(hub: Path, tmp_path: Path) -> None:
    # State (b): teardown removed the worktree but died before pruning the branch,
    # deleting the ready/1 tag, or closing the issue. Re-running must finalize from the
    # surviving merged marker rather than aborting with "no worktree matches".
    wt = _make_spoke(hub, tmp_path, "feature/1-wtgone", push=True, ready=True)
    _complete_ship(hub, "feature/1-wtgone")
    _git(hub, "worktree", "remove", str(wt))
    pre_main = _git(hub, "rev-parse", "HEAD").strip()

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert _git(hub, "rev-parse", "HEAD").strip() == pre_main, "finalize must not re-merge"
    assert "issue close 1" in _log_text(logs["gh"]), "the open issue is finally closed"
    assert "feature/1-wtgone" not in _local_branches(hub), "the merged branch is pruned"
    assert "ready/1" not in _local_tags(hub), "the completion marker is consumed"


def test_reland_refuses_issue_without_ready_marker(hub: Path, tmp_path: Path) -> None:
    # Safety: no resume signal at all (no worktree, no ready tag) ⇒ still abort. The
    # finalize must never close an issue it cannot prove shipped.
    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "issue close" not in _log_text(logs["gh"])


def test_reland_refuses_unmerged_ready_marker(hub: Path, tmp_path: Path) -> None:
    # Safety: a ready/1 marker exists but its commit was NEVER merged into main (the
    # spoke tagged + pushed but never landed, and its worktree is gone). Nothing
    # shipped, so the finalize must NOT fire — the land aborts and closes no issue.
    wt = _make_spoke(hub, tmp_path, "feature/1-unmerged", push=True, ready=True)
    _git(hub, "worktree", "remove", str(wt))  # worktree gone, but NOT merged into main

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert "issue close" not in _log_text(logs["gh"])


def test_reland_does_not_reclose_done_issue_with_lingering_marker(
    hub: Path, tmp_path: Path
) -> None:
    # A merged ready/1 tag can linger after an otherwise-complete land. Re-running must
    # still clean up the stale marker but must NOT re-close / re-comment the already-
    # closed issue — the finalize keys the destructive close off the issue's OPEN state,
    # not the mere presence of the merged tag.
    wt = _make_spoke(hub, tmp_path, "feature/1-done", push=True, ready=True)
    _complete_ship(hub, "feature/1-done")
    _git(hub, "worktree", "remove", str(wt))
    _git(hub, "branch", "-D", "feature/1-done")  # branch already pruned by prior teardown

    proc, logs = _run_land(hub, tmp_path, "1", issue_state="CLOSED")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "issue close 1" not in _log_text(logs["gh"]), "a CLOSED issue must not be re-closed"
    assert "ready/1" not in _local_tags(hub), "the stale marker is still cleaned up"


def test_reland_finalize_fetch_failure_skips_remote_delete(hub: Path, tmp_path: Path) -> None:
    # Issue #195: the finalize's merged-remote guard is only honest right after a
    # SUCCESSFUL fetch. Model the hazard: the spoke pushed one more commit after
    # the hub's last fetch (the stale tracking ref still sits at the merged
    # marker), then the resume-finalize runs with fetch dead. The stale ref would
    # pass the ancestor check and the delete would destroy the remote-only
    # commit — a failed fetch must skip the remote delete, loudly, while the
    # local prune (git branch -d, merged-only, safe) still runs.
    wt = _make_spoke(hub, tmp_path, "feature/1-wtgone", push=True, ready=True)
    marker_sha = _git(hub, "rev-parse", "feature/1-wtgone").strip()
    _complete_ship(hub, "feature/1-wtgone")
    (wt / "late.txt").write_text("pushed after the hub's last fetch\n")
    _git(wt, "add", "late.txt")
    _git(wt, "commit", "-qm", "feat: late", "-m", "Refs #1")
    _git(wt, "push", "-q", "origin", "feature/1-wtgone")
    _git(wt, "reset", "-q", "--hard", marker_sha)  # local branch back at the marker
    # The hub never fetched the late push: rewind the shared tracking ref to the marker.
    _git(hub, "update-ref", "refs/remotes/origin/feature/1-wtgone", marker_sha)
    _git(hub, "worktree", "remove", str(wt))
    _fetch_fail_git_shim(tmp_path)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "feature/1-wtgone" not in _local_branches(hub), "local prune still runs"
    assert _remote_sha(hub, "feature/1-wtgone") != "", "the remote-only commit survives"
    assert "origin/feature/1-wtgone" in proc.stderr, "the skipped delete is loud"


# --- lifecycle-label clear on land (issue #236) -------------------------------
# A landed/torn-down spoke no longer has live local state, so the land clears the
# status:*/mode:*/lane:* labels the dispatch stamped. The close comment is separate
# and unchanged; the clear is best-effort (a failing gh never fails the land) and
# only for numbered issues (ad-hoc branches never carried the labels).


def test_land_clears_lifecycle_labels(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-shipped", push=True)

    proc, logs = _run_land(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    gh = _log_text(logs["gh"])
    edits = [ln for ln in gh.splitlines() if ln.startswith("issue edit 7")]
    assert len(edits) == 1, f"expected one label-clear edit, got {edits}"
    for lbl in (
        "status:in-progress",
        "status:gate",
        "status:ready",
        "status:blocked",
        "mode:afk",
        "mode:attended",
        "lane:spoke",
    ):
        assert f"--remove-label {lbl}" in edits[0]
    assert "--add-label" not in edits[0]


def test_land_close_comment_unchanged_by_label_clear(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-shipped", push=True)

    proc, logs = _run_land(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    gh = _log_text(logs["gh"])
    assert "issue close 7" in gh
    assert "Landed on" in gh, "the close comment must be preserved verbatim"


def test_land_adhoc_branch_clears_no_labels(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "chore/adhoc-task", push=True)

    proc, logs = _run_land(hub, tmp_path, "adhoc-task")

    assert proc.returncode == 0, proc.stderr
    assert "issue edit" not in _log_text(logs["gh"])


def test_land_label_clear_gh_failure_non_fatal(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-ghdown", push=True)

    proc, _ = _run_land(hub, tmp_path, "7", gh_exit=1)

    assert proc.returncode == 0, proc.stderr


def test_land_mirror_opt_out_clears_no_labels(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/7-optout", push=True)

    proc, logs = _run_land(hub, tmp_path, "7", extra_env={"AI_TOOLKIT_GH_LIFECYCLE_LABELS": "0"})

    assert proc.returncode == 0, proc.stderr
    assert "issue edit" not in _log_text(logs["gh"])


# ── #278: one branch carrying N packed subtask issues closes ALL of them ──────
#
# A packed spoke ships several same-scope issues on ONE branch. The branch slug names only
# the PRIMARY, so a slug-derived scalar closes exactly one issue and silently leaves the rest
# open forever — they were shipped, but nothing says so.
#
# The subtask markers are the record of what shipped, but they are NOT all at the tip: under
# the deferred-terminal design the spoke emits ready/<subtask> as each one lands and
# ready/<primary> LAST, so earlier subtasks' markers sit on ANCESTOR commits. A `--points-at
# HEAD` scan therefore finds only the last subtask plus the primary — which is exactly why
# these fixtures are THREE-deep: a two-issue chain passes under both readings and would hide
# the bug. The list must come from markers REACHABLE FROM the tip, bounded to this branch's
# own commits ($DEFAULT..HEAD) so a foreign marker merged in from main is never closed.


def _make_packed_spoke(hub: Path, tmp_path: Path, primary: str, subtasks: list[str]) -> Path:
    """A branch shipping <primary> plus <subtasks>, marked the way a packed spoke marks it.

    Commit 1 is the primary's own work. Each subtask then gets its own commit and its
    ready/<N> tag as it ships. ready/<primary> is emitted LAST, at the final tip — the order
    worktree-land's at-tip marker guard requires. So every subtask marker except the last
    sits on an ancestor.
    """
    branch = f"feature/{primary}-packed"
    wt = tmp_path / branch.replace("/", "-")
    _git(hub, "worktree", "add", "-q", "-b", branch, str(wt))
    (wt / "primary.txt").write_text("primary work\n")
    _git(wt, "add", "primary.txt")
    _git(wt, "commit", "-qm", "feat: primary work", "-m", f"Refs #{primary}")
    for sub in subtasks:
        (wt / f"sub-{sub}.txt").write_text(f"subtask {sub}\n")
        _git(wt, "add", f"sub-{sub}.txt")
        _git(wt, "commit", "-qm", f"feat: subtask {sub}", "-m", f"Refs #{sub}")
        _git(wt, "tag", f"ready/{sub}")  # shipped here; the tip moves on after it
    _git(wt, "tag", f"ready/{primary}")  # terminal, at the final tip
    _git(wt, "push", "-q", "-u", "origin", branch)
    _git(wt, "push", "-q", "origin", "--tags")
    return wt


def test_land_closes_every_packed_subtask_issue(hub: Path, tmp_path: Path) -> None:
    _make_packed_spoke(hub, tmp_path, "263", ["265", "270"])

    proc, logs = _run_land(hub, tmp_path, "263")

    assert proc.returncode == 0, proc.stderr
    gh = _log_text(logs["gh"])
    assert "issue close 263" in gh, "the primary still closes"
    # #270's marker is at the tip; #265's is an ANCESTOR — the case a --points-at scan drops.
    assert "issue close 270" in gh, "the last subtask closes"
    assert "issue close 265" in gh, "an ancestor-marked subtask must close too"


def test_land_clears_lifecycle_labels_for_every_packed_subtask(hub: Path, tmp_path: Path) -> None:
    # The label mirror (#236) is per-issue: leaving status:in-progress on a shipped subtask
    # would show it as live work forever.
    _make_packed_spoke(hub, tmp_path, "263", ["265", "270"])

    proc, logs = _run_land(hub, tmp_path, "263")

    assert proc.returncode == 0, proc.stderr
    gh = _log_text(logs["gh"])
    for issue in ("263", "265", "270"):
        assert f"issue edit {issue}" in gh, f"#{issue}'s lifecycle labels must be cleared"


def test_land_consumes_every_packed_subtask_marker(hub: Path, tmp_path: Path) -> None:
    # The marker-consumption block's own reason applies to all N: a lingering ready/265 would
    # re-flag a FUTURE branch reusing that number as mergeable.
    _make_packed_spoke(hub, tmp_path, "263", ["265", "270"])

    proc, _ = _run_land(hub, tmp_path, "263")

    assert proc.returncode == 0, proc.stderr
    for issue in ("263", "265", "270"):
        tags = _git(hub, "tag", "--list", f"ready/{issue}")
        assert tags.strip() == "", f"ready/{issue} must be consumed once landed"


def test_land_does_not_close_a_foreign_marker_merged_from_main(hub: Path, tmp_path: Path) -> None:
    # The bound is load-bearing: an un-landed ready/<X> from a sibling spoke that reached main
    # is reachable from our tip once main is merged in, but it is NOT our work. Closing it
    # would silently mark someone else's in-flight issue done.
    (hub / "foreign.txt").write_text("a sibling's landed work\n")
    _git(hub, "add", "foreign.txt")
    _git(hub, "commit", "-qm", "feat: sibling work", "-m", "Refs #999")
    _git(hub, "tag", "ready/999")  # a marker sitting on main
    _git(hub, "push", "-q", "origin", "main", "--tags")

    _make_packed_spoke(hub, tmp_path, "263", ["265"])

    proc, logs = _run_land(hub, tmp_path, "263")

    assert proc.returncode == 0, proc.stderr
    gh = _log_text(logs["gh"])
    assert "issue close 263" in gh and "issue close 265" in gh
    assert "issue close 999" not in gh, "a marker already on main is not this branch's work"


def test_single_issue_land_is_unchanged(hub: Path, tmp_path: Path) -> None:
    # The overwhelmingly common path: an unpacked branch closes exactly its one issue.
    _make_spoke(hub, tmp_path, "feature/7-solo", push=True)

    proc, logs = _run_land(hub, tmp_path, "7")

    assert proc.returncode == 0, proc.stderr
    closes = [ln for ln in _log_text(logs["gh"]).splitlines() if ln.startswith("issue close")]
    assert len(closes) == 1 and "issue close 7" in closes[0]


# ── land mutex (issue #315) ───────────────────────────────────────────────────
# A manual/quick operator land and the drain's auto_land both merge+push main via
# THIS script, with nothing coordinating them; the two raced on 2026-07-16 and a
# rejected push left the hub BEHIND origin. worktree-land.sh now takes a shared land
# lock — a `mkdir` dir under ${AFK_STATE_DIR:-<git-common-dir>/ai-toolkit-afk}/land.lock
# (the #300 primitive + state dir) holding an `owner` file "<pid> <host> <ts>" — before
# the merge+push critical section, so concurrent lands serialize. A crashed holder is
# broken (dead pid, or past LAND_LOCK_STALE_SECONDS); a live holder is WAITED on
# (bounded by LAND_LOCK_WAIT_MAX) and the wait is LOGGED, never silent.
def _land_lock_dir(statedir: Path) -> Path:
    return statedir / "land.lock"


def _seed_land_lock(statedir: Path, *, pid: int, ts: int, host: str = "testhost") -> Path:
    """Pre-create the land lock owned by <pid> stamped at <ts> (models a held lock).

    The owner line is "<pid> <ts> <host>" — ts before host so it parses by fixed field.
    """
    lock = _land_lock_dir(statedir)
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "owner").write_text(f"{pid} {ts} {host}\n")
    return lock


def test_land_breaks_dead_holder_lock(hub: Path, tmp_path: Path) -> None:
    # A crashed lander must not wedge landing forever (AC3): a dead owner pid is broken.
    statedir = tmp_path / "afk-state"
    dead = subprocess.Popen(["sleep", "30"])
    dead.terminate()
    dead.wait()  # pid now dead — kill -0 fails
    _seed_land_lock(statedir, pid=dead.pid, ts=int(time.time()))
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", extra_env={"AFK_STATE_DIR": str(statedir)})

    assert proc.returncode == 0, proc.stderr
    assert "broke a stale land lock" in proc.stderr.lower(), proc.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()
    assert not _land_lock_dir(statedir).exists(), "the broken-then-owned lock is released after"


def test_land_breaks_lock_older_than_stale_bound(hub: Path, tmp_path: Path) -> None:
    # Backstop for a wedged-but-alive holder / pid reuse: a lock older than the stale bound is
    # broken even when its owner pid still resolves alive (use this test process's live pid).
    statedir = tmp_path / "afk-state"
    _seed_land_lock(statedir, pid=os.getpid(), ts=int(time.time()) - 100000)
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)

    proc, _ = _run_land(
        hub,
        tmp_path,
        "1",
        extra_env={"AFK_STATE_DIR": str(statedir), "LAND_LOCK_STALE_SECONDS": "10"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "broke a stale land lock" in proc.stderr.lower(), proc.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_waiting_land_logs_and_bounded_timeout_keeps_live_holder(hub: Path, tmp_path: Path) -> None:
    # A LIVE, fresh holder is NOT broken (probe the real process, Principle 4): the waiting land
    # LOGS that it is waiting (AC3, never silent) and gives up LOUDLY at the bound rather than
    # racing the push. The held lock and main are both untouched.
    statedir = tmp_path / "afk-state"
    holder = subprocess.Popen(["sleep", "30"])
    try:
        _seed_land_lock(statedir, pid=holder.pid, ts=int(time.time()))
        _make_spoke(hub, tmp_path, "feature/1-done", push=True)
        before = _remote_sha(hub, "main")

        proc, _ = _run_land(
            hub,
            tmp_path,
            "1",
            extra_env={
                "AFK_STATE_DIR": str(statedir),
                "LAND_LOCK_WAIT_MAX": "2",
                "LAND_LOCK_POLL": "1",
                "LAND_LOCK_STALE_SECONDS": "100000",
            },
        )

        assert proc.returncode != 0, "a bounded wait that never wins must fail loud, not race"
        low = proc.stderr.lower()
        assert "waiting for the land lock" in low, proc.stderr
        assert "timed out" in low, proc.stderr
        assert _land_lock_dir(statedir).exists(), "a live holder's lock must not be broken"
        assert _remote_sha(hub, "main") == before, "the waiting land must not touch main"
    finally:
        holder.terminate()
        holder.wait()


def test_garbage_stale_seconds_does_not_disable_the_mutex(hub: Path, tmp_path: Path) -> None:
    # Principle 2: a non-numeric LAND_LOCK_STALE_SECONDS must sanitize to the SAFE default,
    # not 0 — a zeroed bound would read every live lock as stale and silently break it. With a
    # garbage value, a fresh LIVE holder is still waited on (not broken), so the land times out.
    statedir = tmp_path / "afk-state"
    holder = subprocess.Popen(["sleep", "30"])
    try:
        _seed_land_lock(statedir, pid=holder.pid, ts=int(time.time()))
        _make_spoke(hub, tmp_path, "feature/1-done", push=True)

        proc, _ = _run_land(
            hub,
            tmp_path,
            "1",
            extra_env={
                "AFK_STATE_DIR": str(statedir),
                "LAND_LOCK_STALE_SECONDS": "1800s",  # operator typo — a unit suffix
                "LAND_LOCK_WAIT_MAX": "2",
                "LAND_LOCK_POLL": "1",
            },
        )

        assert proc.returncode != 0, "a garbage stale bound must not silently disable the mutex"
        assert "timed out" in proc.stderr.lower(), proc.stderr
        assert _land_lock_dir(statedir).exists(), "the fresh live holder's lock must survive"
    finally:
        holder.terminate()
        holder.wait()


def test_absent_owner_lock_is_waited_on_not_prematurely_broken(hub: Path, tmp_path: Path) -> None:
    # The mkdir->owner-write gap: a lock dir whose mkdir just won but whose owner file is a
    # microsecond from being written must NOT be broken by a concurrent waiter (else two lands
    # both own it — the exact #315 race). An owner-less lock reads as wait; the land is bounded
    # and fails loud, never racing.
    statedir = tmp_path / "afk-state"
    lock = _land_lock_dir(statedir)
    lock.mkdir(parents=True, exist_ok=True)  # a claimed dir with NO owner file yet
    _make_spoke(hub, tmp_path, "feature/1-done", push=True)
    before = _remote_sha(hub, "main")

    proc, _ = _run_land(
        hub,
        tmp_path,
        "1",
        extra_env={
            "AFK_STATE_DIR": str(statedir),
            "LAND_LOCK_WAIT_MAX": "2",
            "LAND_LOCK_POLL": "1",
        },
    )

    assert proc.returncode != 0, "an owner-less just-claimed lock must be waited on, not broken"
    assert "timed out" in proc.stderr.lower(), proc.stderr
    assert lock.exists(), "the owner-less lock must not be prematurely broken"
    assert _remote_sha(hub, "main") == before, "the waiting land must not touch main"


# ── never leave the hub behind origin (issue #315, AC2 + AC4) ──────────────────
def _advance_origin_main(tmp_path: Path, name: str) -> str:
    """Push a fresh commit to origin/main out-of-band (models a concurrent sibling land).

    Clones the bare remote, commits, pushes to main; returns the new origin/main SHA. The
    hub's own local main is left BEHIND origin (it has not fetched), the exact state a racy
    reset --keep produced on 2026-07-16.
    """
    clone = tmp_path / f"clone-{name}"
    subprocess.run(
        ["git", "clone", "-q", str(tmp_path / "remote.git"), str(clone)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(clone, "config", k, v)
    (clone / f"{name}.txt").write_text(f"sibling {name} landed\n")
    _git(clone, "add", f"{name}.txt")
    _git(clone, "commit", "-qm", f"feat: {name}", "-m", "Refs #0")
    _git(clone, "push", "-q", "origin", "main")
    return _git(clone, "rev-parse", "HEAD").strip()


def test_pre_merge_heal_when_hub_behind_origin(hub: Path, tmp_path: Path) -> None:
    # AC2: a hub whose local main is BEHIND origin (a sibling advanced origin) must heal —
    # fast-forward local main up to origin BEFORE merging — so the push is clean and the hub
    # ends AT origin, never behind. Pre-fix, the land merges into the stale local main, the
    # push is non-ff rejected, and the rollback strands the hub behind (the #315 incident).
    _make_spoke(hub, tmp_path, "feature/1-behindhub", push=True)
    sibling = _advance_origin_main(tmp_path, "sib1")

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + "\n---\n" + proc.stdout
    head = _git(hub, "rev-parse", "HEAD").strip()
    assert _remote_sha(hub, "main") == head, "the hub must end at origin, not behind it"
    reachable = _git(hub, "rev-list", "HEAD").split()
    assert sibling in reachable, "the sibling's landed commit must be an ancestor (healed forward)"
    assert (hub / "feature-1-behindhub.txt").exists(), "the spoke's work landed too"


def _stub_bindir(bindir: Path, sandbox: Path) -> dict[str, str]:
    """gh/pytest logging stubs in `bindir`; returns the land env (shared by threads)."""
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("gh", "pytest"):
        body = "#!/bin/sh\n"
        if name == "gh":
            body += f"case \"$*\" in *\"run list\"*) printf '%s' '{GREEN_RUNS}' ;; esac\n"
            body += 'case "$*" in *"issue view"*state*) printf "OPEN\\n" ;; esac\n'
        body += "exit 0\n"
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env["AFK_TELEMETRY_CONF"] = str(sandbox / "no-such-conf")
    for var in (
        "TMUX",
        "LANGFUSE_BASIC_AUTH",
        "LANGFUSE_HOST",
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
        "WT_SPOKE",
        "AI_TOOLKIT_GH_LIFECYCLE_LABELS",
    ):
        env.pop(var, None)
    return env


def test_two_concurrent_lands_serialize_main_never_rewound(hub: Path, tmp_path: Path) -> None:
    # AC4 (headline regression): two lands launched CONCURRENTLY on one hub checkout produce
    # two clean SEQUENTIAL lands — the mutex serializes them — and main is only ever advanced,
    # never rewound. Pre-mutex, the two `git merge`/`reset` sequences collide in the shared
    # worktree (index.lock contention) or race the push, and one fails or strands the hub.
    _make_spoke(hub, tmp_path, "feature/1-alpha", push=True)
    _make_spoke(hub, tmp_path, "feature/2-beta", push=True)
    seed = _remote_sha(hub, "main")
    env = _stub_bindir(tmp_path / "bin", tmp_path)
    env.update({"LAND_LOCK_POLL": "1", "LAND_LOCK_WAIT_MAX": "180"})

    def _land(target: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(WORKTREE_LAND), target],
            cwd=str(hub),
            capture_output=True,
            text=True,
            env=env,
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_land, "1"), ex.submit(_land, "2")
        r1, r2 = f1.result(), f2.result()

    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    final = _remote_sha(hub, "main")
    reachable = _git(hub, "rev-list", final).split()
    assert seed in reachable, "main was rewound below its starting tip — the race #315 forbids"
    assert (hub / "feature-1-alpha.txt").exists()
    assert (hub / "feature-2-beta.txt").exists()


def test_nonff_push_rejection_recovers_by_re_syncing_the_spoke_and_re_waiting_for_ci(
    hub: Path, tmp_path: Path
) -> None:
    # AC2 recovery clause: if a push still races (origin advances DURING the push, despite the
    # lock — e.g. a non-honoring pusher), the land must AUTOMATICALLY re-fetch, merge the NEW
    # main into the spoke, wait for CI on that merge, and re-push — not die with the hub behind.
    wt = _make_spoke(hub, tmp_path, "feature/1-racy", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    advanced = tmp_path / "origin-advanced"
    remote = tmp_path / "remote.git"
    hook = hub / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        # On the FIRST main push only, a sibling wins the race by advancing origin/main.
        f'if [ ! -f "{advanced}" ]; then\n'
        f'  touch "{advanced}"\n'
        "  d=$(mktemp -d)\n"
        f'  git clone -q "{remote}" "$d/c" >/dev/null 2>&1 || exit 0\n'
        '  ( cd "$d/c" \\\n'
        "    && git config user.email t@t.t && git config user.name t \\\n"
        "    && git config commit.gpgsign false \\\n"
        '    && printf "sib\\n" > sib.txt && git add sib.txt \\\n'
        '    && git commit -qm "feat: sibling" -m "Refs #0" \\\n'
        "    && git push -q origin main ) >/dev/null 2>&1 || exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    # The first push is skipped (CI-proven), so the hook only races a LOCAL-gated land.
    proc, logs = _run_land(hub, tmp_path, "1", "--local-gate")

    assert proc.returncode == 0, proc.stderr + "\n---\n" + proc.stdout
    head = _git(hub, "rev-parse", "HEAD").strip()
    assert _remote_sha(hub, "main") == head, "the hub must end at origin, not behind it"
    assert (hub / "feature-1-racy.txt").exists(), "the spoke's work landed after recovery"
    assert (hub / "sib.txt").exists(), "the sibling's commit is an ancestor (re-synced forward)"
    assert tip != head
    assert "advanced under the land" in proc.stderr  # the recovery path really ran
    assert "run list" not in _log_text(logs["gh"])  # --local-gate never asks CI


def test_nonff_push_rejection_in_ci_mode_re_syncs_and_re_checks_ci(
    hub: Path, tmp_path: Path
) -> None:
    # The same race in the default CI mode: origin advances between the CI-green check and the
    # main push (a non-honoring pusher advancing main out-of-band right before the push). The
    # recovery merges the new main into the spoke, pushes it, re-checks CI on the NEW sha, and
    # fast-forwards — the SHA that reaches main is still one CI passed.
    wt = _make_spoke(hub, tmp_path, "feature/1-racyci", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    remote = tmp_path / "remote.git"
    advanced = tmp_path / "origin-advanced"
    real_git = shutil.which("git")
    assert real_git is not None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    sib = tmp_path / "sib-clone"
    # A git shim that advances origin/main out-of-band just before the FIRST `push origin main`.
    shim = bindir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = push ] && [ "$2" = origin ] && [ "$3" = main ] && [ ! -f "' + str(advanced) + '" ]; then\n'
        f'  touch "{advanced}"\n'
        f'  "{real_git}" clone -q "{remote}" "{sib}" >/dev/null 2>&1\n'
        f'  ( cd "{sib}" && "{real_git}" config user.email t@t.t && "{real_git}" config user.name t \\\n'
        f'    && "{real_git}" config commit.gpgsign false && printf "sib\\n" > sib.txt \\\n'
        f'    && "{real_git}" add sib.txt && "{real_git}" commit -qm "feat: sibling" -m "Refs #0" \\\n'
        f'    && "{real_git}" push -q origin main ) >/dev/null 2>&1\n'
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    proc, logs = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 0, proc.stderr + "\n---\n" + proc.stdout
    head = _git(hub, "rev-parse", "HEAD").strip()
    assert _remote_sha(hub, "main") == head
    assert "advanced under the land" in proc.stderr  # the recovery path really ran
    assert (hub / "sib.txt").exists() and (hub / "feature-1-racyci.txt").exists()
    gh = _log_text(logs["gh"])
    assert f"--commit {tip}" in gh and f"--commit {head}" in gh  # CI re-checked on the new SHA
    assert _log_text(logs["pytest"]) == ""


# --- review follow-ups (#378) ----------------------------------------------------------


def test_reland_after_a_ci_timeout_on_the_merged_sha_succeeds(hub: Path, tmp_path: Path) -> None:
    # The land moved the spoke tip (merge of main pushed), so it must move the ready marker
    # with it — else the re-land reads a "stale marker" and nothing can land the spoke.
    wt = _make_spoke(hub, tmp_path, "feature/1-reland", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _diverge_hub(hub)

    first, _ = _run_land(hub, tmp_path, "1", ci_green_only_for=tip)  # merged SHA stays pending

    assert first.returncode == 1, first.stderr
    moved_tip = _git(wt, "rev-parse", "HEAD").strip()
    assert moved_tip != tip  # the land really pushed a merge onto the spoke
    # the ready marker followed the tip, locally and on origin
    assert _git(hub, "rev-parse", "ready/1^{commit}").strip() == moved_tip
    assert _git(hub, "ls-remote", "origin", "refs/tags/ready/1^{}").split()[0] == moved_tip

    second, _ = _run_land(hub, tmp_path, "1")  # CI has caught up

    assert second.returncode == 0, second.stderr
    assert _remote_sha(hub, "main") == _git(hub, "rev-parse", "HEAD").strip()


def test_moved_ready_marker_keeps_its_annotation(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-annot", push=True, ready=False)
    _git(wt, "tag", "-a", "ready/1", "-m", "ready", "-m", "LOCAL_GATE: audit note")
    _git(wt, "push", "-q", "origin", "ready/1")
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _diverge_hub(hub)

    _run_land(hub, tmp_path, "1", ci_green_only_for=tip)

    assert _git(hub, "tag", "-l", "--format=%(contents:body)", "ready/1").strip() == (
        "LOCAL_GATE: audit note"
    )
    assert _git(hub, "rev-parse", "ready/1^{commit}").strip() != tip  # followed the new tip


def test_failed_spoke_push_leaves_the_spoke_as_it_was(hub: Path, tmp_path: Path) -> None:
    wt = _make_spoke(hub, tmp_path, "feature/1-pushfail", push=True, ready=True)
    tip = _git(wt, "rev-parse", "HEAD").strip()
    _diverge_hub(hub)
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    hook.write_text(
        '#!/bin/sh\nwhile read -r _o _n ref; do\n  case "$ref" in refs/heads/feature/*) '
        'echo "declined" >&2; exit 1 ;; esac\ndone\nexit 0\n'
    )
    hook.chmod(0o755)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode != 0
    assert _git(wt, "rev-parse", "HEAD").strip() == tip  # the unpushed merge was undone
    assert _git(wt, "status", "--porcelain").strip() == ""


def test_local_gate_the_hook_never_ran_is_flagged_unverified(hub: Path, tmp_path: Path) -> None:
    # A hook that exits 0 without running the gate (disabled / skip-configured) must not let
    # the log claim the full suite ran.
    _make_spoke(hub, tmp_path, "feature/1-mute", push=True, ready=True)
    hook = hub / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate")

    assert proc.returncode == 0, proc.stderr
    assert "did not report running the local gate" in proc.stderr
    assert "UNVERIFIED" in proc.stdout


def test_local_gate_ignores_an_inherited_skip(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-inherit", push=True, ready=True)
    seen = tmp_path / "gate-skip.log"
    hook = hub / ".git" / "hooks" / "pre-push"
    # Record the skip flag only for the push that carries the local gate command.
    hook.write_text(
        "#!/bin/sh\n"
        f'[ -z "${{TEST_SELECT_CMD:-}}" ] || echo "skip=[${{TEST_SELECT_SKIP:-}}]" >> "{seen}"\n'
        'echo "test-select: running custom suite (TEST_SELECT_CMD)" >&2\n'
        "exit 0\n"
    )
    hook.chmod(0o755)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate", extra_env={"TEST_SELECT_SKIP": "1"})

    assert proc.returncode == 0, proc.stderr
    assert _log_text(seen).splitlines()[0] == "skip=[]"  # the gate push ran un-skipped


def test_skip_tests_conflicts_with_a_local_gate(hub: Path, tmp_path: Path) -> None:
    _make_spoke(hub, tmp_path, "feature/1-skiplg", push=True)

    proc, _ = _run_land(hub, tmp_path, "1", "--local-gate", "--skip-tests")

    assert proc.returncode != 0
    assert "--skip-tests conflicts" in proc.stderr

# --- teardown through Orca (issue #364) -----------------------------------------


def _land_after_ingest(
    orca_bin: Path, hub: Path, tmp_path: Path, branch: str, *args: str, dispatch: str = "ctx_1"
) -> tuple[subprocess.CompletedProcess, Path]:
    """Land a seeded OTel spoke (so the ingest really runs) with an identity record."""
    wt = _make_spoke(hub, tmp_path, branch, push=True)
    _seed_otel_spoke(hub, wt, raw_bodies=True)
    (wt / ".ai-toolkit" / "identity").write_text(f"orca_dispatch_id={dispatch}\n")
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-364"\n')
    proc, _ = _run_land(
        hub,
        tmp_path,
        _issue_of(branch),
        *args,
        stub_python312=True,
        stub_curl=True,
        extra_env={
            "AFK_TELEMETRY_CONF": str(conf),
            "AI_TOOLKIT_INGEST_FLUSH_WAIT": "0",
            "ORCA_CALLS_LOG": str(orca_bin / ".orca-stub" / "calls.jsonl"),
            "ORCA_SETTLE_SLEEP": "0",
        },
    )
    return proc, wt


def _order(orca_bin: Path) -> list[str]:
    return [" ".join(c[:2]) if c[0] != "INGEST" else "INGEST" for c in orca_calls(orca_bin)]


def test_land_runs_ingest_then_worker_release_then_removal_without_hooks(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    proc, wt = _land_after_ingest(orca_bin, hub, tmp_path, "feature/1-order")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    order = _order(orca_bin)
    release = order.index("orchestration worker-release")
    removal = order.index("worktree rm")
    assert max(i for i, name in enumerate(order) if name == "INGEST") < release < removal
    calls = orca_calls(orca_bin)
    assert calls[release][2:4] == ["--dispatch", "ctx_1"]
    assert "--run-hooks" not in calls[removal], "land ingested already; the archive hook must not"
    assert not wt.exists()


def test_land_with_no_recorded_dispatch_skips_the_release_and_still_lands(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    proc, wt = _land_after_ingest(orca_bin, hub, tmp_path, "feature/1-nodispatch", dispatch="")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "orchestration worker-release" not in _order(orca_bin)
    assert "worktree rm" in _order(orca_bin)
    assert not wt.exists()


def test_land_continues_when_the_worker_release_fails(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    orca_scenario(
        orca_bin, {"orchestration worker-release": [{"rc": 1, "stderr": "release_unknown"}]}
    )

    proc, wt = _land_after_ingest(orca_bin, hub, tmp_path, "feature/1-relfail")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "worker-release failed" in proc.stderr
    assert not wt.exists()


def test_land_exits_3_not_1_when_orca_refuses_the_removal(
    orca_bin: Path, hub: Path, tmp_path: Path
) -> None:
    orca_scenario(orca_bin, {"_rm": {"refuse": True}})
    wt = _make_spoke(hub, tmp_path, "feature/1-refused", push=True)

    proc, _ = _run_land(hub, tmp_path, "1")

    assert proc.returncode == 3, proc.stderr + proc.stdout
    assert "INCOMPLETE" in proc.stdout
    assert wt.exists()
    assert _git(hub, "rev-parse", "main").strip() == _remote_sha(hub, "main")


