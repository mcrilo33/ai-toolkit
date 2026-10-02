"""Unit tests for scripts/provision-worktree.sh (issue #359, Orca migration S1).

The script provisions the policy layer of a worktree (info/exclude, .testmondata pre-warm,
.ai-toolkit/*, the .claude/ tree, settings.local.json allow/deny rules, the spoke OTel env)
and is shared by worktree-new.sh (tmux) and Orca's setup hook. Inputs come from
ORCA_ROOT_PATH / ORCA_WORKTREE_PATH / ORCA_WORKSPACE_NAME, or explicit flags with no Orca.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision-worktree.sh"

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
for _leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
    _GIT_ENV.pop(_leak, None)
for _k in (
    "AI_TOOLKIT_BASE_BRANCH",
    "ORCA_ROOT_PATH",
    "ORCA_WORKTREE_PATH",
    "ORCA_WORKSPACE_NAME",
):
    _GIT_ENV.pop(_k, None)
# OTel is default-on in the hub; keep the base fixtures off it so env-block tests opt in.
_GIT_ENV["AI_TOOLKIT_OTEL"] = "0"

BRANCH = "feature/359-extract"
TITLE = "refactor(worktree): extract provision"
BODY = "## Context\nbody line\n\nScope: scripts/x.sh\nGate: none\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _exclude_file(wt: Path) -> Path:
    out = _git(wt, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    return Path(out.strip())


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=_GIT_ENV)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, env=_GIT_ENV)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    claude = hub / ".claude"
    (claude / "skills" / "s").mkdir(parents=True)
    (claude / "skills" / "s" / "SKILL.md").write_text("skill\n")
    (claude / ".review").mkdir()
    (claude / ".review" / "approval.json").write_text("{}")
    (claude / "worktrees" / "w").mkdir(parents=True)
    (claude / "worktrees" / "w" / "f").write_text("x")
    (claude / "settings.json.bak").write_text("bak")
    (claude / "settings.json").write_text('{"hooks": {}}\n')
    return hub


@pytest.fixture()
def wt(hub: Path, tmp_path: Path) -> Path:
    """A bare `git worktree add` tree: no .claude/, no .ai-toolkit/."""
    path = tmp_path / "hub-359"
    _git(hub, "worktree", "add", "-q", str(path), "-b", BRANCH, "main")
    return path


@pytest.fixture()
def stubs(tmp_path: Path) -> Path:
    """gh + orca stubs: a complete issue, and an orca with both worker skills installed."""
    bin_dir = tmp_path / "stubs"
    body = BODY.replace("\n", "\\n")
    _stub(
        bin_dir,
        "gh",
        f'case "$*" in *title*) printf "%s\\n" "{TITLE}";; *body*) printf "%b" "{body}";; esac',
    )
    _stub(bin_dir, "orca", 'printf \'[{"name":"orca-cli"},{"name":"orchestration"}]\\n\'')
    return bin_dir


def _run(
    cwd: Path, stubs: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = {**_GIT_ENV, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", **(env or {})}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=run_env,
        timeout=120,
    )


def _orca_env(hub: Path, wt: Path) -> dict[str, str]:
    return {
        "ORCA_ROOT_PATH": str(hub),
        "ORCA_WORKTREE_PATH": str(wt),
        "ORCA_WORKSPACE_NAME": "359-extract",
    }


def _explicit(hub: Path, wt: Path, mode: str = "attended") -> list[str]:
    return [
        *("--worktree", str(wt), "--repo-root", str(hub), "--issue", "359"),
        *("--lane", "spoke", "--mode", mode),
    ]


def _snapshot(wt: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for sub in (".ai-toolkit", ".claude"):
        for p in sorted((wt / sub).rglob("*")):
            if p.is_file():
                files[str(p.relative_to(wt))] = p.read_bytes()
    files["info/exclude"] = _exclude_file(wt).read_bytes()
    return files


def _settings(wt: Path) -> dict:
    return json.loads((wt / ".claude" / "settings.local.json").read_text())


def _assert_fully_gated(wt: Path, mode: str = "attended") -> None:
    lines = _exclude_file(wt).read_text().splitlines()
    for entry in (".ai-toolkit/", ".claude/", ".testmondata*"):
        assert lines.count(entry) == 1, entry
    assert (wt / ".ai-toolkit" / "spoke-run-id").read_text().startswith(f"{BRANCH}+")
    assert (wt / ".ai-toolkit" / "lane").read_text() == "spoke\n"
    assert (wt / ".ai-toolkit" / "mode").read_text() == f"{mode}\n"
    task = (wt / ".ai-toolkit" / "task.md").read_text()
    assert task.startswith(f"# Issue #359: {TITLE}\n\n")
    assert "Gate: none" in task
    ledger = (wt / ".ai-toolkit" / "ledger-skeleton.md").read_text()
    assert ledger.count("#359.main · RED") == 1
    assert (wt / ".claude" / "skills" / "s" / "SKILL.md").is_file()
    assert (wt / ".claude" / "settings.json").is_file()
    for excluded in (".review", "worktrees", "settings.json.bak"):
        assert not (wt / ".claude" / excluded).exists(), excluded
    allow = _settings(wt)["permissions"]["allow"]
    assert any("spoke-push.sh" in r for r in allow)
    assert "Bash(git status:*)" in allow
    deny = _settings(wt)["permissions"].get("deny", [])
    assert ("AskUserQuestion" in deny) == (mode == "afk")


def test_script_exists_is_executable_and_parses() -> None:
    assert SCRIPT.is_file(), "scripts/provision-worktree.sh missing"
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_orca_env_alone_turns_a_bare_worktree_into_a_gated_spoke(
    hub: Path, wt: Path, stubs: Path
) -> None:
    result = _run(wt, stubs, env=_orca_env(hub, wt))

    assert result.returncode == 0, result.stderr
    _assert_fully_gated(wt)


def test_explicit_args_without_orca_env_provision_identically(
    hub: Path, wt: Path, stubs: Path, tmp_path: Path
) -> None:
    result = _run(tmp_path, stubs, *_explicit(hub, wt))

    assert result.returncode == 0, result.stderr
    _assert_fully_gated(wt)


def test_afk_mode_seeds_the_askuserquestion_deny(hub: Path, wt: Path, stubs: Path) -> None:
    result = _run(wt, stubs, *_explicit(hub, wt, "afk"))

    assert result.returncode == 0, result.stderr
    _assert_fully_gated(wt, mode="afk")


def test_second_run_is_a_no_op(hub: Path, wt: Path, stubs: Path) -> None:
    assert _run(wt, stubs, env=_orca_env(hub, wt)).returncode == 0
    before = _snapshot(wt)

    result = _run(wt, stubs, env=_orca_env(hub, wt))

    assert result.returncode == 0, result.stderr
    assert _snapshot(wt) == before


def test_rerun_preserves_a_user_curated_settings_file(hub: Path, wt: Path, stubs: Path) -> None:
    assert _run(wt, stubs, env=_orca_env(hub, wt)).returncode == 0
    path = wt / ".claude" / "settings.local.json"
    data = json.loads(path.read_text())
    data["permissions"]["allow"].insert(0, "Bash(custom:*)")
    data["permissions"]["deny"] = ["Bash(rm:*)"]
    path.write_text(json.dumps(data))

    assert _run(wt, stubs, env=_orca_env(hub, wt)).returncode == 0

    perms = _settings(wt)["permissions"]
    assert perms["allow"][0] == "Bash(custom:*)"
    assert len(perms["allow"]) == len(set(perms["allow"]))
    assert perms["deny"] == ["Bash(rm:*)"]


def test_claude_copy_failure_exits_nonzero_loudly(hub: Path, wt: Path, stubs: Path) -> None:
    _stub(stubs, "rsync", "echo boom >&2; exit 23")

    result = _run(wt, stubs, env=_orca_env(hub, wt))

    assert result.returncode != 0
    assert ".claude" in result.stderr


def test_missing_gh_leaves_no_task_contract_but_still_succeeds(
    hub: Path, wt: Path, stubs: Path
) -> None:
    _stub(stubs, "gh", "exit 1")

    result = _run(wt, stubs, env=_orca_env(hub, wt))

    assert result.returncode == 0, result.stderr
    assert not (wt / ".ai-toolkit" / "task.md").exists()
    assert (wt / ".ai-toolkit" / "lane").is_file()


def test_hub_testmon_baseline_is_prewarmed(hub: Path, wt: Path, stubs: Path) -> None:
    (hub / ".git" / ".testmondata-baseline").write_bytes(b"baseline")

    result = _run(wt, stubs, env=_orca_env(hub, wt))

    assert result.returncode == 0, result.stderr
    assert (wt / ".testmondata").read_bytes() == b"baseline"
