"""Unit tests for scripts/worktree-new.sh tmux window naming (issue #8).

The tmux window opened for a new worktree must carry the human-readable branch
leaf (e.g. `8-some-slug` for `feature/8-some-slug`), not the bare issue number,
and the name must be pinned (`automatic-rename off`, `allow-rename off`) so the
process running inside the window cannot clobber it. A logging `tmux` stub on
PATH keeps the test hermetic while a fake TMUX env var steers the script down
the tmux branch.

Per-project session (issue #39): every spoke window lives in a tmux session named
after the project — the parent-dir prefix plus the repo basename (`<parent>-<base>`),
sanitized for tmux (forbidden `.`/`:` → `-`) — not a hardcoded session `0`. Two
repos sharing a basename under different parents get distinct sessions. The script
must target that session explicitly (`new-window -t =<sess>:`), create it detached
when missing (`has-session` → `new-session -d -s <sess>`), work even when invoked
outside tmux ($TMUX unset), and print the exact jump command targeting that session
(`switch-client` inside tmux, `attach ... select-window` outside).

Agent pinning (issue #8 follow-up): the spoke launch must pin model and effort
explicitly (`CLAUDE_EFFORT=<effort> claude --model <model>`) from env vars
`WT_AGENT_MODEL` (config-less default `claude-opus-4-8`, no 1m) / `WT_AGENT_EFFORT` (default `high`) instead
of relying on user-global settings; a seeded `--prompt` stays the trailing arg.

Launch delivery (issue #15): typing the launch command into an interactive zsh
via `send-keys` races shell init (eaten Enter, zvm interference). The command
must instead be passed as the `new-window` shell-command argument, suffixed
with `; exec <shell>` so the window survives claude's exit; `send-keys` must
never deliver the launch. `--no-agent` spawns a plain interactive window.
Command-allowlist templating (issues #11, #37): after the `.claude/` copy, the
script seeds `<worktree>/.claude/settings.local.json` with the spoke's command
allowlist so the routine PUSH + read-only diagnostics run without a permission
prompt — gates, not asks, do the enforcing. Issue #37 replaces the two bare
exact-push rules (which never matched once the spoke decorated/chained the push)
with one allowlistable process rule — `Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)`
— plus a read-only Tier 1 (local) + Tier 2 (network-read) helper allowlist, and a
runner tier (#38: `pytest`/`python -m pytest`/`chmod +x`) for the RED→GREEN→test
loop; the bare-push rules are dropped, and no destructive or arbitrary-exec
wildcard (`git branch:*`, `git tag:*`, `git push:*`, `python:*`, `chmod:*`,
`rm`/`mv`) is ever seeded. The file is
created when the hub has no `.claude/` and merged (never clobbered, never
duplicated, order preserved) when one was copied.

"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

WORKTREE_NEW = Path(__file__).resolve().parents[2] / "scripts" / "worktree-new.sh"

# Pin git config to nothing: a host's global/system config (core.hooksPath,
# init.templateDir, protocol settings) must not reach the commits/pushes the
# tests drive — this repo itself ships installable git hooks.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)
# Strip inherited git-location vars (issue #179 / #259): when these tests run inside a
# `git commit` hook (e.g. red-proof-verify's Tested-RED replay), git sets GIT_DIR /
# GIT_WORK_TREE / GIT_INDEX_FILE / GIT_COMMON_DIR in the environment. Those OVERRIDE
# cwd-based discovery, so
# `git -C <tmp-hub>` and worktree-new.sh's internal git calls would silently operate on the
# REAL repo and trip the isolation tripwire. Popping them restores cwd-based discovery.
for _leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
    _GIT_ENV.pop(_leak, None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _make_hub(base: Path, name: str = "hub") -> Path:
    """Create a main checkout named `name` under `base`, with an `origin` remote.

    Factored out of the `hub` fixture so collision/derivation tests can build
    repos with custom basenames and parent dirs (issue #39).
    """
    base.mkdir(parents=True, exist_ok=True)
    remote = base / f"{name}-remote.git"
    hub = base / name
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
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
    return _make_hub(tmp_path, "hub")


def _run_new(
    hub: Path,
    tmp_path: Path,
    *args: str,
    inside_tmux: bool = True,
    has_session_rc: int = 0,
    new_session_rc: int = 0,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run worktree-new.sh from the hub with a logging `tmux` stub on PATH.

    The stub appends each invocation's argument string to a log (one line per
    call), answers `new-window` with a fake window id `@1` (captured by the
    script via `-P -F '#{window_id}'`), and answers `has-session` /
    `new-session` with exit statuses `has_session_rc` / `new_session_rc`
    (0 = the project session exists / was created). The log file is pre-created
    so a run that never reaches tmux reads as an empty log, not a missing one.

    Args:
        hub: Main checkout to run the script from.
        tmp_path: Per-test scratch dir for the stub and its log.
        *args: Arguments forwarded to worktree-new.sh.
        inside_tmux: If True, export a fake TMUX env var (invoked-inside-tmux);
            if False, leave TMUX unset (invoked from a plain shell).
        has_session_rc: Exit status of the stub's `has-session` answer.
        new_session_rc: Exit status of the stub's `new-session` answer
            (nonzero = no tmux server can be started).
        extra_env: Extra environment variables merged into the subprocess env
            (e.g. `WT_AGENT_MODEL` / `WT_AGENT_EFFORT` overrides).

    Returns:
        The completed process and the tmux call-log path.
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
        'if [ "$1" = "has-session" ]; then exit "${STUB_HAS_SESSION:-0}"; fi\n'
        'if [ "$1" = "new-session" ]; then exit "${STUB_NEW_SESSION:-0}"; fi\n'
        "exit 0\n"
    )
    tmux.chmod(0o755)
    # A logging `gh` stub so the issue fetch (title/body) is hermetic. It answers
    # `gh issue view N --json title …` with $GH_ISSUE_TITLE and `--json body …`
    # with $GH_ISSUE_BODY, so a test can seed a `Model:` line into the body
    # (issue #142). The title fetch runs on the no-slug numeric path; the body
    # fetch (Model: override) runs for ANY numbered issue, so the stub keeps both
    # off the network regardless of whether a slug was passed.
    gh = bindir / "gh"
    # Logs every invocation to $GH_LOG (issue #236 lifecycle-label mirror asserts on
    # `gh issue edit` / `gh issue comment` / `gh label create`), still answering the
    # title/body fetches. $GH_MIRROR_RC forces a nonzero exit for the mirror writes
    # (issue edit / comment / label create) so a test can model an offline gh.
    gh.write_text(
        "#!/bin/sh\n"
        # Log each call on ONE line (a multi-line --body is flattened) so the log
        # stays one-record-per-invocation.
        '{ printf "%s" "$*" | tr "\\n" " "; printf "\\n"; } >> "$GH_LOG"\n'
        'case "$*" in\n'
        '  *"--json title"*) printf "%s\\n" "${GH_ISSUE_TITLE:-Some Issue Title}" ;;\n'
        '  *"--json body"*)  printf "%s\\n" "$GH_ISSUE_BODY" ;;\n'
        '  "issue edit"*|"issue comment"*|"label create"*) exit "${GH_MIRROR_RC:-0}" ;;\n'
        "esac\n"
    )
    gh.chmod(0o755)
    # HOME is sandboxed so the workspace-file default ($HOME/.claude/….code-workspace,
    # issue #134) can never resolve to — let alone rewrite — the host's real file.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **_GIT_ENV,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "STUB_HAS_SESSION": str(has_session_rc),
        "STUB_NEW_SESSION": str(new_session_rc),
        # The gh-call log for the lifecycle-label mirror assertions (issue #236).
        "GH_LOG": str(tmp_path / "gh-calls.log"),
    }
    # The host must not force the mirror on/off or inject a mirror exit code.
    env.pop("AI_TOOLKIT_GH_LIFECYCLE_LABELS", None)
    env.pop("GH_MIRROR_RC", None)
    env.pop("TMUX", None)  # the host's real tmux must never steer the script
    # The host's agent pinning must never leak in — defaults are under test.
    env.pop("WT_AGENT_MODEL", None)
    env.pop("WT_AGENT_EFFORT", None)
    env.pop("WT_AGENT_BUDGET_ARGS", None)
    # The config-sourced spoke defaults (issue #142) and the gh-stub seeds must
    # not leak from the host either — each test sets what it needs via extra_env.
    env.pop("WT_AGENT_MODEL_DEFAULT", None)
    env.pop("WT_AGENT_EFFORT_DEFAULT", None)
    env.pop("GH_ISSUE_TITLE", None)
    env.pop("GH_ISSUE_BODY", None)
    # The native-OTel gate (issue #83; default-on per otel-default) and any inherited
    # OTEL_* / telemetry vars must not leak in either: the default-on/opt-out behaviour
    # and the secret-handling are under test, so popping AI_TOOLKIT_OTEL lets a test
    # exercise the genuine unset→default path; the host's own telemetry config (and the
    # secret vars) must never steer the launch.
    for _k in (
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
        # The off-box content flags (auto-populate) and the message-bridge preflight
        # config: the host's own values must not steer the gate-off default, the
        # endpoint defaulting, or — critically — spawn a real bridge during the
        # hermetic launch tests (LANGFUSE_BASIC_AUTH set on the host would do exactly
        # that, since the preflight launches python3 when auth is present).
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "LANGFUSE_BASIC_AUTH",
        "LANGFUSE_HOST",
        "BRIDGE_PORT",
    ):
        env.pop(_k, None)
    if extra_env:
        env.update(extra_env)
    if inside_tmux:
        env["TMUX"] = "/tmp/fake-tmux-socket,1234,0"
    proc = subprocess.run(
        ["bash", str(WORKTREE_NEW), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, log


def _new_window_name(calls: str) -> str:
    """Extract the value passed to `-n` in the `new-window` invocation."""
    line = next(ln for ln in calls.splitlines() if ln.startswith("new-window"))
    tokens = line.split()
    return tokens[tokens.index("-n") + 1]


def _calls(calls: str, command: str) -> list[str]:
    """All logged stub invocations of the given tmux subcommand."""
    return [ln for ln in calls.splitlines() if ln.startswith(command)]


def _session(calls: str) -> str:
    """Extract the derived session name from the `new-window -t '=<sess>:'` call.

    Keeps the session-derivation tests algorithm-agnostic: assert the jump
    commands reuse whatever session the spawn block targeted, rather than
    hardcoding the derivation (issue #39).
    """
    line = next(ln for ln in calls.splitlines() if ln.startswith("new-window"))
    tokens = line.split()
    target = tokens[tokens.index("-t") + 1]  # '=<sess>:'
    assert target.startswith("=") and target.endswith(":"), target
    return target[1:-1]


def _pins_option_off(calls: str, option: str) -> bool:
    """True if some `set-window-option` call targets @1 and sets `option` off."""
    return any(
        ln.startswith("set-window-option") and "-t @1" in ln and f"{option} off" in ln
        for ln in calls.splitlines()
    )


def test_tmux_window_named_with_branch_leaf(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert _new_window_name(calls) == "8-some-slug"


def test_tmux_window_name_pinned_against_rename(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert _pins_option_off(calls, "automatic-rename")
    assert _pins_option_off(calls, "allow-rename")


def test_window_spawned_into_project_session(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    new_window = _calls(calls, "new-window")
    assert new_window, "expected a new-window invocation"
    sess = _session(calls)
    assert sess != "0", "spoke must not land in the hardcoded session 0"
    # the '=' exact-match guard must be preserved so <sess> can't fuzzy-match
    assert f"-t ={sess}:" in new_window[0]


def test_project_session_created_when_missing(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", has_session_rc=1)

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    sess = _session(calls)
    new_session = _calls(calls, "new-session")
    assert new_session, "expected the project session to be created when has-session fails"
    assert "-d" in new_session[0].split()
    assert f"-s {sess}" in new_session[0]
    assert calls.find("new-session") < calls.find("new-window")


def test_project_session_not_recreated_when_present(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", has_session_rc=0)

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    has_session = _calls(calls, "has-session")
    assert has_session, "expected the script to probe for the project session"
    sess = _session(calls)
    assert f"={sess}" in has_session[0], "has-session must exact-match the project session"
    assert not _calls(calls, "new-session")


def test_spawns_via_tmux_even_outside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=False)

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    new_window = _calls(calls, "new-window")
    assert new_window, "expected a new-window invocation even with TMUX unset"
    assert f"-t ={_session(calls)}:" in new_window[0]


def test_dispatch_prints_switch_client_jump_when_inside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=True)

    assert proc.returncode == 0, proc.stderr
    sess = _session(log.read_text())
    assert f"tmux switch-client -t '{sess}:8-some-slug'" in proc.stdout


def test_dispatch_prints_attach_jump_when_outside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=False)

    assert proc.returncode == 0, proc.stderr
    sess = _session(log.read_text())
    assert f"tmux attach -t '{sess}' \\; select-window -t '{sess}:8-some-slug'" in proc.stdout


def test_session_named_after_project_basename(hub: Path, tmp_path: Path) -> None:
    # The hub fixture's repo is basenamed 'hub' — the derived session must carry it.
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    assert "hub" in _session(log.read_text())


def test_session_name_sanitized_for_tmux(tmp_path: Path) -> None:
    # tmux forbids '.' and ':' in session names — a repo dir with a '.' must map it.
    hub = _make_hub(tmp_path, "svc.api")
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    sess = _session(log.read_text())
    assert "." not in sess and ":" not in sess, sess
    assert "svc-api" in sess


def test_same_basename_repos_get_distinct_sessions(tmp_path: Path) -> None:
    # Two repos both basenamed 'proj' under different parents must not collide.
    hub_a = _make_hub(tmp_path / "alpha", "proj")
    hub_b = _make_hub(tmp_path / "beta", "proj")
    proc_a, log_a = _run_new(hub_a, tmp_path / "alpha", "8", "some-slug", "--no-code")
    proc_b, log_b = _run_new(hub_b, tmp_path / "beta", "8", "some-slug", "--no-code")

    assert proc_a.returncode == 0, proc_a.stderr
    assert proc_b.returncode == 0, proc_b.stderr
    assert _session(log_a.read_text()) != _session(log_b.read_text())


def test_no_server_falls_back_to_manual_advice(hub: Path, tmp_path: Path) -> None:
    # A COMPLETE contract (title AND body, issue #206) is needed for task.md to be
    # written and the default kickoff to point at it, so seed both fields.
    proc, log = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        inside_tmux=False,
        has_session_rc=1,
        new_session_rc=1,
        extra_env={"GH_ISSUE_TITLE": "Some Issue Title", "GH_ISSUE_BODY": "Do the thing.\n"},
    )

    assert proc.returncode == 0, proc.stderr
    assert not _calls(log.read_text(), "new-window")
    assert "Start the agent in a new terminal window:" in proc.stdout
    assert "CLAUDE_EFFORT=high claude --model claude-opus-4-8" in proc.stdout
    # The seeded default kickoff points the spoke at its on-disk task contract.
    assert ".ai-toolkit/task.md" in proc.stdout


def test_agent_launch_pins_model_and_effort_by_default(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=high claude --model claude-opus-4-8" in new_window[0]


def test_agent_launch_respects_model_and_effort_overrides(hub: Path, tmp_path: Path) -> None:
    overrides = {"WT_AGENT_MODEL": "sonnet", "WT_AGENT_EFFORT": "high"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=high claude --model sonnet" in new_window[0]


def test_agent_launch_appends_budget_args_when_set(hub: Path, tmp_path: Path) -> None:
    # A best-effort in-process budget cap for unattended spokes: a caller sets
    # WT_AGENT_BUDGET_ARGS, appended after the model (before any seeded prompt).
    # Attended spokes leave it unset → launch unchanged.
    overrides = {"WT_AGENT_BUDGET_ARGS": "--max-budget-usd 5"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude --model claude-opus-4-8 --max-budget-usd 5" in new_window[0]


# ── afk spokes launch under bypassPermissions (issue #261) ──
#
# An afk spoke must NEVER raise a permission dialog — the PreToolUse deny-wall
# (afk-danger-guard) is the safety boundary, not prompt-then-approve. So
# `worktree-new.sh --mode afk` appends `--permission-mode bypassPermissions` to
# the claude launch. Attended/quick lanes (the default MODE=attended) keep default
# prompting — the human is the wall — so their launch is byte-for-byte unchanged.


def test_afk_mode_launches_bypass_permissions(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "--permission-mode bypassPermissions" in new_window[0]


def test_attended_mode_omits_bypass_permissions(hub: Path, tmp_path: Path) -> None:
    # The default (attended) lane keeps default prompting: no bypass flag is added.
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "--permission-mode" not in new_window[0]


def test_afk_bypass_flag_precedes_seeded_prompt(hub: Path, tmp_path: Path) -> None:
    # The bypass flag is a claude flag, so it must sit before the trailing seeded
    # prompt (which stays the last positional arg the agent receives).
    proc, log = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", "--mode", "afk", "--prompt", "/source"
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    launch = new_window[0]
    assert "--permission-mode bypassPermissions" in launch
    assert launch.index("--permission-mode") < launch.index("/source")


def test_agent_launch_keeps_seeded_prompt_after_pinning(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--prompt", "/source")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert (
        "CLAUDE_EFFORT=high claude --model claude-opus-4-8 /source; exec " in new_window[0]
    )


# ── issue #269: the launcher no longer claims bypass removes the dialog family, ─
# and warns at afk spawn when a global permissions.ask rule would pierce it ─────
# #238 proved an explicit user-global `permissions.ask` RULE outranks the
# permission MODE (rules > mode), so a dialog CAN still reach a bypass spoke. The
# stale comment at worktree-new.sh:559 must not claim otherwise, and an afk spawn
# best-effort warns when such a rule exists.


def test_afk_launch_comment_no_longer_claims_dialog_never_raised() -> None:
    src = WORKTREE_NEW.read_text()

    assert "no permission dialog is EVER raised" not in src, (
        "the stale #238-falsified claim must be gone"
    )
    assert "permissions.ask" in src and "pierce" in src.lower(), (
        "the corrected comment must state a permissions.ask rule pierces bypassPermissions"
    )


def _write_ask_settings(home: Path, rule: str) -> None:
    cfg = home / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"permissions": {"ask": [rule]}}) + "\n")


def test_afk_spawn_warns_when_global_ask_rule_present(hub: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _write_ask_settings(home, "Bash(chmod *)")

    proc, _log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    assert "permissions.ask" in proc.stderr, proc.stderr
    assert "bypass" in proc.stderr.lower(), proc.stderr


def test_afk_spawn_silent_without_ask_rule(hub: Path, tmp_path: Path) -> None:
    # No settings.json / no ask rule -> no preflight warning (the common, healthy case).
    proc, _log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    assert "permissions.ask" not in proc.stderr, proc.stderr


def test_attended_spawn_never_runs_ask_preflight(hub: Path, tmp_path: Path) -> None:
    # The preflight is afk-only: an attended spawn (the human IS the wall) never warns,
    # even when a global ask rule is present.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _write_ask_settings(home, "Bash(chmod *)")

    proc, _log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    assert "permissions.ask" not in proc.stderr, proc.stderr


# ── Task contract on disk + task.md seed prompt (issue #177) ──
#
# Anchoring used to be an LLM errand: the seed prompt told the spoke to run
# /source-task, which shells `gh issue view`. The dispatcher already knows the
# issue, so worktree-new.sh writes the contract to <wt>/.ai-toolkit/task.md at
# spawn and its default seed prompt points the spoke there. /source-task stays
# for crash re-anchor (a lost task.md).


def test_writes_task_contract_at_spawn(hub: Path, tmp_path: Path) -> None:
    body = "Do the thing.\n\nScope: scripts/worktree-new.sh\nGate: none\n"
    env = {"GH_ISSUE_TITLE": "Anchor the spoke at spawn", "GH_ISSUE_BODY": body}

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    task_md = _worktree_dir(hub, "8") / ".ai-toolkit" / "task.md"
    assert task_md.is_file(), "task.md must be written at spawn"
    text = task_md.read_text()
    assert "#8" in text, "task.md must carry the issue number"
    assert "Anchor the spoke at spawn" in text, "task.md must carry the issue title"
    assert "Do the thing." in text, "task.md must carry the issue body"
    assert "Scope: scripts/worktree-new.sh" in text, "task.md must carry the Scope: line"
    assert "Gate: none" in text, "task.md must carry the Gate: line"


def test_default_seed_prompt_points_at_task_contract(hub: Path, tmp_path: Path) -> None:
    # With no --prompt, the spoke is seeded to read task.md rather than run an LLM
    # /source-task round-trip. task.md is written only for a COMPLETE contract
    # (title AND body, issue #206), so seed both fields.
    env = {"GH_ISSUE_TITLE": "Some Issue Title", "GH_ISSUE_BODY": "Do the thing.\nGate: none\n"}
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert ".ai-toolkit/task.md" in new_window[0], "default seed prompt must point at task.md"


# ── issue #271: the seed marker command must be runnable in the spawned worktree ──
# The seed prompt hardcoded `bash .ai-toolkit/scripts/spoke-ready.sh …` — correct for a synced
# TARGET, but WRONG in the ai-toolkit checkout, where the emitters are tracked at scripts/ and a
# fresh worktree's .ai-toolkit/ has no scripts/. A spoke then wasted judge round-trips on a path
# that neither the deny-wall approved nor exec'd, and could never park. worktree-new.sh now
# resolves the path that actually EXISTS in the worktree (wt_marker_script_dir).


def _seed_hub_marker_scripts(hub: Path) -> None:
    """Track scripts/spoke-ready.sh + spoke-push.sh in the hub — THIS repo's layout."""
    scripts = hub / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in ("spoke-ready.sh", "spoke-push.sh"):
        (scripts / name).write_text("#!/usr/bin/env bash\ntrue\n")
        (scripts / name).chmod(0o755)
    _git(hub, "add", "scripts")
    _git(hub, "commit", "-qm", "chore: add marker scripts", "-m", "Refs #0")
    _git(hub, "push", "-q", "origin", "main")


def test_seed_prompt_marker_path_runnable_in_toolkit_worktree(hub: Path, tmp_path: Path) -> None:
    # #271 criterion 4: in a checkout that TRACKS scripts/spoke-ready.sh (the ai-toolkit repo),
    # the dispatched contract's marker command must name `scripts/…`, not the nonexistent
    # `.ai-toolkit/scripts/…`, so it is runnable VERBATIM in the fresh worktree.
    _seed_hub_marker_scripts(hub)
    env = {"GH_ISSUE_TITLE": "Some Issue Title", "GH_ISSUE_BODY": "Do the thing.\nGate: plan\n"}
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    # tmux logs the prompt with backslash-escaped spaces; strip them to test the plain text.
    new_window = _calls(log.read_text(), "new-window")[0].replace("\\", "")
    assert "bash scripts/spoke-ready.sh --gate" in new_window, new_window
    assert "bash scripts/spoke-push.sh --ready" in new_window, new_window
    assert ".ai-toolkit/scripts/spoke-ready.sh" not in new_window, new_window
    wt = _worktree_dir(hub, "8")
    assert (wt / "scripts" / "spoke-ready.sh").is_file(), "the marker script must exist to run"


def test_seed_prompt_marker_allowlist_matches_toolkit_layout(hub: Path) -> None:
    # The seeded harness allow rule must match the SAME path the prompt names, or an attended
    # spoke re-prompts on every marker emission.
    _seed_hub_marker_scripts(hub)
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    assert "Bash(bash scripts/spoke-ready.sh:*)" in allow, allow
    assert "Bash(bash scripts/spoke-push.sh:*)" in allow, allow


def test_seed_prompt_marker_path_defaults_for_synced_layout(hub: Path, tmp_path: Path) -> None:
    # A hub WITHOUT tracked scripts/ (a synced target's shape) keeps the historical
    # `.ai-toolkit/scripts/` path so nothing regresses for targets.
    env = {"GH_ISSUE_TITLE": "T", "GH_ISSUE_BODY": "Do the thing.\nGate: plan\n"}
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")[0].replace("\\", "")
    assert "bash .ai-toolkit/scripts/spoke-ready.sh --gate" in new_window, new_window


def test_partial_gh_failure_writes_no_task_contract(hub: Path, tmp_path: Path) -> None:
    # Issue #206 — if the body fetch fails (empty) after the title fetch succeeds, a
    # title-only task.md would exist but be empty of contract, and its mere existence
    # suppresses the spoke's /source-task fallback. task.md must be written only when
    # COMPLETE (title AND body); a partial fetch leaves none so the fallback engages.
    env = {"GH_ISSUE_TITLE": "Title only", "GH_ISSUE_BODY": ""}
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    task_md = _worktree_dir(hub, "8") / ".ai-toolkit" / "task.md"
    assert not task_md.exists(), "a title-only (partial) fetch must not write a hollow task.md"
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert ".ai-toolkit/task.md" not in new_window[0], (
        "with no complete contract the seed must fall back, not point at a hollow task.md"
    )


def test_task_contract_written_atomically_without_leftover_temp(hub: Path, tmp_path: Path) -> None:
    # Issue #206 — task.md is written atomically (temp file + rename), so no partial
    # scratch file (task.md.*) survives alongside the final contract.
    env = {"GH_ISSUE_TITLE": "T", "GH_ISSUE_BODY": "Do the thing.\nGate: none\n"}
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    ai_dir = _worktree_dir(hub, "8") / ".ai-toolkit"
    assert (ai_dir / "task.md").is_file(), "the complete contract must be written"
    leftovers = sorted(p.name for p in ai_dir.glob("task.md.*"))
    assert leftovers == [], f"atomic write must leave no temp file: {leftovers}"


# ── Ledger skeleton seed (issue #235) ──
#
# The step spine is script-stamped, but the todo-ledger LABELS still come from the
# spoke. So structure is inherited, not invented: worktree-new.sh pre-seeds a
# gitignored .ai-toolkit/ledger-skeleton.md — one `#<issue>.<slug> · <STEP> — <label>`
# row per subtask x ANCHOR/RED/GREEN/REVIEW/PUSH — from the task.md body. A body with
# no machine-readable subtask list gets a single `#<issue>.main` skeleton.


def test_seeds_ledger_skeleton_for_a_complete_contract(hub: Path, tmp_path: Path) -> None:
    env = {"GH_ISSUE_TITLE": "Scripted step spine", "GH_ISSUE_BODY": "Do it.\nGate: none\n"}

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    skeleton = _worktree_dir(hub, "8") / ".ai-toolkit" / "ledger-skeleton.md"
    assert skeleton.is_file(), "a complete contract must seed the ledger skeleton"
    text = skeleton.read_text()
    # No machine-readable subtasks -> a single #8.main subtask across all five steps.
    for step in ("ANCHOR", "RED", "GREEN", "REVIEW", "PUSH"):
        assert f"#8.main · {step} — " in text, f"missing {step} row in the guard format"


def test_ledger_skeleton_derives_a_row_per_subtask(hub: Path, tmp_path: Path) -> None:
    body = (
        "Intro.\n\n"
        "## Subtasks\n\n"
        "- Marker spine builder\n"
        "- Ledger schema guard\n\n"
        "## Acceptance criteria\n\n- [ ] done\nGate: none\n"
    )
    env = {"GH_ISSUE_TITLE": "T", "GH_ISSUE_BODY": body}

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    text = (_worktree_dir(hub, "8") / ".ai-toolkit" / "ledger-skeleton.md").read_text()
    assert "#8.marker-spine-builder · RED — " in text
    assert "#8.ledger-schema-guard · GREEN — " in text
    assert "#8.main" not in text, "with parsed subtasks, no generic main row is emitted"


def test_no_ledger_skeleton_without_a_task_contract(hub: Path, tmp_path: Path) -> None:
    # A partial gh fetch writes no task.md; without a contract there is nothing to seed.
    env = {"GH_ISSUE_TITLE": "Title only", "GH_ISSUE_BODY": ""}

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    skeleton = _worktree_dir(hub, "8") / ".ai-toolkit" / "ledger-skeleton.md"
    assert not skeleton.exists(), "no task contract -> no ledger skeleton"


def test_explicit_prompt_overrides_default_task_contract_seed(hub: Path, tmp_path: Path) -> None:
    # An explicit --prompt (start-task, hub-afk's kickoff_for) still wins — the
    # default task.md seed only fills an unset prompt.
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--prompt", "/source")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    # The explicit prompt is the trailing arg; the default task.md seed never fires.
    # Match the model→prompt boundary without pinning $SHELL (the `; exec <shell>`
    # suffix is shell-dependent and covered by test_window_survives_agent_exit_via_exec_shell).
    assert "claude --model claude-opus-4-8 /source; exec " in new_window[0]
    assert ".ai-toolkit/task.md" not in new_window[0]


def test_no_task_contract_for_adhoc_slug(hub: Path, tmp_path: Path) -> None:
    # Ad-hoc (non-numeric) work has no issue to fetch — no task.md, and the
    # default seed does not point at one.
    proc, log = _run_new(hub, tmp_path, "refactor-sync", "-t", "chore", "--no-code")

    assert proc.returncode == 0, proc.stderr
    task_md = _worktree_dir(hub, "refactor-sync") / ".ai-toolkit" / "task.md"
    assert not task_md.exists(), "ad-hoc slugs have no issue, so no task.md"
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert ".ai-toolkit/task.md" not in new_window[0]


# ── Per-issue Model: override + config spoke default (issue #142) ──


def test_issue_model_line_pins_spoke_model(hub: Path, tmp_path: Path) -> None:
    # A numeric issue (hub flow, no slug) whose body carries a `Model:` line
    # spawns its spoke on that model. A leading lowercase `model:` proves
    # case-insensitivity; the second `Model:` proves first-match-wins.
    body = "Some description.\n\nmodel: claude-sonnet-5\nModel: claude-opus-4-8\n"

    proc, log = _run_new(hub, tmp_path, "8", "--no-code", extra_env={"GH_ISSUE_BODY": body})

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude --model claude-sonnet-5" in new_window[0]


def test_env_model_overrides_issue_model_line(hub: Path, tmp_path: Path) -> None:
    # An explicit WT_AGENT_MODEL always wins over a body `Model:` line.
    env = {"GH_ISSUE_BODY": "Model: claude-sonnet-5\n", "WT_AGENT_MODEL": "opus-pinned"}

    proc, log = _run_new(hub, tmp_path, "8", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude --model opus-pinned" in new_window[0]


def test_spoke_default_sourced_from_config_env(hub: Path, tmp_path: Path) -> None:
    # Absent an explicit model / Model: line, the spoke driver takes the
    # config-sourced default (WT_AGENT_MODEL_DEFAULT / WT_AGENT_EFFORT_DEFAULT,
    # emitted into spoke-model.env by sync), not a hardcoded literal.
    env = {"WT_AGENT_MODEL_DEFAULT": "team-opus", "WT_AGENT_EFFORT_DEFAULT": "high"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=high claude --model team-opus" in new_window[0]


def test_spoke_default_resolved_from_config_file(hub: Path, tmp_path: Path) -> None:
    # The hub fallback reads the config directly (via the parser) and evals its
    # spoke-env output — driving the real load path, not just the env fallback.
    cfg = tmp_path / "ai-toolkit.yml"
    cfg.write_text("model:\n  spoke:\n    model: config-file-model\n    effort: low\n")

    proc, log = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", extra_env={"AI_TOOLKIT_CONFIG": str(cfg)}
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=low claude --model config-file-model" in new_window[0]


def test_agent_launch_shell_quotes_metacharacter_overrides(hub: Path, tmp_path: Path) -> None:
    overrides = {"WT_AGENT_MODEL": "foo bar"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude --model foo\\ bar" in new_window[0]


def test_agent_launch_never_typed_via_send_keys(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--prompt", "/source")

    assert proc.returncode == 0, proc.stderr
    assert not _calls(log.read_text(), "send-keys"), (
        "launch must be the new-window shell-command argument, never typed via send-keys"
    )


def test_window_survives_agent_exit_via_exec_shell(hub: Path, tmp_path: Path) -> None:
    overrides = {"SHELL": "/bin/zsh"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert new_window[0].endswith("; exec /bin/zsh")


def test_no_agent_spawns_plain_window(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--no-agent")

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    new_window = _calls(calls, "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude" not in new_window[0]
    assert not _calls(calls, "send-keys")


# ── WT_SPOKE session marker (issue #26) ──────────────────────────────────────
# worktree-new.sh tags the spawned spoke's environment with WT_SPOKE=<issue-or-
# slug>. Every command that spoke runs inherits it (regardless of how it cd's
# around), which is what worktree-land.sh / worktree-done.sh key off to refuse a
# spoke that tries to land or tear down its own worktree.


def test_agent_launch_injects_wt_spoke_marker_for_numbered_issue(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    # The marker prefixes the launch and precedes the existing CLAUDE_EFFORT pin.
    assert "WT_SPOKE=8 CLAUDE_EFFORT=high claude --model claude-opus-4-8" in new_window[0]


def test_agent_launch_injects_wt_spoke_marker_for_adhoc_slug(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "fix-parser", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert (
        "WT_SPOKE=fix-parser CLAUDE_EFFORT=high claude --model claude-opus-4-8"
        in new_window[0]
    )


def test_manual_fallback_advice_carries_wt_spoke_marker(hub: Path, tmp_path: Path) -> None:
    # When no tmux server can be started, the printed manual launch command must
    # still carry the marker so a hand-started spoke is governed by the guard too.
    proc, _ = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        inside_tmux=False,
        has_session_rc=1,
        new_session_rc=1,
    )

    assert proc.returncode == 0, proc.stderr
    assert "WT_SPOKE=8 CLAUDE_EFFORT=high claude --model claude-opus-4-8" in proc.stdout


# ── Native-OTel default-on (issues #83, otel-default) ────────────────────────
# The spoke launch is prefixed with Claude Code's native OpenTelemetry trace env so
# the interactive `claude` streams ONE nested trace per spoke, grouped by the
# already-minted spoke_run_id (carried as a resource attribute). The prefix is the
# same WT_SPOKE/CLAUDE_EFFORT command-prefix lever, so it reaches the interactive
# session AND the manual-fallback advice. It is ON BY DEFAULT (the prefix is present
# unless the operator sets AI_TOOLKIT_OTEL=0 for a clean full opt-out), distinct from
# the custom push layer's AI_TOOLKIT_TELEMETRY gate. Only the non-secret enabling + identity vars
# are wired by the script; the connection target (OTEL_EXPORTER_OTLP_ENDPOINT and
# the auth-bearing OTEL_EXPORTER_OTLP_HEADERS) is operator-provided via the
# inherited environment and must never be written into the command line (it would
# leak via `ps`) or printed.

_OTEL_NONSECRET_VARS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY=1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1",
    "OTEL_TRACES_EXPORTER=otlp",
    # gRPC for the normal stream: the beta detailed exporter is HTTP-only, so normal
    # takes gRPC and beta takes HTTP — the arrangement proven to land in Langfuse.
    "OTEL_EXPORTER_OTLP_PROTOCOL=grpc",
    # Metrics + detailed-tracing signals (issue #88): metrics carry token-by-type/
    # skill/agent + cost_usd, detailed tracing adds response.model_output /
    # system_reminders. Logs export is made explicit (not inherited). account_uuid
    # is forced off for metrics — PII rides every datapoint otherwise.
    "OTEL_METRICS_EXPORTER=otlp",
    "OTEL_LOGS_EXPORTER=otlp",
    "ENABLE_BETA_TRACING_DETAILED=1",
    "OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false",
)

# Auto-populate (this issue): with the gate on, the spoke also ships its prompts +
# tool I/O off-box so Langfuse renders conversation + per-tool content. These flags
# send content off the machine, so they are consciously gated by AI_TOOLKIT_OTEL.
_OTEL_CONTENT_FLAGS = (
    "OTEL_LOG_USER_PROMPTS=1",
    "OTEL_LOG_TOOL_DETAILS=1",
    "OTEL_LOG_TOOL_CONTENT=1",
)

# The connection endpoints are non-secret URLs (auth rides the OTEL_EXPORTER_OTLP_HEADERS
# secret, never wired). Auto-populate defaults them to the local collector when the
# operator left them unset — the normal stream over gRPC (4317), the beta detailed
# stream over HTTP (4418). They MUST differ in host:port, or beta silently kills all
# trace+log export. Both are wired onto the command line (unlike the auth header).
_OTEL_ENDPOINT_DEFAULTS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317",
    "BETA_TRACING_ENDPOINT=http://localhost:4418",
    # Workflow-span sink (#126): telemetry.sh's second OTLP fan-out posts HTTP-JSON
    # to $AI_TOOLKIT_OTEL_SPAN_ENDPOINT/v1/traces, so its default is the collector's
    # OTLP-HTTP listener — not the gRPC :4317 the native stream uses.
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT=http://localhost:4318",
)


def test_agent_launch_injects_otel_env_by_default(hub: Path, tmp_path: Path) -> None:
    # Gate UNSET → OTel is ON by default (issue: otel-default): the launch carries the
    # full native-OTel prefix — all non-secret enabling vars + content flags + endpoint
    # defaults + the resource attribute — with no opt-in needed. Every spoke streams
    # its trace. The only opt-out is an explicit AI_TOOLKIT_OTEL=0 (tested separately).
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    cmd = new_window[0]
    for var in (*_OTEL_NONSECRET_VARS, *_OTEL_CONTENT_FLAGS, *_OTEL_ENDPOINT_DEFAULTS):
        assert var in cmd, f"expected {var} in the default (on) launch"
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=feature/8-some-slug+" in cmd
    assert cmd.index("CLAUDE_CODE_ENABLE_TELEMETRY=1") < cmd.index("WT_SPOKE=8")


def test_agent_launch_omits_otel_env_when_disabled(hub: Path, tmp_path: Path) -> None:
    # AI_TOOLKIT_OTEL=0 → a clean, full opt-out: the launch is byte-for-byte the
    # non-OTel launch (no telemetry env, no content flags, no resource attribute, no
    # endpoint defaults). No half-on state.
    proc, log = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", extra_env={"AI_TOOLKIT_OTEL": "0"}
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    absent = (
        *_OTEL_NONSECRET_VARS,
        *_OTEL_CONTENT_FLAGS,
        "OTEL_RESOURCE_ATTRIBUTES=",
        "OTEL_EXPORTER_OTLP_ENDPOINT=",
        "BETA_TRACING_ENDPOINT=",
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT=",
    )
    for var in absent:
        assert var not in new_window[0], f"{var} must be absent when AI_TOOLKIT_OTEL=0"


def test_agent_launch_injects_otel_env_when_opted_in(hub: Path, tmp_path: Path) -> None:
    # AI_TOOLKIT_OTEL=1 → all non-secret enabling vars precede the WT_SPOKE pin,
    # and the resource attribute carries the minted spoke_run_id (<branch>+<epoch>),
    # so every span of this spoke groups under it.
    proc, log = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", extra_env={"AI_TOOLKIT_OTEL": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    cmd = new_window[0]
    for var in (*_OTEL_NONSECRET_VARS, *_OTEL_CONTENT_FLAGS, *_OTEL_ENDPOINT_DEFAULTS):
        assert var in cmd, f"expected {var} in the opted-in launch"
    # The footgun: the beta detailed endpoint MUST be a different host:port than the
    # normal OTLP endpoint, or it silently kills ALL trace+log export. The defaults
    # honour the split (gRPC :4317 vs HTTP :4418).
    assert ":4317" in cmd and ":4418" in cmd
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=feature/8-some-slug+" in cmd
    # The whole OTel prefix precedes the existing WT_SPOKE/CLAUDE_EFFORT pin, so
    # the launch still pins model+effort+role unchanged.
    assert "WT_SPOKE=8 CLAUDE_EFFORT=high claude --model claude-opus-4-8" in cmd
    assert cmd.index("CLAUDE_CODE_ENABLE_TELEMETRY=1") < cmd.index("WT_SPOKE=8")
    assert cmd.index("OTEL_RESOURCE_ATTRIBUTES=") < cmd.index("WT_SPOKE=8")


def test_agent_launch_wires_raw_request_body_file_mode(hub: Path, tmp_path: Path) -> None:
    # AI_TOOLKIT_OTEL=1 → Claude Code dumps each untruncated request to a per-spoke
    # dir under the gitignored .ai-toolkit/ (file mode, not the 60KB inline cap), and
    # that dir is exported as AI_TOOLKIT_OTEL_BODY_DIR so the post-run spoke-tree
    # builder can itemize loaded context from it (#87). The logs exporter is wired
    # too, since the raw-body path rides the logs signal.
    proc, log = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", extra_env={"AI_TOOLKIT_OTEL": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    cmd = _calls(log.read_text(), "new-window")[0]
    assert "OTEL_LOGS_EXPORTER=otlp" in cmd
    assert "OTEL_LOG_RAW_API_BODIES=file:" in cmd
    assert ".ai-toolkit/raw-bodies" in cmd
    assert "AI_TOOLKIT_OTEL_BODY_DIR=" in cmd


def test_agent_launch_wires_endpoint_but_never_auth_header(hub: Path, tmp_path: Path) -> None:
    # Auto-populate reclassifies the OTLP endpoint as a non-secret URL: with the gate
    # on, an operator override is PRESERVED and wired onto the command line (only
    # defaulted when unset). The auth-bearing header (OTEL_EXPORTER_OTLP_HEADERS) is
    # the real secret — it is never wired and must NEVER reach the command line
    # (ps-visible); it stays inherited env for `claude` to read.
    operator_endpoint = "http://collector.internal:4317"
    secret_headers = "Authorization=Basic c2VjcmV0LXRva2VuLXZhbHVl"
    proc, log = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        extra_env={
            "AI_TOOLKIT_OTEL": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": operator_endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": secret_headers,
        },
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    cmd = new_window[0]
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={operator_endpoint}" in cmd, (
        "operator override must be wired"
    )
    assert secret_headers not in cmd, "auth header value must never be on the command line"
    assert "OTEL_EXPORTER_OTLP_HEADERS=" not in cmd, "auth header var must never be wired"


def test_agent_launch_wires_beta_tracing_endpoint(hub: Path, tmp_path: Path) -> None:
    # BETA_TRACING_ENDPOINT is the detailed-tracing target — a non-secret URL that —
    # per the probe footgun — MUST point at a different host:port than the normal
    # OTLP endpoint or it silently kills all trace+log export. Auto-populate preserves
    # an operator override (and defaults it to the HTTP beta port when unset), wiring
    # it onto the command line alongside the enabling flag.
    beta_endpoint = "http://collector.internal:4418"
    proc, log = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        extra_env={"AI_TOOLKIT_OTEL": "1", "BETA_TRACING_ENDPOINT": beta_endpoint},
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "ENABLE_BETA_TRACING_DETAILED=1" in new_window[0], "detailed flag must be wired"
    assert f"BETA_TRACING_ENDPOINT={beta_endpoint}" in new_window[0], (
        "operator override must be wired"
    )


def test_agent_launch_wires_workflow_span_endpoint_override(hub: Path, tmp_path: Path) -> None:
    # AI_TOOLKIT_OTEL_SPAN_ENDPOINT is the workflow-span family's sink (#126): the
    # cycle step:/script/hook spans telemetry.sh emits are gated on it, and no launch
    # path exported it — the family was built but unplugged. Like the sibling
    # endpoints it is a non-secret URL: defaulted when unset (asserted via
    # _OTEL_ENDPOINT_DEFAULTS in the launch tests), operator override preserved here.
    operator_span_endpoint = "http://collector.internal:4318"
    proc, log = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        extra_env={
            "AI_TOOLKIT_OTEL": "1",
            "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": operator_span_endpoint,
        },
    )

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert f"AI_TOOLKIT_OTEL_SPAN_ENDPOINT={operator_span_endpoint}" in new_window[0], (
        "operator override must be wired"
    )


def test_manual_fallback_advice_never_prints_otel_secrets(hub: Path, tmp_path: Path) -> None:
    # The manual-fallback echo is the real stdout-leak vector (the tmux path never
    # echoes the launch). With the gate on and an auth header present, the printed
    # advice must carry the non-secret prefix (including the endpoint URL) yet never
    # reveal the auth header — the one secret that must stay off both cmdline and stdout.
    operator_endpoint = "http://collector.internal:4317"
    secret_headers = "Authorization=Basic c2VjcmV0LXRva2VuLXZhbHVl"
    proc, _ = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        inside_tmux=False,
        has_session_rc=1,
        new_session_rc=1,
        extra_env={
            "AI_TOOLKIT_OTEL": "1",
            "OTEL_EXPORTER_OTLP_ENDPOINT": operator_endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": secret_headers,
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in proc.stdout, "fallback must carry the prefix"
    assert operator_endpoint in proc.stdout, "non-secret endpoint URL may (and does) appear"
    assert secret_headers not in proc.stdout, "auth header must never be printed in the fallback"
    assert "OTEL_EXPORTER_OTLP_HEADERS=" not in proc.stdout, "auth header var must never be printed"


def test_manual_fallback_advice_carries_otel_env(hub: Path, tmp_path: Path) -> None:
    # No tmux server → the printed manual launch command must carry the OTel prefix
    # too, so a hand-started spoke still streams its trace.
    proc, _ = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        inside_tmux=False,
        has_session_rc=1,
        new_session_rc=1,
        extra_env={"AI_TOOLKIT_OTEL": "1"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in proc.stdout
    assert "OTEL_RESOURCE_ATTRIBUTES=spoke_run_id=feature/8-some-slug+" in proc.stdout


# ── Command-allowlist templating (issues #11, #37) ───────────────────────────
# A quiet runner (no VS Code, no tmux) for the settings.local.json assertions —
# distinct from the tmux-stub `_run_new` above, which the window/agent tests need.

# The single allowlistable PUSH process rule (#37) — branch-independent, unlike
# the old bare-push rules it replaces.
SCRIPT_RULE = "Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)"

# The single allowlistable marker-emit process rule (#45) — emits ready/N and
# gate/N via one command so the chained `git tag … && git push …` never re-prompts.
READY_SCRIPT_RULE = "Bash(bash .ai-toolkit/scripts/spoke-ready.sh:*)"

# Tier 1 — read-only, no side effects.
TIER1_RULES = [
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git rev-parse:*)",
    "Bash(git branch --show-current)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(grep:*)",
    "Bash(rg:*)",
    "Bash(find:*)",
    "Bash(echo:*)",
    "Bash(tree:*)",
]

# Tier 2 — network-read / read-only GitHub.
TIER2_RULES = [
    "Bash(git fetch:*)",
    "Bash(git remote -v)",
    "Bash(git stash list)",
    "Bash(gh issue view:*)",
    "Bash(gh pr view:*)",
]

# Test runner + `chmod +x` (issue #38). The spoke's inner loop is RED→GREEN→test,
# so prompting on every run defeats autonomy; the GIT_DIR-leak hazard is confined to
# pytest *inside the pre-push hook*, not manual worktree runs, so these are safe.
# Scoped narrowly: only the runner verbs and `chmod +x`, never bare `python`/`chmod`.
TIER_RUNNER_RULES = [
    "Bash(python -m pytest:*)",
    "Bash(.venv/bin/python -m pytest:*)",
    "Bash(pytest:*)",
    "Bash(chmod +x:*)",
]

# Own-worktree staging (issue #149). The RED-commit selective stage —
# `git reset -q; git add <own file>` — stalled every spoke's FIRST red commit under
# /afk. `git add` is worktree-confined (cannot stage a path outside the repo); the
# `git reset` unstage forms are seeded ONLY in their non-destructive shapes — never
# the broad `git reset:*`, which would hand over `git reset --hard` (a working-tree
# wipe). Residual reset shapes the supervisor's classifier auto-approves instead.
TIER_STAGING_RULES = [
    "Bash(git add:*)",
    "Bash(git reset)",
    "Bash(git reset -q)",
    "Bash(git reset HEAD:*)",
    "Bash(git reset -q HEAD:*)",
]

# In-worktree self-script execution (issue #259). Claude Code evaluates a COMPOUND Bash
# command per-segment against `permissions.allow` and a PreToolUse hook's whole-command
# `allow` does NOT satisfy that per-segment check (deny > ask > allow > default-prompt). So
# the #253 afk-permission-hook — which classifies the WHOLE command correctly and emits
# `allow` — cannot suppress the dialog for the #238 compound `chmod +x X && ./X`: the `./X`
# segment matches no rule and re-prompts. Seeding `Bash(./:*)` covers that segment
# deterministically (the exec lane classify_permission already APPROVEs, #240). Unlike the
# hook it is ALWAYS-ON, so a bare `Bash(./:*)` breadth is the deliberate trade-off — the
# rm/push/chmod/main/spoke-main DENY hooks + the ship gates remain authoritative.
TIER_EXEC_RULES = [
    "Bash(./:*)",
]

SEEDED_RULES = [
    SCRIPT_RULE,
    READY_SCRIPT_RULE,
    *TIER1_RULES,
    *TIER2_RULES,
    *TIER_RUNNER_RULES,
    *TIER_STAGING_RULES,
    *TIER_EXEC_RULES,
]

# Wildcards that must NEVER be seeded — each would hand over a destructive verb
# (`git branch -D`, `git tag -d`, an arbitrary push refspec, etc.) or arbitrary
# code execution (`python … -c …`) / unrestricted mode bits (`chmod -R 777 …`).
FORBIDDEN_RULES = [
    "Bash(git branch:*)",
    "Bash(git tag:*)",
    "Bash(git push:*)",
    "Bash(git checkout:*)",
    "Bash(git reset:*)",
    # The destructive working-tree wipe stays gated even though narrow reset
    # unstage forms are now seeded (issue #149).
    "Bash(git reset --hard)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(python:*)",
    "Bash(python -c:*)",
    "Bash(chmod:*)",
    "Bash(rm:*)",
    "Bash(mv:*)",
]


# The AskUserQuestion deny-wall (issue #281). A spoke must ask in prose plus a gate marker,
# never an AskUserQuestion: the /afk injector's Esc-first menu-cancel (built for the #74
# PLAN-gate QCM) can only CANCEL a spoke's QCM, never select an option, so the pane is left
# showing "User declined to answer questions" while the injector types a free-text answer to a
# question the spoke never got answered. Denying the tool structurally removes the QCM the
# broker cannot drive. `AskUserQuestion` is the CANONICAL tool name — rules match the canonical
# name only, and a rule written against a display label silently never matches.
DENY_RULES = [
    "AskUserQuestion",
]


def _bare_push_rules(branch: str) -> list[str]:
    """The two exact-match push rules issue #37 drops."""
    return [f"Bash(git push origin {branch})", f"Bash(git push -u origin {branch})"]


def _run_new_quiet(hub: Path, *args: str) -> subprocess.CompletedProcess:
    """Run worktree-new.sh from the hub, hermetically (no VS Code, tmux, or gh).

    The per-issue Model: fetch (issue #142) calls `gh issue view` for any numbered
    issue, so a no-output `gh` stub on PATH (plus stripped GH_* auth) keeps these
    runs off the network — the issue body is empty, so no Model: override applies.
    """
    bindir = hub.parent / f"{hub.name}-quiet-bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    for _k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_REPO", "GH_HOST"):
        env.pop(_k, None)
    return subprocess.run(
        ["bash", str(WORKTREE_NEW), *args, "--no-code", "--no-terminal"],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _worktree_dir(hub: Path, tag: str) -> Path:
    """The spoke path worktree-new.sh derives: <parent>/<hub-basename>-<tag>."""
    return hub.parent / f"{hub.name}-{tag}"


def _load_allowlist(wt: Path) -> dict:
    settings = wt / ".claude" / "settings.local.json"
    assert settings.is_file(), f"missing {settings}"
    return json.loads(settings.read_text())


def _seed_hub_claude(hub: Path, settings: dict | None = None) -> Path:
    """Give the hub a `.claude/` with a marker skill file and optional settings."""
    marker = hub / ".claude" / "skills" / "x.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("marker\n")
    if settings is not None:
        (hub / ".claude" / "settings.local.json").write_text(json.dumps(settings))
    return marker


def test_seeds_spoke_command_allowlist(hub: Path) -> None:
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in SEEDED_RULES:
        assert rule in allow, f"missing seeded rule: {rule}"


def test_afk_spoke_denies_askuserquestion(hub: Path) -> None:
    """#281 head (c): an afk spoke gets a structural AskUserQuestion deny-wall.

    The /afk injector cannot answer a QCM — inject_answer sends Escape first (the #74
    PLAN-gate menu-cancel), which CANCELS a spoke's AskUserQuestion rather than selecting an
    option, then types free text at the prompt behind it. In #271 that left the pane reading
    "User declined to answer questions" while the drain believed it had answered. The spoke's
    own PLAN gate does not need the tool: it is tag-based (spoke-ready.sh --gate writes
    gate/<N>), so the wall cannot strand it.

    The rule is BARE (no parenthesised specifier), which removes the tool from the spoke's
    context entirely rather than erroring on use — so the spoke never renders a QCM at all.
    """
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    settings = _load_allowlist(_worktree_dir(hub, "99"))
    deny = settings["permissions"].get("deny", [])
    for rule in DENY_RULES:
        assert rule in deny, f"missing seeded deny rule: {rule} (got {deny})"


def test_attended_spoke_keeps_askuserquestion(hub: Path) -> None:
    """The wall is afk-ONLY: an attended spoke has a human at the keyboard who can answer a
    QCM, and nothing Esc-cancels it. Mirrors the afk-only `--permission-mode bypassPermissions`
    gating — attended lanes keep the interactive surfaces the human is there to drive.
    """
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    settings = _load_allowlist(_worktree_dir(hub, "99"))
    deny = settings["permissions"].get("deny", [])
    assert "AskUserQuestion" not in deny, f"attended spoke must not be walled: {deny}"


def test_deny_merge_preserves_existing_rules(hub: Path) -> None:
    """The jq merge path must APPEND to a pre-existing deny list, never replace it — dropping
    a user's own deny rule would silently widen what the spoke may do.
    """
    _seed_hub_claude(hub, settings={"permissions": {"deny": ["Bash(curl:*)"], "allow": []}})

    proc = _run_new_quiet(hub, "99", "pushguard", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    deny = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["deny"]
    assert "Bash(curl:*)" in deny, f"pre-existing deny rule dropped: {deny}"
    assert "AskUserQuestion" in deny, f"seeded deny rule missing: {deny}"


def test_drops_bare_push_rules(hub: Path) -> None:
    # The two exact-match push rules are replaced by the script rule (#37) — a
    # decorated/chained push never matched them, so they only added noise.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in _bare_push_rules("feature/99-pushguard"):
        assert rule not in allow, f"dropped bare-push rule still seeded: {rule}"


def test_no_destructive_wildcards_seeded(hub: Path) -> None:
    # Read-only tiers only — never a wildcard that hands over a destructive verb.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in FORBIDDEN_RULES:
        assert rule not in allow, f"destructive wildcard seeded: {rule}"


def test_seeds_test_runner_and_chmod_exec(hub: Path) -> None:
    # Issue #38 — the spoke's RED→GREEN→test loop and `chmod +x` on new scripts
    # run without a prompt; the bare `python`/`chmod` verbs stay gated.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in TIER_RUNNER_RULES:
        assert rule in allow, f"missing seeded runner rule: {rule}"
    for rule in ("Bash(python:*)", "Bash(python -c:*)", "Bash(chmod:*)"):
        assert rule not in allow, f"arbitrary-exec wildcard seeded: {rule}"


def test_seeds_own_worktree_staging(hub: Path) -> None:
    # Issue #149 — the RED-commit selective stage `git reset -q; git add <file>` runs
    # without a prompt so an unattended spoke's first red commit never stalls; only the
    # non-destructive reset shapes are seeded, never `git reset --hard`.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in TIER_STAGING_RULES:
        assert rule in allow, f"missing seeded staging rule: {rule}"
    for rule in ("Bash(git reset:*)", "Bash(git reset --hard)", "Bash(git reset --hard:*)"):
        assert rule not in allow, f"destructive reset wildcard seeded: {rule}"


def test_seeds_exec_lane_for_compound_self_ops(hub: Path) -> None:
    # Issue #259 — the #238 compound-chmod smoke `chmod +x X && ./X` re-prompted because CC
    # evaluates the compound per-segment: `chmod +x X` matched `Bash(chmod +x:*)` but the
    # `./X` segment matched no rule, and the afk-permission-hook's whole-command `allow` does
    # not cover a per-segment gap. Seeding `Bash(./:*)` covers the exec segment so BOTH
    # segments of the smoke are allowlisted and the drain resolves it with no dialog.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    for rule in TIER_EXEC_RULES:
        assert rule in allow, f"missing seeded exec-lane rule: {rule}"
    # The #238 compound's two segments are each covered by a seeded rule.
    assert "Bash(chmod +x:*)" in allow, "chmod segment of the #238 compound not covered"
    assert "Bash(./:*)" in allow, "./script segment of the #238 compound not covered"


def test_seeds_read_access_to_hub_root(hub: Path) -> None:
    # Issue #181 — spokes routinely study hub scripts/hooks OUTSIDE their own worktree (e.g.
    # reading the hub's .git/hooks/pre-push to understand the push cage). Seed read access to
    # the hub root subtree so those write-free research reads never fire a permission dialog.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    expected = f"Read(/{os.path.realpath(hub)}/**)"
    assert expected in allow, f"missing hub-root read rule: {expected}\n{allow}"


def test_seeds_no_write_access_to_hub_root(hub: Path) -> None:
    # The #181 seeding is READ-only — never an Edit/Write into the hub, which a spoke must
    # never mutate. Guard against a broadened rule slipping in.
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    root = os.path.realpath(hub)
    for forbidden in (f"Edit(/{root}/**)", f"Write(/{root}/**)"):
        assert forbidden not in allow, f"write access to the hub root seeded: {forbidden}"


def test_copied_runtime_config_still_present(hub: Path) -> None:
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    copied_marker = _worktree_dir(hub, "99") / ".claude" / "skills" / "x.md"
    assert copied_marker.is_file()


def test_creates_allowlist_without_hub_claude_dir(hub: Path) -> None:
    assert not (hub / ".claude").exists()

    proc = _run_new_quiet(hub, "7", "bare")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "7"))["permissions"]["allow"]
    for rule in SEEDED_RULES:
        assert rule in allow, f"missing seeded rule: {rule}"


# ── Managed runtime-artifact excludes (issue #206) ──
#
# The pre-push test gate writes .testmondata* (the testmon DB plus its -shm/-wal
# WAL sidecars) at the worktree root. Without a managed exclude those untracked
# files make `git status --porcelain` non-empty, which the #172 ready gate reads
# as a dirty tree and refuses ready/<N> — stalling a drain on any checkout that
# lacks a happenstance local exclude. worktree-new seeds the .testmondata* glob
# into the worktree's git-dir info/exclude alongside the .ai-toolkit/ / .claude/
# entries it already manages.


def _info_exclude_path(wt: Path) -> Path:
    """Resolve the git-dir info/exclude file for a worktree (absolute)."""
    raw = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout.strip()
    p = Path(raw)
    return p if p.is_absolute() else wt / p


def test_seeds_testmondata_exclude(hub: Path) -> None:
    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    lines = _info_exclude_path(_worktree_dir(hub, "99")).read_text().splitlines()
    assert ".testmondata*" in lines, f"managed .testmondata* exclude not seeded\n{lines}"


def test_testmondata_exclude_seeded_at_most_once(hub: Path) -> None:
    # The exclude append is guarded by grep -qxF: pre-seed the entry (a machine
    # whose exclude already carries it, e.g. a re-run) so the run must find it
    # present and NOT append a duplicate. A linked worktree resolves info/exclude to
    # the shared common git dir, so seeding the hub's exclude is what the run sees.
    hub_exclude = _info_exclude_path(hub)
    hub_exclude.parent.mkdir(parents=True, exist_ok=True)
    with hub_exclude.open("a") as f:
        f.write(".testmondata*\n")

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    lines = _info_exclude_path(_worktree_dir(hub, "99")).read_text().splitlines()
    assert lines.count(".testmondata*") == 1, f"duplicate .testmondata* exclude\n{lines}"


# ── Pre-warmed .testmondata baseline (issue #276) ──
#
# The first push per fresh worktree runs the FULL suite solely to build .testmondata
# (12-47 min observed). worktree-new copies a maintained baseline .testmondata from
# the hub's git-common-dir into the new worktree so that first push runs a testmon
# INCREMENTAL (only the branch diff's affected tests) instead of the full-suite seed.
# testmon's own environment row (system_packages + python_version) invalidates a
# copied baseline whose venv differs, so a missing/stale baseline degrades to today's
# full-suite seed — never a wrong-green.


def _hub_common_dir(hub: Path) -> Path:
    """The hub's git-common-dir (absolute) — where the baseline .testmondata lives."""
    raw = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout.strip()
    p = Path(raw)
    return p if p.is_absolute() else hub / p


def test_copies_baseline_testmondata_into_new_worktree(hub: Path) -> None:
    baseline = _hub_common_dir(hub) / ".testmondata-baseline"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(b"BASELINE-TESTMON-DB")

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    copied = _worktree_dir(hub, "99") / ".testmondata"
    assert copied.exists(), "baseline .testmondata was not copied into the new worktree"
    assert copied.read_bytes() == b"BASELINE-TESTMON-DB"


def test_missing_baseline_leaves_no_testmondata_and_still_succeeds(hub: Path) -> None:
    # No baseline present: the spawn still succeeds and copies nothing, so the first
    # push falls back to today's full-suite seed (never a wrong-green from a stale DB).
    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    assert not (_worktree_dir(hub, "99") / ".testmondata").exists()


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_merges_existing_settings_local(hub: Path) -> None:
    existing = {
        "permissions": {"allow": ["Bash(ls *)"], "deny": ["Bash(rm *)"]},
        "other": True,
    }
    _seed_hub_claude(hub, settings=existing)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    data = _load_allowlist(_worktree_dir(hub, "99"))
    assert "Bash(ls *)" in data["permissions"]["allow"]
    assert "Bash(rm *)" in data["permissions"]["deny"]
    assert data["other"] is True
    for rule in SEEDED_RULES:
        assert rule in data["permissions"]["allow"], f"missing seeded rule: {rule}"
    # The dynamic hub-root read rule (#181) survives the jq merge branch too, not just
    # the initial-write branch — the merge is rule-agnostic ($rules - $cur).
    assert f"Read(/{os.path.realpath(hub)}/**)" in data["permissions"]["allow"]


def test_adhoc_branch_allowlist(hub: Path) -> None:
    proc = _run_new_quiet(hub, "fix-parser")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "fix-parser"))["permissions"]["allow"]
    # The seeded rules are branch-independent now, so an ad-hoc spoke gets the
    # same allowlist as a numbered one.
    for rule in SEEDED_RULES:
        assert rule in allow, f"missing seeded rule: {rule}"


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_malformed_settings_local_warns_but_does_not_abort(hub: Path) -> None:
    # A hub-copied settings.local.json that jq cannot parse must not abort the
    # worktree wiring mid-flight, and must not be rewritten blind — warn with
    # the rules and leave the file as it was.
    marker = _seed_hub_claude(hub)
    (hub / ".claude" / "settings.local.json").write_text("not json{")
    assert marker.is_file()

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    spoke_settings = _worktree_dir(hub, "99") / ".claude" / "settings.local.json"
    assert spoke_settings.read_text() == "not json{"
    assert "add the allow rules yourself" in proc.stderr + proc.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_empty_settings_local_is_not_silently_truncated(hub: Path) -> None:
    # jq emits zero documents for a zero-byte file and still exits 0 — the
    # merge must not claim success while leaving the spoke with no rules.
    _seed_hub_claude(hub)
    (hub / ".claude" / "settings.local.json").write_text("")

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    assert "merged" not in proc.stdout
    assert "add the allow rules yourself" in proc.stderr + proc.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_merge_preserves_existing_allow_order(hub: Path) -> None:
    # The merge must not lexicographically churn a user-curated allow list —
    # existing entries keep their order; the seeded rules append at the end.
    _seed_hub_claude(hub, settings={"permissions": {"allow": ["Bash(z *)", "Bash(m *)"]}})

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    assert allow[:2] == ["Bash(z *)", "Bash(m *)"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_no_duplicate_rules_when_rerun_source_present(hub: Path) -> None:
    # A rule already present in the copied settings must not be re-appended.
    _seed_hub_claude(hub, settings={"permissions": {"allow": [SCRIPT_RULE]}})

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    assert allow.count(SCRIPT_RULE) == 1
    for rule in SEEDED_RULES:
        assert rule in allow, f"missing seeded rule: {rule}"


# --- mode/lane pointer files (issue #102) ------------------------------------
# worktree-new.sh stamps the spoke's execution `lane` and `mode` into the same
# gitignored `.ai-toolkit/` dir as `spoke-run-id`, so langfuse_spoke_tree.py can
# tag the reconstructed trace. `lane` is derived (issue-backed → spoke, ad-hoc
# slug → express); `mode` defaults to `attended`, overridden to `afk` by the
# afk supervisor via `--mode afk`.


def _pointer(hub: Path, tag: str, name: str) -> str:
    """Read a `.ai-toolkit/<name>` pointer file the script wrote for a worktree."""
    pointer = _worktree_dir(hub, tag) / ".ai-toolkit" / name
    assert pointer.is_file(), f"missing pointer {pointer}"
    return pointer.read_text().strip()


def test_issue_backed_spoke_tagged_lane_spoke(hub: Path) -> None:
    proc = _run_new_quiet(hub, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    assert _pointer(hub, "8", "lane") == "spoke"


def test_adhoc_slug_tagged_lane_express(hub: Path) -> None:
    proc = _run_new_quiet(hub, "refactor-sync")

    assert proc.returncode == 0, proc.stderr
    assert _pointer(hub, "refactor-sync", "lane") == "express"


def test_mode_defaults_to_attended(hub: Path) -> None:
    proc = _run_new_quiet(hub, "8", "some-slug")

    assert proc.returncode == 0, proc.stderr
    assert _pointer(hub, "8", "mode") == "attended"


def test_mode_flag_stamps_afk(hub: Path) -> None:
    proc = _run_new_quiet(hub, "8", "some-slug", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    assert _pointer(hub, "8", "mode") == "afk"


# --- configurable base branch (issue #117) --------------------------------------
# New spokes branch from the RESOLVED base (origin/<base> when the remote ref
# exists, else local <base>) — never implicitly from whatever the hub's HEAD
# happens to be. `git config ai-toolkit.base-branch` is the per-clone knob.


def _add_develop(hub: Path) -> str:
    """Create `develop` one commit ahead of main, push it, return its tip sha.

    Leaves the hub checked out back on main so the old branch-from-HEAD
    behavior and the new branch-from-base behavior produce DIFFERENT commits.
    """
    _git(hub, "checkout", "-q", "-b", "develop")
    (hub / "develop.txt").write_text("develop\n")
    _git(hub, "add", "develop.txt")
    _git(hub, "commit", "-qm", "feat: develop seed", "-m", "Refs #0")
    _git(hub, "push", "-q", "-u", "origin", "develop")
    tip = _git(hub, "rev-parse", "HEAD").strip()
    _git(hub, "checkout", "-q", "main")
    return tip


def test_new_branches_from_configured_base(hub: Path, tmp_path: Path) -> None:
    develop_tip = _add_develop(hub)
    _git(hub, "config", "ai-toolkit.base-branch", "develop")

    proc, _ = _run_new(hub, tmp_path, "9", "cfg-base", "--no-code", "--no-terminal")

    assert proc.returncode == 0, proc.stderr
    wt = hub.parent / f"{hub.name}-9"
    assert _git(wt, "rev-parse", "HEAD").strip() == develop_tip


def test_new_unset_config_branches_from_origin_base(hub: Path, tmp_path: Path) -> None:
    # Nothing configured: the resolved base is main, and the spoke starts at
    # origin/main — an unpushed hub-local commit is deliberately NOT inherited
    # (#117: spokes start from the shared integration state, not hub drift).
    origin_tip = _git(hub, "rev-parse", "origin/main").strip()
    (hub / "unpushed.txt").write_text("local only\n")
    _git(hub, "add", "unpushed.txt")
    _git(hub, "commit", "-qm", "chore: unpushed hub drift", "-m", "Refs #0")

    proc, _ = _run_new(hub, tmp_path, "9", "cfg-base", "--no-code", "--no-terminal")

    assert proc.returncode == 0, proc.stderr
    wt = hub.parent / f"{hub.name}-9"
    assert _git(wt, "rev-parse", "HEAD").strip() == origin_tip


def test_new_dies_when_configured_base_missing(hub: Path, tmp_path: Path) -> None:
    # A config typo must die loudly BEFORE creating anything — never silently
    # branch from some other ref.
    _git(hub, "config", "ai-toolkit.base-branch", "ghost")

    proc, _ = _run_new(hub, tmp_path, "9", "cfg-base", "--no-code", "--no-terminal")

    assert proc.returncode != 0
    assert "ghost" in proc.stderr
    assert not (hub.parent / f"{hub.name}-9").exists()


# --- review workspace file: direct append over `code --add` (issue #134) ------
# `code --add` targets the last-focused VS Code window and routinely misses, so
# when the review workspace file exists the script appends the folder entry to
# it directly (VS Code hot-reloads the file) and must NOT also call `code --add`.
# The legacy `code --add` remains strictly the missing-file fallback. Tests pin
# `git config ai-toolkit.workspace-file` to a per-test path so the host's real
# review workspace is never touched.


def _code_stub(tmp_path: Path) -> Path:
    """Drop a logging `code` stub into _run_new's bin dir; return the log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "code-calls.log"
    log.touch()
    stub = bindir / "code"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    return log


def test_new_appends_entry_to_workspace_file_not_code_add(hub: Path, tmp_path: Path) -> None:
    # Workspace file present → one appended {"name","path"} entry (path relative
    # to the workspace file's dir) and no `code --add` call at all.
    code_log = _code_stub(tmp_path)
    ws = tmp_path / "claude" / "review.code-workspace"
    ws.parent.mkdir()
    ws.write_text(json.dumps({"folders": [], "settings": {}}, indent="\t") + "\n")
    _git(hub, "config", "ai-toolkit.workspace-file", str(ws))

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-terminal")

    assert proc.returncode == 0, proc.stderr
    doc = json.loads(ws.read_text())
    assert doc["folders"] == [{"name": f"{hub.name}-8", "path": f"../{hub.name}-8"}]
    assert code_log.read_text() == "", (
        "direct append and the `code` CLI must never both fire (double-add)"
    )


def test_new_falls_back_to_code_add_when_workspace_file_missing(hub: Path, tmp_path: Path) -> None:
    # No workspace file at the configured location → the legacy `code --add`
    # fallback fires exactly as before.
    code_log = _code_stub(tmp_path)
    _git(
        hub,
        "config",
        "ai-toolkit.workspace-file",
        str(tmp_path / "claude" / "absent.code-workspace"),
    )

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-terminal")

    assert proc.returncode == 0, proc.stderr
    assert f"--add {hub.parent / f'{hub.name}-8'}" in code_log.read_text()


def test_new_no_code_touches_neither_workspace_file_nor_code(hub: Path, tmp_path: Path) -> None:
    # --no-code opts out of the whole VS Code fold: no file edit, no CLI call.
    code_log = _code_stub(tmp_path)
    ws = tmp_path / "claude" / "review.code-workspace"
    ws.parent.mkdir()
    before = json.dumps({"folders": [], "settings": {}}, indent="\t") + "\n"
    ws.write_text(before)
    _git(hub, "config", "ai-toolkit.workspace-file", str(ws))

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-terminal", "--no-code")

    assert proc.returncode == 0, proc.stderr
    assert ws.read_text() == before
    assert code_log.read_text() == ""


# --- dispatch lifecycle-label mirror (issue #236) -----------------------------
# A dispatched issue-backed spoke stamps its GitHub issue so the issue list shows
# it is live: status:in-progress + mode:<afk|attended> + lane:spoke, plus a
# one-time dispatch comment linking back to the branch / worktree / tmux window /
# spoke_run_id. Best-effort: a failing gh never fails the spawn; ad-hoc (no-issue)
# lanes mirror nothing by construction.


def _gh_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _one_issue_edit(calls: list[str]) -> str:
    edits = [c for c in calls if c.startswith("issue edit")]
    assert len(edits) == 1, f"expected exactly one gh issue-edit, got {edits}"
    return edits[0]


def test_dispatch_adds_in_progress_mode_lane_labels(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    edit = _one_issue_edit(_gh_calls(tmp_path))
    assert edit.startswith("issue edit 8 ")
    assert "--add-label status:in-progress" in edit
    assert "--add-label mode:attended" in edit
    assert "--add-label lane:spoke" in edit


def test_dispatch_afk_mode_label(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--mode", "afk")

    assert proc.returncode == 0, proc.stderr
    edit = _one_issue_edit(_gh_calls(tmp_path))
    assert "--add-label mode:afk" in edit
    assert "--remove-label mode:attended" in edit


def test_dispatch_posts_comment_linking_the_live_spoke(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = _gh_calls(tmp_path)
    comments = [c for c in calls if c.startswith("issue comment 8 ")]
    assert len(comments) == 1, f"expected one dispatch comment, got {comments}"
    body = comments[0]
    # The comment links the issue back to its live spoke: branch, worktree path,
    # tmux window, spoke_run_id.
    assert "feature/8-some-slug" in body  # branch
    assert f"{hub.name}-8" in body  # worktree dir basename
    assert "8-some-slug" in body  # tmux window name


def test_dispatch_adhoc_slug_mirrors_nothing(hub: Path, tmp_path: Path) -> None:
    # An ad-hoc (non-numeric) target has no issue to mirror onto — no labels, no
    # comment, no label seeding.
    proc, _ = _run_new(hub, tmp_path, "refactor-sync", "--no-code")

    assert proc.returncode == 0, proc.stderr
    calls = _gh_calls(tmp_path)
    assert not [c for c in calls if c.startswith("issue edit")]
    assert not [c for c in calls if c.startswith("issue comment")]
    assert not [c for c in calls if c.startswith("label create")]


def test_dispatch_gh_failure_never_fails_the_spawn(hub: Path, tmp_path: Path) -> None:
    # Offline / unauthed gh (the mirror writes exit nonzero) must not fail dispatch.
    proc, _ = _run_new(
        hub, tmp_path, "8", "some-slug", "--no-code", extra_env={"GH_MIRROR_RC": "1"}
    )

    assert proc.returncode == 0, proc.stderr


def test_dispatch_mirror_opt_out_makes_no_label_calls(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(
        hub,
        tmp_path,
        "8",
        "some-slug",
        "--no-code",
        extra_env={"AI_TOOLKIT_GH_LIFECYCLE_LABELS": "0"},
    )

    assert proc.returncode == 0, proc.stderr
    calls = _gh_calls(tmp_path)
    assert not [c for c in calls if c.startswith("issue edit")]
    assert not [c for c in calls if c.startswith("issue comment")]


# ── #278: --subtasks, the dispatch-time pack's ordered extra issues ───────────
#
# batch-plan now emits an ordered GROUP per spoke ("263,265 270") instead of one issue per
# spoke: same-scope issues that could never run concurrently ride ONE branch as ordered
# subtasks rather than paying the whole spoke lifecycle tax each. This is the spawn side.
#
# The branch still leads with the PRIMARY — inflight_worktrees and worktree-land both derive
# the issue from the leading digits of the branch slug — and the extra issues are seeded into
# the queued-subtask channel, which is what makes the spoke work them before its terminal
# ready/<primary>.


def _queued_dir(state_dir: Path, primary: str) -> Path:
    """The queued-subtask channel's per-spoke dir (the contract shared with the broker)."""
    return state_dir / f"queued-{primary}"


def test_subtasks_are_seeded_into_the_queued_channel(hub: Path, tmp_path: Path) -> None:
    # The seeding must happen HERE, not only in hub-afk's routing pass: /next-batch dispatches
    # interactively with no drain running, so a packed spoke would otherwise find an empty
    # queue, emit ready/<primary>, and silently drop its subtasks on the floor.
    state = tmp_path / "afk-state"
    env = {
        "GH_ISSUE_TITLE": "Primary of a packed group",
        "GH_ISSUE_BODY": "Do the thing.\nScope: a.py\nGate: none\n",
        "AFK_STATE_DIR": str(state),
    }

    proc, _ = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "265,270", extra_env=env
    )

    assert proc.returncode == 0, proc.stderr
    assert (_queued_dir(state, "263") / "265").is_file()
    assert (_queued_dir(state, "263") / "270").is_file()


def test_subtasks_do_not_queue_the_primary_itself(hub: Path, tmp_path: Path) -> None:
    # The primary IS the branch, not a queued subtask. Queueing it would make the spoke
    # re-anchor on the issue it is already working and never reach its terminal ready.
    state = tmp_path / "afk-state"
    env = {
        "GH_ISSUE_TITLE": "Primary",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(state),
    }

    proc, _ = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "263,265", extra_env=env
    )

    assert proc.returncode == 0, proc.stderr
    assert not (_queued_dir(state, "263") / "263").exists(), "the primary must never self-queue"
    assert (_queued_dir(state, "263") / "265").is_file()


def test_branch_slug_still_leads_with_the_primary_when_packed(hub: Path, tmp_path: Path) -> None:
    # Load-bearing: inflight_worktrees and worktree-land both parse the issue out of the
    # LEADING digits of the branch slug. A branch named for the group would strand both.
    env = {
        "GH_ISSUE_TITLE": "Primary of a packed group",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
    }

    proc, _ = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "265,270", extra_env=env
    )

    assert proc.returncode == 0, proc.stderr
    assert "feature/263-some-slug" in _git(hub, "branch", "--list", "feature/263-some-slug")
    assert _worktree_dir(hub, "263").is_dir(), "the worktree tag stays the primary"


def test_subtasks_absent_seeds_no_queue(hub: Path, tmp_path: Path) -> None:
    # The overwhelmingly common path: an unpacked single-issue spoke must leave the channel
    # untouched, or a stray queue would refuse its ready/<N>.
    state = tmp_path / "afk-state"
    env = {
        "GH_ISSUE_TITLE": "Solo issue",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(state),
    }

    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=env)

    assert proc.returncode == 0, proc.stderr
    assert not _queued_dir(state, "8").exists()


def test_subtasks_rejects_a_non_numeric_entry(hub: Path, tmp_path: Path) -> None:
    # Fail loudly at spawn: a malformed entry silently skipped would drop a real issue from
    # the group, and nothing downstream would notice it was meant to ship on this branch.
    env = {
        "GH_ISSUE_TITLE": "Primary",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
    }

    proc, _ = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "265,oops", extra_env=env
    )

    assert proc.returncode != 0, "a non-numeric subtask must abort the spawn"
    # Name the offending value, not just the flag: "unknown option: --subtasks" would satisfy
    # a laxer assertion while the feature does not exist at all.
    assert "oops" in proc.stderr + proc.stdout


def test_subtasks_seed_prompt_names_the_queued_group(hub: Path, tmp_path: Path) -> None:
    # With no --prompt, the default seed must tell the spoke it owns a chain — otherwise it
    # reads task.md, sees one issue, and has no idea the queue is waiting on it.
    env = {
        "GH_ISSUE_TITLE": "Primary of a packed group",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
    }

    proc, log = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "265,270", extra_env=env
    )

    assert proc.returncode == 0, proc.stderr
    launch = log.read_text()
    assert "265" in launch and "270" in launch, "the seed prompt must name the queued subtasks"


def test_subtasks_note_reaches_an_explicit_prompt_too(hub: Path, tmp_path: Path) -> None:
    # THE path that matters: every real packed dispatch comes from hub-afk's dispatch_batch,
    # which always passes --prompt "$(kickoff_for ...)". A chain note wired only into the
    # default-prompt branch would therefore never reach a single genuinely packed spoke —
    # it would be seeded a queue it was never told about, and hit an unexplained ready
    # refusal at the end of its run.
    env = {
        "GH_ISSUE_TITLE": "Primary of a packed group",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
    }

    proc, log = _run_new(
        hub,
        tmp_path,
        "263",
        "some-slug",
        "--no-code",
        "--subtasks",
        "265,270",
        "--prompt",
        "CALLER_SUPPLIED_KICKOFF",  # single token: the launch %q-quotes the prompt
        extra_env=env,
    )

    assert proc.returncode == 0, proc.stderr
    launch = log.read_text()
    assert "CALLER_SUPPLIED_KICKOFF" in launch, "the caller's prompt must survive"
    assert "265" in launch and "270" in launch, "the chain note must be appended to it"


def test_subtasks_do_not_glob_against_the_cwd(hub: Path, tmp_path: Path) -> None:
    # The split expansion is deliberately unquoted (it word-splits), so without noglob a
    # value like '2*' expands against the cwd — and files named 20/21/22 would then PASS the
    # numeric check as if they were real issue numbers, silently dispatching a spoke bound to
    # invented work. Validation cannot catch it: the shell substitutes first.
    for name in ("20", "21", "22"):
        (hub / name).write_text("decoy\n")
    env = {
        "GH_ISSUE_TITLE": "Primary",
        "GH_ISSUE_BODY": "Do the thing.\nGate: none\n",
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
    }

    proc, _ = _run_new(
        hub, tmp_path, "263", "some-slug", "--no-code", "--subtasks", "2*", extra_env=env
    )

    assert proc.returncode != 0, "a glob must be rejected, never silently expanded"
    assert "2*" in proc.stderr + proc.stdout, "the literal value is what gets refused"
    assert not (tmp_path / "afk-state" / "queued-263").exists()
