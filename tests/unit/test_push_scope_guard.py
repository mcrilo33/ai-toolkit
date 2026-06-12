"""RED contract for the push-scope-guard PreToolUse hook (Issue #11).

The hook (``shared/hooks/push-scope-guard.sh``) guards `git push` scope:

* In a LINKED WORKTREE (an execution "spoke"), only pushes targeting the
  worktree's OWN current branch are allowed. Pushes touching the repo's
  default branch or any other branch ref — including ``--delete``,
  ``--mirror``, ``--all``, and ``src:dst`` refspecs judged by dst — go
  through ``ship_gate_enforce``: hard deny (exit 2) on a Cursor
  ``beforeShellExecution`` payload, advisory (exit 0 + ``[Hook]`` stderr)
  on the Claude/Copilot generic shape.
* On the MAIN CHECKOUT (the "hub"), pushing the default branch or the
  current branch is allowed silently; pushing some OTHER task branch warns
  (advisory on every platform); ``--delete`` from the hub is teardown
  cleanup and stays silent.
* A bare ``git push`` resolves against the branch upstream: own branch →
  allow, default branch (e.g. origin/main) → deny/warn, no upstream →
  allow (git itself will demand ``-u``).
* Non-push commands and runs outside any git repo are no-ops.

Hermetic setup: a local bare ``origin`` (no network), a hub checkout on
``main``, and a spoke created with ``git worktree add`` (its git-dir lives
under ``hub/.git/worktrees/``, which is how the hook tells spoke from hub).
Git config is pinned to nothing (``GIT_CONFIG_GLOBAL/SYSTEM=/dev/null``) for
all git commands AND for hook invocations, and ``CURSOR_PROJECT_DIR`` is
stripped so project-root resolution uses the payload/cwd.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "push-scope-guard.sh"

BLOCK = 2
ALLOW = 0

OWN = "feature/11-own"
OTHER = "feature/22-other"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# protocol settings, signing) must not reach the fixture repos or the hook.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _payload(command: str) -> str:
    """Claude/Copilot generic shape: command under tool_input."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _cursor_shell_payload(command: str, *, root: Path) -> str:
    """Cursor beforeShellExecution shape: top-level command + workspace_roots."""
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": str(root),
            "workspace_roots": [str(root)],
        }
    )


def _hook_env() -> dict[str, str]:
    """Hook env: git config pinned, CURSOR_PROJECT_DIR stripped so the
    project-root resolution falls to the payload/cwd (cf. _hub_env in
    test_commit_hooks.py)."""
    env = dict(_GIT_ENV)
    env.pop("CURSOR_PROJECT_DIR", None)
    return env


def run_guard(payload: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run push-scope-guard with an explicit payload; return the process."""
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_hook_env(),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with a local bare `origin` remote.

    Also carries `feature/22-other` — another task's branch — for the
    foreign-branch cases.
    """
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
    _git(hub, "branch", OTHER)
    return hub


@pytest.fixture()
def spoke(hub: Path, tmp_path: Path) -> Path:
    """A linked worktree of the hub on its own task branch, one commit ahead.

    No upstream configured by default; tests that need one set it explicitly.
    """
    wt = tmp_path / "spoke"
    _git(hub, "worktree", "add", "-q", "-b", OWN, str(wt))
    (wt / "work.txt").write_text("spoke work\n")
    _git(wt, "add", "work.txt")
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #11")
    return wt


@pytest.fixture()
def spoke30(hub: Path, tmp_path: Path) -> Path:
    """A spoke on a branch ending in digits — the redirect-fd parsing edge."""
    wt = tmp_path / "spoke30"
    _git(hub, "worktree", "add", "-q", "-b", "feature/30", str(wt))
    return wt


# ── Spoke, Cursor shape: out-of-scope pushes hard-deny ────


def test_spoke_cursor_denies_default_branch_push(spoke: Path) -> None:
    # The default branch is published by the hub; a spoke must never push it.
    payload = _cursor_shell_payload("git push origin main", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git push origin {OTHER}", id="other-task-branch"),
        pytest.param(f"git push origin {OWN}:main", id="refspec-dst-is-default"),
        pytest.param(f"git push origin :{OTHER}", id="empty-src-refspec-delete"),
        pytest.param(f"git push --delete origin {OWN}", id="delete-flag"),
        pytest.param("git push --mirror origin", id="mirror"),
        pytest.param("git push --all origin", id="all"),
        pytest.param("cd /tmp && git push origin main", id="chained-not-bypassed"),
    ],
)
def test_spoke_cursor_denies_out_of_scope_push(spoke: Path, command: str) -> None:
    payload = _cursor_shell_payload(command, root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


def test_spoke_cursor_denies_bare_push_when_upstream_is_default(spoke: Path) -> None:
    # A bare `git push` resolves against the upstream: tracking origin/main
    # means the push would land on the default branch → deny.
    _git(spoke, "branch", "--set-upstream-to=origin/main", OWN)
    payload = _cursor_shell_payload("git push", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


# ── Spoke, Cursor shape: own-branch pushes allowed ────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git push origin {OWN}", id="own-branch"),
        pytest.param(f"git push -u origin {OWN}", id="first-push-with-u"),
        pytest.param("git push origin HEAD", id="head-counts-as-own"),
        pytest.param(f"git push --force-with-lease origin {OWN}", id="force-with-lease"),
        pytest.param("ls -la", id="non-push-command"),
    ],
)
def test_spoke_cursor_allows_own_branch_push(spoke: Path, command: str) -> None:
    payload = _cursor_shell_payload(command, root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW


def test_spoke_cursor_allows_bare_push_with_own_upstream(spoke: Path) -> None:
    # Upstream is the spoke's own branch → a bare `git push` stays in scope.
    _git(spoke, "push", "-q", "-u", "origin", OWN)
    payload = _cursor_shell_payload("git push", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW


def test_spoke_cursor_allows_bare_push_without_upstream(spoke: Path) -> None:
    # No upstream → the target cannot be resolved; degrade to allow (git
    # itself will refuse and demand -u).
    payload = _cursor_shell_payload("git push", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW


# ── Spoke: shell dressing must not flip the verdict ───────
# Redirections, quoting, and unexpandable variables are not refspecs; a -C
# global option must not detach the push clause from the parser.


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git push -u origin {OWN} 2>&1", id="stderr-redirect"),
        pytest.param(f"git push -u origin {OWN} >push.log 2>&1", id="file-and-stderr-redirect"),
        pytest.param(f"git push -u origin '{OWN}'", id="single-quoted-branch"),
        pytest.param(f'git push -u origin "{OWN}"', id="double-quoted-branch"),
        pytest.param('git push -u origin "$BRANCH"', id="unexpanded-variable-degrades"),
    ],
)
def test_spoke_cursor_allows_shell_dressed_own_branch_push(spoke: Path, command: str) -> None:
    payload = _cursor_shell_payload(command, root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW


def test_spoke_cursor_denies_dash_c_push_of_default_branch(spoke: Path) -> None:
    # `git -C <path> push` is still a push of this repo's default branch; the
    # -C global option must not detach the clause from the refspec parser.
    payload = _cursor_shell_payload(f"git -C {spoke} push origin main", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git -C . push origin main", id="dash-c-relative"),
        pytest.param("VAR=1 git push origin main", id="env-prefixed"),
        pytest.param("git push origin +main", id="forced-refspec"),
        pytest.param("git push origin refs/heads/main", id="fully-qualified-ref"),
        pytest.param("git push origin HEAD:main", id="head-to-default"),
    ],
)
def test_spoke_cursor_denies_dressed_default_branch_push(spoke: Path, command: str) -> None:
    payload = _cursor_shell_payload(command, root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


# ── Spoke: EVERY push clause is judged, not just one ──────


def test_spoke_cursor_denies_default_branch_in_first_clause(spoke: Path) -> None:
    # A compliant second clause must not launder an out-of-scope first one.
    payload = _cursor_shell_payload(f"git push origin main && git push origin {OWN}", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


def test_spoke_cursor_denies_default_branch_in_second_clause(spoke: Path) -> None:
    payload = _cursor_shell_payload(f"git push origin {OWN} && git push origin main", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


# ── Spoke: a dynamic token must not smuggle a concrete refspec ─


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git push origin main $EXTRA", id="trailing-variable"),
        pytest.param("git push --force-with-lease=$SHA origin main", id="variable-flag-value"),
    ],
)
def test_spoke_cursor_denies_concrete_default_despite_variable(spoke: Path, command: str) -> None:
    # The $-degrade covers tokens the hook cannot expand — a CONCRETE refspec
    # naming the default branch is still adjudicable and still out of scope.
    payload = _cursor_shell_payload(command, root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK


# ── Spoke: redirect-target edge cases stay in scope ───────


def test_spoke_cursor_allows_own_branch_push_with_quoted_log_target(spoke: Path) -> None:
    payload = _cursor_shell_payload(f'git push -u origin {OWN} > "my log.txt"', root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW


def test_spoke_cursor_allows_digit_branch_with_glued_redirect(spoke30: Path) -> None:
    # `feature/30>log` redirects stdout — the 30 belongs to the branch, not to
    # a file descriptor (fd digits only count as their own word).
    payload = _cursor_shell_payload("git push -u origin feature/30>log", root=spoke30)

    result = run_guard(payload, cwd=spoke30)

    assert result.returncode == ALLOW


# ── Spoke, Claude shape: advisory only ────────────────────


def test_spoke_claude_warns_on_default_branch_push(spoke: Path) -> None:
    # The generic (Claude/Copilot) payload cannot hard-deny: exit 0 + warning.
    payload = _payload("git push origin main")

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW
    assert "[Hook]" in result.stderr


def test_spoke_claude_silent_on_own_branch_push(spoke: Path) -> None:
    payload = _payload(f"git push origin {OWN}")

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


# ── Hub (main checkout): publishing flows stay open ───────


def test_hub_cursor_allows_default_branch_push_silently(hub: Path) -> None:
    # Publishing the default branch is exactly the hub's job.
    payload = _cursor_shell_payload("git push origin main", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


def test_hub_cursor_allows_bare_push(hub: Path) -> None:
    # Bare push from the hub on main resolves to its own upstream → allow.
    payload = _cursor_shell_payload("git push", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW


def test_hub_cursor_allows_default_branch_push_with_redirect(hub: Path) -> None:
    # Redirection suffixes are not refspecs — the hub's own publish step often
    # captures output and must stay silent.
    payload = _cursor_shell_payload("git push origin main 2>&1", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


def test_hub_cursor_warns_on_other_task_branch_push(hub: Path) -> None:
    # Pushing some other task's branch from the hub is suspicious but legal:
    # advisory warn on every platform, never a deny.
    payload = _cursor_shell_payload(f"git push origin {OTHER}", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" in result.stderr


def test_hub_cursor_allows_delete_of_task_branch(hub: Path) -> None:
    # Remote branch deletion from the hub is teardown cleanup → silent allow.
    payload = _cursor_shell_payload(f"git push --delete origin {OTHER}", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


def test_hub_cursor_silent_on_refspec_form_delete(hub: Path) -> None:
    # `:branch` is the refspec spelling of --delete — same teardown cleanup.
    payload = _cursor_shell_payload(f"git push origin :{OTHER}", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


def test_hub_cursor_silent_on_tag_push(hub: Path) -> None:
    # A qualified tag ref is a release artifact, not some spoke's task branch.
    payload = _cursor_shell_payload("git push origin refs/tags/v1.0.0", root=hub)

    result = run_guard(payload, cwd=hub)

    assert result.returncode == ALLOW
    assert "[Hook]" not in result.stderr


# ── Deny messages explain the topology ────────────────────


def test_spoke_deny_message_mentions_hub(spoke: Path) -> None:
    # The default-branch denial must say the default branch is published by
    # the hub, so the agent knows where that push belongs.
    payload = _cursor_shell_payload("git push origin main", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK
    assert "hub" in result.stderr


def test_spoke_deny_message_mentions_own_branch(spoke: Path) -> None:
    # The foreign-branch denial must convey that spokes push only their own
    # branch.
    payload = _cursor_shell_payload(f"git push origin {OTHER}", root=spoke)

    result = run_guard(payload, cwd=spoke)

    assert result.returncode == BLOCK
    assert "own branch" in result.stderr


# ── Outside any git repo: no-op ───────────────────────────


def test_outside_git_repo_allows(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    payload = _cursor_shell_payload("git push origin main", root=outside)

    result = run_guard(payload, cwd=outside)

    assert result.returncode == ALLOW
