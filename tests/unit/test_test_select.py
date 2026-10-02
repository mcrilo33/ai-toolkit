"""Unit tests for shared/hooks/test-select.sh — the fast-tier pre-push selector.

Issue #378 makes CI the gate: the local pre-push hook runs only the fast tier —
mapped/selected tests, the control-plane meta-test and `pytest --testmon` — and
never the whole suite. Anything that used to escalate to the full suite (an
unmapped change, testmon absent, an unresolvable range) now runs the mapped tests
that exist and says that CI is the full gate.

Hermetic, like the land-script tests: a throwaway git repo plus a `pytest` stub
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
    # A seeded testmon database (worktree-new copies the hub's baseline in) is what lets the
    # hook run `--testmon` incrementally; excluded so `_commit`'s `git add -A` never tracks it.
    (r / ".git" / "info" / "exclude").write_text(".testmondata\n")
    (r / ".testmondata").write_text("db\n")
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
    bindir: Path,
    runlog: Path,
    *,
    testmon: bool,
    xdist: bool = True,
    exit_code: int = 0,
    collect_nodes: int = 0,
    collect_exit: int = 0,
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
        # A `--collect-only` pass (the testmon impact probe) lists `collect_nodes` node ids.
        'for a in "$@"; do\n'
        '  if [ "$a" = "--collect-only" ]; then\n'
        f'    i=0; while [ "$i" -lt {collect_nodes} ]; do echo "tests/t.py::test_$i"; i=$((i+1)); done\n'
        f"    exit {collect_exit}\n"
        "  fi\n"
        "done\n"
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
        'for a in "$@"; do\n'
        '  if [ "$a" = "--collect-only" ]; then\n'
        f'    for f in {impact_words}; do echo "$f::test_x"; done\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
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


def _reverse_index_key(repo: Path) -> str:
    """The reverse-index cache key: a hash over the tree objects the map depends
    on — tests/ (token map) and the shipped shell dirs (source graph, #326).
    Mirrors ``_reverse_index_key`` in lib/test-reverse-index.sh."""
    parts = ""
    for d in ("tests", "scripts", "shared", "dashboard"):
        proc = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"HEAD:{d}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and sha:
            parts += f"{d}:{sha};"
    return subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(repo),
        input=parts,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    ).stdout.strip()


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


# --- the python tier: pytest --testmon ------------------------------------------


def test_python_only_with_testmon_runs_testmon(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)




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
















# --- the git-hook env strip reaches the pytest child (issue #30) -----------------
def test_runner_child_does_not_inherit_leaked_git_dir(repo: Path, tmp_path: Path) -> None:
    # GIT_DIR points at the (valid) test repo, mimicking git's native-hook export.
    # Classification still resolves the diff under it, but the pytest child must
    # run with GIT_DIR stripped so a git-shelling test can't reach the real repo.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})  # python → testmon leg
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
    # A non-python change that demands tests fails closed with no runner too
    # (issue #213) — it is no different from a python one.
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
    # A mapped non-python change selects its tests; with no runner it fails closed
    # too, proving the block is not specific to python diffs (issue #213).
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




def test_unmapped_shell_change_emits_witness_warning(repo: Path, tmp_path: Path) -> None:
    # #191 witness signal (feeds #187's fail-open audit): a changed *.sh that no
    # test references is a bash blind spot — testmon tracks python imports only, so
    # nothing re-exercises the script by subprocess/source. The gate must emit a
    # distinct, greppable warning that names it.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")  # tests/ exists, refs do.sh only
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/new.sh": "echo new\n"})  # unmapped shell change
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN -n auto -m not serial\n" not in _runlog(runlog)  # never the full suite
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
    # The witness is scoped to shell: an unmapped .yml is not the testmon-blind
    # bash blind spot, so it must not raise the shell signal.
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




def test_selected_failing_suite_blocks_push(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, exit_code=7)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 7  # a red selection aborts the push


# --- the enforcement meta-test rides every push that changes non-doc files (#123) --

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




def test_python_tier_without_meta_file_appends_nothing(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "TestControlPlaneCoverage" not in _runlog(runlog)  # synced repos unaffected


















# --- review-carryover pins from subtask B (verified by probe, now pinned) ---------






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




# ── Part 1 (issue #276): pytest-xdist on the selected leg ────────────────────────
# The SELECTED mapped-files leg is I/O-bound and embarrassingly parallel, so
# test-select.sh threads `-n auto` onto it. The `--testmon` leg stays
# single-process — testmon serializes a single-writer DB and does not compose with
# xdist (`pytest --testmon -n auto` is unsupported). The runlog records each leg's
# argv as `RUN <args>`, so these pin exactly which legs got parallelized.






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




# ── Issue #326: the shell source-dependency graph prunes lib changes ─────────────
# A changed sourced-lib .sh with no test of its own maps — through the graph — to
# the mirror tests of the scripts that source it, so the gate runs SELECTED instead
# of escalating to FULL. An unmapped lib (no test, no tested dependent) still goes
# FULL (safe direction preserved).


def test_lib_only_diff_maps_via_source_graph_to_selected(repo: Path, tmp_path: Path) -> None:
    _write_ref_test(repo, "tests/unit/test_consumer.py", "consumer.sh")
    base = _commit(
        repo,
        {
            "shared/hooks/consumer.sh": '#!/bin/sh\nsource "$D/lib/base.sh"\n',
            "shared/hooks/lib/base.sh": "#!/bin/sh\n",
        },
        "feat: consumer sources base",
    )
    tip = _commit(repo, {"shared/hooks/lib/base.sh": "#!/bin/sh\n# edit\n"})  # lib-only diff
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "RUN -n auto tests/unit/test_consumer.py\n" in log  # SELECTED via the graph
    assert "RUN -n auto\n" not in log  # NOT the full suite




# --- #334: persistent per-project config (durable TEST_SELECT_SKIP/CMD) ----------
# The gate reads a persistent per-project runner/behavior config from git config
# (ai-toolkit.hook.test-select.*) so a host is not limited to the one-shot env
# vars. A LIVE env var still wins; absent config keeps today's tiered behavior.


def test_persistent_skip_config_skips(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})  # would otherwise run
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _git(repo, "config", "ai-toolkit.hook.test-select.skip", "true")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_persistent_command_config_runs(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    custom = tmp_path / "custom.log"
    _git(repo, "config", "ai-toolkit.hook.test-select.command", f"echo ran >> {custom}")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert custom.exists() and "ran" in custom.read_text()  # persistent cmd beats tiered
    assert "RUN" not in _runlog(runlog)


def test_live_env_skip_still_wins(repo: Path, tmp_path: Path) -> None:
    # No persistent config: the one-shot env var must still work (unchanged).
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TEST_SELECT_SKIP": "1"}
    )

    assert proc.returncode == 0
    assert "RUN" not in _runlog(runlog)


def test_hook_disabled_skips_gate(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _git(repo, "config", "ai-toolkit.hook.test-select.enabled", "false")

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN" not in _runlog(runlog)


def test_live_env_cmd_wins_over_persistent(repo: Path, tmp_path: Path) -> None:
    # The contract point: a LIVE TEST_SELECT_CMD wins over a conflicting persistent
    # git-config command — only the env one runs, the persistent one never fires.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    envlog = tmp_path / "env.log"
    persist = tmp_path / "persist.log"
    _git(repo, "config", "ai-toolkit.hook.test-select.command", f"echo persist >> {persist}")

    proc = _run_select(
        repo,
        _stdin(tip, base),
        tmp_path / "bin",
        env_extra={"TEST_SELECT_CMD": f"echo env >> {envlog}"},
    )

    assert proc.returncode == 0, proc.stderr
    assert envlog.exists() and "env" in envlog.read_text()  # live env command ran
    assert not persist.exists()  # persistent config command did NOT run


# --- CI is the gate (#378): the local hook never runs the whole suite ---------------
# Every diff shape that used to escalate to the full suite now runs only what is mapped
# plus the meta-test and says that CI is the full gate. The stub runner records every
# invocation, so "never the whole suite" is asserted on the recorded argv: each RUN
# line must name a mapped file, the meta node, or testmon — never a bare/`-m` run.

_ESCALATING_DIFFS = {
    "unmapped-shell": {"scripts/new.sh": "echo new\n"},
    "unmapped-yaml": {"ci/build.yml": "on: push\n"},
    "unmapped-extension": {"notes.txt": "plain text, not a doc type\n"},
    "python-and-unmapped-shell": {"pkg/mod.py": "x = 1\n", "scripts/new.sh": "echo new\n"},
    "exempt-list-edit": {".test-select-exempt": "notes.txt\n"},
    "lib-without-dependents": {"shared/hooks/lib/orphan.sh": "#!/bin/sh\n"},
}


def _assert_only_fast_tier_ran(log: str) -> None:
    runs = [ln for ln in log.splitlines() if ln.startswith("RUN ")]
    assert runs, "the fast tier should still run the meta-test"
    for line in runs:
        assert "-m not serial" not in line and "-m serial" not in line, line
        assert line.strip() != "RUN" and line != "RUN -n auto", f"bare whole-suite run: {line}"
        assert META_NODE in line or "--testmon" in line or "tests/unit/test_" in line, line


@pytest.mark.parametrize("files", _ESCALATING_DIFFS.values(), ids=_ESCALATING_DIFFS.keys())
def test_escalating_diff_runs_only_the_fast_tier(
    repo: Path, tmp_path: Path, files: dict[str, str]
) -> None:
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, files)
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    _assert_only_fast_tier_ran(_runlog(runlog))


def test_unmapped_change_says_ci_is_the_full_gate(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"scripts/new.sh": "echo new\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "CI is the full gate" in proc.stderr
    assert "scripts/new.sh" in proc.stderr
    assert "RUN" not in _runlog(runlog)  # no mapped tests and no meta file: nothing to run


def test_python_without_testmon_runs_meta_and_says_ci_is_the_full_gate(
    repo: Path, tmp_path: Path
) -> None:
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert f"RUN -n auto {META_NODE}\n" in log
    assert "--testmon" not in log
    assert "CI is the full gate" in proc.stderr


def test_unresolvable_range_runs_the_meta_test_and_says_ci_is_the_full_gate(
    repo: Path, tmp_path: Path
) -> None:
    _write_meta_stub(repo)
    tip = _commit(repo, {}, "test: seed meta test")
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    bogus_remote = "1" * 40  # a remote sha this clone has never seen

    proc = _run_select(repo, _stdin(tip, bogus_remote), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert f"RUN -n auto {META_NODE}\n" in _runlog(runlog)
    assert "range unresolved" in proc.stderr
    assert "CI is the full gate" in proc.stderr


def test_python_change_runs_testmon_and_the_meta_node_once(repo: Path, tmp_path: Path) -> None:
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert f"RUN -n auto {META_NODE}\n" in log
    # testmon never double-runs the meta file: it is --ignore'd, never an explicit node.
    assert "RUN --testmon --ignore=tests/unit/test_test_reverse_index.py\n" in log


def test_mapping_to_a_vanished_test_is_skipped_not_escalated(repo: Path, tmp_path: Path) -> None:
    # A poisoned cache (mapping names a nonexistent test) must not run a phantom file
    # and must not escalate to the full suite either.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)
    _run_select(repo, _stdin(tip, base), tmp_path / "bin")  # builds the cache
    cache = repo / ".git" / ".test-reverse-index" / _reverse_index_key(repo)
    cache.write_text("do.sh\ttests/unit/test_gone.py\n")
    runlog.unlink()

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "mapped test tests/unit/test_gone.py is missing" in proc.stderr
    assert "test_gone.py" not in _runlog(runlog)
    assert "-m not serial" not in _runlog(runlog)


def test_missing_reverse_index_lib_maps_nothing(repo: Path, tmp_path: Path) -> None:
    # An installed hook copy predating lib/test-reverse-index.sh (the #45 stale-hook
    # trap): nothing maps, so no selection runs — and nothing escalates to the suite.
    hookdir = tmp_path / "installed"
    (hookdir / "lib").mkdir(parents=True)
    src = TEST_SELECT.parent
    shutil.copy(TEST_SELECT, hookdir / "test-select.sh")
    for lib in ("utils.sh", "telemetry.sh"):
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
    assert "test_do.py" not in _runlog(runlog)
    assert "-m not serial" not in _runlog(runlog)


def test_selected_leg_degrades_to_single_process_without_xdist(
    repo: Path, tmp_path: Path
) -> None:
    # A runner whose `--help` does not advertise pytest-xdist runs the selection
    # single-process — the gate never blocks a push on `unrecognized -n`.
    _write_ref_test(repo, "tests/unit/test_do.py", "do.sh")
    base = _commit(repo, {}, "test: seed referencing tests")
    tip = _commit(repo, {"scripts/do.sh": "echo hi\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, xdist=False)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN tests/unit/test_do.py\n" in _runlog(runlog)
    assert "-n auto" not in _runlog(runlog)


def test_the_gate_mints_no_green_tree_stamp(repo: Path, tmp_path: Path) -> None:
    # Stamps cached "this tree already ran the full suite locally" — a proof the gate no
    # longer produces. A green run must leave the stamp dir absent.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert not (repo / ".git" / ".gate-stamps").exists()


def test_python_change_without_a_testmon_database_never_seeds_one(
    repo: Path, tmp_path: Path
) -> None:
    # A first `pytest --testmon` in a tree with no database executes the WHOLE suite to
    # build it. The fast tier must never do that: it skips the leg and leaves the full run
    # to CI, whatever else it runs.
    (repo / ".testmondata").unlink()
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" not in _runlog(runlog)
    assert f"RUN -n auto {META_NODE}\n" in _runlog(runlog)  # the cheap selection still ran
    assert "no testmon database" in proc.stderr and "CI is the full gate" in proc.stderr


def test_testmon_database_path_follows_testmon_datafile(repo: Path, tmp_path: Path) -> None:
    (repo / ".testmondata").unlink()
    elsewhere = tmp_path / "baseline.db"
    elsewhere.write_text("db\n")
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TESTMON_DATAFILE": str(elsewhere)}
    )

    assert proc.returncode == 0, proc.stderr
    assert "--testmon" in _runlog(runlog)


# --- the testmon leg is bounded: never a serial run of most of the suite (#378, #375) ---
# A stale / invalidated / unrepresentative database makes `pytest --testmon` select most of
# the suite, and testmon cannot run under xdist: one tests-only push ran ~5800 tests serially
# for 34 minutes. The leg first asks testmon what it WOULD run (collect-only) and skips it —
# deferring to CI — past TEST_SELECT_TESTMON_MAX, or when that cannot be established.


def test_testmon_selecting_most_of_the_suite_is_skipped(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, collect_nodes=5800)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    log = _runlog(runlog)
    assert "--testmon --collect-only" in log  # the impact was probed ...
    assert not [ln for ln in log.splitlines() if "--testmon" in ln and "--collect-only" not in ln]
    assert "testmon would select 5800 tests" in proc.stderr  # ... and the skip is loud
    assert "CI is the full gate" in proc.stderr


def test_testmon_selecting_a_small_impact_set_runs(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, collect_nodes=12)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN --testmon\n" in _runlog(runlog)
    assert "12 impacted test(s)" in proc.stderr


def test_testmon_cap_is_tunable(repo: Path, tmp_path: Path) -> None:
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, collect_nodes=12)

    proc = _run_select(
        repo, _stdin(tip, base), tmp_path / "bin", env_extra={"TEST_SELECT_TESTMON_MAX": "5"}
    )

    assert proc.returncode == 0, proc.stderr
    assert "RUN --testmon\n" not in _runlog(runlog)


def test_testmon_leg_skipped_when_its_impact_cannot_be_established(
    repo: Path, tmp_path: Path
) -> None:
    # An unreadable impact set is no basis for an unbounded serial run.
    base = _rev(repo)
    tip = _commit(repo, {"pkg/mod.py": "x = 1\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, collect_exit=2)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    assert "RUN --testmon\n" not in _runlog(runlog)
    assert "could not be established" in proc.stderr


def test_a_tests_only_python_push_never_runs_an_unbounded_testmon_leg(
    repo: Path, tmp_path: Path
) -> None:
    # The #375 shape: a tests-only diff in a worktree whose database selects everything.
    _write_meta_stub(repo)
    base = _commit(repo, {}, "test: seed meta test")
    tip = _commit(repo, {"tests/unit/test_new.py": "def test_a():\n    pass\n"})
    runlog = tmp_path / "run.log"
    _make_pytest_stub(tmp_path / "bin", runlog, testmon=True, collect_nodes=5800)

    proc = _run_select(repo, _stdin(tip, base), tmp_path / "bin")

    assert proc.returncode == 0, proc.stderr
    runs = [ln for ln in _runlog(runlog).splitlines() if ln.startswith("RUN ")]
    # only the meta node (parallel) and the testmon impact probe — never a serial testmon run
    assert f"RUN -n auto {META_NODE}" in runs
    assert all("--collect-only" in r or META_NODE in r for r in runs), runs
