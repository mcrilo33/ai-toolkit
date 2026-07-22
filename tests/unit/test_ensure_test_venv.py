"""Unit tests for scripts/ensure-test-venv.sh (issue #342).

The pre-push test gate silently degrades to the FULL, single-process suite on any host
whose resolved `pytest` lacks pytest-testmon / pytest-xdist: `detect_pytest`
(shared/hooks/lib/utils.sh) keys on `.venv/bin/pytest`, so with no provisioned `.venv` the
bare interpreter's missing plugins force `test-select.sh` down its worst-case path. This
best-effort helper provisions a project `.venv` carrying those plugins so the gate
self-heals. It must:

- skip cleanly when the project has no requirements-dev.txt (exit 0, no `.venv`, no loud line);
- create + install when `.venv` is absent (loud line, `pip install -r requirements-dev.txt`);
- repair a `.venv` that exists but whose plugins do not import (install ONLY testmon/xdist);
- be a clean idempotent no-op once provisioned (exit 0, no pip call, no duplicate exclude);
- exclude `.venv/` from the TARGET repo's .git/info/exclude — even when invoked from a cwd
  OUTSIDE the target (worktree-new.sh runs from REPO_ROOT and passes $WT_DIR);
- NEVER fail its caller: exit 0 even when `python3 -m venv` fails (AFK Design Principle #6).

Hermetic: a temp git repo plus a PATH shim whose fake `python3 -m venv <dir>` installs stub
`pip`/`python` into the venv. The venv `pip` stub records its argv and creates `bin/pytest`
plus a `.plugins_ok` marker; the venv `python` stub's `import testmon, xdist` check succeeds
iff that marker exists — so each branch is driven deterministically with no real interpreter
or network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ENSURE = Path(__file__).resolve().parents[2] / "scripts" / "ensure-test-venv.sh"

# Pin git config to nothing so a host's global/system config cannot reach the throwaway repos.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


def _write_shims(shim_dir: Path, log: Path, *, venv_fails: bool = False) -> None:
    """A PATH shim providing a fake `python3` whose `-m venv` installs venv stubs."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    # venv python: `import testmon, xdist` (any `-c`) passes iff the .plugins_ok marker is present.
    (shim_dir / "venv_python").write_text(
        "#!/usr/bin/env bash\n"
        'd="$(cd "$(dirname "$0")/.." && pwd)"\n'
        '[ -f "$d/.plugins_ok" ] && exit 0 || exit 1\n'
    )
    # venv pip: record argv, then simulate a successful install (bin/pytest + the marker).
    (shim_dir / "venv_pip").write_text(
        "#!/usr/bin/env bash\n"
        'd="$(cd "$(dirname "$0")/.." && pwd)"\n'
        f'printf "pip %s\\n" "$*" >> "{log}"\n'
        'touch "$d/bin/pytest" "$d/.plugins_ok"\n'
        'chmod +x "$d/bin/pytest"\n'
    )
    if venv_fails:
        py_body = "#!/usr/bin/env bash\nexit 1\n"
    else:
        py_body = (
            "#!/usr/bin/env bash\n"
            f'S="{shim_dir}"\n'
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            '  for a in "$@"; do v="$a"; done\n'  # last arg is the venv dir
            '  mkdir -p "$v/bin"\n'
            '  cp "$S/venv_python" "$v/bin/python"\n'
            '  cp "$S/venv_pip" "$v/bin/pip"\n'
            '  chmod +x "$v/bin/python" "$v/bin/pip"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
    (shim_dir / "python3").write_text(py_body)
    for f in ("python3", "venv_python", "venv_pip"):
        (shim_dir / f).chmod(0o755)


def _seed_venv(root: Path, shim_dir: Path, *, plugins_ok: bool) -> None:
    """Pre-create a `.venv` with the stub pip/python/pytest, optionally already provisioned."""
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True)
    for name, stub in (("python", "venv_python"), ("pip", "venv_pip")):
        dst = bindir / name
        dst.write_text((shim_dir / stub).read_text())
        dst.chmod(0o755)
    (bindir / "pytest").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "pytest").chmod(0o755)
    if plugins_ok:
        (root / ".venv" / ".plugins_ok").touch()


def _run(target: Path, *, cwd: Path, shim_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "PATH": f"{shim_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(ENSURE), str(target)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _init_repo(root)
    (root / "requirements-dev.txt").write_text("pytest\npytest-testmon\npytest-xdist\n")
    return root


def _exclude_lines(root: Path) -> list[str]:
    exclude = root / ".git" / "info" / "exclude"
    return exclude.read_text().splitlines() if exclude.is_file() else []


def test_skips_cleanly_without_requirements_dev(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _init_repo(root)  # no requirements-dev.txt
    shim = tmp_path / "bin"
    _write_shims(shim, tmp_path / "pip.log")

    result = _run(root, cwd=root, shim_dir=shim)

    assert result.returncode == 0, result.stderr
    assert not (root / ".venv").exists(), "no requirements-dev.txt must provision nothing"
    combined = result.stdout + result.stderr
    assert "provisioning" not in combined and "installing" not in combined


def test_creates_venv_and_installs_when_absent(project: Path, tmp_path: Path) -> None:
    log = tmp_path / "pip.log"
    shim = tmp_path / "bin"
    _write_shims(shim, log)

    result = _run(project, cwd=project, shim_dir=shim)

    assert result.returncode == 0, result.stderr
    assert (project / ".venv" / "bin" / "pytest").is_file(), "the gate keys on .venv/bin/pytest"
    assert "provisioning" in (result.stdout + result.stderr), "a create must emit a loud line"
    assert "-r" in log.read_text(), "the create branch installs the full requirements-dev.txt"


def test_repairs_venv_missing_plugins_installs_only_those(project: Path, tmp_path: Path) -> None:
    log = tmp_path / "pip.log"
    shim = tmp_path / "bin"
    _write_shims(shim, log)
    _seed_venv(project, shim, plugins_ok=False)  # pytest present, plugins do not import

    result = _run(project, cwd=project, shim_dir=shim)

    assert result.returncode == 0, result.stderr
    pip_calls = log.read_text()
    assert "testmon" in pip_calls and "xdist" in pip_calls, "must install the missing plugins"
    assert "-r" not in pip_calls, (
        "a repair installs only testmon/xdist, not all of requirements-dev"
    )


def test_idempotent_noop_when_already_provisioned(project: Path, tmp_path: Path) -> None:
    log = tmp_path / "pip.log"
    shim = tmp_path / "bin"
    _write_shims(shim, log)
    _seed_venv(project, shim, plugins_ok=True)

    result = _run(project, cwd=project, shim_dir=shim)

    assert result.returncode == 0, result.stderr
    assert not log.exists() or log.read_text() == "", "a provisioned .venv must trigger no pip call"
    combined = result.stdout + result.stderr
    assert "provisioning" not in combined and "installing" not in combined


def test_exit_zero_when_venv_creation_fails(project: Path, tmp_path: Path) -> None:
    shim = tmp_path / "bin"
    _write_shims(shim, tmp_path / "pip.log", venv_fails=True)

    result = _run(project, cwd=project, shim_dir=shim)

    assert result.returncode == 0, "a venv-create failure must never fail the caller"
    assert "failed" in (result.stdout + result.stderr), "a failed provisioning must be loud"
    assert not (project / ".venv" / "bin" / "pytest").is_file()


def test_excludes_venv_from_target_info_exclude(project: Path, tmp_path: Path) -> None:
    shim = tmp_path / "bin"
    _write_shims(shim, tmp_path / "pip.log")

    _run(project, cwd=project, shim_dir=shim)

    assert ".venv/" in _exclude_lines(project), ".venv must be excluded from git status"


def test_exclude_targets_the_project_when_invoked_from_outside_cwd(
    project: Path, tmp_path: Path
) -> None:
    # worktree-new.sh runs from REPO_ROOT and passes $WT_DIR: the exclude path resolved from
    # `git -C "$DIR" rev-parse --git-path info/exclude` is RELATIVE, so without absolutizing it
    # against $DIR the append lands in the CALLER's cwd — the wrong repo — and `.venv` stays
    # visible in the target's `git status`. Invoke from an outside cwd and pin the entry to the
    # target (amendment to the #342 plan).
    shim = tmp_path / "bin"
    _write_shims(shim, tmp_path / "pip.log")
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    result = _run(project, cwd=outside, shim_dir=shim)

    assert result.returncode == 0, result.stderr
    assert ".venv/" in _exclude_lines(project), (
        "the exclude entry must land in the TARGET repo, not the caller's cwd"
    )


def test_exclude_entry_not_duplicated_on_second_run(project: Path, tmp_path: Path) -> None:
    shim = tmp_path / "bin"
    _write_shims(shim, tmp_path / "pip.log")

    _run(project, cwd=project, shim_dir=shim)
    _run(project, cwd=project, shim_dir=shim)

    assert _exclude_lines(project).count(".venv/") == 1, "the exclude append must be idempotent"
