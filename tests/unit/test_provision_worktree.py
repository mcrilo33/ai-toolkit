"""Unit tests for scripts/provision-worktree.sh (issue #359, Orca migration S1).

The script provisions the policy layer of a worktree (info/exclude, .testmondata pre-warm,
.ai-toolkit/*, the .claude/ tree, settings.local.json allow/deny rules, the spoke OTel env)
and is shared by worktree-new.sh (tmux) and Orca's setup hook. Inputs come from
ORCA_ROOT_PATH / ORCA_WORKTREE_PATH / ORCA_WORKSPACE_NAME, or explicit flags with no Orca.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
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
    "ORCA_WORKTREE_ID",
    "ORCA_DISPATCH_ID",
):
    _GIT_ENV.pop(_k, None)
# OTel is default-on in the hub; keep the base fixtures off it so env-block tests opt in.
_GIT_ENV["AI_TOOLKIT_OTEL"] = "0"

HUB_SETTINGS_LOCAL = '{"permissions": {"allow": ["Bash(hub:*)"]}}\n'
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


# ── spoke OTel env block (issue #359) ─────────────────────────────────────────

_OTEL_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "BETA_TRACING_ENDPOINT",
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT",
)
_OTEL_OVERRIDES = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:1",
    "BETA_TRACING_ENDPOINT": "http://y:2",
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": "http://z:3",
}
WT_LIB = SCRIPT.parent / "worktree-lib.sh"


def _otel_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in _GIT_ENV.items() if k not in _OTEL_ENDPOINT_VARS}
    return {**env, "AI_TOOLKIT_OTEL": "1", **(extra or {})}


def _prefix_pairs(run_id: str, body_dir: Path, repo: str, env: dict[str, str]) -> dict[str, str]:
    """The launch prefix worktree-new/spoke-relaunch splice onto the claude command."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{WT_LIB}"; wt_native_otel_prefix "$1" "$2" "$3"',
            "_",
            run_id,
            str(body_dir),
            repo,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return dict(tok.split("=", 1) for tok in shlex.split(result.stdout))


def _run_with_otel(
    hub: Path, wt: Path, stubs: Path, extra: dict[str, str] | None = None, *flags: str
) -> subprocess.CompletedProcess[str]:
    env = {**_otel_env(extra), **_orca_env(hub, wt)}
    return _run(wt, stubs, *flags, env=env)


def _spoke_env(pairs: dict[str, str]) -> dict[str, str]:
    """The settings env block: WT_SPOKE (the issue tag) plus the launch-prefix pairs."""
    return {"WT_SPOKE": "359", **pairs}


def test_otel_off_writes_only_wt_spoke_to_the_env_block(hub: Path, wt: Path, stubs: Path) -> None:
    result = _run(wt, stubs, env={**_orca_env(hub, wt), "AI_TOOLKIT_OTEL": "0"})

    assert result.returncode == 0, result.stderr
    assert _settings(wt)["env"] == {"WT_SPOKE": "359"}


def test_an_express_lane_tags_wt_spoke_with_its_slug(hub: Path, wt: Path, stubs: Path) -> None:
    args = ["--worktree", str(wt), "--repo-root", str(hub), "--issue", "fix-typo"]

    result = _run(wt, stubs, *args, "--lane", "express")

    assert result.returncode == 0, result.stderr
    assert _settings(wt)["env"]["WT_SPOKE"] == "fix-typo"


def test_env_block_holds_exactly_the_launch_prefix_pairs(hub: Path, wt: Path, stubs: Path) -> None:
    result = _run_with_otel(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    run_id = (wt / ".ai-toolkit" / "spoke-run-id").read_text().strip()
    expected = _prefix_pairs(run_id, wt / ".ai-toolkit" / "raw-bodies", "remote", _otel_env())
    assert _settings(wt)["env"] == _spoke_env(expected)
    assert _settings(wt)["env"]["OTEL_RESOURCE_ATTRIBUTES"] == f"spoke_run_id={run_id},repo=remote"
    assert (wt / ".ai-toolkit" / "raw-bodies").is_dir()


def test_env_block_matches_the_prefix_with_overridden_endpoints(
    hub: Path, wt: Path, stubs: Path
) -> None:
    result = _run_with_otel(hub, wt, stubs, _OTEL_OVERRIDES)

    assert result.returncode == 0, result.stderr
    run_id = (wt / ".ai-toolkit" / "spoke-run-id").read_text().strip()
    expected = _prefix_pairs(
        run_id, wt / ".ai-toolkit" / "raw-bodies", "remote", _otel_env(_OTEL_OVERRIDES)
    )
    assert _settings(wt)["env"] == _spoke_env(expected)
    assert _settings(wt)["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://x:1"


def test_explicit_body_dir_and_repo_are_honoured_and_created(
    hub: Path, wt: Path, stubs: Path, tmp_path: Path
) -> None:
    body_dir = tmp_path / "custom bodies"

    result = _run_with_otel(
        hub, wt, stubs, None, "--otel-body-dir", str(body_dir), "--repo-name", "named"
    )

    assert result.returncode == 0, result.stderr
    run_id = (wt / ".ai-toolkit" / "spoke-run-id").read_text().strip()
    assert body_dir.is_dir()
    assert _settings(wt)["env"] == _spoke_env(_prefix_pairs(run_id, body_dir, "named", _otel_env()))


def test_an_empty_repo_name_is_omitted_never_written_empty(
    hub: Path, wt: Path, stubs: Path
) -> None:
    result = _run_with_otel(hub, wt, stubs, None, "--repo-name", "")

    assert result.returncode == 0, result.stderr
    resource = _settings(wt)["env"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert "repo" not in resource
    assert resource.startswith("spoke_run_id=")


def test_env_block_never_carries_secrets(hub: Path, wt: Path, stubs: Path) -> None:
    secrets = {
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic hunter2",
        "LANGFUSE_SECRET_KEY": "sk-lf-hunter2",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-hunter2",
    }

    result = _run_with_otel(hub, wt, stubs, secrets)

    assert result.returncode == 0, result.stderr
    raw = (wt / ".claude" / "settings.local.json").read_text()
    assert "hunter2" not in raw
    assert not set(secrets) & set(_settings(wt)["env"])
    assert len(_settings(wt)["env"]) == 18


def test_env_merge_is_additive_and_idempotent(hub: Path, wt: Path, stubs: Path) -> None:
    assert _run_with_otel(hub, wt, stubs).returncode == 0
    path = wt / ".claude" / "settings.local.json"
    data = json.loads(path.read_text())
    data["env"]["MY_KEY"] = "mine"
    path.write_text(json.dumps(data, indent=2) + "\n")
    before = _snapshot(wt)

    result = _run_with_otel(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert _settings(wt)["env"]["MY_KEY"] == "mine"
    assert "OTEL_RESOURCE_ATTRIBUTES" in _settings(wt)["env"]
    assert _snapshot(wt) == before


def test_env_pairs_helper_is_the_single_source_of_the_prefix(tmp_path: Path) -> None:
    env = _otel_env()
    body_dir = tmp_path / "raw bodies"

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{WT_LIB}"; wt_native_otel_env_pairs "b+1" "$1" "r"',
            "_",
            str(body_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    pairs = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert pairs == _prefix_pairs("b+1", body_dir, "r", env)


# ── Orca worker-skills probe (issue #359) ─────────────────────────────────────


def _path_without(tmp_path: Path, tool: str) -> str:
    """A PATH holding every tool the host has except `tool`: symlink farm of the real PATH."""
    farm = tmp_path / f"no-{tool}-bin"
    farm.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            link = farm / entry.name
            if entry.name != tool and not link.exists() and os.access(entry.path, os.X_OK):
                link.symlink_to(entry.path)
    return str(farm)


def _run_probe(
    hub: Path, wt: Path, stubs: Path, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(wt, stubs, env={**_orca_env(hub, wt), **(extra or {})})


def test_orca_with_both_worker_skills_is_silent_and_succeeds(
    hub: Path, wt: Path, stubs: Path
) -> None:
    result = _run_probe(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert "orca" not in result.stderr.lower().replace("orca_", "")


def test_a_missing_orca_skill_warns_and_still_succeeds(hub: Path, wt: Path, stubs: Path) -> None:
    _stub(stubs, "orca", 'printf \'[{"name":"orca-cli"}]\\n\'')

    result = _run_probe(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert "orchestration" in result.stderr
    assert "orca-cli" not in result.stderr


def test_a_failing_orca_probe_warns_and_still_succeeds(hub: Path, wt: Path, stubs: Path) -> None:
    _stub(stubs, "orca", "echo 'daemon disconnected' >&2; exit 3")

    result = _run_probe(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert "orca skills installed" in result.stderr


def test_unparseable_orca_output_warns_and_still_succeeds(hub: Path, wt: Path, stubs: Path) -> None:
    _stub(stubs, "orca", "echo 'not json {'")

    result = _run_probe(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert "orca skills installed" in result.stderr


def test_a_hung_orca_probe_times_out_with_a_warning(hub: Path, wt: Path, stubs: Path) -> None:
    _stub(stubs, "orca", "sleep 30")

    result = _run_probe(hub, wt, stubs, {"PROVISION_ORCA_TIMEOUT": "1"})

    assert result.returncode == 0, result.stderr
    assert "timed out" in result.stderr


def test_absent_orca_cli_is_silent_and_succeeds(hub: Path, wt: Path, tmp_path: Path) -> None:
    empty_stubs = tmp_path / "gh-only"
    _stub(empty_stubs, "gh", "exit 1")
    env = {
        **_orca_env(hub, wt),
        "PATH": f"{empty_stubs}{os.pathsep}{_path_without(tmp_path, 'orca')}",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(wt),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, **env},
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "orca skills" not in result.stderr
    assert "orchestration" not in result.stderr


# ── review follow-ups (issue #359) ────────────────────────────────────────────


def test_a_successful_run_never_reports_a_provisioning_failure(
    hub: Path, wt: Path, stubs: Path
) -> None:
    # A repo with no origin makes wt_repo_name's pipeline fail inside a $(...) the script
    # tolerates; that must not surface as a false "provisioning FAILED" on a green run.
    _git(hub, "remote", "remove", "origin")

    result = _run_with_otel(hub, wt, stubs)

    assert result.returncode == 0, result.stderr
    assert "FAILED" not in result.stderr


def test_cp_fallback_without_rsync_copies_and_keeps_the_local_settings(
    hub: Path, wt: Path, tmp_path: Path
) -> None:
    gh_only = tmp_path / "gh-only"
    _stub(gh_only, "gh", "exit 1")
    env = {
        **_orca_env(hub, wt),
        "PATH": f"{gh_only}{os.pathsep}{_path_without(tmp_path, 'rsync')}",
    }

    def provision() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=str(wt),
            capture_output=True,
            text=True,
            env={**_GIT_ENV, **env},
            timeout=120,
        )

    first = provision()
    path = wt / ".claude" / "settings.local.json"
    data = json.loads(path.read_text())
    data["permissions"]["allow"].insert(0, "Bash(custom:*)")
    path.write_text(json.dumps(data, indent=2) + "\n")
    # The hub carries its own, different settings.local.json that a naive re-copy would restore.
    (hub / ".claude" / "settings.local.json").write_text(HUB_SETTINGS_LOCAL)
    second = provision()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (wt / ".claude" / "skills" / "s" / "SKILL.md").is_file()
    for excluded in (".review", "worktrees", "settings.json.bak"):
        assert not (wt / ".claude" / excluded).exists(), excluded
    assert _settings(wt)["permissions"]["allow"][0] == "Bash(custom:*)"


def test_cp_fallback_failure_exits_nonzero_and_keeps_the_local_settings(
    hub: Path, wt: Path, tmp_path: Path
) -> None:
    gh_only = tmp_path / "gh-only"
    _stub(gh_only, "gh", "exit 1")
    env = {
        **_GIT_ENV,
        **_orca_env(hub, wt),
        "PATH": f"{gh_only}{os.pathsep}{_path_without(tmp_path, 'rsync')}",
    }
    assert subprocess.run(["bash", str(SCRIPT)], cwd=str(wt), env=env).returncode == 0
    path = wt / ".claude" / "settings.local.json"
    data = json.loads(path.read_text())
    data["permissions"]["allow"].insert(0, "Bash(custom:*)")
    path.write_text(json.dumps(data, indent=2) + "\n")
    (hub / ".claude" / "settings.local.json").write_text(HUB_SETTINGS_LOCAL)
    # A plain file where the hub has a directory makes `cp -R` fail on the re-run.
    shutil.rmtree(wt / ".claude" / "skills")
    (wt / ".claude" / "skills").write_text("conflict")

    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=str(wt), env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "copy failed (cp)" in result.stderr
    assert _settings(wt)["permissions"]["allow"][0] == "Bash(custom:*)"


# --- .ai-toolkit/identity record (#360, S2a) ------------------------------------

IDENTITY_KEYS = [
    "issue",
    "type",
    "slug",
    "mode",
    "lane",
    "spoke_run_id",
    "orca_worktree_id",
    "orca_dispatch_id",
    "run_id",
]


def _identity(wt: Path) -> dict[str, str]:
    text = (wt / ".ai-toolkit" / "identity").read_text()
    assert text.endswith("\n") and "\r" not in text
    pairs = [line.partition("=") for line in text.splitlines()]
    assert all(sep == "=" for _, sep, _ in pairs)
    return {k: v for k, _, v in pairs}


def test_identity_record_holds_all_nine_keys_in_order(hub: Path, wt: Path, stubs: Path) -> None:
    result = _run(wt, stubs, *_explicit(hub, wt, "afk"))

    assert result.returncode == 0, result.stderr
    record = _identity(wt)
    assert list(record) == IDENTITY_KEYS
    assert record["issue"] == "359"
    assert (record["type"], record["slug"]) == ("feature", "359-extract")
    assert (record["mode"], record["lane"]) == ("afk", "spoke")
    assert record["spoke_run_id"] == (wt / ".ai-toolkit" / "spoke-run-id").read_text().strip()


def test_identity_record_leaves_absent_orca_ids_and_run_id_empty(
    hub: Path, wt: Path, stubs: Path
) -> None:
    assert _run(wt, stubs, *_explicit(hub, wt)).returncode == 0

    record = _identity(wt)
    assert (record["orca_worktree_id"], record["orca_dispatch_id"], record["run_id"]) == (
        "",
        "",
        "",
    )


def test_identity_record_takes_orca_ids_from_the_environment(
    hub: Path, wt: Path, stubs: Path
) -> None:
    env = {**_orca_env(hub, wt), "ORCA_WORKTREE_ID": "wt-42"}

    assert _run(wt, stubs, env=env).returncode == 0

    record = _identity(wt)
    assert (record["orca_worktree_id"], record["orca_dispatch_id"]) == ("wt-42", "")


def test_identity_record_of_an_express_lane_leaves_issue_empty(
    hub: Path, wt: Path, stubs: Path
) -> None:
    args = ["--worktree", str(wt), "--repo-root", str(hub), "--issue", "fix-typo"]

    result = _run(wt, stubs, *args, "--lane", "express")

    assert result.returncode == 0, result.stderr
    record = _identity(wt)
    assert (record["issue"], record["lane"]) == ("", "express")


def test_identity_value_with_a_newline_is_refused_not_injected(
    hub: Path, wt: Path, stubs: Path
) -> None:
    env = {**_orca_env(hub, wt), "ORCA_WORKTREE_ID": "wt-1\nrun_id=evil"}

    result = _run(wt, stubs, env=env)

    assert result.returncode != 0
    assert not (wt / ".ai-toolkit" / "identity").exists()


def test_identity_record_rewrite_is_byte_identical_and_leaves_no_temp_files(
    hub: Path, wt: Path, stubs: Path
) -> None:
    assert _run(wt, stubs, *_explicit(hub, wt)).returncode == 0
    first = (wt / ".ai-toolkit" / "identity").read_bytes()

    assert _run(wt, stubs, *_explicit(hub, wt)).returncode == 0

    assert (wt / ".ai-toolkit" / "identity").read_bytes() == first
    assert sorted(p.name for p in (wt / ".ai-toolkit").glob("identity*")) == ["identity"]


def test_quick_lane_is_accepted_and_recorded(hub: Path, wt: Path, stubs: Path) -> None:
    args = ["--worktree", str(wt), "--repo-root", str(hub), "--issue", "tweak", "--lane", "quick"]

    result = _run(wt, stubs, *args)

    assert result.returncode == 0, result.stderr
    assert (wt / ".ai-toolkit" / "lane").read_text() == "quick\n"
    assert _identity(wt)["lane"] == "quick"


def test_dispatch_flags_are_recorded_and_beat_the_environment(
    hub: Path, wt: Path, stubs: Path
) -> None:
    env = {**_orca_env(hub, wt), "ORCA_WORKTREE_ID": "from-env"}
    flags = ["--orca-worktree-id", "wt-9", "--orca-dispatch-id", "ctx_9", "--run-id", "run_9"]

    result = _run(wt, stubs, *flags, env=env)

    assert result.returncode == 0, result.stderr
    record = _identity(wt)
    assert (record["orca_worktree_id"], record["orca_dispatch_id"], record["run_id"]) == (
        "wt-9",
        "ctx_9",
        "run_9",
    )


def test_identity_only_adds_the_dispatch_id_and_changes_nothing_else(
    hub: Path, wt: Path, stubs: Path
) -> None:
    first = [*_explicit(hub, wt, "afk"), "--orca-worktree-id", "wt-9", "--run-id", "run_9"]
    assert _run(wt, stubs, *first).returncode == 0
    shutil.rmtree(wt / ".claude" / "skills")  # a full re-run would restore it
    settings = (wt / ".claude" / "settings.local.json").read_bytes()

    result = _run(wt, stubs, *first, "--orca-dispatch-id", "ctx_9", "--identity-only")

    assert result.returncode == 0, result.stderr
    record = _identity(wt)
    assert (record["orca_worktree_id"], record["orca_dispatch_id"], record["run_id"]) == (
        "wt-9",
        "ctx_9",
        "run_9",
    )
    assert not (wt / ".claude" / "skills").exists()
    assert (wt / ".claude" / "settings.local.json").read_bytes() == settings


def test_a_rerun_keeps_the_ids_no_flag_names(hub: Path, wt: Path, stubs: Path) -> None:
    flags = ["--orca-worktree-id", "wt-9", "--orca-dispatch-id", "ctx_9", "--run-id", "run_9"]
    assert _run(wt, stubs, *_explicit(hub, wt), *flags).returncode == 0

    assert _run(wt, stubs, *_explicit(hub, wt), "--identity-only").returncode == 0

    record = _identity(wt)
    assert (record["orca_worktree_id"], record["orca_dispatch_id"], record["run_id"]) == (
        "wt-9",
        "ctx_9",
        "run_9",
    )


# The spoke command allowlist and the afk deny-wall (issues #11, #37, #38, #149, #259, #281):
# worktree-new.sh used to be the only place these were pinned, one rule list at a time.
_SEEDED_RULES = [
    "Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)",
    "Bash(bash .ai-toolkit/scripts/spoke-ready.sh:*)",
    *(
        f"Bash({c})"
        for c in (
            "git status:*",
            "git diff:*",
            "git log:*",
            "git show:*",
            "git rev-parse:*",
            "git branch --show-current",
            "ls:*",
            "cat:*",
            "head:*",
            "tail:*",
            "wc:*",
            "grep:*",
            "rg:*",
            "find:*",
            "echo:*",
            "tree:*",
            "git fetch:*",
            "git remote -v",
            "git stash list",
            "gh issue view:*",
            "gh pr view:*",
            "python -m pytest:*",
            ".venv/bin/python -m pytest:*",
            "pytest:*",
            "chmod +x:*",
            "git add:*",
            "git reset",
            "git reset -q",
            "git reset HEAD:*",
            "git reset -q HEAD:*",
            "./:*",
        )
    ),
]
# Each would hand over a destructive verb or arbitrary code execution; none may ever be seeded.
_FORBIDDEN_RULES = [
    f"Bash({c})"
    for c in (
        "git branch:*",
        "git tag:*",
        "git push:*",
        "git checkout:*",
        "git reset:*",
        "git reset --hard",
        "git reset --hard:*",
        "git clean:*",
        "python:*",
        "python -c:*",
        "chmod:*",
        "rm:*",
        "mv:*",
    )
]


def test_seeded_allowlist_has_every_tier_and_no_destructive_wildcard(
    hub: Path, wt: Path, stubs: Path
) -> None:
    assert _run(wt, stubs, *_explicit(hub, wt)).returncode == 0

    allow = _settings(wt)["permissions"]["allow"]
    assert [r for r in _SEEDED_RULES if r not in allow] == []
    assert [r for r in _FORBIDDEN_RULES if r in allow] == []
    assert not any(r.startswith("Bash(git push origin") for r in allow)
    assert allow[-1] == f"Read(/{hub}/**)"


def test_merge_into_an_existing_settings_file_keeps_its_rules_and_order(
    hub: Path, wt: Path, stubs: Path
) -> None:
    (wt / ".claude").mkdir()
    (wt / ".claude" / "settings.local.json").write_text(
        json.dumps(
            {"permissions": {"allow": ["Bash(mine:*)", "Bash(git status:*)"], "deny": ["X"]}}
        )
    )

    assert _run(wt, stubs, *_explicit(hub, wt, "afk")).returncode == 0

    perms = _settings(wt)["permissions"]
    assert perms["allow"][:2] == ["Bash(mine:*)", "Bash(git status:*)"]
    assert perms["allow"].count("Bash(git status:*)") == 1
    assert perms["deny"] == ["X", "AskUserQuestion"]


@pytest.mark.parametrize(("mode", "denied"), [("afk", True), ("attended", False)])
def test_askuserquestion_is_denied_for_afk_spokes_only(
    hub: Path, wt: Path, stubs: Path, mode: str, denied: bool
) -> None:
    assert _run(wt, stubs, *_explicit(hub, wt, mode)).returncode == 0

    assert ("AskUserQuestion" in _settings(wt)["permissions"].get("deny", [])) is denied


def test_a_foreign_orca_worktree_id_in_the_environment_never_overwrites_the_record(
    hub: Path, wt: Path, stubs: Path, tmp_path: Path
) -> None:
    # Every Orca terminal exports ORCA_WORKTREE_ID for ITS worktree (the dispatching hub's); it
    # only describes this worktree when ORCA_WORKTREE_PATH names it (Orca's setup hook).
    assert _run(wt, stubs, *_explicit(hub, wt), "--orca-worktree-id", "wt-9").returncode == 0
    hub_terminal = {"ORCA_WORKTREE_ID": "the-hubs-id", "ORCA_WORKTREE_PATH": str(hub)}

    result = _run(tmp_path, stubs, *_explicit(hub, wt), "--identity-only", env=hub_terminal)

    assert result.returncode == 0, result.stderr
    assert _identity(wt)["orca_worktree_id"] == "wt-9"
