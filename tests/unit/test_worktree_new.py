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
"""

from __future__ import annotations

import os
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
    assert "/source" in proc.stdout
