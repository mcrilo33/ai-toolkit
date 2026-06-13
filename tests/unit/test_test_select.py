"""Unit tests for shared/hooks/test-select.sh — the tiered, diff-aware selector.

Issue #19 makes the pre-push hook the single owner of test execution. This
script classifies the pushed diff and runs nothing / `pytest --testmon` / the
full suite accordingly, with default-to-full safety (anything not provably
docs-only or python-only runs the full suite, and testmon-absent falls back to
the full suite rather than silently skipping).

Hermetic, like test_worktree_land.py: a throwaway git repo plus a `pytest` stub
on PATH whose `--help` advertises (or hides) `--testmon` and whose normal
invocation logs `RUN <args>` and exits a chosen code. The diff range git feeds
the pre-push hook on stdin (`<local ref> <local sha> <remote ref> <remote sha>`)
is synthesized from real commit SHAs. No real pytest runs recursively.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TEST_SELECT = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "test-select.sh"
ZERO_SHA = "0" * 40

# Pin git config to nothing so a host's global config (core.hooksPath, gpg,
# templateDir) can't reach the commits these tests drive.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit (the merge-base for branches)."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _commit(repo: Path, files: dict[str, str], msg: str = "change") -> str:
    """Write `files` (path → contents, dirs created), commit them, return the SHA."""
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _rev(repo)


def _stdin(local_sha: str, remote_sha: str, ref: str = "refs/heads/feature/x") -> str:
    """One pre-push stdin line: <local ref> <local sha> <remote ref> <remote sha>."""
    return f"{ref} {local_sha} {ref} {remote_sha}\n"


def _make_pytest_stub(bindir: Path, runlog: Path, *, testmon: bool, exit_code: int = 0) -> None:
    """Install a `pytest` stub on PATH.

    `--help` prints usage, advertising `--testmon` only when `testmon` is True
    (this is how test-select.sh probes plugin availability). Any other call logs
    `RUN <args>` to `runlog` and exits `exit_code` — so a test can assert which
    tier ran and that a failure propagates.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    testmon_line = '  echo "  --testmon  select impacted tests"' if testmon else ":"
    (bindir / "pytest").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  --help|-h)\n"
        '    echo "usage: pytest [options]"\n'
        f"    {testmon_line}\n"
        "    exit 0 ;;\n"
        "esac\n"
        f'printf "RUN %s\\n" "$*" >> "{runlog}"\n'
        f"exit {exit_code}\n"
    )
    (bindir / "pytest").chmod(0o755)


def _run_select(
    repo: Path, stdin: str, bindir: Path, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def _runlog(path: Path) -> str:
    return path.read_text() if path.exists() else ""


# --- the docs-only tier: run nothing --------------------------------------------


def test_docs_only_markdown_runs_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"README.md": "seed\nmore\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_docs_directory_runs_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"docs/guide.txt": "a doc under docs/\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_image_change_runs_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"assets/logo.png": "binary-ish\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


# --- the python tier: pytest --testmon (or full when testmon is absent) ----------


def test_python_only_with_testmon_runs_testmon(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)


def test_python_only_without_testmon_runs_full_suite(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN" in log  # the full suite ran
    assert "--testmon" not in log  # but never via testmon


def test_mixed_docs_and_python_runs_testmon(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n", "README.md": "seed\nx\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)  # docs alongside python stay python-tier


# --- the full-suite tier: default-to-full safety ---------------------------------


def test_shell_change_runs_full_suite(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"scripts/do.sh": "#!/bin/sh\necho hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN" in log
    assert "--testmon" not in log  # .sh forces the full suite even with testmon present


def test_yaml_config_runs_full_suite(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" in _runlog(runlog)
    assert "--testmon" not in _runlog(runlog)


def test_unrecognized_extension_runs_full_suite(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"notes.txt": "plain text, not a doc type\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" in _runlog(runlog)
    assert "--testmon" not in _runlog(runlog)


def test_mixed_python_and_shell_runs_full_suite(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n", "scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" not in _runlog(runlog)  # one non-py file downgrades to full


# --- range resolution from the pre-push stdin ------------------------------------


def test_new_branch_uses_merge_base_fallback(repo: Path, tmp_path: Path) -> None:
    # A new branch has an all-zero remote sha; the range falls back to
    # merge-base(default, local) so only the branch's own changes are classified.
    base = _rev(repo)  # main stays here
    _git(repo, "checkout", "-q", "-b", "feature/new")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    assert base != tip
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, ZERO_SHA, "refs/heads/feature/new"), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)


def test_branch_deletion_runs_nothing(repo: Path, tmp_path: Path) -> None:
    # A delete push has an all-zero local sha — nothing is being added to test.
    base = _rev(repo)
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(ZERO_SHA, base, "refs/heads/feature/x"), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_empty_stdin_runs_nothing(repo: Path, tmp_path: Path) -> None:
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, "", tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


# --- safe fallback: no pytest at all ---------------------------------------------


def test_no_pytest_runs_nothing(repo: Path, tmp_path: Path) -> None:
    # With no pytest resolvable there is nothing to run, even for a python diff —
    # the gate degrades to a no-op rather than erroring the push.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    sandbox = tmp_path / "nopy"
    sandbox.mkdir()
    git_bin = shutil.which("git")
    assert git_bin, "git must be on PATH for this test"
    os.symlink(git_bin, sandbox / "git")  # git stays reachable
    for name in ("python3", "python"):  # but `import pytest` always fails
        stub = sandbox / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    env = {**_GIT_ENV, "PATH": f"{sandbox}:/usr/bin:/bin"}

    proc = subprocess.run(
        ["/bin/bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr


# --- env escape hatches (threaded from worktree-land's --skip-tests/--test-cmd) ---


def test_skip_env_overrides_to_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})  # would otherwise run
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TEST_SELECT_SKIP": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_cmd_env_runs_custom_command(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"README.md": "seed\nx\n"})  # docs-only: tiered would skip
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    custom = tmp_path / "custom.log"

    proc = _run_select(
        repo,
        _stdin(tip, base),
        tmp_path / "bin",
        env_extra={"TEST_SELECT_CMD": f"echo ran >> {custom}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert custom.exists() and "ran" in custom.read_text()  # override beats tiered
    assert "RUN" not in _runlog(runlog)  # the tiered pytest path was not taken


def test_cmd_env_propagates_exit_code(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TEST_SELECT_CMD": "exit 9"}
    )

    assert proc.returncode == 9  # a failing custom suite blocks the push


# --- the blocking contract: a failing suite aborts the push ----------------------


def test_failing_suite_blocks_with_nonzero_exit(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, exit_code=1)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 1  # non-zero exit is what aborts the pre-push
    assert "--testmon" in _runlog(runlog)
