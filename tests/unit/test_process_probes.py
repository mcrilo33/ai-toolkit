"""Behaviour tests for the locale-hardened process probes in worktree-lib.sh.

``wt_pgrep`` and ``wt_ps_start_epoch`` are the shared helpers issue #189 adds so
every control-plane process probe (a) forces ``LC_ALL=C`` — a non-C host locale
makes ``pgrep -f`` die "illegal byte sequence" on non-ASCII argv (read by callers
as a false "not running") and makes ``ps -o lstart=`` emit a locale-formatted date
the parser cannot read (stranding staleness detection) — (b) excludes the caller's
own process so a monitor loop grepping its own keyword never self-matches, and (c)
return an exit code that distinguishes "not running" from "the probe itself failed".

These source the real lib and drive the helpers against real (and stubbed) processes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

WT_LIB = Path(__file__).resolve().parents[2] / "scripts" / "worktree-lib.sh"


def _run(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh and run a bash script fragment against it."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


# --- wt_pgrep -----------------------------------------------------------------


def test_wt_pgrep_finds_a_live_process_by_argv() -> None:
    # A backgrounded process with a distinctive argv is matched by `-f`, its pid
    # printed on stdout, exit 0.
    result = _run(
        r"""
        ( exec -a "wtpgrep-alive-$$" sleep 30 ) &
        bg=$!
        sleep 0.3
        pids="$(wt_pgrep -f "wtpgrep-alive-$$")"; rc=$?
        kill "$bg" 2>/dev/null
        echo "RC=$rc"
        printf '%s\n' "$pids" | grep -qx "$bg" && echo "FOUND"
        """
    )

    assert result.returncode == 0, result.stderr
    assert "RC=0" in result.stdout.splitlines(), result.stdout
    assert "FOUND" in result.stdout, result.stdout


def test_wt_pgrep_returns_1_when_nothing_matches() -> None:
    # No process matches -> exit 1 ("not running"), nothing on stdout.
    result = _run(
        r"""
        pids="$(wt_pgrep -f "no-such-proc-marker-xyzzy-$$")"; rc=$?
        echo "RC=$rc"
        echo "PIDS=[$pids]"
        """
    )

    assert "RC=1" in result.stdout.splitlines(), result.stdout
    assert "PIDS=[]" in result.stdout, result.stdout


def test_wt_pgrep_excludes_the_caller_so_it_never_self_matches() -> None:
    # The only process whose argv holds SELF_ONLY_TOKEN_189 is this very shell
    # (the pattern literal is in its argv). Without self-exclusion wt_pgrep would
    # report the caller; with it, the match set is empty -> exit 1.
    result = _run(
        r"""
        pids="$(wt_pgrep -f "SELF_ONLY_TOKEN_189")"; rc=$?
        echo "RC=$rc"
        echo "PIDS=[$pids]"
        """
    )

    assert "RC=1" in result.stdout.splitlines(), result.stdout
    assert str(os.getpid()) not in result.stdout  # never leaks any pid here
    assert "PIDS=[]" in result.stdout, result.stdout


def test_wt_pgrep_distinguishes_probe_failure_from_not_running() -> None:
    # An unusable pgrep invocation (bad flag -> pgrep exit >=2) must surface as
    # exit 2 ("probe failed"), NOT exit 1 ("not running"). Conflating the two is
    # exactly the false-negative the issue is about.
    result = _run(
        r"""
        wt_pgrep --no-such-flag-zzz foo >/dev/null 2>&1; echo "RC=$?"
        """
    )

    assert "RC=2" in result.stdout.splitlines(), result.stdout


def test_wt_pgrep_is_locale_independent() -> None:
    # A process with non-ASCII argv must be found even when the caller inherits a
    # non-C locale: the helper forces LC_ALL=C internally, so `pgrep -f` neither
    # dies nor false-negatives. Mirrors the ps-lstart locale test's approach.
    env = {"LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8"}
    result = _run(
        r"""
        ( exec -a "café-marker-$$" sleep 30 ) &
        bg=$!
        sleep 0.3
        pids="$(wt_pgrep -f "café-marker-$$")"; rc=$?
        kill "$bg" 2>/dev/null
        echo "RC=$rc"
        printf '%s\n' "$pids" | grep -qx "$bg" && echo "FOUND"
        """,
        env=env,
    )

    assert "RC=0" in result.stdout.splitlines(), result.stderr
    assert "FOUND" in result.stdout, result.stdout


# --- wt_ps_start_epoch --------------------------------------------------------


def test_wt_ps_start_epoch_returns_epoch_for_a_live_pid() -> None:
    # The live shell's own pid resolves to a positive epoch, exit 0.
    result = _run('out="$(wt_ps_start_epoch $$)"; rc=$?; echo "$out"; echo "RC=$rc"')

    lines = result.stdout.splitlines()
    assert "RC=0" in lines, result.stderr
    assert lines[0].isdigit() and int(lines[0]) > 0, result.stdout


def test_wt_ps_start_epoch_returns_1_for_a_dead_pid() -> None:
    # A reaped pid is "not running" -> exit 1, empty stdout (never confused with a
    # probe failure).
    result = _run(
        r"""
        sleep 30 & p=$!
        kill "$p" 2>/dev/null; wait "$p" 2>/dev/null
        out="$(wt_ps_start_epoch "$p")"; rc=$?
        echo "RC=$rc"
        echo "OUT=[$out]"
        """
    )

    assert "RC=1" in result.stdout.splitlines(), result.stdout
    assert "OUT=[]" in result.stdout, result.stdout


def test_wt_ps_start_epoch_returns_2_when_the_start_time_is_unparseable(
    tmp_path: Path,
) -> None:
    # ps yields a value but it is not a date the converter can read -> exit 2
    # ("probe failed"), distinct from the dead-pid exit 1. A stub `ps` on PATH
    # forces the unparseable-output branch deterministically.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ps_stub = bindir / "ps"
    ps_stub.write_text("#!/bin/sh\necho 'not-a-real-date'\n")
    ps_stub.chmod(0o755)

    result = _run(
        r"""
        out="$(wt_ps_start_epoch 1)"; rc=$?
        echo "RC=$rc"
        echo "OUT=[$out]"
        """,
        env={"PATH": f"{bindir}:{os.environ['PATH']}"},
    )

    assert "RC=2" in result.stdout.splitlines(), result.stdout
    assert "OUT=[]" in result.stdout, result.stdout
