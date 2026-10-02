"""Unit tests for scripts/worktree-quick.sh — the /quick express lane (issues #89, #363).

worktree-quick.sh creates an isolated worktree with `orca worktree create` (no agent: the
current hub session drives it), renames the branch to `quick/<slug>` (or `chore/<slug>`), drops
its upstream (#120), provisions it synchronously as `lane=quick, mode=attended`, and grants the
hub-guard escape hatch so the hub session can commit into it. It never creates an issue, seeds
a prompt, or launches an agent. `orca` is a PATH stub (`_orca_stub.py`); `tmux`, `code` and
`git worktree add` fail the test if invoked.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from _orca_stub import install_forbidden_stubs, install_orca_stub, orca_calls, stub_env

WORKTREE_QUICK = Path(__file__).resolve().parents[2] / "scripts" / "worktree-quick.sh"

# Pin git config to nothing so a host's global config never reaches the commits the tests drive
# (this repo itself ships installable git hooks); the host's base-branch override (#117) must
# never steer the script under test.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout on `main` with an `origin` bare remote and a `.claude/` dir to copy."""
    remote, hub = tmp_path / "hub-remote.git", tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=_GIT_ENV)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, env=_GIT_ENV)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    (hub / ".claude" / "skills").mkdir(parents=True)
    (hub / ".claude" / "settings.json").write_text("{}\n")
    return hub


def _run_quick(
    hub: Path,
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    stub_curl: bool = False,
    version: str = "1.4.218",
) -> subprocess.CompletedProcess:
    """Run worktree-quick.sh from the hub against the stubbed `orca`.

    Telemetry isolation (#127): the script resolves Langfuse auth itself, so the harness pins
    AFK_TELEMETRY_CONF to a nonexistent path and strips the LANGFUSE_* / span-endpoint env; a
    test that wants auth opts in via `extra_env`. `stub_curl` captures OTLP span POSTs (argv,
    then the stdin payload) into `tmp_path/curl-calls.log` so nothing is ever sent.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    install_forbidden_stubs(bindir)
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
    env = {**_GIT_ENV, **install_orca_stub(bindir, version=version)}
    for var in (
        "TMUX",
        "WT_SPOKE",
        "LANGFUSE_BASIC_AUTH",
        "LANGFUSE_HOST",
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    ):
        env.pop(var, None)
    env["AFK_TELEMETRY_CONF"] = str(tmp_path / "no-such-conf")
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(WORKTREE_QUICK), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=stub_env(bindir, env),
    )


def _wt(tmp_path: Path, slug: str) -> Path:
    return tmp_path / "orca-ws" / slug


def _pointer(tmp_path: Path, slug: str, name: str) -> str:
    return (_wt(tmp_path, slug) / ".ai-toolkit" / name).read_text().strip()


def test_creates_the_worktree_through_orca_without_an_agent(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    create = next(c for c in orca_calls(tmp_path / "bin") if c[:2] == ["worktree", "create"])
    assert create[create.index("--name") + 1] == "fix-typo"
    assert create[create.index("--setup") + 1] == "skip"
    assert "--no-parent" in create
    assert "--agent" not in create
    assert (tmp_path / "bin" / "forbidden.log").read_text() == ""
    assert "claude --model" not in proc.stdout


@pytest.mark.parametrize(
    ("args", "branch"),
    [(("fix-typo",), "quick/fix-typo"), (("bump-dep", "-t", "chore"), "chore/bump-dep")],
)
def test_branch_is_renamed_to_type_slash_slug(
    hub: Path, tmp_path: Path, args: tuple, branch: str
) -> None:
    proc = _run_quick(hub, tmp_path, *args)

    assert proc.returncode == 0, proc.stderr
    assert _git(_wt(tmp_path, args[0]), "branch", "--show-current").strip() == branch


def test_provisions_lane_quick_mode_attended_and_a_run_id(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert _pointer(tmp_path, "fix-typo", "lane") == "quick"
    assert _pointer(tmp_path, "fix-typo", "mode") == "attended"
    assert _pointer(tmp_path, "fix-typo", "spoke-run-id").startswith("quick/fix-typo+")
    identity = _pointer(tmp_path, "fix-typo", "identity")
    assert "lane=quick" in identity
    assert f"orca_worktree_id=stub-repo::{_wt(tmp_path, 'fix-typo')}" in identity


def test_sets_the_excludes_and_copies_the_claude_runtime_config(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    wt = _wt(tmp_path, "fix-typo")
    exclude = Path(_git(wt, "rev-parse", "--git-path", "info/exclude").strip()).read_text()
    assert ".ai-toolkit/" in exclude and ".claude/" in exclude
    assert (wt / ".claude" / "settings.json").is_file()


def test_drops_the_hub_guard_allow_marker(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert (Path(_git(hub, "rev-parse", "--absolute-git-dir").strip()) / "hub-guard-allow").exists()


def test_prints_the_worktree_path_last(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[-1] == str(_wt(tmp_path, "fix-typo"))


def test_rejects_unknown_type(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo", "-t", "feature")

    assert proc.returncode != 0
    assert "type" in proc.stderr.lower()


def test_existing_branch_dies_before_asking_orca(hub: Path, tmp_path: Path) -> None:
    _git(hub, "branch", "quick/fix-typo")

    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode != 0
    assert not [c for c in orca_calls(tmp_path / "bin") if c[:2] == ["worktree", "create"]]


def test_refuses_below_the_orca_version_floor(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo", version="1.4.217")

    assert proc.returncode != 0
    assert "1.4.218" in proc.stderr
    assert not (tmp_path / "orca-ws").exists()


def test_quick_branch_has_no_upstream(hub: Path, tmp_path: Path) -> None:
    # An inherited upstream trips the worktree-land.sh --local micro-spoke guard (issue #120).
    proc = _run_quick(hub, tmp_path, "fix-typo")

    assert proc.returncode == 0, proc.stderr
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
        cwd=str(_wt(tmp_path, "fix-typo")),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    assert upstream.returncode != 0, f"expected no upstream, got: {upstream.stdout.strip()}"


def test_quick_branches_from_the_configured_base(hub: Path, tmp_path: Path) -> None:
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "push", "-q", "-u", "origin", "develop")
    develop_tip = _git(hub, "rev-parse", "HEAD").strip()
    _git(hub, "checkout", "-q", "main")
    _git(hub, "config", "ai-toolkit.base-branch", "develop")

    proc = _run_quick(hub, tmp_path, "cfg-base")

    assert proc.returncode == 0, proc.stderr
    assert _git(_wt(tmp_path, "cfg-base"), "rev-parse", "HEAD").strip() == develop_tip


# --- hub-side Langfuse auth resolution (issue #127) ------------------------------
# The quick lane never launches an OTel'd claude, so its only Langfuse footprint is the spawn
# lifecycle/script span pair. The script resolves auth itself (env wins, then
# ${AFK_TELEMETRY_CONF:-~/.afk-telemetry}); unresolvable auth leaves the sink dark.


def _wait_for_content(log: Path, needle: str, tries: int = 40) -> str:
    """Poll a detached-writer log until `needle` appears (the OTLP sink's curl is disowned)."""
    for _ in range(tries):
        text = log.read_text() if log.exists() else ""
        if needle in text:
            return text
        time.sleep(0.1)
    return log.read_text() if log.exists() else ""


def test_quick_spawn_span_posted_when_conf_present(hub: Path, tmp_path: Path) -> None:
    conf = tmp_path / "afk-telemetry"
    conf.write_text('LANGFUSE_BASIC_AUTH="Basic-test-127"\n')

    proc = _run_quick(
        hub, tmp_path, "fix-typo", stub_curl=True, extra_env={"AFK_TELEMETRY_CONF": str(conf)}
    )

    assert proc.returncode == 0, proc.stderr
    curl_log = _wait_for_content(tmp_path / "curl-calls.log", "worktree-quick")
    assert "/v1/traces" in curl_log and "http://localhost:4318" in curl_log
    assert "worktree-quick" in curl_log
    assert "Basic-test-127" not in curl_log + proc.stdout + proc.stderr


def test_quick_emits_no_span_when_auth_unresolvable(hub: Path, tmp_path: Path) -> None:
    proc = _run_quick(hub, tmp_path, "fix-typo", stub_curl=True)

    assert proc.returncode == 0, proc.stderr
    curl_log = tmp_path / "curl-calls.log"
    assert not curl_log.exists() or curl_log.read_text() == ""
