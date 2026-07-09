"""Unit tests for block-no-verify.sh — the sole --no-verify defense (issue #211).

``block-no-verify.sh`` is the only gate that catches ``git commit|push
--no-verify`` — the one flag that disarms BOTH native cages (commit-msg and
pre-push). It cannot have a native backstop (``--no-verify`` skips the very hooks
it would live in), so it must FAIL CLOSED: on Claude Code an exit != 2 is a
non-blocking error and the tool PROCEEDS, so a crash or a malformed payload that
yields no command would otherwise let ``--no-verify`` through crash-open.

These tests pin both halves of the fix:

* **Fail-closed** — a malformed/empty-command payload and a lib-source crash
  DENY (exit 2) instead of silently passing (the confirmed fail-open, whose
  repro is ``printf '{"tool_name":"Bash"}' | bash block-no-verify.sh`` -> 0).
* **Boundary forms** — every chained/prefixed/quoted spelling of ``--no-verify``
  is caught by the hook's position-independent scan once it runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "shared" / "hooks"
BLOCK_NO_VERIFY = HOOKS_DIR / "block-no-verify.sh"

BLOCK = 2
ALLOW = 0

# The literal flag, split so this test file never carries a string a naive
# scanner would flag as a bypass attempt in its own diff.
NO_VERIFY = "--no-" + "verify"


def _run(script: Path, payload: str, *, cwd: Path | None = None) -> int:
    """Run the hook with a raw stdin payload; return its exit code."""
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    ).returncode


def _claude(command: str) -> str:
    """Claude/Copilot generic shape: command under tool_input."""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


# ── Fail-closed: malformed / crash payloads DENY, never silently pass ──


def test_no_command_payload_fails_closed() -> None:
    # The confirmed fail-open repro: no command extracted must now DENY, not
    # exit 0. This is the sole --no-verify defense with no native backstop.
    assert _run(BLOCK_NO_VERIFY, '{"tool_name":"Bash"}') == BLOCK


def test_empty_command_fails_closed() -> None:
    assert _run(BLOCK_NO_VERIFY, _claude("")) == BLOCK


def test_garbage_stdin_fails_closed() -> None:
    # Non-JSON stdin yields no command; a hook that cannot read the command it
    # is meant to check must fail closed.
    assert _run(BLOCK_NO_VERIFY, "not json at all") == BLOCK


def test_missing_lib_fails_closed(tmp_path: Path) -> None:
    # Copy the hook WITHOUT its lib/, so the readable guard trips before the
    # source line: an absent utils.sh must DENY, not crash-open at exit 1.
    orphan = tmp_path / "block-no-verify.sh"
    shutil.copy(BLOCK_NO_VERIFY, orphan)
    assert _run(orphan, _claude(f"git commit {NO_VERIFY} -m x")) == BLOCK


def test_missing_transitive_lib_fails_closed(tmp_path: Path) -> None:
    # utils.sh present but telemetry.sh (which utils.sh sources UNCONDITIONALLY)
    # absent. `source utils.sh` would then exit the shell as a special builtin
    # with code 1 — bypassing the ERR trap — so the hook must guard the transitive
    # lib by hand and DENY. Without that guard this returns 1 (crash-open).
    orphan = tmp_path / "block-no-verify.sh"
    shutil.copy(BLOCK_NO_VERIFY, orphan)
    lib = tmp_path / "lib"
    shutil.copytree(HOOKS_DIR / "lib", lib)
    (lib / "telemetry.sh").unlink()
    assert _run(orphan, _claude(f"git commit {NO_VERIFY} -m x")) == BLOCK


# ── --no-verify is caught in every boundary form once the hook runs ────


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"git commit {NO_VERIFY} -m x", id="plain-commit"),
        pytest.param(f"git push {NO_VERIFY}", id="plain-push"),
        pytest.param(f"cd sub && git commit {NO_VERIFY} -m x", id="chained"),
        pytest.param(f"VAR=1 git commit {NO_VERIFY} -m x", id="env-prefixed"),
        pytest.param(f"sh -c 'git commit {NO_VERIFY} -m x'", id="sh-c-quoted"),
        pytest.param(f"git -C path commit {NO_VERIFY} -m x", id="dash-C"),
    ],
)
def test_no_verify_blocks(command: str) -> None:
    assert _run(BLOCK_NO_VERIFY, _claude(command)) == BLOCK


# ── Clean commands still pass; force-push guard unchanged ──────────────


@pytest.mark.parametrize(
    "command,expected",
    [
        pytest.param('git commit -m "feat: x"', ALLOW, id="clean-commit"),
        pytest.param("git push origin feature/1-x", ALLOW, id="clean-push"),
        pytest.param("ls -la", ALLOW, id="non-git"),
        pytest.param("git push --force", BLOCK, id="force-no-lease"),
        pytest.param("git push --force-with-lease", ALLOW, id="force-with-lease"),
    ],
)
def test_clean_and_force_push(command: str, expected: int) -> None:
    assert _run(BLOCK_NO_VERIFY, _claude(command)) == expected


# ── Fail-closed must not override the #154 global off switch ───────────


def test_disabled_toolkit_passes_through(tmp_path: Path) -> None:
    # When the toolkit is disabled (the <git-common-dir>/ai-toolkit-off marker),
    # utils.sh exits 0 during sourcing — BEFORE the telemetry span arms. A naive
    # fail-closed EXIT trap would flip that passthrough into a deny and lock out
    # every command; the readable-guard approach must leave the off switch intact.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    gcd = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / gcd / "ai-toolkit-off").touch()
    assert _run(BLOCK_NO_VERIFY, _claude(f"git commit {NO_VERIFY} -m x"), cwd=tmp_path) == ALLOW
