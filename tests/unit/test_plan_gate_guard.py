"""RED contract for the plan-gate-guard PreToolUse hook (Issue #173).

The PLAN-gate park is *requested*, not *enforced*: #117 showed a spoke emitting
``gate/<N>`` then self-approving and continuing to code. This guard makes the
wait mechanical — the LLM authors the plan; parking becomes physics.

The hook (``shared/hooks/plan-gate-guard.sh``) is a PreToolUse deny-or-silent
guard. While a ``gate/<N>`` tag sits AT the branch tip of the cwd worktree — N
parsed from the branch slug (``feature/173-foo`` → 173) — it DENIES (exit 2 on
every platform via deny()):

* ``Edit`` / ``Write`` / ``NotebookEdit`` / ``MultiEdit`` tool calls, and
* a ``git commit`` Bash segment (compound/prefixed forms included).

It STILL ALLOWS everything that lets the spoke present its plan and park:

* reads / searches / ``git status`` / ``git diff`` / ``git log`` (non-write Bash),
* ``spoke-ready.sh`` (the marker emitter — how the spoke parks / un-parks),
* a non-commit git write like ``git add`` (only ``commit`` is the code-landing
  segment the gate cares about).

The wait is self-clearing with NO new machinery: the gate answer path already
deletes the tag (``_consume_gate_tag``), and once the tip advances past the gate
commit the tag is no longer at the tip — either un-blocks. On the hub (branch
``main`` → non-numeric slug) and in any spoke with no gate tag at the tip it is
a pure no-op (fail-open — a deny guard must never false-block real work).

Hermetic setup mirrors test_spoke_main_guard.py: a local bare ``origin`` (no
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

HOOK = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "plan-gate-guard.sh"

BLOCK = 2
ALLOW = 0

ISSUE = "173"
BRANCH = f"feature/{ISSUE}-hooks-enforce-the-plan"

# Pin git config to nothing: a host's global/system config must not reach the
# fixture repos or the hook.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)


def _bash_payload(command: str) -> str:
    """Claude/Copilot generic shape: command under tool_input."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _edit_payload(tool: str, path: str = "work.txt") -> str:
    """Claude Edit/Write/NotebookEdit shape."""
    return json.dumps(
        {"tool_name": tool, "tool_input": {"file_path": path, "content": "x", "new_string": "x"}}
    )


def _cursor_commit_payload(command: str, *, root: Path) -> str:
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
    env = dict(_GIT_ENV)
    env.pop("CURSOR_PROJECT_DIR", None)
    # The guard keys on the branch slug + the gate tag, NOT on WT_SPOKE — strip
    # it so a leaked marker can't be what makes (or breaks) the deny.
    env.pop("WT_SPOKE", None)
    return env


def run_guard(payload: str, cwd: Path) -> subprocess.CompletedProcess:
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
    return hub


@pytest.fixture()
def spoke(hub: Path, tmp_path: Path) -> Path:
    """A linked worktree of the hub on the issue's task branch, one commit ahead."""
    wt = tmp_path / "spoke"
    _git(hub, "worktree", "add", "-q", "-b", BRANCH, str(wt))
    (wt / "work.txt").write_text("spoke work\n")
    _git(wt, "add", "work.txt")
    _git(wt, "commit", "-qm", "feat: work", "-m", f"Refs #{ISSUE}")
    return wt


@pytest.fixture()
def parked_spoke(spoke: Path) -> Path:
    """The spoke PARKED at its PLAN gate: gate/<N> annotated tag AT the tip."""
    _git(spoke, "tag", "-a", f"gate/{ISSUE}", "-m", "plan")
    return spoke


# ── Parked: denied forms (exit 2) ─────────────────────────


@pytest.mark.parametrize(
    "tool",
    ["Edit", "Write", "NotebookEdit", "MultiEdit"],
)
def test_parked_denies_file_writes(parked_spoke: Path, tool: str) -> None:
    result = run_guard(_edit_payload(tool), parked_spoke)

    assert result.returncode == BLOCK, result.stdout + result.stderr


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git commit -m 'feat: x'", id="commit"),
        pytest.param("git commit -am wip", id="commit-am"),
        pytest.param("cd sub && git commit -m x", id="chained-commit"),
        pytest.param("VAR=1 git commit -m x", id="env-prefixed-commit"),
        pytest.param("git -C . commit -m x", id="dashC-commit"),
    ],
)
def test_parked_denies_git_commit(parked_spoke: Path, command: str) -> None:
    result = run_guard(_bash_payload(command), parked_spoke)

    assert result.returncode == BLOCK, result.stdout + result.stderr


def test_parked_deny_message_mentions_plan_gate(parked_spoke: Path) -> None:
    result = run_guard(_edit_payload("Write"), parked_spoke)

    assert result.returncode == BLOCK, result.stdout + result.stderr
    out = (result.stdout + result.stderr).lower()
    assert "plan" in out and "gate" in out


def test_parked_denies_via_cursor_commit_payload(parked_spoke: Path) -> None:
    result = run_guard(_cursor_commit_payload("git commit -m x", root=parked_spoke), parked_spoke)

    assert result.returncode == BLOCK, result.stdout + result.stderr


# ── Parked: the plan-and-park flow stays allowed ──────────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git status", id="status"),
        pytest.param("git diff", id="diff"),
        pytest.param("git log --oneline", id="log"),
        pytest.param("git add work.txt", id="add-not-commit"),
        pytest.param("grep -r foo .", id="grep"),
        pytest.param("cat work.txt", id="cat"),
        pytest.param("bash .ai-toolkit/scripts/spoke-ready.sh --gate 173", id="spoke-ready-gate"),
    ],
)
def test_parked_allows_reads_and_marker(parked_spoke: Path, command: str) -> None:
    result = run_guard(_bash_payload(command), parked_spoke)

    assert result.returncode == ALLOW, result.stdout + result.stderr


# ── Un-blocking: tag consumed or tip advanced ─────────────


def test_allows_writes_when_no_gate_tag(spoke: Path) -> None:
    # No gate tag was ever emitted → not parked → writes flow.
    assert run_guard(_edit_payload("Write"), spoke).returncode == ALLOW
    assert run_guard(_bash_payload("git commit -m x"), spoke).returncode == ALLOW


def test_allows_writes_when_tag_consumed(parked_spoke: Path) -> None:
    # The gate answer path deletes the tag (_consume_gate_tag) → un-blocked.
    _git(parked_spoke, "tag", "-d", f"gate/{ISSUE}")

    assert run_guard(_edit_payload("Write"), parked_spoke).returncode == ALLOW
    assert run_guard(_bash_payload("git commit -m x"), parked_spoke).returncode == ALLOW


def test_allows_writes_when_tip_advances(parked_spoke: Path) -> None:
    # Once the tip moves past the gate commit the tag is no longer at the tip.
    (parked_spoke / "more.txt").write_text("more\n")
    _git(parked_spoke, "add", "more.txt")
    _git(parked_spoke, "commit", "-qm", "feat: more", "-m", f"Refs #{ISSUE}")

    assert run_guard(_edit_payload("Write"), parked_spoke).returncode == ALLOW


def test_ignores_gate_tag_for_a_different_issue(spoke: Path) -> None:
    # The issue is parsed from the branch slug (173); a stray gate/999 at the tip
    # is not THIS spoke's gate and must not block.
    _git(spoke, "tag", "-a", "gate/999", "-m", "plan")

    assert run_guard(_edit_payload("Write"), spoke).returncode == ALLOW


# ── Hub / non-spoke: pure no-op (fail-open) ───────────────


def test_hub_is_noop_even_with_stray_gate_tag(hub: Path) -> None:
    # On `main` the slug has no leading issue number → nothing to enforce, even
    # if a gate tag somehow sits at the tip.
    _git(hub, "tag", "-a", "gate/0", "-m", "plan")

    assert run_guard(_edit_payload("Write"), hub).returncode == ALLOW
    assert run_guard(_bash_payload("git commit -m x"), hub).returncode == ALLOW


def test_outside_git_repo_is_noop(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert run_guard(_edit_payload("Write"), plain).returncode == ALLOW
