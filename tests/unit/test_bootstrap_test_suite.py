"""Acceptance + mirror test for the ``bootstrap-test-suite`` skill (issue #335).

The skill ships ``bootstrap-test-suite.sh``, a generator that stands up an
ai-toolkit-conformant test scaffold in a host project so the pre-push gate
(``shared/hooks/test-select.sh``) stops being pure friction on a fresh host.

This file doubles as the generator's **mirror test**: naming
``bootstrap-test-suite.sh`` as an exact token satisfies the shell-mirror
governance guard (``test_shell_mirror_coverage.py``), so the shipped ``.sh``
reaches a collectible test and the SELECTED tier can run it.

Each case runs the real generator into a throwaway git repo (``tmp_path``) and
pins one acceptance property from the issue:

* the scaffold files are created;
* ``pyproject.toml`` registers the ``serial`` marker but keeps ``-n auto`` out of
  ``addopts`` (baking it in would break the gate's ``pytest --testmon`` leg,
  which must never run under xdist — ``test-select.sh:346-362``);
* the reverse index maps the starter mirror test to its non-python target;
* the ``serial`` leg collects nothing on a suite with none (exit 5 → green);
* a pytest runner resolves (not the fail-closed ``exit 1``);
* re-running is non-destructive (guard-on-absence writes).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT / "shared" / "skills" / "bootstrap-test-suite" / "scripts" / "bootstrap-test-suite.sh"
)
LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "test-reverse-index.sh"
UTILS = REPO_ROOT / "shared" / "hooks" / "lib" / "utils.sh"

# Pin git config to nothing so a host's global config can't reach the throwaway repo.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _init_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(
            ["git", "config", k, v], cwd=str(root), check=True, capture_output=True, env=_GIT_ENV
        )


def _run_generator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _generate(tmp_path: Path) -> Path:
    """Init a bare repo and run the generator into it; return the repo root."""
    root = tmp_path / "host"
    root.mkdir()
    _init_repo(root)
    proc = _run_generator(root)
    assert proc.returncode == 0, f"generator failed: {proc.stderr}"
    return root


def test_generator_creates_scaffold_files(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    expected = [
        "pyproject.toml",
        "tests/conftest.py",
        "tests/unit/test_example.py",
        "scripts/example.sh",
        "requirements-dev.txt",
        ".test-select-exempt",
    ]
    missing = [rel for rel in expected if not (root / rel).is_file()]
    assert missing == [], f"generator did not create: {missing}"


def test_pyproject_registers_serial_without_xdist_addopts(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    text = (root / "pyproject.toml").read_text()
    assert "serial" in text, "pyproject.toml must register the `serial` marker"
    # Decision 1 (issue #335): the gate supplies `-n auto` itself on the full/selected
    # legs and NEVER on the `--testmon` leg. Baking it into addopts would make the
    # PYTHON/testmon tier error instead of run — the regression this pins.
    assert "-n auto" not in text, "addopts must not carry `-n auto` (poisons the testmon leg)"
    assert "numprocesses" not in text, "addopts must not carry `--numprocesses`"


def test_reverse_index_maps_starter_mirror_test(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    # The starter target is a NON-python .sh on purpose: python targets route to the
    # testmon tier, so only a non-python target exercises the reverse-index SELECTED
    # tier the acceptance criterion is about.
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{LIB}" && reverse_index_tests_for "$1"',
            "_",
            "scripts/example.sh",
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    mapped = [line for line in proc.stdout.splitlines() if line]
    assert "tests/unit/test_example.py" in mapped, (
        f"starter mirror test not mapped for scripts/example.sh; got {mapped}"
    )


def test_serial_leg_collects_nothing_on_a_suite_with_none(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    # run_full_two_phase's serial tail runs `-m serial`; on a fresh scaffold nothing is
    # marked serial, so pytest collects nothing and exits 5 — normalized to green.
    proc = subprocess.run(
        ["pytest", "-m", "serial", "--collect-only", "-q"],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    assert proc.returncode == 5, (
        f"expected exit 5 (no tests collected) from the serial leg; got {proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


def test_detect_pytest_resolves_a_runner(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    proc = subprocess.run(
        ["bash", "-c", f'source "{UTILS}" && detect_pytest "$1"', "_", str(root)],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    assert proc.stdout.strip(), "detect_pytest resolved no runner on the generated tree"


def test_starter_mirror_test_passes(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    proc = subprocess.run(
        ["pytest", "tests/unit/test_example.py", "-q"],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    assert proc.returncode == 0, f"starter mirror test did not pass:\n{proc.stdout}\n{proc.stderr}"


def test_generator_is_non_destructive(tmp_path: Path) -> None:
    assert SCRIPT.exists(), f"generator script missing: {SCRIPT}"
    root = _generate(tmp_path)

    conftest = root / "tests" / "conftest.py"
    conftest.write_text("# host-owned sentinel — must survive a re-run\n")
    proc = _run_generator(root)
    assert proc.returncode == 0, f"second run failed: {proc.stderr}"
    assert conftest.read_text() == "# host-owned sentinel — must survive a re-run\n", (
        "generator overwrote an existing file (writes must guard on absence)"
    )
