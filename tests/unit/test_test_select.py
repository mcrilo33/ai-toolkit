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

# What the pytest stubs answer to `--version` — the gate records it as the
# green-tree stamp's env fingerprint (issue #122).
STUB_ENV_FINGERPRINT = "pytest 9.9-stub"

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


def _make_pytest_stub(
    bindir: Path, runlog: Path, *, testmon: bool, xdist: bool = True, exit_code: int = 0
) -> None:
    """Install a `pytest` stub on PATH.

    `--help` prints usage, advertising `--testmon` only when `testmon` is True and
    xdist's `-n numprocesses` only when `xdist` is True (this is how test-select.sh
    probes plugin availability). Any other call logs `RUN <args>` plus the `GIT_DIR`
    it inherited as `GITDIR=[<val>|UNSET]` to `runlog`, then exits `exit_code` — so a
    test can assert which tier ran, that a failure propagates, and that the git-hook
    env strip reached the pytest child.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    testmon_line = '  echo "  --testmon  select impacted tests"' if testmon else ":"
    xdist_line = '  echo "  -n numprocesses, --numprocesses=numprocesses"' if xdist else ":"
    (bindir / "pytest").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  --help|-h)\n"
        '    echo "usage: pytest [options]"\n'
        f"    {testmon_line}\n"
        f"    {xdist_line}\n"
        "    exit 0 ;;\n"
        "  --version)\n"
        f'    echo "{STUB_ENV_FINGERPRINT}"\n'
        "    exit 0 ;;\n"
        "esac\n"
        f'printf "RUN %s\\n" "$*" >> "{runlog}"\n'
        f'printf "GITDIR=[%s]\\n" "${{GIT_DIR-UNSET}}" >> "{runlog}"\n'
        f"exit {exit_code}\n"
    )
    (bindir / "pytest").chmod(0o755)


def _make_python_module_stub(bindir: Path, runlog: Path, *, testmon: bool) -> None:
    """Install a `python3` stub that resolves as the `python3 -m pytest` runner.

    With no `pytest` binary on PATH, detect_pytest falls back to `python3 -m
    pytest` when `python3 -c 'import pytest'` succeeds. This stub answers that
    import probe, advertises `--testmon` in `-m pytest --help` (per `testmon`),
    and logs `RUN <args>` for `-m pytest <args>` — covering the multi-word runner.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    testmon_line = 'echo "  --testmon  select impacted tests"' if testmon else ":"
    (bindir / "python3").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'  # `import pytest` succeeds
        'if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then\n'
        "  shift 2\n"
        '  case "$1" in\n'
        f'    --help|-h) echo "usage: pytest"; {testmon_line}; '
        'echo "  -n numprocesses"; exit 0 ;;\n'
        f'    --version) echo "{STUB_ENV_FINGERPRINT}"; exit 0 ;;\n'
        "  esac\n"
        f'  printf "RUN %s\\n" "$*" >> "{runlog}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    (bindir / "python3").chmod(0o755)


def _make_testmon_modeling_stub(bindir: Path, runlog: Path, *, impact: list[str]) -> None:
    """Install a `pytest` stub that MODELS testmon selection + `--ignore` + exit 5.

    The plain `_make_pytest_stub` only echoes its argv, so it cannot observe the
    SELECTED-mixed double-run (issue #270): testmon re-running a mapped mirror test
    the explicit leg already ran. This stub does model it.

    For every invocation it logs one `RAN:<file>` line per test FILE it actually
    executes, so a test can count executions (never confused with a file name that
    merely appears inside a `--ignore=` flag):

    - a plain (non-`--testmon`) call runs each positional test-file arg (a
      `path::node` node-id is counted at its file);
    - a `--testmon` call runs `impact` MINUS any `--ignore=<file>`'d files — and
      when that leaves nothing, it exits 5 ("no tests collected"), exactly as real
      pytest does when `--ignore` covers testmon's whole impact set.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    impact_words = " ".join(impact)
    (bindir / "pytest").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --help|-h) echo "usage: pytest"; echo "  --testmon"; '
        'echo "  -n numprocesses"; exit 0 ;;\n'
        f'  --version) echo "{STUB_ENV_FINGERPRINT}"; exit 0 ;;\n'
        "esac\n"
        f'printf "RUN %s\\n" "$*" >> "{runlog}"\n'
        "testmon=0\n"
        'ignored=" "\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    --testmon) testmon=1 ;;\n"
        '    --ignore=*) ignored="$ignored${a#--ignore=} " ;;\n'
        "    auto) : ;;\n"  # the `-n auto` value (xdist, #276) — not a test file
        "    -*) : ;;\n"
        '    *) f="${a%%::*}"; printf "RAN:%s\\n" "$f" >> "'
        f'{runlog}" ;;\n'
        "  esac\n"
        "done\n"
        'if [ "$testmon" = "1" ]; then\n'
        "  ran=0\n"
        f"  for f in {impact_words}; do\n"
        '    case "$ignored" in\n'
        '      *" $f "*) : ;;\n'
        f'      *) printf "RAN:%s\\n" "$f" >> "{runlog}"; ran=1 ;;\n'
        "    esac\n"
        "  done\n"
        '  [ "$ran" = "1" ] || exit 5\n'  # no tests collected after --ignore
        "fi\n"
        "exit 0\n"
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


def test_python_under_docs_runs_testmon(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"docs/conf.py": "project = 'x'\n"})  # code, not a doc
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)  # a *.py is python even under docs/


# --- the full-suite tier: default-to-full safety ---------------------------------


def test_full_tier_runs_two_phase_serial_split(repo: Path, tmp_path: Path) -> None:
    # Issue #328: the FULL suite runs two-phase — the parallel-safe bulk under
    # `-n auto -m "not serial"`, then the ref-mutating tail single-process under
    # `-m serial` (never under xdist workers). A shell change forces the FULL tier.
    base = _rev(repo)
    tip = _commit(repo, {"scripts/thing.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto -m not serial\n" in log  # parallel bulk, serial deselected
    assert "RUN -m serial\n" in log  # serial tail, single-process (no -n)
    assert "RUN -n auto\n" not in log  # never the old bare full run under xdist


def test_full_tier_serial_leg_no_tests_is_green(repo: Path, tmp_path: Path) -> None:
    # The serial leg exits 5 ("no tests collected") when nothing is marked serial —
    # a GREEN outcome the two-phase runner normalizes so it never blocks a clean push.
    base = _rev(repo)
    tip = _commit(repo, {"scripts/thing.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    # The serial leg (its argv contains `serial` but not `not`) exits 5; every other
    # invocation exits 0.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pytest").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --help|-h) echo "usage: pytest"; echo "  --testmon"; '
        'echo "  -n numprocesses"; exit 0 ;;\n'
        f'  --version) echo "{STUB_ENV_FINGERPRINT}"; exit 0 ;;\n'
        "esac\n"
        f'printf "RUN %s\\n" "$*" >> "{runlog}"\n'
        "is_serial=0; is_not=0\n"
        'for a in "$@"; do\n'
        '  [ "$a" = "serial" ] && is_serial=1\n'
        '  [ "$a" = "not serial" ] && is_not=1\n'
        "done\n"
        '[ "$is_serial" = "1" ] && [ "$is_not" = "0" ] && exit 5\n'
        "exit 0\n"
    )
    (bindir / "pytest").chmod(0o755)

    proc = _run_select(repo, _stdin(tip, base), bindir)

    assert proc.returncode == 0, proc.stderr + "\n" + _runlog(runlog)


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


# --- the git-hook env strip reaches the pytest child (issue #30) -----------------
def test_runner_child_does_not_inherit_leaked_git_dir(repo: Path, tmp_path: Path) -> None:
    # GIT_DIR points at the (valid) test repo, mimicking git's native-hook export.
    # Classification still resolves the diff under it, but the pytest child must
    # run with GIT_DIR stripped so a git-shelling test can't reach the real repo.
    base = _rev(repo)
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})  # non-py → FULL tier
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"GIT_DIR": str(repo / ".git")}
    )

    assert proc.returncode == 0, proc.stderr
    assert "RUN" in _runlog(runlog)  # classification under GIT_DIR worked, tier ran
    assert "GITDIR=[UNSET]" in _runlog(runlog)  # the child saw GIT_DIR stripped


def test_custom_suite_does_not_inherit_leaked_git_dir(repo: Path, tmp_path: Path) -> None:
    # The TEST_SELECT_CMD escape hatch (worktree-land --test-cmd) is a test command
    # too, so it must run under the same strip.
    out = tmp_path / "cmd.log"
    proc = _run_select(
        repo,
        _stdin(_rev(repo), ZERO_SHA),
        tmp_path / "bin",
        env_extra={
            "GIT_DIR": str(repo / ".git"),
            "TEST_SELECT_CMD": f'printf "CMDGITDIR=[%s]\\n" "${{GIT_DIR-UNSET}}" >> "{out}"',
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert out.read_text().strip() == "CMDGITDIR=[UNSET]"


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


# --- tag-only marker pushes: carry no code, skip the suite (issue #45) ------------


def test_tag_only_push_runs_nothing(repo: Path, tmp_path: Path) -> None:
    # Pushing a marker tag (ready/N, gate/N) carries no new code — the testable
    # unit is the branch push, not the pointer. A tag-only push must skip the
    # suite even though the tagged commit (ahead of the default branch) touches
    # python, which would otherwise trip the merge-base fallback into testmon.
    _git(repo, "checkout", "-q", "-b", "feature/ahead")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, ZERO_SHA, "refs/tags/ready/45"), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog), "a tag-only push must not run the suite"


def test_tag_only_push_with_shell_change_runs_nothing(repo: Path, tmp_path: Path) -> None:
    # Even a tag over a .sh change (which would otherwise force the FULL suite)
    # is skipped — it is the tag ref, not the code, that is being pushed.
    _git(repo, "checkout", "-q", "-b", "feature/ahead")
    tip = _commit(repo, {"scripts/do.sh": "#!/bin/sh\necho hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, ZERO_SHA, "refs/tags/gate/45"), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_branch_and_tag_mix_runs_the_suite(repo: Path, tmp_path: Path) -> None:
    # A push that carries a branch ref alongside a tag is NOT tag-only — the
    # branch carries code, so the suite runs as usual.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    stdin = _stdin(tip, base, "refs/heads/feature/x") + _stdin(tip, ZERO_SHA, "refs/tags/ready/45")

    proc = _run_select(repo, stdin, tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog), "a branch+tag push still tests the branch"


# --- no runner but tests are demanded: fail closed (issue #213) -------------------


def _make_no_pytest_sandbox(tmp_path: Path) -> Path:
    """Build a PATH sandbox with git reachable but no resolvable pytest runner.

    `git` is symlinked in so the script's own classification calls work; the
    `python3`/`python` stubs fail the `import pytest` probe, so `detect_pytest`
    resolves nothing (no `.venv/bin/pytest` and no `pytest` on PATH either).
    """
    sandbox = tmp_path / "nopy"
    sandbox.mkdir()
    git_bin = shutil.which("git")
    assert git_bin, "git must be on PATH for this test"
    os.symlink(git_bin, sandbox / "git")  # git stays reachable
    for py in ("python3", "python"):  # but `import pytest` always fails
        stub = sandbox / py
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    return sandbox


def test_no_pytest_blocks_python_diff(repo: Path, tmp_path: Path) -> None:
    # A python diff demands tests; with no runner resolvable the gate cannot
    # prove the tree green, so it fails closed (nonzero) rather than shipping an
    # untested diff on a silent exit 0 (issue #213).
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    sandbox = _make_no_pytest_sandbox(tmp_path)
    env = {**_GIT_ENV, "PATH": f"{sandbox}:/usr/bin:/bin"}

    proc = subprocess.run(
        ["/bin/bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0, proc.stderr
    assert "no pytest" in proc.stderr


def test_no_pytest_blocks_full_suite_demand(repo: Path, tmp_path: Path) -> None:
    # An unmapped shell change escalates to the full suite; with no runner that
    # full-suite demand also fails closed (issue #213) — a non-python diff that
    # needs tests is no different from a python one.
    base = _rev(repo)
    tip = _commit(repo, {"scripts/unmapped.sh": "echo hi\n"})
    sandbox = _make_no_pytest_sandbox(tmp_path)
    env = {**_GIT_ENV, "PATH": f"{sandbox}:/usr/bin:/bin"}

    proc = subprocess.run(
        ["/bin/bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0, proc.stderr
    assert "no pytest" in proc.stderr


def test_no_pytest_blocks_selected_diff(repo: Path, tmp_path: Path) -> None:
    # A mapped non-python change resolves to the SELECTED tier; with no runner it
    # fails closed too, proving the block is tier-agnostic across all three
    # test-demanding tiers (issue #213), not just PYTHON/FULL.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "#!/bin/sh\necho hi\n"})
    sandbox = _make_no_pytest_sandbox(tmp_path)
    env = {**_GIT_ENV, "PATH": f"{sandbox}:/usr/bin:/bin"}

    proc = subprocess.run(
        ["/bin/bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0, proc.stderr
    assert "no pytest" in proc.stderr


def test_no_pytest_still_allows_docs_only(repo: Path, tmp_path: Path) -> None:
    # A docs-only diff needs no runner at all — the missing-runner block sits
    # after the NOTHING short-circuit, so this legitimate no-op still exits 0.
    base = _rev(repo)
    tip = _commit(repo, {"README.md": "seed\nmore\n"})
    sandbox = _make_no_pytest_sandbox(tmp_path)
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


def test_no_pytest_with_skip_env_still_passes(repo: Path, tmp_path: Path) -> None:
    # TEST_SELECT_SKIP is handled before the runner probe, so the explicit
    # override still lets a runner-less checkout push (issue #213 keeps the
    # escape hatch working — only the silent fail-open is closed).
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    sandbox = _make_no_pytest_sandbox(tmp_path)
    env = {**_GIT_ENV, "PATH": f"{sandbox}:/usr/bin:/bin", "TEST_SELECT_SKIP": "1"}

    proc = subprocess.run(
        ["/bin/bash", str(TEST_SELECT)],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr


def test_module_runner_form_uses_testmon(repo: Path, tmp_path: Path) -> None:
    # With no `pytest` binary, the runner resolves to `python3 -m pytest`; the
    # multi-word form must still probe testmon and run it for a python diff.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    sandbox = tmp_path / "mbin"
    runlog = tmp_path / "run.log"
    _make_python_module_stub(sandbox, runlog, testmon=True)
    git_bin = shutil.which("git")
    assert git_bin, "git must be on PATH for this test"
    os.symlink(git_bin, sandbox / "git")  # no pytest binary, only python3 -m pytest
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
    assert "--testmon" in _runlog(runlog)


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


# --- green-tree stamps: never re-run a suite already proven on this tree (#122) ---


def _stamp_path(repo: Path) -> Path:
    """The stamp file the gate would mint for the repo's current HEAD tree."""
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    return repo / ".git" / ".gate-stamps" / tree


def test_gate_pass_mints_stamp_with_tier_and_env(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    content = _stamp_path(repo).read_text()
    assert "tier=testmon\n" in content
    assert f"env={STUB_ENV_FINGERPRINT}\n" in content


def test_python_without_testmon_mints_full_stamp(repo: Path, tmp_path: Path) -> None:
    # The tier stamped is the tier that RAN: a python diff without testmon falls
    # back to the full suite, so its stamp records the stronger proof.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "tier=full\n" in _stamp_path(repo).read_text()


def test_second_run_same_tree_equal_demand_skips_without_pytest(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # first run mints
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert _runlog(runlog) == after_first  # pytest was NOT invoked again
    assert "green-tree stamp" in proc.stderr  # loud, distinct skip note …
    assert "TEST_SELECT_SKIP" not in proc.stderr  # … never mistakable for the hatch


def test_stronger_stamp_covers_weaker_demand(repo: Path, tmp_path: Path) -> None:
    # A full-tier stamp (from a .sh-bearing push) covers a later testmon-tier
    # demand on the very same tree.
    base = _rev(repo)
    py_tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    sh_tip = _commit(repo, {"scripts/do.sh": "#!/bin/sh\necho hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(sh_tip, base), tmp_path / "bin")  # FULL run mints full
    after_first = _runlog(runlog)

    # Same working tree (HEAD = sh_tip), but this push's range is python-only,
    # so the gate demands only testmon — the full stamp is at least as strong.
    proc = _run_select(repo, _stdin(py_tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert _runlog(runlog) == after_first
    assert "green-tree stamp" in proc.stderr


def test_weaker_stamp_stronger_demand_runs_and_upgrades(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # testmon run mints testmon
    after_first = _runlog(runlog)

    # An unresolvable remote sha forces the FULL tier (cannot prove safe) — a
    # stronger demand than the testmon stamp, so the suite must run …
    proc = _run_select(repo, _stdin(tip, "deadbeef" * 5), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # … and it did
    assert "tier=full\n" in _stamp_path(repo).read_text()  # … and upgraded the stamp


def test_changed_tracked_file_yields_new_key_and_no_skip(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # mints for this tree
    after_first = _runlog(runlog)

    tip2 = _commit(repo, {"tests/test_mod.py": "def test_x(): pass\n"})
    proc = _run_select(repo, _stdin(tip2, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # new tree ⇒ no skip
    assert _stamp_path(repo).is_file()  # and the new tree got its own stamp


def test_env_fingerprint_mismatch_never_skips(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # mints with the stub env
    stamp = _stamp_path(repo)
    stamp.write_text(
        stamp.read_text().replace(f"env={STUB_ENV_FINGERPRINT}", "env=py3.9-elsewhere")
    )
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # a wrong env re-proves, never skips


def test_skip_env_does_not_mint(repo: Path, tmp_path: Path) -> None:
    # TEST_SELECT_SKIP behaves exactly as today: nothing runs, nothing is proven,
    # so no stamp may appear.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TEST_SELECT_SKIP": "1"}
    )

    assert proc.returncode == 0, proc.stderr
    assert not _stamp_path(repo).exists()


def test_cmd_env_neither_consumes_nor_mints(repo: Path, tmp_path: Path) -> None:
    # TEST_SELECT_CMD behaves exactly as today: the custom command runs even when
    # a covering stamp exists (no consume), and its pass proves an unknown tier
    # (no mint/upgrade).
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # mints testmon
    custom = tmp_path / "custom.log"

    proc = _run_select(
        repo,
        _stdin(tip, base),
        tmp_path / "bin",
        env_extra={"TEST_SELECT_CMD": f"echo ran >> {custom}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert custom.exists() and "ran" in custom.read_text()  # ran despite the stamp
    assert "tier=testmon\n" in _stamp_path(repo).read_text()  # and did not upgrade it


def test_dirty_tree_does_not_consume_stamp(repo: Path, tmp_path: Path) -> None:
    # Deviation-1 soundness guard: the suite runs against the working tree, so a
    # dirty checkout must not be covered by a stamp for HEAD's (different) tree.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # clean run mints
    after_first = _runlog(runlog)
    (repo / "pkg" / "mod.py").write_text("x = 2  # uncommitted\n")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # the suite ran
    assert "working tree dirty" in proc.stderr  # logged distinctly


def test_dirty_tree_does_not_mint(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    (repo / "pkg" / "mod.py").write_text("x = 2  # uncommitted\n")
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" in _runlog(runlog)  # the suite still ran normally
    assert not _stamp_path(repo).exists()  # but proved nothing about HEAD's tree
    assert "working tree dirty" in proc.stderr


def test_failing_suite_does_not_mint(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, exit_code=1)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 1
    assert not _stamp_path(repo).exists()  # only a green run mints


def test_missing_stamp_lib_degrades_to_running_the_suite(repo: Path, tmp_path: Path) -> None:
    # An installed hook copy that predates gate-stamp.sh (the #45 stale-hook trap)
    # must behave exactly as today: run the suite, mint nothing, crash nothing.
    hookdir = tmp_path / "installed"
    (hookdir / "lib").mkdir(parents=True)
    src = TEST_SELECT.parent
    shutil.copy(TEST_SELECT, hookdir / "test-select.sh")
    shutil.copy(src / "lib" / "utils.sh", hookdir / "lib" / "utils.sh")
    shutil.copy(src / "lib" / "telemetry.sh", hookdir / "lib" / "telemetry.sh")
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = subprocess.run(
        ["bash", str(hookdir / "test-select.sh")],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)  # today's behavior, unchanged
    assert not _stamp_path(repo).exists()  # stamps silently disabled


def _write_ref_test(repo: Path, test_rel: str, script_basename: str) -> None:
    """A minimal tests/**/test_*.py referencing `script_basename` as a token."""
    path = repo / test_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'"""Covers {script_basename} behavior."""\n')


def test_mapped_shell_change_runs_only_mapped_tests(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "#!/bin/sh\necho hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto tests/unit/test_do.py\n" in log  # exactly the mapped set
    assert "--testmon" not in log
    assert "RUN \n" not in log  # never the bare full suite


def test_two_mapped_files_run_deduped_sorted_union(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_a.py", "one.sh")
    (repo / "tests/unit/test_b.py").write_text('"""one.sh and two.sh together."""\n')
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/one.sh": "echo 1\n", "scripts/two.sh": "echo 2\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto tests/unit/test_a.py tests/unit/test_b.py\n" in _runlog(runlog)


def test_mixed_py_and_mapped_shell_runs_mapped_plus_testmon(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n", "pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto tests/unit/test_do.py\n" in log  # the mapped set …
    # ... plus testmon for the python part, with the mapped file --ignore'd so
    # testmon cannot re-run what the explicit leg already ran (issue #270).
    assert "RUN --testmon --ignore=tests/unit/test_do.py\n" in log
    assert "RUN \n" not in log


def test_mixed_mirror_test_runs_exactly_once(repo: Path, tmp_path: Path) -> None:
    # The common tooling shape (issue #270): a diff editing a mapped *.sh and its
    # mirror *.py together. The explicit leg runs the mirror test file, and testmon
    # — seeing the changed .py — would re-run the SAME file, so the ~292-test suite
    # runs twice. The fix --ignore's the mapped files from the testmon leg, so the
    # mirror executes EXACTLY once. When the mirror IS testmon's whole impact set,
    # the ignored testmon leg collects nothing (exit 5) — a green outcome the gate
    # must not propagate as a failure.
    _write_ref_test(repo, "tests/unit/test_gate_broker.py", "gate-broker.sh")
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(
        repo,
        {
            "shared/skills/hub/scripts/gate-broker.sh": "echo hi\n",
            # the mirror .py changes too, but still references the script's basename
            "tests/unit/test_gate_broker.py": '"""Covers gate-broker.sh behavior. edited."""\n',
        },
    )
    runlog = tmp_path / "run.log"
    _make_testmon_modeling_stub(tmp_path / "bin", runlog, impact=["tests/unit/test_gate_broker.py"])

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr  # empty testmon leg (exit 5) is green
    log = _runlog(runlog)
    # the core acceptance: the mirror test file executes exactly once, not twice
    assert log.count("RAN:tests/unit/test_gate_broker.py\n") == 1
    # coverage unchanged: the enforcement meta-test still runs (once, explicit leg)
    assert "RAN:tests/unit/test_test_reverse_index.py\n" in log


def test_unmapped_shell_change_escalates_to_full(repo: Path, tmp_path: Path) -> None:
    # A tests/ dir exists but nothing references new.sh: conservative fallback.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/new.sh": "echo new\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # the full suite, not a selection


def test_unmapped_shell_change_emits_witness_warning(repo: Path, tmp_path: Path) -> None:
    # #191 witness signal (feeds #187's fail-open audit): a changed *.sh that no
    # test references is a bash blind spot — testmon tracks python imports only, so
    # nothing re-exercises the script by subprocess/source. It still escalates to
    # FULL, but the gate must emit a distinct, greppable warning that names it.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")  # tests/ exists, refs do.sh only
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/new.sh": "echo new\n"})  # unmapped shell change
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # conservative fallback: FULL still runs
    assert "witness: unmapped-shell" in proc.stderr  # the greppable audit signal …
    assert "scripts/new.sh" in proc.stderr  # … naming the offending script


def test_witness_warning_deduped_across_refs(repo: Path, tmp_path: Path) -> None:
    # A push carrying the same unmapped .sh on two refs lists the path twice in
    # the diff set; the witness must name it once, not double the #187 audit
    # stream (review finding: UNMAPPED_SH lacked the once-guard MAPPED_TESTS has).
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/new.sh": "echo new\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    stdin = _stdin(tip, base, "refs/heads/feature/a") + _stdin(tip, base, "refs/heads/feature/b")

    proc = _run_select(repo, stdin, tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("witness: unmapped-shell") == 1


def test_mapped_shell_change_emits_no_witness_warning(repo: Path, tmp_path: Path) -> None:
    # A *.sh WITH a referencing test is not a blind spot — no witness warning.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "witness: unmapped-shell" not in proc.stderr


def test_unmapped_nonshell_change_emits_no_shell_witness(repo: Path, tmp_path: Path) -> None:
    # The witness is scoped to shell: an unmapped .yml escalates to FULL but is not
    # the testmon-blind bash blind spot, so it must not raise the shell signal.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"ci/build.yml": "on: push\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "witness: unmapped-shell" not in proc.stderr


def test_exempt_shell_change_emits_no_witness_warning(repo: Path, tmp_path: Path) -> None:
    # An exempt shell script has intentionally no test surface — it skips the suite
    # entirely and must not raise the blind-spot witness.
    _commit(repo, {".test-select-exempt": "scripts/\n"}, "chore: exempt scripts")
    base = _rev(repo)
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "witness: unmapped-shell" not in proc.stderr


def test_mixed_mapped_and_unmapped_escalates_to_full(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n", "scripts/new.sh": "echo new\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto\n" in log
    assert "RUN tests/unit/test_do.py\n" not in log  # full subsumes the selection


def test_exempt_file_change_runs_nothing(repo: Path, tmp_path: Path) -> None:
    _commit(repo, {".test-select-exempt": "notes.txt\n"})
    base = _rev(repo)
    tip = _commit(repo, {"notes.txt": "unrecognized but exempt\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_exempt_directory_prefix_covers_children(repo: Path, tmp_path: Path) -> None:
    _commit(repo, {".test-select-exempt": "settings/\n"})
    base = _rev(repo)
    tip = _commit(repo, {"settings/editor.json": "{}\n", "pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "--testmon" in log  # python tier preserved for the py part
    assert "RUN \n" not in log  # the exempt settings/ file never escalates


def test_exempt_list_change_itself_escalates_to_full(repo: Path, tmp_path: Path) -> None:
    # Editing the exempt list is high-stakes: its basename can never be a
    # filename-shaped token, so it is unmapped by construction → full suite.
    _commit(repo, {".test-select-exempt": "notes.txt\n"})
    base = _rev(repo)
    tip = _commit(repo, {".test-select-exempt": "notes.txt\nLICENSE\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)


def test_selected_failing_suite_blocks_push(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, exit_code=7)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 7  # a red selection aborts the push


def test_selected_pass_mints_selected_stamp_with_set(repo: Path, tmp_path: Path) -> None:
    # #123-D: a green SELECTED run is a durable proof of exactly the set that
    # ran — the stamp records tier AND set so it can never cover a different
    # selection (or a testmon demand) on the same tree.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    content = _stamp_path(repo).read_text()
    assert "tier=selected-set\n" in content
    assert "set=tests/unit/test_do.py\n" in content


def test_missing_reverse_index_lib_degrades_to_full(repo: Path, tmp_path: Path) -> None:
    # An installed hook copy predating lib/test-reverse-index.sh (the #45
    # stale-hook trap) must behave exactly as today: mapped or not, a shell
    # change runs the full suite.
    hookdir = tmp_path / "installed"
    (hookdir / "lib").mkdir(parents=True)
    src = TEST_SELECT.parent
    shutil.copy(TEST_SELECT, hookdir / "test-select.sh")
    for lib in ("utils.sh", "telemetry.sh", "gate-stamp.sh"):
        shutil.copy(src / "lib" / lib, hookdir / "lib" / lib)
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = subprocess.run(
        ["bash", str(hookdir / "test-select.sh")],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # today's behavior, unchanged


# --- the enforcement meta-test rides every pytest-running tier (#123) ------------

META_NODE = "tests/unit/test_test_reverse_index.py::TestControlPlaneCoverage"


def _write_meta_stub(repo: Path) -> None:
    """The meta-test file existing is what arms the append in fixture repos."""
    path = repo / "tests/unit/test_test_reverse_index.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("class TestControlPlaneCoverage:\n    def test_ok(self):\n        pass\n")


def test_selected_tier_appends_meta_test(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert f"RUN -n auto tests/unit/test_do.py {META_NODE}\n" in _runlog(runlog)


def test_python_tier_appends_meta_test_invocation(repo: Path, tmp_path: Path) -> None:
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN --testmon\n" in log
    assert f"RUN {META_NODE}\n" in log  # separate invocation, never inside --testmon


def test_python_tier_without_meta_file_appends_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "TestControlPlaneCoverage" not in _runlog(runlog)  # synced repos unaffected


def test_full_tier_does_not_append_meta_test(repo: Path, tmp_path: Path) -> None:
    # The full suite already contains the meta-test; a second invocation would
    # double-run it.
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"scripts/unmapped.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto\n" in log
    assert "TestControlPlaneCoverage" not in log


def test_script_under_docs_dir_is_never_a_doc(repo: Path, tmp_path: Path) -> None:
    # C-review finding: shared/hooks/docs/helper.sh matched is_doc's */docs/*
    # before mapping, landing an unreferenced control-plane script with a green
    # gate while the meta-test claims the path — red for the NEXT pusher. A
    # script suffix is never docs, wherever it lives.
    base = _rev(repo)
    tip = _commit(repo, {"shared/hooks/docs/helper.sh": "#!/bin/sh\necho hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # unmapped script → FULL, never NOTHING


# --- selected-tier stamps: consume and mint with the set that ran (#123-D) --------


def test_identical_selected_repush_consumes_stamp(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # mints selected+set
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert _runlog(runlog) == after_first  # pytest not invoked again
    assert "green-tree stamp" in proc.stderr


def test_different_selection_same_tree_runs(repo: Path, tmp_path: Path) -> None:
    # Two pushes with different diffs against the SAME working tree: the stamp
    # from the first selection must not cover the second (different set).
    _commit(
        repo,
        {
            "tests/unit/test_one.py": '"""Covers one.sh."""\n',
            "tests/unit/test_two.py": '"""Covers two.sh."""\n',
        },
        "test: seed referencing tests",
    )
    base = _rev(repo)
    c1 = _commit(repo, {"scripts/one.sh": "echo 1\n"})
    c2 = _commit(repo, {"scripts/two.sh": "echo 2\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(c2, c1), tmp_path / "bin")  # mints set={test_two}
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(c1, base), tmp_path / "bin")  # demands {test_one}

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # no unsound cover: it ran
    assert "RUN -n auto tests/unit/test_one.py" in _runlog(runlog)


def test_python_push_after_selected_stamp_still_runs_testmon(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    c1 = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    c2 = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(c2, c1), tmp_path / "bin")  # selected stamp for tree(c2)
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(c1, base), tmp_path / "bin")  # py-only: testmon

    assert proc.returncode == 0, proc.stderr
    assert len(_runlog(runlog)) > len(after_first)  # selected never covers testmon
    assert "--testmon" in _runlog(runlog)


def test_full_stamp_covers_selected_demand_and_skips(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    c1 = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    c2 = _commit(repo, {"scripts/unmapped.sh": "echo new\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(c2, c1), tmp_path / "bin")  # unmapped → FULL mints full
    after_first = _runlog(runlog)

    proc = _run_select(repo, _stdin(c1, base), tmp_path / "bin")  # selected demand

    assert proc.returncode == 0, proc.stderr
    assert _runlog(runlog) == after_first  # full proof covers any selection
    assert "green-tree stamp" in proc.stderr


def test_mixed_selected_green_mints_testmon_flag_and_repush_skips(
    repo: Path, tmp_path: Path
) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n", "pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")
    after_first = _runlog(runlog)

    content = _stamp_path(repo).read_text()
    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert "tier=selected-set\n" in content
    assert "testmon=1\n" in content  # the mixed run proved testmon too
    assert proc.returncode == 0, proc.stderr
    assert _runlog(runlog) == after_first  # identical mixed re-push skips


# --- review-carryover pins from subtask B (verified by probe, now pinned) ---------


def test_mixed_diff_without_testmon_falls_back_to_full(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n", "pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto\n" in log  # the python part demands the full suite
    assert "--testmon" not in log
    assert "RUN tests/unit/test_do.py" not in log  # full subsumes the selection


def test_mapping_to_vanished_test_escalates_full(repo: Path, tmp_path: Path) -> None:
    # A poisoned cache (clean tests/, mapping names a nonexistent test) must
    # escalate, not run a selection that proves nothing.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # builds the cache
    key = _git(repo, "rev-parse", "HEAD:tests").strip()
    cache = repo / ".git" / ".test-reverse-index" / key
    cache.write_text("do.sh\ttests/unit/test_gone.py\n")
    _stamp_path(repo).unlink(missing_ok=True)  # drop any stamp so the tier re-decides

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # escalated to full
    assert "mapped test missing" in proc.stderr


def test_exempt_entry_cannot_hide_mapped_coverage(repo: Path, tmp_path: Path) -> None:
    # Lookup-first hardening (B review): an exempt entry only mutes escalation
    # for UNMAPPED files; a file the index maps still runs its tests.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    _commit(repo, {".test-select-exempt": "scripts/\n"}, "chore: exempt scripts/")
    base = _rev(repo)
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto tests/unit/test_do.py" in _runlog(runlog)  # mapped coverage ran


def test_exempt_only_diff_notes_exemption(repo: Path, tmp_path: Path) -> None:
    _commit(repo, {".test-select-exempt": "notes.txt\n"}, "chore: exempt notes")
    base = _rev(repo)
    tip = _commit(repo, {"notes.txt": "exempt change\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)
    assert "exempt" in proc.stderr  # the audit trail names the real reason


def test_missing_lib_ignores_exempt_entries_and_runs_full(repo: Path, tmp_path: Path) -> None:
    # E relocation pin: the exempt parser lives in lib/test-reverse-index.sh;
    # without the lib (a stale installed hook) there are no exemptions — an
    # exempt-only diff escalates to FULL instead of skipping. Conservative,
    # like the index degradation.
    hookdir = tmp_path / "installed"
    (hookdir / "lib").mkdir(parents=True)
    src = TEST_SELECT.parent
    shutil.copy(TEST_SELECT, hookdir / "test-select.sh")
    for lib in ("utils.sh", "telemetry.sh", "gate-stamp.sh"):
        shutil.copy(src / "lib" / lib, hookdir / "lib" / lib)
    _commit(repo, {".test-select-exempt": "notes.txt\n"}, "chore: exempt notes")
    base = _rev(repo)
    tip = _commit(repo, {"notes.txt": "exempt change\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = subprocess.run(
        ["bash", str(hookdir / "test-select.sh")],
        cwd=str(repo),
        input=_stdin(tip, base),
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # FULL, never a silent exempt skip


# ── Part 1 (issue #276): pytest-xdist on the non-testmon legs ────────────────────
# The FULL suite and the SELECTED mapped-files leg are I/O-bound and embarrassingly
# parallel, so test-select.sh threads `-n auto` onto them. The `--testmon` legs stay
# single-process — testmon serializes a single-writer DB and does not compose with
# xdist (`pytest --testmon -n auto` is unsupported). The runlog records each leg's
# argv as `RUN <args>`, so these pin exactly which legs got parallelized.


def test_full_suite_runs_under_xdist(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"scripts/thing.sh": "echo hi\n"})  # unmapped shell → FULL
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)  # the full suite, parallelized


def test_python_without_testmon_full_suite_runs_under_xdist(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})  # python, but no testmon → FULL
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto\n" in _runlog(runlog)


def test_selected_mapped_leg_runs_under_xdist(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})  # mapped shell → SELECTED
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto tests/unit/test_do.py\n" in _runlog(runlog)


def test_selected_mixed_leg_parallelizes_mapped_but_not_testmon(repo: Path, tmp_path: Path) -> None:
    # A mixed mapped-shell + python diff: the explicit mapped-files leg runs under
    # `-n auto`, but the `--testmon` leg for the python part stays single-process.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n", "pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto tests/unit/test_do.py\n" in log  # mapped leg parallelized …
    # … the testmon leg is NOT — verbatim, no -n auto spliced in (issue #270 dedup form)
    assert "RUN --testmon --ignore=tests/unit/test_do.py\n" in log


def test_testmon_leg_is_never_parallelized(repo: Path, tmp_path: Path) -> None:
    # Regression guard: a python-only diff runs `pytest --testmon` and never gains
    # `-n auto` (testmon's single-writer DB does not compose with xdist).
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})  # python-only → testmon
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN --testmon\n" in log  # the testmon leg ran …
    assert "-n auto" not in log  # … and was never parallelized


def test_full_suite_degrades_to_single_process_without_xdist(repo: Path, tmp_path: Path) -> None:
    # Graceful degrade (issue #276): a runner whose `--help` does not advertise
    # pytest-xdist runs the FULL suite single-process — the gate never blocks a push
    # on `pytest: unrecognized -n` when the plugin is absent.
    base = _rev(repo)
    tip = _commit(repo, {"scripts/thing.sh": "echo hi\n"})  # unmapped shell → FULL
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, xdist=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN \n" in log  # bare full suite, no -n auto
    assert "-n auto" not in log
