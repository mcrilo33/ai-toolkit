"""Unit tests for scripts/install-git-hooks.sh — the native pre-push test gate.

Issue #19 makes the native pre-push hook the single owner of test execution. The
installer must (1) copy test-select.sh into the hooks dir alongside the other
cage scripts, and (2) wire it into the emitted pre-push hook as a BLOCKING gate —
a non-zero exit aborts the push — fed git's pre-push stdin, while the advisory
red-proof / reviewer-sep warns keep running non-blocking.

Hermetic, like test_worktree_land.py: a throwaway repo plus a bare origin. After
install, the COPIED ai-toolkit-scripts/test-select.sh (and the advisory scripts)
are replaced with logging stubs so each push's outcome is driven deterministically
— the selector's own logic is covered by test_test_select.py. The commit to push
is made BEFORE install so the commit-msg hook never enters the picture.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "scripts" / "install-git-hooks.sh"
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo on `main` tracking a bare origin, with one pushed seed commit."""
    remote = tmp_path / "remote.git"
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(r, "remote", "add", "origin", str(remote))
    _git(r, "push", "-q", "-u", "origin", "main")
    return r


def _install(repo: Path) -> Path:
    """Install the native hooks; return the hooks directory."""
    proc = subprocess.run(
        ["bash", str(INSTALL), str(repo)], capture_output=True, text=True, env=_GIT_ENV
    )
    assert proc.returncode == 0, proc.stderr
    return repo / ".git" / "hooks"


def _scripts_dir(hooks: Path) -> Path:
    return hooks / "ai-toolkit-scripts"


def _stub_selector(hooks: Path, *, exit_code: int, stdin_log: Path | None = None) -> None:
    """Overwrite the copied test-select.sh with a stub of a known exit code."""
    sel = _scripts_dir(hooks) / "test-select.sh"
    body = "#!/bin/sh\n"
    if stdin_log is not None:
        body += f'cat >> "{stdin_log}"\n'
    body += f"exit {exit_code}\n"
    sel.write_text(body)
    sel.chmod(0o755)


def _unpushed_commit(repo: Path, fname: str = "change.txt") -> str:
    """Make a commit to push BEFORE hooks exist (so commit-msg never fires)."""
    (repo / fname).write_text("work\n")
    _git(repo, "add", fname)
    _git(repo, "commit", "-qm", "feat: work", "-m", "Refs #1")
    return _git(repo, "rev-parse", "HEAD").strip()


def _push(repo: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, **(extra_env or {})},
    )


def _unpushed_gutting_commit(repo: Path) -> str:
    """Commit a test file with a tautological assert (a gutting signature)."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_g.py").write_text("def test_g():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "test: weaken", "-m", "Refs #40")
    return _git(repo, "rev-parse", "HEAD").strip()


def _remote_sha(repo: Path, branch: str = "main") -> str:
    out = _git(repo, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out.strip() else ""


# --- the script is copied into the hooks dir ------------------------------------


def test_test_select_copied_into_hooks(repo: Path) -> None:
    hooks = _install(repo)

    sel = _scripts_dir(hooks) / "test-select.sh"
    assert sel.is_file()
    assert os.access(sel, os.X_OK)


def test_anti_gutting_copied_into_hooks(repo: Path) -> None:
    # The anti-gutting tripwire (issue #40) ships alongside the other cage scripts
    # so the native pre-push hook can run it.
    hooks = _install(repo)

    scan = _scripts_dir(hooks) / "anti-gutting-scan.sh"
    assert scan.is_file()
    assert os.access(scan, os.X_OK)


# --- the anti-gutting tripwire is advisory: it warns but never blocks -----------


def test_pre_push_allows_gutting_but_warns(repo: Path) -> None:
    # The tripwire is advisory — a human's ordinary test edit (which may
    # legitimately remove/weaken an assert) is never gated, but the smell is
    # surfaced on stderr so it can be eyeballed before landing.
    _unpushed_gutting_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)  # isolate: only anti-gutting could block

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()
    assert "weakens tests" in push.stderr, "advisory tripwire must still warn"


# --- the blocking contract: a failing selector aborts the push ------------------


def test_pre_push_blocks_when_selector_fails(repo: Path) -> None:
    seed = _remote_sha(repo)
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=1)

    push = _push(repo)

    assert push.returncode != 0  # the gate aborted the push
    assert _remote_sha(repo) == seed  # nothing shipped


def test_pre_push_allows_when_selector_passes(repo: Path) -> None:
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()


# --- the selector is fed git's pre-push stdin -----------------------------------


def test_selector_receives_prepush_stdin(repo: Path, tmp_path: Path) -> None:
    local = _unpushed_commit(repo)
    hooks = _install(repo)
    log = tmp_path / "stdin.log"
    _stub_selector(hooks, exit_code=0, stdin_log=log)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert log.is_file(), "selector was not invoked"
    assert local in log.read_text()  # the pushed local sha reached the selector


# --- advisory warns stay non-blocking -------------------------------------------


def test_advisory_warns_do_not_block(repo: Path) -> None:
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)
    # Make the advisory scripts "fail": the hook must swallow it and still push.
    for name in ("red-proof-warn.sh", "reviewer-sep-warn.sh"):
        adv = _scripts_dir(hooks) / name
        adv.write_text("#!/bin/sh\nexit 1\n")
        adv.chmod(0o755)

    push = _push(repo)

    assert push.returncode == 0, push.stderr  # advisory exit codes never block
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()


def test_pre_push_blocks_when_selector_missing(repo: Path) -> None:
    # Fail closed: a missing selector means the gate can't run, so the push must
    # be refused rather than shipping untested ("never silently skip tests").
    seed = _remote_sha(repo)
    _unpushed_commit(repo)
    hooks = _install(repo)
    (_scripts_dir(hooks) / "test-select.sh").unlink()

    push = _push(repo)

    assert push.returncode != 0  # no gate, no ship
    assert _remote_sha(repo) == seed


def test_reverse_index_lib_copied_into_hooks(repo: Path) -> None:
    # #123-D review: the installed pre-push cage is the actual ship gate; if
    # lib/test-reverse-index.sh is missing from the copy list the selector
    # runs with RINDEX=0 forever and the SELECTED tier never activates.
    hooks = _install(repo)

    lib = _scripts_dir(hooks) / "lib" / "test-reverse-index.sh"
    assert lib.is_file()


# --- the commit-msg hook synthesizes a RAW `git commit` command (issue #185) ----
#
# The native commit-msg hook must feed the cage scripts a payload whose
# tool_input.command is a real `git commit -m …` invocation — the same shape the
# agent path produces — so commit-quality's boundary-aware `is_git_commit` matcher
# fires. The `jq -nc` bug double-encoded CMD (outer quotes + escapes), so the
# command reaching the matcher started with a literal `"` and the format/anchor
# gate silently never ran (a safety gate failing open).


def _stub_capture(hooks: Path, name: str, log: Path) -> None:
    """Overwrite a copied cage script with a stub that logs its stdin and passes."""
    s = _scripts_dir(hooks) / f"{name}.sh"
    s.write_text(f'#!/bin/sh\ncat >> "{log}"\nexit 0\n')
    s.chmod(0o755)


def _stub_pass(hooks: Path, name: str) -> None:
    """Overwrite a copied cage script with a no-op that passes."""
    s = _scripts_dir(hooks) / f"{name}.sh"
    s.write_text("#!/bin/sh\nexit 0\n")
    s.chmod(0o755)


def _commit(repo: Path, *msg_args: str) -> subprocess.CompletedProcess[str]:
    """Stage a change and run a real `git commit` (drives the commit-msg hook)."""
    (repo / "change.txt").write_text("work\n")
    _git(repo, "add", "change.txt")
    return subprocess.run(
        ["git", "commit", *msg_args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def test_commit_msg_synthesizes_raw_command(repo: Path, tmp_path: Path) -> None:
    # The captured payload's command must be byte-identical to a real commit
    # invocation: `git commit -m ` + the JSON-encoded message (quotes/newlines
    # escaped), with `git` at a command boundary — NOT wrapped in outer quotes.
    hooks = _install(repo)
    log = tmp_path / "payload.json"
    _stub_capture(hooks, "commit-quality", log)
    _stub_pass(hooks, "commit-gauntlet")

    commit = _commit(repo, "-m", 'feat(x): add "q" thing', "-m", "Refs #185")

    assert commit.returncode == 0, commit.stderr
    payload = json.loads(log.read_text())
    body = _git(repo, "show", "-s", "--format=%B", "HEAD").rstrip("\n")
    expected = "git commit -m " + json.dumps(body)
    assert payload["tool_input"]["command"] == expected


def test_commit_msg_gate_blocks_non_conventional_message(repo: Path) -> None:
    # End-to-end proof the real commit-quality matcher fires: a non-conventional
    # subject must be BLOCKED. Under the jq -nc bug the matcher never saw a git
    # commit command, so the commit went through ungated.
    hooks = _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    commit = _commit(repo, "-m", "not a conventional message")

    assert commit.returncode != 0, "commit-quality must block a non-conventional subject"
    assert "conventional commits" in (commit.stdout + commit.stderr).lower()
    assert _git(repo, "rev-parse", "HEAD").strip() == seed  # nothing committed


def test_telemetry_lib_copied_and_utils_sources_clean(repo: Path) -> None:
    # utils.sh sources lib/telemetry.sh unconditionally; an install without it
    # ships hooks that die at source-time with every push blocked (the
    # 2026-07-04 hub outage). The installed utils must source successfully.
    hooks = _install(repo)
    lib_dir = _scripts_dir(hooks) / "lib"

    assert (lib_dir / "telemetry.sh").is_file()
    proc = subprocess.run(
        ["bash", "-c", f'source "{lib_dir / "utils.sh"}" && echo OK'],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
