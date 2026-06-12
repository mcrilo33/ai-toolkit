"""Unit tests for scripts/worktree-new.sh tmux window naming (issue #8).

The tmux window opened for a new worktree must carry the human-readable branch
leaf (e.g. `8-some-slug` for `feature/8-some-slug`), not the bare issue number,
and the name must be pinned (`automatic-rename off`, `allow-rename off`) so the
process running inside the window cannot clobber it. A logging `tmux` stub on
PATH keeps the test hermetic while a fake TMUX env var steers the script down
the tmux branch.

Spoke-home decision (issue #8 follow-up): every spoke window lives in tmux
session `0`. The script must target that session explicitly (`new-window -t =0:`),
create it detached when missing (`has-session` → `new-session -d -s 0`), work
even when invoked outside tmux ($TMUX unset), and print the exact jump command
(`switch-client` inside tmux, `attach ... select-window` outside).

Agent pinning (issue #8 follow-up): the spoke launch must pin model and effort
explicitly (`CLAUDE_EFFORT=<effort> claude --model <model>`) from env vars
`WT_AGENT_MODEL` (default `fable`) / `WT_AGENT_EFFORT` (default `max`) instead
of relying on user-global settings; a seeded `--prompt` stays the trailing arg.

Launch delivery (issue #15): typing the launch command into an interactive zsh
via `send-keys` races shell init (eaten Enter, zvm interference). The command
must instead be passed as the `new-window` shell-command argument, suffixed
with `; exec <shell>` so the window survives claude's exit; `send-keys` must
never deliver the launch. `--no-agent` spawns a plain interactive window.
Push-allowlist templating (issue #11): after the `.claude/` copy, the script
seeds `<worktree>/.claude/settings.local.json` with two narrow allow rules so
the spoke's own-branch ship push runs without a permission prompt — gates, not
asks, do the enforcing. The file is created when the hub has no `.claude/` and
merged (never clobbered, never duplicated, order preserved) when one was copied.

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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A main checkout ('hub') on `main` with an `origin` bare remote."""
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
    (0 = session 0 exists / was created). The log file is pre-created so a run
    that never reaches tmux reads as an empty log, not a missing one.

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
    env = {
        **_GIT_ENV,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_HAS_SESSION": str(has_session_rc),
        "STUB_NEW_SESSION": str(new_session_rc),
    }
    env.pop("TMUX", None)  # the host's real tmux must never steer the script
    # The host's agent pinning must never leak in — defaults are under test.
    env.pop("WT_AGENT_MODEL", None)
    env.pop("WT_AGENT_EFFORT", None)
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


def test_window_spawned_into_session_zero(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "-t =0:" in new_window[0]


def test_session_zero_created_when_missing(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", has_session_rc=1)

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    new_session = _calls(calls, "new-session")
    assert new_session, "expected session 0 to be created when has-session fails"
    assert "-d" in new_session[0].split()
    assert "-s 0" in new_session[0]
    assert calls.find("new-session") < calls.find("new-window")


def test_session_zero_not_recreated_when_present(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", has_session_rc=0)

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert _calls(calls, "has-session"), "expected the script to probe for session 0"
    assert not _calls(calls, "new-session")


def test_spawns_via_tmux_even_outside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=False)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation even with TMUX unset"
    assert "-t =0:" in new_window[0]


def test_dispatch_prints_switch_client_jump_when_inside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=True)

    assert proc.returncode == 0, proc.stderr
    assert "tmux switch-client -t '0:8-some-slug'" in proc.stdout


def test_dispatch_prints_attach_jump_when_outside_tmux(hub: Path, tmp_path: Path) -> None:
    proc, _ = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", inside_tmux=False)

    assert proc.returncode == 0, proc.stderr
    assert "tmux attach -t 0 \\; select-window -t '0:8-some-slug'" in proc.stdout


def test_no_server_falls_back_to_manual_advice(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(
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
    assert not _calls(log.read_text(), "new-window")
    assert "Start the agent in a new terminal window:" in proc.stdout
    assert "CLAUDE_EFFORT=max claude --model fable" in proc.stdout
    assert "/source" in proc.stdout


def test_agent_launch_pins_model_and_effort_by_default(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=max claude --model fable; exec " in new_window[0]


def test_agent_launch_respects_model_and_effort_overrides(hub: Path, tmp_path: Path) -> None:
    overrides = {"WT_AGENT_MODEL": "sonnet", "WT_AGENT_EFFORT": "high"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=high claude --model sonnet; exec " in new_window[0]


def test_agent_launch_keeps_seeded_prompt_after_pinning(hub: Path, tmp_path: Path) -> None:
    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", "--prompt", "/source")

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "CLAUDE_EFFORT=max claude --model fable /source; exec " in new_window[0]


def test_agent_launch_shell_quotes_metacharacter_overrides(hub: Path, tmp_path: Path) -> None:
    overrides = {"WT_AGENT_MODEL": "foo bar"}

    proc, log = _run_new(hub, tmp_path, "8", "some-slug", "--no-code", extra_env=overrides)

    assert proc.returncode == 0, proc.stderr
    new_window = _calls(log.read_text(), "new-window")
    assert new_window, "expected a new-window invocation"
    assert "claude --model foo\\ bar; exec " in new_window[0]


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


# ── Push-allowlist templating (issue #11) ────────────────────────────────────
# A quiet runner (no VS Code, no tmux) for the settings.local.json assertions —
# distinct from the tmux-stub `_run_new` above, which the window/agent tests need.


def _run_new_quiet(hub: Path, *args: str) -> subprocess.CompletedProcess:
    """Run worktree-new.sh from the hub, hermetically (no VS Code, no tmux)."""
    env = {**_GIT_ENV}
    env.pop("TMUX", None)
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


def _push_rules(branch: str) -> list[str]:
    return [f"Bash(git push origin {branch})", f"Bash(git push -u origin {branch})"]


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


def test_seeds_own_branch_push_allowlist(hub: Path) -> None:
    _seed_hub_claude(hub)

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    data = _load_allowlist(_worktree_dir(hub, "99"))
    allow = data["permissions"]["allow"]
    for rule in _push_rules("feature/99-pushguard"):
        assert rule in allow


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
    data = _load_allowlist(_worktree_dir(hub, "7"))
    allow = data["permissions"]["allow"]
    for rule in _push_rules("feature/7-bare"):
        assert rule in allow


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
    for rule in _push_rules("feature/99-pushguard"):
        assert rule in data["permissions"]["allow"]


def test_adhoc_branch_allowlist(hub: Path) -> None:
    proc = _run_new_quiet(hub, "fix-parser")

    assert proc.returncode == 0, proc.stderr
    data = _load_allowlist(_worktree_dir(hub, "fix-parser"))
    allow = data["permissions"]["allow"]
    for rule in _push_rules("feature/fix-parser"):
        assert rule in allow


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_malformed_settings_local_warns_but_does_not_abort(hub: Path) -> None:
    # A hub-copied settings.local.json that jq cannot parse must not abort the
    # worktree wiring mid-flight, and must not be rewritten blind — warn with
    # the rules and leave the file as it was.
    marker = _seed_hub_claude(hub)
    (hub / ".claude" / "settings.local.json").write_text("not json{")
    assert marker.is_file()

    proc = _run_new(hub, "99", "pushguard")

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

    proc = _run_new(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    assert "merged" not in proc.stdout
    assert "add the allow rules yourself" in proc.stderr + proc.stdout


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_merge_preserves_existing_allow_order(hub: Path) -> None:
    # The merge must not lexicographically churn a user-curated allow list —
    # existing entries keep their order; the push rules append at the end.
    _seed_hub_claude(hub, settings={"permissions": {"allow": ["Bash(z *)", "Bash(m *)"]}})

    proc = _run_new(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    assert allow[:2] == ["Bash(z *)", "Bash(m *)"]


@pytest.mark.skipif(shutil.which("jq") is None, reason="merge templating requires jq")
def test_no_duplicate_rules_when_rerun_source_present(hub: Path) -> None:
    plain_rule, u_rule = _push_rules("feature/99-pushguard")
    _seed_hub_claude(hub, settings={"permissions": {"allow": [plain_rule]}})

    proc = _run_new_quiet(hub, "99", "pushguard")

    assert proc.returncode == 0, proc.stderr
    allow = _load_allowlist(_worktree_dir(hub, "99"))["permissions"]["allow"]
    assert allow.count(plain_rule) == 1
    assert u_rule in allow
