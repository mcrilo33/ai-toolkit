"""Shared writer for executable test stubs (#376).

A freshly written script's FIRST exec can take seconds under xdist on macOS (the OS vets each
new executable) while a re-exec takes ~10ms (#374). A stub raced against a bounded wait must
therefore be exec'd once up front. `write_stub` writes the script, marks it executable and runs
it once with `WARM_ENV` set; the guard it injects after the shebang makes that run a no-op.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

WARM_ENV = "AI_TOOLKIT_STUB_WARM"
_WARM_GUARD = f'[ -z "${{{WARM_ENV}:-}}" ] || exit 0\n'


def write_stub(path: Path, script: str, *, warm: bool = True) -> None:
    """Write `script` (shebang included) to `path`, mark it executable and warm it.

    Args:
        path: Where the stub lands.
        script: Full script text; must start with a `#!` line, after which the warm guard
            is injected.
        warm: Exec the stub once now. Pass False to write a batch and call `warm_stubs`.

    Raises:
        ValueError: `script` has no shebang line.
        subprocess.CalledProcessError: the warm-up exec failed.
    """
    shebang, sep, rest = script.partition("\n")
    if not shebang.startswith("#!"):
        raise ValueError(f"stub {path.name} must start with a shebang, got {shebang[:40]!r}")
    path.write_text(f"{shebang}\n{_WARM_GUARD}{rest if sep else ''}")
    path.chmod(0o755)
    if warm:
        warm_stubs([path])


def warm_stubs(paths: Iterable[Path]) -> None:
    """Exec every stub once, concurrently, with the warm guard armed (a no-op run)."""
    env = {**os.environ, WARM_ENV: "1"}
    procs = [
        subprocess.Popen([str(p)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for p in paths
    ]
    for proc in procs:
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, proc.args)
