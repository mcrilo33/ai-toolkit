"""Unit tests for scripts/worktree-quick.sh — the /quick express lane (issue #89).

worktree-quick.sh is a trimmed worktree-new.sh: it creates an isolated worktree
on a `quick/<slug>` (or `chore/<slug>`) branch, copies the gitignored `.claude/`
runtime config, mints the `spoke_run_id`, and sets the `.ai-toolkit/` git exclude
— exactly like worktree-new.sh — but DOES NOT create an issue, seed a kickoff
prompt, spawn a tmux window, or launch a separate `claude` agent. The current
hub session enters the printed worktree path itself.

To let that hub session drive commits into the worktree (the hub-guard otherwise
denies a commit run with the hub's cwd on the default branch), the script drops
the explicit `hub-guard-allow` escape-hatch marker in the common git-dir — the
same file hub-guard.sh honors.

A logging `tmux` stub on PATH keeps the test hermetic and lets us assert the
script never touches tmux (no window, no kickoff).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

WORKTREE_QUICK = Path(__file__).resolve().parents[2] / "scripts" / "worktree-quick.sh"

# Pin git config to nothing so a host's global config never reaches the commits
# the tests drive (this repo itself ships installable git hooks).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote and a
    gitignored `.claude/` runtime dir to copy."""
    base = tmp_path
    remote = base / "hub-remote.git"
    hub = base / "hub"
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
    # A representative .claude/ runtime config (gitignored; copied verbatim).
    (hub / ".claude" / "skills").mkdir(parents=True)
    (hub / ".claude" / "settings.json").write_text("{}\n")
    return hub


def _run_quick(
    hub: Path,
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    stub_curl: bool = False,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-quick.sh from the hub with a logging `tmux` stub on PATH.

    The stub records every invocation so a test can assert the script NEVER
    drives tmux. Returns the completed process and the tmux call-log path.

    Telemetry isolation (issue #127): the script resolves Langfuse auth itself,
    so the harness pins AFK_TELEMETRY_CONF to a nonexistent sandbox path and
    strips the LANGFUSE_* / span-endpoint env (belt to the conftest pin); a test
    that wants auth opts in via `extra_env` with its own tmp conf. `stub_curl`
    captures OTLP span POSTs (argv, then the stdin payload) into
    ``tmp_path / "curl-calls.log"`` so nothing is ever sent.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "tmux-calls.log"
    log.touch()
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [ "$1" = "new-window" ]; then printf "@1\\n"; fi\n'
        "exit 0\n"
    )
    tmux.chmod(0o755)
    if stub_curl:
        curl_log = tmp_path / "curl-calls.log"
        curl = bindir / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            f'printf "ARGV %s\\n" "$*" >> "{curl_log}"\n'
            f'cat >> "{curl_log}"\nprintf "\\n" >> "{curl_log}"\n'
            "exit 0\n"
        )
        curl.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    env.pop("WT_SPOKE", None)
    for var in ("LANGFUSE_BASIC_AUTH", "LANGFUSE_HOST", "AI_TOOLKIT_OTEL_SPAN_ENDPOINT"):
        env.pop(var, None)
    env["AFK_TELEMETRY_CONF"] = str(tmp_path / "no-such-conf")
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(WORKTREE_QUICK), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _branches(hub: Path) -> list[str]:
    return _git(hub, "branch", "--format=%(refname:short)").split()


def _common_git_dir(hub: Path) -> Path:
    return Path(_git(hub, "rev-parse", "--absolute-git-dir").strip())


def _worktree_dir(hub: Path, slug: str) -> Path:
    return hub.parent / f"{hub.name}-{slug}"


def test_creates_worktree_on_quick_branch(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert "quick/fix-typo" in _branches(hub)
    assert _worktree_dir(hub, "fix-typo").is_dir()


def test_chore_type_creates_chore_branch(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "bump-dep", "-t", "chore")

    assert proc.returncode == 0, proc.stderr
    assert "chore/bump-dep" in _branches(hub)


def test_mints_spoke_run_id(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    run_id = (_worktree_dir(hub, "fix-typo") / ".ai-toolkit" / "spoke-run-id").read_text().strip()
    assert run_id.startswith("quick/fix-typo+")


def test_stamps_lane_quick(hub: Path, tmp_path: Path) -> None:
    # The /quick express lane tags its trace `lane=quick` (issue #102).
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    lane = (_worktree_dir(hub, "fix-typo") / ".ai-toolkit" / "lane").read_text().strip()
    assert lane == "quick"


def test_stamps_mode_attended(hub: Path, tmp_path: Path) -> None:
    # /quick is always human-driven, so its mode is `attended` (issue #102).
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    mode = (_worktree_dir(hub, "fix-typo") / ".ai-toolkit" / "mode").read_text().strip()
    assert mode == "attended"


def test_sets_ai_toolkit_exclude(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    wt = _worktree_dir(hub, "fix-typo")
    exclude = Path(_git(wt, "rev-parse", "--git-path", "info/exclude").strip())
    content = exclude.read_text()
    assert ".ai-toolkit/" in content
    # .claude/ rides the same exclude: the copied runtime config must never
    # count as untracked dirt at teardown/land (issue #132).
    assert ".claude/" in content


def test_copies_claude_runtime_config(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert (_worktree_dir(hub, "fix-typo") / ".claude" / "settings.json").is_file()


def test_drops_hub_guard_allow_marker(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert (_common_git_dir(hub) / "hub-guard-allow").exists()


def test_does_not_touch_tmux(hub: Path, tmp_path: Path) -> None:
    # No kickoff, no separate session: the script must never invoke tmux.
    proc, log = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert log.read_text() == ""


def test_does_not_launch_an_agent(hub: Path, tmp_path: Path) -> None:
    # The current session enters the worktree; no `claude` agent is launched.
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert "claude --model" not in proc.stdout


def test_prints_worktree_path(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert str(_worktree_dir(hub, "fix-typo")) in proc.stdout


def test_rejects_unknown_type(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_quick(hub, tmp_path, "fix-typo", "-t", "feature")

    assert proc.returncode != 0
    assert "type" in proc.stderr.lower()


# --- no inherited upstream (issue #120) ------------------------------------------


def test_quick_branch_has_no_upstream(hub: Path, tmp_path: Path) -> None:
    # Branching from origin/<base> must not auto-set it as upstream: a quick
    # branch is never pushed, and an inherited upstream trips the
    # worktree-land.sh --local micro-spoke guard (issue #120).
    proc, _ = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
        cwd=str(_worktree_dir(hub, "fix-typo")),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    assert upstream.returncode != 0, f"expected no upstream, got: {upstream.stdout.strip()}"


# --- configurable base branch (issue #117) --------------------------------------


def test_quick_branches_from_configured_base(hub: Path, tmp_path: Path) -> None:
    # The quick lane branches from the resolved base too (origin/<base> when
    # pushed), not from the hub's current HEAD.
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "push", "-q", "-u", "origin", "develop")
    develop_tip = _git(hub, "rev-parse", "HEAD").strip()
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")

    proc, _ = _run_quick(hub, tmp_path, "cfg-base")

    assert proc.returncode == 0, proc.stderr
    wt = hub.parent / f"{hub.name}-cfg-base"
    assert _git(wt, "rev-parse", "HEAD").strip() == develop_tip


# --- hub-side Langfuse auth resolution (issue #127) ------------------------------
# The quick lane never launches an OTel'd claude, so its only Langfuse footprint
# is the spawn lifecycle/script span pair emitted at the end of the script. The
# script resolves auth itself (wt_resolve_langfuse_auth: env wins, then
# ${AFK_TELEMETRY_CONF:-~/.afk-telemetry}) so those spans get
# AI_TOOLKIT_OTEL_SPAN_ENDPOINT and reach the collector from any hub session;
# unresolvable auth leaves the sink dark and the spawn untouched.


def _wait_for_content(log: Path, needle: str, tries: int = 40) -> str:
    """Poll a detached-writer log until `needle` appears (or ~4s elapse).

    The OTLP span sink runs curl backgrounded and disowned, so its stub may
    still be writing after the quick script has exited.
    """
    for _ in range(tries):
        text = log.read_text() if log.exists() else ""
        if needle in text:
            return text
        time.sleep(0.1)
    return log.read_text() if log.exists() else ""


def test_quick_spawn_span_posted_when_conf_present(hub: Path, tmp_path: Path) -> None:
    # Conf present + fresh hub env ⇒ the spawn span pair must POST to the
    # defaulted OTLP endpoint, and the credential must never surface — not on
    # the curl argv/payload, not in the script's own output.
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc, _ = _run_quick(
        hub,
        tmp_path,
        "fix-typo",
        stub_curl=True,
        extra_env={"AFK_TELEMETRY_CONF": str(conf)},
    )

    assert proc.returncode == 0, proc.stderr
    curl_log = _wait_for_content(tmp_path / "curl-calls.log", "worktree-quick")
    assert "/v1/traces" in curl_log, "spawn span must POST to the OTLP traces endpoint"
    assert "http://localhost:4318" in curl_log, "endpoint defaults to the local collector"
    assert "worktree-quick" in curl_log, "the quick script span carries its script name"
    assert "Basic-test-127" not in curl_log, "credential must never reach the curl argv/payload"
    assert "Basic-test-127" not in proc.stdout + proc.stderr, (
        "credential must never surface in the script output"
    )


def test_quick_emits_no_span_when_auth_unresolvable(hub: Path, tmp_path: Path) -> None:
    # No conf + no env ⇒ nothing exported, the sink stays dark, and the spawn
    # still succeeds — resolution is best-effort, never a spawn guard.
    proc, _ = _run_quick(hub, tmp_path, "fix-typo", stub_curl=True)

    assert proc.returncode == 0, proc.stderr
    curl_log = tmp_path / "curl-calls.log"
    assert not curl_log.exists() or curl_log.read_text() == "", (
        "no span POST may fire without resolved auth"
    )
