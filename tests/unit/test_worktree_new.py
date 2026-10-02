"""Unit tests for scripts/worktree-new.sh, the Orca dispatch (#363).

The script creates the worktree with `orca worktree create`, renames the branch, provisions it,
launches claude through `orca terminal create`, and delivers the seed with `orca orchestration
worker-start --terminal`. `orca` is a PATH stub (`_orca_stub.py`) that records argv, replays
canned JSON and materialises a real git worktree; `tmux`, `code` and `git worktree add` are
stubbed to FAIL the test if the retired launch paths run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from _orca_stub import install_forbidden_stubs, install_orca_stub, orca_calls, stub_env
from _stubs import write_stub

WORKTREE_NEW = Path(__file__).resolve().parents[2] / "scripts" / "worktree-new.sh"

# A host's git config must not reach the repos under test, and git's own location vars (set
# inside a commit hook) must not redirect `git -C <tmp-hub>` onto the real repo (#179).
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
for _leak in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "AI_TOOLKIT_BASE_BRANCH",
):
    _GIT_ENV.pop(_leak, None)

# Host state that would steer the script: agent pinning, config-sourced defaults, the stubbed gh
# seeds, the native-OTel gate and every OTEL_* / Langfuse variable.
_HOST_VARS = (
    "WT_AGENT_MODEL",
    "WT_AGENT_EFFORT",
    "WT_AGENT_MODEL_DEFAULT",
    "WT_AGENT_EFFORT_DEFAULT",
    "GH_ISSUE_TITLE",
    "GH_ISSUE_BODY",
    "GH_MIRROR_RC",
    "AI_TOOLKIT_GH_LIFECYCLE_LABELS",
    "AI_TOOLKIT_OTEL",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "ENABLE_BETA_TRACING_DETAILED",
    "OTEL_METRICS_INCLUDE_ACCOUNT_UUID",
    "BETA_TRACING_ENDPOINT",
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_TOOL_DETAILS",
    "OTEL_LOG_TOOL_CONTENT",
    "LANGFUSE_BASIC_AUTH",
    "LANGFUSE_HOST",
    "BRIDGE_PORT",
    "TMUX",
    "ORCA_TERMINAL_HANDLE",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout on `main` with an `origin` bare remote."""
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
    return hub


def _run_new(
    hub: Path,
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    scenario: dict | None = None,
    version: str = "1.4.218",
    handle: str | None = "term_hub",
) -> subprocess.CompletedProcess:
    """Run worktree-new.sh from the hub against the stubbed `orca` (bin dir: tmp_path/bin)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    install_forbidden_stubs(bindir)
    # gh answers the title/body fetches ($GH_ISSUE_TITLE / $GH_ISSUE_BODY) and logs every call
    # to $GH_LOG; $GH_MIRROR_RC makes the lifecycle-mirror writes fail (an offline gh).
    gh = bindir / "gh"
    write_stub(
        gh,
        "#!/bin/sh\n"
        '{ printf "%s" "$*" | tr "\\n" " "; printf "\\n"; } >> "$GH_LOG"\n'
        'case "$*" in\n'
        '  *"--json title"*) printf "%s\\n" "${GH_ISSUE_TITLE:-Some Issue Title}" ;;\n'
        '  *"--json body"*)  printf "%s\\n" "$GH_ISSUE_BODY" ;;\n'
        '  "issue edit"*|"issue comment"*|"label create"*) exit "${GH_MIRROR_RC:-0}" ;;\n'
        "esac\n",
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in _GIT_ENV.items() if k not in _HOST_VARS}
    env.update(install_orca_stub(bindir, scenario=scenario, version=version))
    env.update(
        HOME=str(home),
        GH_LOG=str(tmp_path / "gh-calls.log"),
        ORCA_SETTLE_SLEEP="0",
        ORCA_SETTLE_TRIES="3",
        ORCA_AGENT_SLEEP="0",
        ORCA_AGENT_TRIES="3",
    )
    if handle:
        env["ORCA_TERMINAL_HANDLE"] = handle
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(WORKTREE_NEW), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=stub_env(bindir, env),
    )


def _calls(tmp_path: Path, noun_verb: str | None = None) -> list[list[str]]:
    calls = orca_calls(tmp_path / "bin")
    if noun_verb is None:
        return calls
    return [c for c in calls if " ".join(c[:2]) == noun_verb]


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _wt(tmp_path: Path, name: str) -> Path:
    return tmp_path / "orca-ws" / name


def _launch_cmd(tmp_path: Path) -> str:
    return _arg(_calls(tmp_path, "terminal create")[0], "--command")


def _spec(tmp_path: Path) -> str:
    return _arg(_calls(tmp_path, "orchestration worker-start")[0], "--spec")


def _identity(tmp_path: Path, name: str) -> dict[str, str]:
    text = (_wt(tmp_path, name) / ".ai-toolkit" / "identity").read_text()
    return dict(ln.split("=", 1) for ln in text.splitlines())


def _gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-calls.log"
    return log.read_text().splitlines() if log.exists() else []


# ── the Orca sequence ──
@pytest.mark.parametrize(
    "args",
    [
        ("8", "some-slug"),
        ("8", "some-slug", "--mode", "afk"),
        ("263", "some-slug", "--subtasks", "265"),
        ("adhoc", "--prompt", "x"),
    ],
)
def test_dispatch_runs_the_orca_sequence_and_never_the_retired_paths(
    hub: Path, tmp_path: Path, args: tuple[str, ...]
) -> None:
    proc = _run_new(hub, tmp_path, *args, extra_env={"AFK_STATE_DIR": str(tmp_path / "afk-state")})

    assert proc.returncode == 0, proc.stderr
    steps = [" ".join(c[:2]) for c in _calls(tmp_path) if c[0] != "--version"]
    # `skills installed` is the provisioner's own worker-skills probe; `terminal show` the agent poll
    kept = [s for s in steps if s not in ("terminal show", "skills installed")]
    assert kept == [
        "orchestration run-current",
        "worktree create",
        "worktree set",
        "terminal create",
        "terminal wait",
        "orchestration worker-start",
    ]
    assert (tmp_path / "bin" / "forbidden.log").read_text() == ""


def test_worktree_is_created_from_the_base_with_the_issue_linked(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    create = _calls(tmp_path, "worktree create")[0]
    assert _arg(create, "--name") == "8-some-slug"
    assert _arg(create, "--issue") == "8"
    assert _arg(create, "--base-branch") == "origin/main"
    assert _arg(create, "--setup") == "skip"
    assert "--no-parent" in create


def test_branch_is_renamed_to_type_slash_issue_slug(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", "fix")

    assert proc.returncode == 0, proc.stderr
    assert (
        _git(_wt(tmp_path, "8-some-slug"), "branch", "--show-current").strip() == "fix/8-some-slug"
    )


def test_worktree_is_marked_in_progress(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    work_set = _calls(tmp_path, "worktree set")[0]
    assert _arg(work_set, "--workspace-status") == "in-progress"
    assert _arg(work_set, "--worktree").startswith("id:stub-repo::")


def test_adhoc_slug_names_the_worktree_by_slug_without_an_issue(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "refactor-sync", "-t", "chore", "--prompt", "tidy")

    assert proc.returncode == 0, proc.stderr
    create = _calls(tmp_path, "worktree create")[0]
    assert _arg(create, "--name") == "refactor-sync"
    assert "--issue" not in create
    branch = _git(_wt(tmp_path, "refactor-sync"), "branch", "--show-current").strip()
    assert branch == "chore/refactor-sync"
    assert not (_wt(tmp_path, "refactor-sync") / ".ai-toolkit" / "task.md").exists()


def test_adhoc_dispatch_without_a_prompt_fails_loud_before_creating_anything(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "refactor-sync", "-t", "chore")

    assert proc.returncode != 0
    assert "--prompt" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


def test_existing_local_branch_dies_before_asking_orca(hub: Path, tmp_path: Path) -> None:
    _git(hub, "branch", "feature/8-some-slug")

    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode != 0
    assert "already exists" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


# ── preflight: Orca, a coordinator terminal, a bound Run ──
def test_refuses_below_the_orca_version_floor(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", version="1.4.217")

    assert proc.returncode != 0
    assert "1.4.218" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


def test_refuses_without_the_orca_cli(hub: Path, tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["bash", str(WORKTREE_NEW), "8", "some-slug"],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, **env},
    )

    assert proc.returncode != 0
    assert "orca" in proc.stderr


def test_refuses_outside_an_orca_terminal(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", handle=None)

    assert proc.returncode != 0
    assert "Orca terminal" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


def test_refuses_without_a_bound_run_and_names_the_fix(hub: Path, tmp_path: Path) -> None:
    scenario = {"orchestration run-current": [{"out": {"ok": True, "result": {"run": None}}}]}

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=scenario)

    assert proc.returncode != 0
    assert "run-create" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


# ── launch ──
def test_launch_command_pins_model_effort_role_and_skips_permissions(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    cmd = _launch_cmd(tmp_path)
    assert "WT_SPOKE=8 claude --model claude-sonnet-5-5 --effort high" in cmd
    assert cmd.endswith("--dangerously-skip-permissions")
    assert "--permission-mode" not in cmd
    assert "CLAUDE_EFFORT" not in cmd


def test_launch_is_in_the_worktree_and_titled_by_the_branch_leaf(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    create = _calls(tmp_path, "terminal create")[0]
    assert _arg(create, "--worktree") == f"path:{_wt(tmp_path, '8-some-slug')}"
    assert _arg(create, "--title") == "8-some-slug"


def test_seed_is_delivered_with_worker_start_into_the_launched_terminal(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", "--prompt", "KICKOFF")

    assert proc.returncode == 0, proc.stderr
    start = _calls(tmp_path, "orchestration worker-start")
    assert len(start) == 1
    assert _arg(start[0], "--terminal") == "term_stub"
    assert _arg(start[0], "--worktree") == f"path:{_wt(tmp_path, '8-some-slug')}"
    assert _arg(start[0], "--run") == "run_stub"
    assert _spec(tmp_path) == "KICKOFF"
    assert "KICKOFF" not in _launch_cmd(tmp_path), "the prompt must not ride the command line"


def test_worker_start_waits_for_the_agent_to_exist(hub: Path, tmp_path: Path) -> None:
    show = [
        {"out": {"ok": True, "result": {"terminal": {"agentIdentity": None}}}},
        {"out": {"ok": True, "result": {"terminal": {"agentIdentity": "claude"}}}},
    ]

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario={"terminal show": show})

    assert proc.returncode == 0, proc.stderr
    kinds = [" ".join(c[:2]) for c in _calls(tmp_path)]
    assert kinds.index("terminal show") < kinds.index("orchestration worker-start")
    # two polls to see the agent, then one re-check right before the seed is sent
    assert kinds.count("terminal show") == 3
    assert kinds[kinds.index("orchestration worker-start") - 1] == "terminal show"


def test_an_agent_that_never_starts_fails_loud_without_sending_the_seed(
    hub: Path, tmp_path: Path
) -> None:
    show = [{"out": {"ok": True, "result": {"terminal": {"agentIdentity": None}}}}]

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario={"terminal show": show})

    assert proc.returncode != 0
    assert "did not start" in proc.stderr
    assert not _calls(tmp_path, "orchestration worker-start")


def test_a_trust_blocked_launch_fails_loud_names_the_fix_and_sends_nothing(
    hub: Path, tmp_path: Path
) -> None:
    blocked = {
        "terminal wait": [
            {
                "rc": 1,
                "out": {
                    "ok": False,
                    "error": {
                        "code": "timeout",
                        "data": {"blockedReason": "agent-trust-workspace"},
                    },
                },
            }
        ]
    }

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=blocked)

    assert proc.returncode != 0
    assert "hasTrustDialogAccepted" in proc.stderr
    assert not _calls(tmp_path, "orchestration worker-start")


def test_a_definite_worker_start_failure_is_not_retried(hub: Path, tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "consumer_fenced"}}}
        ]
    }

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=scenario)

    assert proc.returncode != 0
    assert len(_calls(tmp_path, "orchestration worker-start")) == 1


def test_a_runtime_unavailable_that_settles_ready_dispatches_exactly_once(
    hub: Path, tmp_path: Path
) -> None:
    scenario = {
        "orchestration worker-start": [
            {
                "rc": 1,
                "out": {
                    "ok": False,
                    "error": {
                        "code": "runtime_unavailable",
                        "data": {"orchestrationRequestId": "req-1"},
                    },
                },
            }
        ],
        "orchestration request-show": [
            {
                "out": {
                    "ok": True,
                    "result": {
                        "state": "completed",
                        "receipt": {"dispatchId": "ctx_late", "state": "ready"},
                    },
                }
            }
        ],
    }

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=scenario)

    assert proc.returncode == 0, proc.stderr
    assert len(_calls(tmp_path, "orchestration worker-start")) == 1
    assert _identity(tmp_path, "8-some-slug")["orca_dispatch_id"] == "ctx_late"


def test_a_runtime_unavailable_worktree_create_settles_through_the_listing(
    hub: Path, tmp_path: Path
) -> None:
    # The create really happens (the stub materialises it) but the reply is lost.
    scenario = {
        "worktree create": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "runtime_unavailable"}}}
        ]
    }
    subprocess.run(
        [
            "git",
            "-C",
            str(hub),
            "worktree",
            "add",
            "-q",
            "-b",
            "8-some-slug",
            str(_wt(tmp_path, "8-some-slug")),
        ],
        check=True,
        env=_GIT_ENV,
        capture_output=True,
    )
    listing = {
        "ok": True,
        "result": {
            "worktrees": [
                {
                    "id": "stub-repo::x",
                    "path": str(_wt(tmp_path, "8-some-slug")),
                    "displayName": "8-some-slug",
                }
            ]
        },
    }
    scenario["worktree list"] = [{"out": listing}]

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=scenario)

    assert proc.returncode == 0, proc.stderr
    assert len(_calls(tmp_path, "worktree create")) == 1
    listing = _calls(tmp_path, "worktree list")[0]
    assert _arg(listing, "--repo") == f"path:{hub}"


# ── identity, provisioning, spoke env ──
def test_identity_record_holds_the_final_type_slug_mode_lane_and_orca_ids(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", "fix", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    ident = _identity(tmp_path, "8-some-slug")
    assert (ident["issue"], ident["type"], ident["slug"]) == ("8", "fix", "8-some-slug")
    assert (ident["mode"], ident["lane"]) == ("afk", "spoke")
    assert ident["orca_worktree_id"] == f"stub-repo::{_wt(tmp_path, '8-some-slug')}"
    assert (ident["orca_dispatch_id"], ident["run_id"]) == ("ctx_stub", "run_stub")
    assert ident["spoke_run_id"].startswith("fix/8-some-slug+")


def test_mode_afk_exists_before_the_agent_is_launched(hub: Path, tmp_path: Path) -> None:
    mode_file = _wt(tmp_path, "8-some-slug") / ".ai-toolkit" / "mode"
    scenario = {"_snapshot": [{"key": "terminal create", "path": str(mode_file)}]}

    proc = _run_new(hub, tmp_path, "8", "some-slug", "--mode", "afk", scenario=scenario)

    assert proc.returncode == 0, proc.stderr
    snaps = [
        json.loads(ln)
        for ln in (tmp_path / "bin/.orca-stub/snapshots.jsonl").read_text().splitlines()
    ]
    assert snaps[0]["content"] == "afk\n"


@pytest.mark.parametrize(
    ("args", "lane"), [(("8", "some-slug"), "spoke"), (("adhoc", "--prompt", "x"), "express")]
)
def test_lane_follows_the_issue_kind(hub: Path, tmp_path: Path, args: tuple, lane: str) -> None:
    proc = _run_new(hub, tmp_path, *args)

    assert proc.returncode == 0, proc.stderr
    name = "8-some-slug" if lane == "spoke" else "adhoc"
    assert _identity(tmp_path, name)["lane"] == lane


def test_settings_env_carries_wt_spoke_and_the_otel_pairs_not_the_secret(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(
        hub, tmp_path, "8", "some-slug", extra_env={"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=x"}
    )

    assert proc.returncode == 0, proc.stderr
    settings = json.loads(
        (_wt(tmp_path, "8-some-slug") / ".claude/settings.local.json").read_text()
    )
    assert settings["env"]["WT_SPOKE"] == "8"
    assert settings["env"]["OTEL_RESOURCE_ATTRIBUTES"].startswith(
        "spoke_run_id=feature/8-some-slug+"
    )
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in settings["env"]


def test_task_contract_and_default_seed_point_at_it(hub: Path, tmp_path: Path) -> None:
    env = {"GH_ISSUE_TITLE": "Anchor it", "GH_ISSUE_BODY": "Do the thing.\nGate: none\n"}

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    task = (_wt(tmp_path, "8-some-slug") / ".ai-toolkit" / "task.md").read_text()
    assert "Anchor it" in task and "Gate: none" in task
    assert ".ai-toolkit/task.md" in _spec(tmp_path)


def test_without_a_task_contract_the_seed_re_anchors_from_the_live_issue(
    hub: Path, tmp_path: Path
) -> None:
    env = {"GH_ISSUE_TITLE": "Title only", "GH_ISSUE_BODY": ""}

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    assert not (_wt(tmp_path, "8-some-slug") / ".ai-toolkit" / "task.md").exists()
    assert _spec(tmp_path) == "/source-task 8"


def _seed_marker_scripts(hub: Path) -> None:
    (hub / "scripts").mkdir()
    for name in ("spoke-ready.sh", "spoke-push.sh"):
        (hub / "scripts" / name).write_text("#!/usr/bin/env bash\ntrue\n")
    _git(hub, "add", "scripts")
    _git(hub, "commit", "-qm", "chore: add marker scripts", "-m", "Refs #0")
    _git(hub, "push", "-q", "origin", "main")


@pytest.mark.parametrize(
    ("toolkit", "marker"),
    [
        (True, "bash scripts/spoke-ready.sh --gate"),
        (False, "bash .ai-toolkit/scripts/spoke-ready.sh --gate"),
    ],
)
def test_seed_names_the_marker_path_that_exists_in_the_worktree(
    hub: Path, tmp_path: Path, toolkit: bool, marker: str
) -> None:
    if toolkit:
        _seed_marker_scripts(hub)
    env = {"GH_ISSUE_TITLE": "T", "GH_ISSUE_BODY": "Do it.\nGate: plan\n"}

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    assert marker in _spec(tmp_path)


def test_explicit_prompt_overrides_the_default_seed(hub: Path, tmp_path: Path) -> None:
    env = {"GH_ISSUE_TITLE": "T", "GH_ISSUE_BODY": "Do it.\nGate: none\n"}

    proc = _run_new(hub, tmp_path, "8", "some-slug", "--prompt", "/source", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    assert _spec(tmp_path) == "/source"


# ── model + effort ──
def test_model_and_effort_default_to_the_safe_tier(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    assert "--model claude-sonnet-5-5 --effort high" in _launch_cmd(tmp_path)


def test_model_and_effort_resolve_from_spoke_model_env(hub: Path, tmp_path: Path) -> None:
    env = {"WT_AGENT_MODEL_DEFAULT": "team-model", "WT_AGENT_EFFORT_DEFAULT": "medium"}

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    assert "--model team-model --effort medium" in _launch_cmd(tmp_path)


def test_model_and_effort_resolve_from_the_config_file(hub: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "ai-toolkit.yml"
    cfg.write_text("model:\n  spoke:\n    model: config-file-model\n    effort: low\n")

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env={"AI_TOOLKIT_CONFIG": str(cfg)})

    assert proc.returncode == 0, proc.stderr
    assert "--model config-file-model --effort low" in _launch_cmd(tmp_path)


def test_issue_model_line_beats_the_default_and_env_beats_the_line(
    hub: Path, tmp_path: Path
) -> None:
    body = "Desc.\n\nmodel: claude-sonnet-5\nModel: ignored\n"
    pinned = _run_new(hub, tmp_path / "a", "8", "s", extra_env={"GH_ISSUE_BODY": body})
    env = {"GH_ISSUE_BODY": body, "WT_AGENT_MODEL": "opus-pinned", "WT_AGENT_EFFORT": "max"}
    forced = _run_new(hub, tmp_path / "b", "9", "s", extra_env=env)

    assert pinned.returncode == 0 and forced.returncode == 0, pinned.stderr + forced.stderr
    assert "--model claude-sonnet-5 " in _launch_cmd(tmp_path / "a")
    assert "--model opus-pinned --effort max" in _launch_cmd(tmp_path / "b")


def test_model_override_is_shell_quoted(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env={"WT_AGENT_MODEL": "foo bar"})

    assert proc.returncode == 0, proc.stderr
    assert "--model foo\\ bar" in _launch_cmd(tmp_path)


# ── afk ask-rule preflight ──
def _write_ask_rule(tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"permissions": {"ask": ["Bash(chmod *)"]}}))


@pytest.mark.parametrize(
    ("mode", "rule", "warns"),
    [("afk", True, True), ("afk", False, False), ("attended", True, False)],
)
def test_ask_rule_preflight_warns_only_for_afk_with_a_global_rule(
    hub: Path, tmp_path: Path, mode: str, rule: bool, warns: bool
) -> None:
    if rule:
        _write_ask_rule(tmp_path)

    proc = _run_new(hub, tmp_path, "8", "some-slug", "--mode", mode)

    assert proc.returncode == 0, proc.stderr
    assert ("permissions.ask" in proc.stderr) is warns


# ── native OTel launch env ──
def test_launch_prefix_carries_the_otel_pairs_by_default_and_never_the_auth_header(
    hub: Path, tmp_path: Path
) -> None:
    env = {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic c2VjcmV0"}

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    cmd = _launch_cmd(tmp_path)
    for var in (
        "CLAUDE_CODE_ENABLE_TELEMETRY=1",
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317",
        "BETA_TRACING_ENDPOINT=http://localhost:4418",
        "OTEL_LOG_RAW_API_BODIES=file:",
    ):
        assert var in cmd
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=feature/8-some-slug+" in cmd
    assert ",repo=hub-remote" in cmd
    assert cmd.index("CLAUDE_CODE_ENABLE_TELEMETRY=1") < cmd.index("WT_SPOKE=8")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in cmd and "c2VjcmV0" not in cmd


def test_launch_prefix_preserves_operator_endpoints(hub: Path, tmp_path: Path) -> None:
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317",
        "BETA_TRACING_ENDPOINT": "http://collector:4418",
    }

    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    cmd = _launch_cmd(tmp_path)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317" in cmd
    assert "BETA_TRACING_ENDPOINT=http://collector:4418" in cmd


def test_otel_opt_out_leaves_a_plain_launch_and_no_env_block_pairs(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env={"AI_TOOLKIT_OTEL": "0"})

    assert proc.returncode == 0, proc.stderr
    assert _launch_cmd(tmp_path).startswith("WT_SPOKE=8 claude ")
    settings = json.loads(
        (_wt(tmp_path, "8-some-slug") / ".claude/settings.local.json").read_text()
    )
    assert settings["env"] == {"WT_SPOKE": "8"}


# ── configurable base branch (issue #117) ──
def _add_develop(hub: Path) -> str:
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "push", "-q", "-u", "origin", "develop")
    tip = _git(hub, "rev-parse", "HEAD").strip()
    _git(hub, "checkout", "-q", "main")
    return tip


def test_new_branches_from_the_configured_base(hub: Path, tmp_path: Path) -> None:
    develop_tip = _add_develop(hub)
    _git(hub, "config", "ai-toolkit.base-branch", "develop")

    proc = _run_new(hub, tmp_path, "9", "cfg-base")

    assert proc.returncode == 0, proc.stderr
    assert _arg(_calls(tmp_path, "worktree create")[0], "--base-branch") == "origin/develop"
    assert _git(_wt(tmp_path, "9-cfg-base"), "rev-parse", "HEAD").strip() == develop_tip


def test_unpushed_hub_drift_is_not_inherited(hub: Path, tmp_path: Path) -> None:
    origin_tip = _git(hub, "rev-parse", "origin/main").strip()
    (hub / "unpushed.txt").write_text("local only\n")
    _git(hub, "add", "unpushed.txt")
    _git(hub, "commit", "-qm", "chore: unpushed hub drift", "-m", "Refs #0")

    proc = _run_new(hub, tmp_path, "9", "cfg-base")

    assert proc.returncode == 0, proc.stderr
    assert _git(_wt(tmp_path, "9-cfg-base"), "rev-parse", "HEAD").strip() == origin_tip


def test_a_missing_configured_base_dies_before_creating_anything(hub: Path, tmp_path: Path) -> None:
    _git(hub, "config", "ai-toolkit.base-branch", "ghost")

    proc = _run_new(hub, tmp_path, "9", "cfg-base")

    assert proc.returncode != 0
    assert "ghost" in proc.stderr
    assert not _calls(tmp_path, "worktree create")


# ── dispatch lifecycle-label mirror (issue #236) ──
def _one_issue_edit(calls: list[str]) -> str:
    edits = [c for c in calls if c.startswith("issue edit")]
    assert len(edits) == 1, f"expected exactly one gh issue-edit, got {edits}"
    return edits[0]


@pytest.mark.parametrize(
    ("mode", "labels"),
    [
        (
            "attended",
            [
                "--add-label status:in-progress",
                "--add-label mode:attended",
                "--add-label lane:spoke",
            ],
        ),
        ("afk", ["--add-label mode:afk", "--remove-label mode:attended"]),
    ],
)
def test_dispatch_stamps_the_issue_labels(
    hub: Path, tmp_path: Path, mode: str, labels: list[str]
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", "--mode", mode)

    assert proc.returncode == 0, proc.stderr
    edit = _one_issue_edit(_gh_calls(tmp_path))
    assert edit.startswith("issue edit 8 ")
    for label in labels:
        assert label in edit


def test_dispatch_comment_links_the_branch_worktree_and_orca_worktree_id(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    comments = [c for c in _gh_calls(tmp_path) if c.startswith("issue comment 8 ")]
    assert len(comments) == 1
    assert "feature/8-some-slug" in comments[0]
    assert str(_wt(tmp_path, "8-some-slug")) in comments[0]
    assert f"orca worktree: stub-repo::{_wt(tmp_path, '8-some-slug')}" in comments[0]
    assert "tmux" not in comments[0]


def test_adhoc_dispatch_mirrors_nothing_to_github(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "adhoc", "--prompt", "x")

    assert proc.returncode == 0, proc.stderr
    assert not [
        c
        for c in _gh_calls(tmp_path)
        if c.startswith(("issue edit", "issue comment", "label create"))
    ]


@pytest.mark.parametrize("env", [{"GH_MIRROR_RC": "1"}, {"AI_TOOLKIT_GH_LIFECYCLE_LABELS": "0"}])
def test_a_failing_or_disabled_mirror_never_fails_the_dispatch(
    hub: Path, tmp_path: Path, env: dict
) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=env)

    assert proc.returncode == 0, proc.stderr


# ── #278: --subtasks, the packed group's ordered extra issues ──
_PACKED_ENV = {
    "GH_ISSUE_TITLE": "Primary of a packed group",
    "GH_ISSUE_BODY": "Do it.\nGate: none\n",
}


def _queued(tmp_path: Path, primary: str) -> Path:
    return tmp_path / "afk-state" / f"queued-{primary}"


def _packed(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**_PACKED_ENV, "AFK_STATE_DIR": str(tmp_path / "afk-state"), **extra}


def test_subtasks_are_seeded_into_the_queue_but_never_the_primary(
    hub: Path, tmp_path: Path
) -> None:
    proc = _run_new(
        hub, tmp_path, "263", "some-slug", "--subtasks", "263,265, 270", extra_env=_packed(tmp_path)
    )

    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in _queued(tmp_path, "263").iterdir()) == ["265", "270"]


def test_the_branch_still_leads_with_the_primary_when_packed(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(
        hub, tmp_path, "263", "some-slug", "--subtasks", "265", extra_env=_packed(tmp_path)
    )

    assert proc.returncode == 0, proc.stderr
    branch = _git(_wt(tmp_path, "263-some-slug"), "branch", "--show-current").strip()
    assert branch == "feature/263-some-slug"


def test_no_subtasks_seeds_no_queue(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "8", "some-slug", extra_env=_packed(tmp_path))

    assert proc.returncode == 0, proc.stderr
    assert not _queued(tmp_path, "8").exists()


@pytest.mark.parametrize("bad", ["265,oops", "2*"])
def test_a_malformed_subtask_aborts_the_spawn_naming_it(
    hub: Path, tmp_path: Path, bad: str
) -> None:
    for name in ("20", "21"):
        (hub / name).write_text("decoy\n")

    proc = _run_new(
        hub, tmp_path, "263", "some-slug", "--subtasks", bad, extra_env=_packed(tmp_path)
    )

    assert proc.returncode != 0
    assert bad.split(",")[-1] in proc.stderr + proc.stdout
    assert not _queued(tmp_path, "263").exists()
    assert not _calls(tmp_path, "worktree create")


@pytest.mark.parametrize("prompt", [None, "CALLER_KICKOFF"])
def test_the_chain_note_reaches_default_and_explicit_prompts(
    hub: Path, tmp_path: Path, prompt: str | None
) -> None:
    args = ["263", "some-slug", "--subtasks", "265,270", *(["--prompt", prompt] if prompt else [])]

    proc = _run_new(hub, tmp_path, *args, extra_env=_packed(tmp_path))

    assert proc.returncode == 0, proc.stderr
    spec = _spec(tmp_path)
    assert "265 270" in spec
    assert (prompt is None) or spec.startswith(prompt)


def test_a_failed_launch_leaves_no_subtask_queue_behind(hub: Path, tmp_path: Path) -> None:
    scenario = {
        "orchestration worker-start": [
            {"rc": 1, "out": {"ok": False, "error": {"code": "consumer_fenced"}}}
        ]
    }

    proc = _run_new(
        hub,
        tmp_path,
        "263",
        "some-slug",
        "--subtasks",
        "265,270",
        extra_env=_packed(tmp_path),
        scenario=scenario,
    )

    assert proc.returncode != 0
    assert not _queued(tmp_path, "263").exists()


def test_a_missing_dispatch_id_warns_but_does_not_fail_a_live_spoke(
    hub: Path, tmp_path: Path
) -> None:
    scenario = {"orchestration worker-start": [{"out": {"ok": True, "result": {"state": "ready"}}}]}

    proc = _run_new(hub, tmp_path, "8", "some-slug", scenario=scenario)

    assert proc.returncode == 0, proc.stderr
    assert "dispatch id" in proc.stderr
    assert any(c.startswith("issue edit 8 ") for c in _gh_calls(tmp_path))
    assert _identity(tmp_path, "8-some-slug")["orca_dispatch_id"] == ""


def test_adhoc_settings_wt_spoke_matches_the_launch_prefix(hub: Path, tmp_path: Path) -> None:
    proc = _run_new(hub, tmp_path, "My Fix", "--prompt", "x")

    assert proc.returncode == 0, proc.stderr
    assert "WT_SPOKE=my-fix " in _launch_cmd(tmp_path)
    settings = json.loads((_wt(tmp_path, "my-fix") / ".claude/settings.local.json").read_text())
    assert settings["env"]["WT_SPOKE"] == "my-fix"
