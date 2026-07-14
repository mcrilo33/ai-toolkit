"""A persistent-bash coprocess harness for the control-plane bash suites (issue #276).

``test_gate_broker.py`` and ``test_hub_afk.py`` used to run
``bash -c 'source "<lib>"; <fn>'`` once per test, re-parsing a multi-thousand-line
control-plane lib on every one of hundreds of tests (~0.7s/test). ``BashSession``
sources the lib ONCE into a long-lived bash, then runs each call in a fresh
*subshell* so the parent shell's environment stays pristine between tests.

Per-test env is applied as an ``export`` / ``unset`` DELTA vs. the coprocess's
start-env, so the autouse per-test ``AFK_STATE_DIR`` / ``AFK_HEARTBEAT`` isolation
(set via ``monkeypatch`` in the Python process AFTER the coprocess started) is
faithfully re-injected on every call. Per-call stdin, and captured
stdout/stderr/returncode, match ``subprocess.run(..., capture_output=True)`` so the
call sites' assertions are unchanged.

Both libs use ``set -uo pipefail`` (NOT ``set -e``), so a call that returns
non-zero never aborts the read-loop, and each subshell inherits ``-uo pipefail`` —
exactly the options the old per-call ``bash -c`` shell ran under.

Calls whose per-call env touches a SOURCE-TIME resolution key (``SCRIPT_DIR`` and
the lib-override vars, which the libs read while being sourced to locate their
co-located helpers) cannot reuse the already-sourced coprocess. Those route to a
fresh ``bash -c 'source; fn'`` subprocess via :func:`fresh_call`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from shlex import quote as shlex_quote

# The persistent coprocess program. $1 = lib path, $2 = source-count witness.
# It sources the lib ONCE (recording one byte in the witness), signals READY, then
# for each tab-separated control line runs the fragment in a subshell with the
# call's stdin/stdout/stderr redirected to the named files and reports the exit
# code. `set -uo pipefail` is inherited by every subshell from the sourced lib, so
# calls run under the same options the old `bash -c 'source; fn'` used.
_COPROCESS_PROGRAM = r"""
LIB="$1"; WITNESS="$2"
printf 'x' >> "$WITNESS"
source "$LIB"
printf 'READY\n'
while IFS=$'\t' read -r frag out err stin; do
  ( source "$frag" ) >"$out" 2>"$err" <"$stin"
  printf 'RC=%d\n' "$?"
done
"""


class BashSession:
    """A long-lived bash that sources ``lib`` once and runs calls in subshells."""

    def __init__(self, lib: Path | str, *, base_env: dict[str, str] | None = None) -> None:
        """Start the coprocess and source ``lib``.

        Args:
            lib: Absolute path to the bash lib to source once.
            base_env: Env applied to every call on top of ``os.environ`` (e.g. a
                suite-wide ``TZ`` or a default toggle). ``TZ=UTC`` is always set.
        """
        self._lib = str(lib)
        self._base_env = {"TZ": "UTC", **(base_env or {})}
        # The env the coprocess was launched with — every per-call delta is computed
        # against this snapshot, so a monkeypatched-in AFK_STATE_DIR is re-exported.
        self._start_env: dict[str, str] = {**os.environ, **self._base_env}
        self._tmp = Path(tempfile.mkdtemp(prefix="bash-session-"))
        self._witness = self._tmp / "source-count"
        self._witness.write_bytes(b"")
        self._frag = self._tmp / "frag.sh"
        self._out = self._tmp / "out"
        self._err = self._tmp / "err"
        self._stin = self._tmp / "stin"
        self._proc = subprocess.Popen(
            ["bash", "-c", _COPROCESS_PROGRAM, "bash", self._lib, str(self._witness)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**self._start_env},
            text=True,
            bufsize=1,
        )
        self._await("READY")

    @property
    def source_count(self) -> int:
        """How many times the coprocess has sourced the lib (one byte per source)."""
        return len(self._witness.read_bytes())

    def call(
        self, fn_call: str, *, env: dict[str, str] | None = None, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run ``fn_call`` in a subshell of the sourced coprocess.

        Returns a CompletedProcess with the call's captured stdout, stderr, and
        returncode — shaped exactly like ``subprocess.run(capture_output=True)``.
        """
        desired = {**os.environ, **self._base_env}
        if env:
            desired.update(env)
        exports = "".join(
            f"export {k}={shlex_quote(v)}\n"
            for k, v in desired.items()
            if self._start_env.get(k) != v
        )
        unsets = "".join(f"unset {k}\n" for k in self._start_env if k not in desired)
        self._frag.write_text(exports + unsets + fn_call + "\n")
        self._stin.write_text(stdin or "")
        self._out.write_bytes(b"")
        self._err.write_bytes(b"")
        assert self._proc.stdin is not None
        self._proc.stdin.write(f"{self._frag}\t{self._out}\t{self._err}\t{self._stin}\n")
        self._proc.stdin.flush()
        rc = self._read_rc()
        return subprocess.CompletedProcess(
            args=fn_call,
            returncode=rc,
            stdout=self._out.read_text(),
            stderr=self._err.read_text(),
        )

    def close(self) -> None:
        """Close the coprocess (idempotent)."""
        if self._proc.poll() is None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                self._proc.kill()

    def _await(self, token: str) -> None:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                raise RuntimeError(f"bash coprocess died before {token}")
            if line.strip() == token:
                return

    def _read_rc(self) -> int:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if line == "":
                raise RuntimeError("bash coprocess died mid-call")
            stripped = line.strip()
            if stripped.startswith("RC="):
                return int(stripped[3:])


def fresh_call(
    lib: Path | str,
    fn_call: str,
    *,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    base_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source ``lib`` in a FRESH bash and run ``fn_call`` — the pre-#276 harness.

    Used for calls whose per-call env changes SOURCE-TIME resolution (SCRIPT_DIR,
    lib-override vars), which the already-sourced :class:`BashSession` cannot honor.
    """
    full_env = {**os.environ, "TZ": "UTC", **(base_env or {})}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{lib}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
        input=stdin,
    )
