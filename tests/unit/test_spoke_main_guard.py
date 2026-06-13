"""RED contract for the spoke-main-guard PreToolUse hook (Issue #32).

The hook (``shared/hooks/spoke-main-guard.sh``) closes the self-land hole left
by push-scope-guard (#26): fix/26 blocked a spoke's *push* of the default
branch, but not the *local mutation* of the ``main`` ref. The #27/#31 spokes ran
``worktree-land.sh`` from inside their worktrees, which merged their feature
branch into the shared local ``main`` ref before the push was even attempted.

This guard makes a spoke unable to touch the local default branch AT ALL. A
"spoke" is any session carrying ``WT_SPOKE`` (the role marker worktree-new.sh
stamps) or running from a linked worktree. In a spoke it DENIES (deny-or-silent,
exit 2 on every platform — unlike the allow-or-silent rm/push/chmod guards):

* ``git checkout main`` / ``git switch main`` (and ``-b``/``-c`` force-create).
* ``git merge`` while ``main`` is HEAD (a merge always targets HEAD).
* ``git branch -f main`` / ``--force`` / move / delete of the default branch.
* ``git push`` to the LOCAL ``main`` ref (``git push . <ref>:main``).
* ``git update-ref refs/heads/main``.
* ``git reset`` while on ``main``.
* invoking ``worktree-land.sh`` (the hub-only land script).

It MUST still ALLOW the sanctioned reconciliation flow:

* ``git merge origin/main`` INTO the current feature branch.
* ``git fetch origin``.

On the hub (no ``WT_SPOKE``, not a linked worktree) it is a pure no-op.

Hermetic setup mirrors test_push_scope_guard.py: a local bare ``origin`` (no
network), a hub checkout on ``main``, and a spoke created with
``git worktree add``. Git config is pinned to nothing and ``CURSOR_PROJECT_DIR``
is stripped so project-root resolution uses the payload/cwd.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "spoke-main-guard.sh"

BLOCK = 2
ALLOW = 0

OWN = "feature/32-spoke"
OTHER = "feature/22-other"

# Pin git config to nothing: a host's global/system config must not reach the
# fixture repos or the hook.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _payload(command: str) -> str:
    """Claude/Copilot generic shape: command under tool_input."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _cursor_payload(command: str, *, root: Path) -> str:
    """Cursor beforeShellExecution shape: top-level command + workspace_roots."""
    return json.dumps(
        {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": str(root),
            "workspace_roots": [str(root)],
        }
    )


def _hook_env(*, spoke: bool) -> dict[str, str]:
    """Hook env: git config pinned, CURSOR_PROJECT_DIR stripped. ``spoke`` sets
    or clears WT_SPOKE (the role marker the guard keys on)."""
    env = dict(_GIT_ENV)
    env.pop("CURSOR_PROJECT_DIR", None)
    if spoke:
        env["WT_SPOKE"] = "32"
    else:
        env.pop("WT_SPOKE", None)
    return env


def run_guard(payload: str, cwd: Path, *, spoke: bool) -> subprocess.CompletedProcess:
    """Run spoke-main-guard with an explicit payload and spoke/hub env."""
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=_hook_env(spoke=spoke),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with a local bare `origin` remote."""
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
    """A linked worktree of the hub on its own task branch, one commit ahead."""
    wt = tmp_path / "spoke"
    _git(hub, "worktree", "add", "-q", "-b", OWN, str(wt))
    (wt / "work.txt").write_text("spoke work\n")
    _git(wt, "add", "work.txt")
    _git(wt, "commit", "-qm", "feat: work", "-m", "Refs #32")
    return wt


# ── Spoke: denied forms (exit 2 on every platform) ────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git checkout main", id="checkout-main"),
        pytest.param("git switch main", id="switch-main"),
        pytest.param("git checkout -b main", id="checkout-create-main"),
        pytest.param("git switch -c main", id="switch-create-main"),
        pytest.param("git branch -f main origin/main", id="branch-force-main"),
        pytest.param("git branch --force main", id="branch-force-long-main"),
        pytest.param("git branch -M main", id="branch-move-main"),
        pytest.param("git branch -D main", id="branch-delete-main"),
        pytest.param("git push . HEAD:main", id="push-local-headmain"),
        pytest.param(f"git push . {OWN}:main", id="push-local-refmain"),
        pytest.param("git push ./ HEAD:main", id="push-local-dotslash"),
        pytest.param("git update-ref refs/heads/main HEAD", id="update-ref-main"),
        pytest.param("git update-ref -d refs/heads/main", id="update-ref-delete-main"),
        pytest.param("worktree-land.sh 32", id="land-script"),
        pytest.param("scripts/worktree-land.sh 32", id="land-script-path"),
        pytest.param("cd /tmp && git checkout main", id="chained-checkout-main"),
        pytest.param(f"git checkout main && git merge {OWN}", id="compound-checkout-merge"),
        pytest.param("git checkout main | tee log", id="piped-checkout-main"),
        # A `-c key=val` value must not orphan and break the chain to the verb.
        pytest.param("git -c core.pager=cat checkout main", id="dashc-config-bypass"),
        pytest.param("git -c x=y switch main", id="dashc-switch-bypass"),
        # Grouping / subshell punctuation must not launder a forbidden command.
        pytest.param("(git checkout main)", id="subshell-checkout-main"),
        pytest.param("{ git checkout main; }", id="brace-group-checkout-main"),
        pytest.param("true && (git checkout main)", id="and-subshell-checkout-main"),
        pytest.param("git branch main", id="branch-bare-create-main"),
    ],
)
def test_spoke_denies_main_mutation(spoke: Path, command: str) -> None:
    result = run_guard(_payload(command), spoke, spoke=True)

    assert result.returncode == BLOCK, result.stdout + result.stderr


def test_spoke_denies_merge_while_on_main(hub: Path) -> None:
    # A merge always targets HEAD. A spoke session (WT_SPOKE) whose HEAD is the
    # default branch merging anything = mutating local main → deny.
    result = run_guard(_payload(f"git merge {OTHER}"), hub, spoke=True)

    assert result.returncode == BLOCK, result.stdout + result.stderr


def test_spoke_denies_reset_while_on_main(hub: Path) -> None:
    # git reset on the default branch can move the main ref → deny.
    result = run_guard(_payload("git reset --hard origin/main"), hub, spoke=True)

    assert result.returncode == BLOCK, result.stdout + result.stderr


def test_spoke_denies_via_cursor_payload(spoke: Path) -> None:
    # The deny is cross-platform: the Cursor beforeShellExecution shape blocks too.
    result = run_guard(_cursor_payload("git checkout main", root=spoke), spoke, spoke=True)

    assert result.returncode == BLOCK, result.stdout + result.stderr


def test_spoke_deny_emits_reason(spoke: Path) -> None:
    result = run_guard(_payload("git checkout main"), spoke, spoke=True)

    assert "main" in (result.stdout + result.stderr).lower()


# ── Spoke: the sanctioned reconciliation flow stays allowed ─


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git merge origin/main", id="merge-origin-main"),
        pytest.param("git merge origin/main --no-edit", id="merge-origin-main-noedit"),
        pytest.param("git merge --ff-only origin/main", id="merge-ff-only-origin-main"),
        pytest.param("git fetch origin", id="fetch-origin"),
        pytest.param("git fetch origin main", id="fetch-origin-main"),
        pytest.param(f"git checkout {OTHER}", id="checkout-other-branch"),
        pytest.param("git switch -c feature/32-extra", id="switch-create-feature"),
        pytest.param("git reset --hard HEAD~1", id="reset-on-feature"),
        pytest.param(f"git push origin {OWN}", id="push-own-branch-to-origin"),
        pytest.param("git push origin HEAD", id="push-head-to-origin"),
        pytest.param("git branch feature/32-tmp", id="branch-create-feature"),
        pytest.param("git status", id="status"),
        pytest.param("ls -la", id="non-git"),
    ],
)
def test_spoke_allows_reconciliation_and_own_work(spoke: Path, command: str) -> None:
    result = run_guard(_payload(command), spoke, spoke=True)

    assert result.returncode == ALLOW, result.stdout + result.stderr


# ── Silent degradation: ambiguous/dynamic/unparseable → allow, never block ─


@pytest.mark.parametrize(
    "command",
    [
        # A $-dynamic branch token cannot be adjudicated by a hook → silent.
        pytest.param("git checkout $BR", id="dynamic-checkout-target"),
        pytest.param("git merge $REF", id="dynamic-merge-on-feature"),
        # A file restore from main moves no ref → not a branch switch.
        pytest.param("git checkout main -- work.txt", id="checkout-main-file-restore"),
        # Unbalanced quotes are unparseable → degrade to silent (git rejects it).
        pytest.param('git checkout "main', id="unbalanced-quote"),
    ],
)
def test_spoke_degrades_to_silent(spoke: Path, command: str) -> None:
    result = run_guard(_payload(command), spoke, spoke=True)

    assert result.returncode == ALLOW, result.stdout + result.stderr


def test_spoke_allows_merge_on_detached_head(spoke: Path) -> None:
    # Detached HEAD is not the default branch, so a merge moves no `main` ref.
    head = _git(spoke, "rev-parse", "HEAD").strip()
    _git(spoke, "checkout", "-q", head)

    result = run_guard(_payload(f"git merge {OWN}"), spoke, spoke=True)

    assert result.returncode == ALLOW, result.stdout + result.stderr


# ── Hub: no WT_SPOKE, not a linked worktree → pure no-op ────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git checkout main", id="checkout-main"),
        pytest.param("git merge feature/22-other", id="merge-into-main"),
        pytest.param("git branch -f main origin/main", id="branch-force-main"),
        pytest.param("git push . HEAD:main", id="push-local-main"),
        pytest.param("git update-ref refs/heads/main HEAD", id="update-ref-main"),
        pytest.param("git reset --hard origin/main", id="reset-on-main"),
        pytest.param("worktree-land.sh 32", id="land-script"),
    ],
)
def test_hub_is_noop(hub: Path, command: str) -> None:
    result = run_guard(_payload(command), hub, spoke=False)

    assert result.returncode == ALLOW, result.stdout + result.stderr
