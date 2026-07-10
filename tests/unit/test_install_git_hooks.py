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
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[2] / "scripts" / "install-git-hooks.sh"
# Strip any ambient arming signal: the composed pre-push runs the REAL
# anti-gutting-scan.sh, which fails closed on gutting diffs under a truthy
# UNATTENDED (#193) — a drain-time leak would flip the attended-advisory
# assertions (the #169 env-leak class).
_GIT_ENV = {
    **{k: v for k, v in os.environ.items() if k != "UNATTENDED"},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


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


# --- the anti-gutting tripwire is advisory when ATTENDED (fail-closed lives
# --- under unattended /afk — #193; see test_anti_gutting.py) --------------------


def test_pre_push_allows_gutting_but_warns(repo: Path) -> None:
    # Attended, the tripwire is advisory — a human's ordinary test edit (which may
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


def _stub_cage(hooks: Path, name: str, log: Path | None = None) -> None:
    """Overwrite a copied cage script with a stub that passes, optionally logging stdin."""
    s = _scripts_dir(hooks) / f"{name}.sh"
    body = "#!/bin/sh\n"
    if log is not None:
        body += f'cat >> "{log}"\n'
    body += "exit 0\n"
    s.write_text(body)
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


@pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="asserts the jq per-paragraph command shape; a jq-less host takes the sed fallback",
)
def test_commit_msg_synthesizes_raw_command(repo: Path, tmp_path: Path) -> None:
    # The captured payload's command must be byte-identical to a real commit
    # invocation in the agent multi-`-m` shape: ONE `-m "<line>"` per NON-BLANK
    # physical line, each with only backslash + double-quote escaped, `git` at a
    # command boundary — NOT wrapped in outer quotes, NOT a single @json-encoded -m,
    # and never a `-m` carrying an embedded newline (issue #226). The exact escaping
    # is jq-specific (the no-jq fallback collapses newlines to spaces), so this
    # precise-equality check is guarded on jq.
    hooks = _install(repo)
    log = tmp_path / "payload.json"
    _stub_cage(hooks, "commit-quality", log)
    _stub_cage(hooks, "commit-gauntlet")

    commit = _commit(repo, "-m", 'feat(x): add "q" thing', "-m", "Refs #185")

    assert commit.returncode == 0, commit.stderr
    payload = json.loads(log.read_text())
    body = _git(repo, "show", "-s", "--format=%B", "HEAD").rstrip("\n")
    lines = [ln for ln in body.split("\n") if ln]
    expected = "git commit" + "".join(
        ' -m "' + ln.replace("\\", "\\\\").replace('"', '\\"') + '"' for ln in lines
    )
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


# --- issue #226: a body-line anchor / Tested-RED survives the synthesis ----------
#
# The commit-msg hook synthesizes the `git commit` command from the real message.
# The #185-era single `-m` shape encoded the body through jq `@json`, turning the
# message's newlines into a literal backslash-n in the command string. A body-line
# anchor (`Refs #1`) then sat right after the `n` of that `\n` — an [:alpha:] char —
# defeating commit-quality's `(^|[^[:alpha:]])` boundary (a fail-CLOSED false
# rejection) and commit-gauntlet's `(^|[[:space:]"'])Tested-RED:` carve-out. The fix
# emits ONE `-m` per non-blank LINE (the agent multi-`-m` shape), so no `-m` ever
# carries an embedded newline: the subject stays its own single-line `-m` and
# body-line anchors / Tested-RED sit at the start of their own `-m`.


def _commit_file(
    repo: Path, rel: str, content: str, *msg_args: str
) -> subprocess.CompletedProcess[str]:
    """Stage a specific file and run a real `git commit` (drives the commit-msg hook)."""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(repo, "add", rel)
    return subprocess.run(
        ["git", "commit", *msg_args], cwd=str(repo), capture_output=True, text=True, env=_GIT_ENV
    )


def test_commit_msg_accepts_body_line_anchor(repo: Path) -> None:
    # A multi-line message whose ONLY issue anchor is a body-line `Refs #N`, on a
    # branch with no issue ID (the fixture is on `main`), must be ACCEPTED — matching
    # the agent two-`-m` path. Runs the REAL cage scripts (no stubs) so the fix is
    # proven end-to-end through the synthesized command. Pre-fix: falsely rejected
    # as "not anchored".
    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode == 0, commit.stdout + commit.stderr
    assert _git(repo, "rev-parse", "HEAD").strip() != seed  # the commit landed


def test_commit_msg_multiline_bad_subject_still_denied(repo: Path) -> None:
    # Negative guard against a fail-OPEN regression: a single `-m` carrying embedded
    # real newlines would leave commit-quality's line-oriented subject extraction with
    # an unterminated quote on line 1 → empty MSG → the commit passes UNGATED. A
    # multi-line commit with a non-conventional subject AND no anchor must still be
    # DENIED. The per-line shape keeps the subject on its own terminated `-m`.
    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    commit = _commit(repo, "-m", "not a conventional message", "-m", "some body text")

    assert commit.returncode != 0, "a bad-subject multi-line commit must be blocked"
    assert "conventional commits" in (commit.stdout + commit.stderr).lower()
    assert _git(repo, "rev-parse", "HEAD").strip() == seed  # nothing committed


def test_commit_msg_multiline_subject_block_still_denied(repo: Path) -> None:
    # Regression (code-review of the #226 fix): a message whose SUBJECT BLOCK spans
    # multiple physical lines with NO blank-line separator is ONE paragraph. Merging
    # those lines into a single `-m` would carry a real newline, leaving commit-
    # quality's line-oriented subject extraction with an unterminated quote on line 1
    # → empty MSG → the commit passes UNGATED — the same fail-OPEN a single embedded-
    # newline `-m` causes, just triggered by an intra-paragraph line break. The synth
    # emits one `-m` per NON-BLANK LINE so every `-m` is single-line: a non-conventional
    # multi-line subject block with no anchor is still DENIED.
    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    # A single `-m` with an embedded newline: subject "wip changes", body line
    # "some detail", no blank line between them (one paragraph). Non-conventional,
    # unanchored — must be blocked, not slipped through.
    commit = _commit(repo, "-m", "wip changes\nsome detail")

    assert commit.returncode != 0, "a multi-line non-conventional subject block must be blocked"
    assert "conventional commits" in (commit.stdout + commit.stderr).lower()
    assert _git(repo, "rev-parse", "HEAD").strip() == seed  # nothing committed


@pytest.mark.skipif(
    shutil.which("pyright") is None,
    reason="the gauntlet carve-out is only observable when a typechecker produces a "
    "type error to skip; without pyright the typecheck is skipped regardless",
)
def test_commit_msg_recognizes_body_line_tested_red(repo: Path) -> None:
    # A `Tested-RED:` trailer at the start of its own body paragraph must trigger
    # commit-gauntlet's typecheck carve-out through the synthesized command. On a
    # branch with an issue ID (so commit-quality is anchor-satisfied regardless of the
    # message), the gauntlet's RED carve-out is the ONLY discriminator: a staged .py
    # with an unresolved import (pyright error, ruff-clean) is ALLOWED only when the
    # carve-out fires. Pre-fix the literal `\n` before `Tested-RED:` misses the
    # carve-out, pyright runs, and the commit is blocked.
    _install(repo)
    _git(repo, "checkout", "-q", "-b", "feature/1-red")

    commit = _commit_file(
        repo,
        "pkg/red.py",
        "from pkg._nope import thing\n\nvalue = thing\n",
        "-m",
        "test(x): failing red",
        "-m",
        "Tested-RED: pkg/red.py::test_x",
    )

    assert commit.returncode == 0, commit.stdout + commit.stderr


# --- the commit-msg stage fails CLOSED on a missing/non-executable cage script --
#
# Symmetric with the pre-push test-select fail-closed check (issue #212): the
# commit-msg loop's `if [ -x … ]` had no else, so a deleted or de-executable'd
# commit-quality / commit-gauntlet / red-proof-verify was SILENTLY skipped and the
# commit proceeded ungated. A configured gate that cannot run must block the commit
# (re-run install-git-hooks.sh to restore it), never wave it through.


_CAGE_SCRIPTS = ("commit-quality", "commit-gauntlet", "red-proof-verify")


def _stub_other_cages(hooks: Path, target: str) -> None:
    """Replace every cage script except `target` with a passing stub.

    Isolates the fail-closed contract to the state of `target` alone (the
    file's stub-the-copies philosophy), so a passing message cannot depend on
    the real gates accepting the synthesized payload.
    """
    for other in _CAGE_SCRIPTS:
        if other != target:
            _stub_cage(hooks, other)


@pytest.mark.parametrize("script", _CAGE_SCRIPTS)
def test_commit_msg_blocks_when_cage_script_missing(repo: Path, script: str) -> None:
    hooks = _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()
    _stub_other_cages(hooks, script)  # the missing gate is the only variable
    (_scripts_dir(hooks) / f"{script}.sh").unlink()

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode != 0, f"a missing {script}.sh must block the commit"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed  # nothing committed


@pytest.mark.parametrize("script", _CAGE_SCRIPTS)
def test_commit_msg_blocks_when_cage_script_not_executable(repo: Path, script: str) -> None:
    # Losing the exec bit is the same fail-closed case as deletion.
    hooks = _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()
    _stub_other_cages(hooks, script)
    (_scripts_dir(hooks) / f"{script}.sh").chmod(0o644)

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode != 0, f"a non-executable {script}.sh must block the commit"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed


# --- a foreign hook cannot shadow the cage block; it is chained, not appended ----
#
# install_hook APPENDED the cage block after any pre-existing foreign hook. A
# foreign hook ending in `exit 0` (the common case) returned before the appended
# block ran, so the gate never fired yet install reported success. The installer
# now moves the foreign hook to a `<hook>.ai-toolkit-foreign` sidecar and installs
# the cage block in its place; the cage block invokes the sidecar as a SEPARATE
# process (its own shebang / shell options / argv / stdin) once the gates pass.


def _foreign(hooks_dir: Path, name: str, body: str) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / name
    hook.write_text(body)
    hook.chmod(0o755)
    return hook


def test_commit_msg_gate_runs_despite_foreign_hook_ending_in_exit_0(repo: Path) -> None:
    # The foreign hook's `exit 0` no longer short-circuits our gate: the cage block
    # runs FIRST (the foreign hook is only chained afterwards), so a bad message is
    # still blocked. Pre-fix the appended block sat behind the foreign `exit 0`.
    _foreign(repo / ".git" / "hooks", "commit-msg", "#!/bin/sh\nexit 0\n")

    _install(repo)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    commit = _commit(repo, "-m", "not a conventional message")

    assert commit.returncode != 0, "cage gate must still block a bad message"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed


def test_foreign_commit_msg_hook_is_chained_in_a_clean_shell(repo: Path, tmp_path: Path) -> None:
    # The foreign hook runs as its OWN process: a body that returns non-zero before
    # its `exit 0` (here an unmatched `grep -q`) must not be aborted by our
    # `set -euo pipefail`. Inlining the body would turn this benign hook into a
    # commit-blocker; chaining it in a fresh `/bin/sh` lets it complete and write
    # its sentinel, and the commit succeeds.
    sentinel = tmp_path / "foreign_ran"
    _foreign(
        repo / ".git" / "hooks",
        "commit-msg",
        f'#!/bin/sh\ngrep -q NEVER_MATCHES "$1"\necho ran > "{sentinel}"\nexit 0\n',
    )

    hooks = _install(repo)
    for name in _CAGE_SCRIPTS:
        _stub_cage(hooks, name)  # isolate: only the chained foreign hook can act

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode == 0, commit.stderr
    assert sentinel.is_file(), "chained foreign commit-msg hook must run to completion"


def test_foreign_commit_msg_hook_can_still_veto_commit(repo: Path) -> None:
    # The chained foreign hook stays fail-closed: its non-zero exit blocks the
    # commit even after the cage gates pass.
    _foreign(repo / ".git" / "hooks", "commit-msg", "#!/bin/sh\nexit 3\n")

    hooks = _install(repo)
    for name in _CAGE_SCRIPTS:
        _stub_cage(hooks, name)
    seed = _git(repo, "rev-parse", "HEAD").strip()

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode != 0, "a foreign hook's non-zero exit must block the commit"
    assert _git(repo, "rev-parse", "HEAD").strip() == seed


def test_disabled_foreign_hook_stays_disabled(repo: Path, tmp_path: Path) -> None:
    # A foreign hook the user disabled (exec bit cleared) must not be re-enabled by
    # install: `mv` preserves the mode and the emitted `[ -x ]` guard skips it, so a
    # body that would veto never runs.
    sentinel = tmp_path / "foreign_ran"
    foreign = _foreign(
        repo / ".git" / "hooks", "commit-msg", f'#!/bin/sh\necho ran > "{sentinel}"\nexit 3\n'
    )
    foreign.chmod(0o644)  # disabled

    hooks = _install(repo)
    for name in _CAGE_SCRIPTS:
        _stub_cage(hooks, name)

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode == 0, commit.stderr  # disabled foreign hook did not veto
    assert not sentinel.is_file(), "a disabled foreign hook must not be chained"


def test_foreign_hook_chaining_survives_reinstall(repo: Path, tmp_path: Path) -> None:
    # Re-running the installer (the fail-closed error tells users to) must not lose
    # the foreign hook: the sidecar persists across a second install.
    sentinel = tmp_path / "foreign_ran"
    _foreign(repo / ".git" / "hooks", "commit-msg", f'#!/bin/sh\necho ran > "{sentinel}"\nexit 0\n')

    _install(repo)
    hooks = _install(repo)  # second install — must preserve the sidecar
    for name in _CAGE_SCRIPTS:
        _stub_cage(hooks, name)

    commit = _commit(repo, "-m", "feat(x): valid subject", "-m", "Refs #1")

    assert commit.returncode == 0, commit.stderr
    assert sentinel.is_file(), "foreign hook must survive a re-install"


def test_foreign_pre_push_hook_is_chained_with_stdin(repo: Path, tmp_path: Path) -> None:
    # A foreign pre-push hook is chained too, and receives git's ref lines on stdin
    # (the cage block drained them for the test gate, then re-feeds them).
    log = tmp_path / "foreign_stdin.log"
    _foreign(repo / ".git" / "hooks", "pre-push", f'#!/bin/sh\ncat >> "{log}"\nexit 0\n')
    local = _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert log.is_file(), "foreign pre-push hook was not chained"
    assert local in log.read_text()  # git's ref lines reached the foreign hook


def test_foreign_pre_push_hook_can_still_veto_push(repo: Path) -> None:
    # Symmetric with the commit-msg veto: a foreign pre-push hook's non-zero exit
    # aborts the push after the cage test gate has passed.
    seed = _remote_sha(repo)
    _foreign(repo / ".git" / "hooks", "pre-push", "#!/bin/sh\nexit 5\n")
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)

    push = _push(repo)

    assert push.returncode != 0, "a foreign pre-push hook's non-zero exit must abort the push"
    assert _remote_sha(repo) == seed


def test_foreign_pre_push_hook_ignoring_stdin_does_not_abort_push(repo: Path) -> None:
    # A foreign pre-push hook that never reads stdin must not abort the push: the
    # here-string feed means the writer can't SIGPIPE and taint the exit code.
    _foreign(repo / ".git" / "hooks", "pre-push", "#!/bin/sh\nexit 0\n")
    _unpushed_commit(repo)
    hooks = _install(repo)
    _stub_selector(hooks, exit_code=0)

    push = _push(repo)

    assert push.returncode == 0, push.stderr
    assert _remote_sha(repo) == _git(repo, "rev-parse", "HEAD").strip()


def test_uninstall_restores_foreign_hook(repo: Path) -> None:
    original = "#!/bin/sh\n# my own hook\nexit 0\n"
    _foreign(repo / ".git" / "hooks", "commit-msg", original)

    _install(repo)
    subprocess.run(
        ["bash", str(INSTALL), "--uninstall", str(repo)],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )

    restored = repo / ".git" / "hooks" / "commit-msg"
    assert restored.read_text() == original, "uninstall must restore the foreign hook verbatim"
    assert not (repo / ".git" / "hooks" / "commit-msg.ai-toolkit-foreign").exists()


# ── Managed runtime-artifact excludes (issue #206) ──
#
# The pre-push test gate writes .testmondata* (plus its -shm/-wal WAL sidecars)
# and, under AI_TOOLKIT_OTEL, per-run OTel artifacts under .ai-toolkit/. On an
# EXISTING checkout without a committed .gitignore for these, the untracked files
# make `git status --porcelain` non-empty and the #172 ready gate refuses
# ready/<N>. worktree-new seeds these into a spoke worktree; the installer does
# the same for an existing checkout (the hub, or a pre-#206 worktree).


def _info_exclude_lines(repo: Path) -> list[str]:
    raw = _git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = repo / p
    return p.read_text().splitlines() if p.exists() else []


def test_seeds_runtime_artifact_excludes(repo: Path) -> None:
    _install(repo)

    lines = _info_exclude_lines(repo)
    assert ".testmondata*" in lines, f".testmondata* not seeded into info/exclude\n{lines}"
    assert ".ai-toolkit/" in lines, f".ai-toolkit/ not seeded into info/exclude\n{lines}"


def test_runtime_artifact_excludes_seeded_at_most_once(repo: Path) -> None:
    # A repeat install (routine after a hook change) must not duplicate entries.
    _install(repo)
    _install(repo)

    lines = _info_exclude_lines(repo)
    assert lines.count(".testmondata*") == 1, f"duplicate .testmondata* exclude\n{lines}"
    assert lines.count(".ai-toolkit/") == 1, f"duplicate .ai-toolkit/ exclude\n{lines}"


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
