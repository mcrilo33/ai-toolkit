"""Shared gate-broker test fixtures (issue #275 partition).

Auto-discovered by every test_gate_broker*.py; helper deps come from
_gate_broker_support."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from _gate_broker_support import (
    _ask_record,
    _install_fake_claude,
    _project_dir_for,
)


@pytest.fixture
def spoke_repo(tmp_path: Path) -> Path:
    wt = tmp_path / "spoke"
    wt.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    # Commit a .gitignore modelling the production spoke worktree: the runtime artifacts a
    # parked spoke writes (`.testmondata*`, OTel dumps under `.ai-toolkit/`) are IGNORED, so
    # the untracked-not-ignored fingerprint (#203) never blames them on the reasoner.
    (wt / ".gitignore").write_text(".testmondata*\n.ai-toolkit/\n.venv/\n")
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True, env=env, capture_output=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=wt, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=wt, check=True, env=env, capture_output=True
    )
    return wt


@pytest.fixture
def linked_spoke_repo(tmp_path: Path) -> Path:
    """A REAL linked worktree as the spoke: its `.git` is a gitfile pointing at the shared
    common gitdir (`git worktree add`), the production shape #237's `spoke_repo` (a
    standalone `git init`, `.git` a directory) never models. Commit subjects are
    conventional — the repo's commit-quality hook rejects a bare subject."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    main = tmp_path / "main"
    main.mkdir()
    (main / ".gitignore").write_text(".testmondata*\n.ai-toolkit/\n.venv/\n")
    subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env, capture_output=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=main, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: init"],
        cwd=main,
        check=True,
        env=env,
        capture_output=True,
    )
    wt = tmp_path / "spoke"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature/x", str(wt)],
        cwd=main,
        check=True,
        env=env,
        capture_output=True,
    )
    assert (wt / ".git").is_file(), "the spoke's .git must be a gitfile (linked worktree)"
    return wt


@pytest.fixture
def waiting_spoke_env(tmp_path: Path, spoke_repo: Path) -> dict[str, str]:
    """A spoke parked on a question + a recording spoke-ready stub + a fake gh."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n"
    )

    ready_log = tmp_path / "ready.log"
    ready_stub = tmp_path / "spoke-ready.sh"
    ready_stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{ready_log}"\n')
    ready_stub.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text('#!/usr/bin/env bash\necho "Title\\n\\nbody"\n')
    (fake_bin / "gh").chmod(0o755)

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "SPOKE_READY": str(ready_stub),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "_READY_LOG": str(ready_log),
    }


@pytest.fixture
def reasoner_env(spoke_repo: Path, tmp_path: Path) -> dict[str, str]:
    """A spoke parked on a question + a fake `claude` reasoner on PATH (default command)."""
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    spoke_jsonl = pd / "session.jsonl"
    spoke_jsonl.write_text(json.dumps(_ask_record("Which store?", [("Redis", "fast")])) + "\n")
    os.utime(spoke_jsonl, (1_000_000_000, 1_000_000_000))

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_claude(fake_bin, "ANSWER: go ahead")

    return {
        "CLAUDE_PROJECTS_DIR": str(projects),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "_SPOKE_JSONL": str(spoke_jsonl),
        "_FAKE_BIN": str(fake_bin),
    }


@pytest.fixture
def afk_spoke(spoke_repo: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A spoke on an issue-numbered branch with a LIVE afk heartbeat — the hook's gate wide open.

    The heartbeat names THIS pytest process's pid, which is alive for the duration of the
    ``_call`` subprocess, so the hook's ``kill -0`` liveness probe succeeds.
    """
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/253-hook"],
        cwd=spoke_repo,
        check=True,
        env=git_env,
        capture_output=True,
    )
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(f"{os.getpid()} 1000 wake1\n")
    statedir = tmp_path / "afk-state"
    statedir.mkdir()
    env = {
        "AFK_HEARTBEAT": str(heartbeat),
        "AFK_STATE_DIR": str(statedir),
        "AFK_TASKS_ROOT": str(tmp_path / "tasks"),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_NOW": "1000",
    }
    return spoke_repo, env


@pytest.fixture
def afk_bypass_spoke(spoke_repo: Path, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A spoke on an issue-numbered branch with .ai-toolkit/mode == afk (launched under bypass)."""
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature/261-wall"],
        cwd=spoke_repo,
        check=True,
        env=git_env,
        capture_output=True,
    )
    (spoke_repo / ".ai-toolkit").mkdir(exist_ok=True)
    (spoke_repo / ".ai-toolkit" / "mode").write_text("afk\n")
    env = {
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_JOURNAL_GH_COMMENT": "0",
        "AFK_TASKS_ROOT": str(tmp_path / "tasks"),
        # A safe-verdict judge stub by default; deny/timeout tests override it.
        "AFK_JUDGE_CMD": "printf 'VERDICT: safe\\n'",
    }
    return spoke_repo, env
