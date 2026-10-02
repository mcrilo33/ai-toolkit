"""Tests for the shared executable-stub writer `tests/_stubs.py` (#376)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _stubs import WARM_ENV, warm_stubs, write_stub


def test_write_stub_makes_an_executable_that_runs_its_body(tmp_path: Path) -> None:
    stub = tmp_path / "tool"

    write_stub(stub, '#!/usr/bin/env bash\nprintf "ran"\n')

    assert subprocess.run([str(stub)], capture_output=True, text=True).stdout == "ran"


def test_write_stub_warm_up_exec_runs_none_of_the_body(tmp_path: Path) -> None:
    log = tmp_path / "ran.log"

    write_stub(tmp_path / "tool", f'#!/usr/bin/env bash\necho hit >> "{log}"\n')

    assert not log.exists()


def test_write_stub_injects_the_guard_right_after_the_shebang(tmp_path: Path) -> None:
    stub = tmp_path / "tool"

    write_stub(stub, "#!/bin/sh\nexit 3\n", warm=False)

    lines = stub.read_text().splitlines()
    assert lines[0] == "#!/bin/sh" and WARM_ENV in lines[1] and lines[2] == "exit 3"


def test_write_stub_without_warm_skips_the_exec(tmp_path: Path) -> None:
    stub = tmp_path / "tool"

    write_stub(stub, "#!/bin/sh\nexit 3\n", warm=False)

    assert stub.stat().st_mode & 0o111
    assert subprocess.run([str(stub)]).returncode == 3


def test_write_stub_rejects_a_script_without_a_shebang(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="shebang"):
        write_stub(tmp_path / "tool", "exit 0\n")


def test_warm_stubs_raises_when_a_stub_cannot_run(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/no/such/interpreter\n")
    broken.chmod(0o755)

    with pytest.raises((OSError, subprocess.CalledProcessError)):
        warm_stubs([broken])


def test_write_stub_rejects_a_non_shell_shebang(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sh/bash"):
        write_stub(tmp_path / "tool", "#!/usr/bin/env python3\nprint(1)\n")
