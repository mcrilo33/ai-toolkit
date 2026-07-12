"""Unit tests for shared/skills/hub/scripts/hub-inject.sh.

hub-inject.sh is the ONE hardened tmux-inject + delivery-proof primitive (issue #251),
factored out of gate-broker.sh so the /afk answerer and the tier-2 hub-watchdog share a
single tested helper. These tests source it STANDALONE (not via gate-broker) to prove the
factoring is self-contained, and pin the mechanical keystroke contract the whole design
rests on: Esc-first menu cancel, a literal paste, a SEPARATE Enter, and a bare-Enter retry
that never re-pastes. The behavior-preservation guarantee is carried by the existing
test_gate_broker.py / test_hub_afk.py suites; this file is the standalone characterization.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# hub-inject.sh reads transcript mtimes/sizes with BSD `stat -f` and drives the macOS tmux
# hub, exactly like its parent hub-afk.sh — so the suite is macOS-only for the same reason.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="hub-inject.sh targets the macOS BSD-stat + tmux hub"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_INJECT = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-inject.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-inject.sh standalone and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_INJECT}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke hub-inject.sh DIRECTLY (the CLI path, BASH_SOURCE == $0)."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(HUB_INJECT), *args], capture_output=True, text=True, env=full_env
    )


def _recording_tmux(tmp_path: Path) -> tuple[Path, Path]:
    """A tmux stub that appends each invocation's args to a log and exits 0."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    (fake_bin / "tmux").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    (fake_bin / "tmux").chmod(0o755)
    return fake_bin, log


def _pane_tmux(tmp_path: Path, *, pane_path: Path, capture: str = "") -> tuple[Path, Path]:
    """A tmux stub that answers list-panes/capture-pane from fixtures and logs send-keys.

    list-panes -> one pane `hub:0<TAB><pane_path>`; capture-pane -> the `capture` text;
    every other call (send-keys) is appended to the log. Enough to drive _spoke_pane_target,
    _pane_shows_permission_prompt, and the keystroke ordering without a live tmux server.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    cap_file = tmp_path / "capture.txt"
    cap_file.write_text(capture)
    script = f"""#!/usr/bin/env bash
case "$1" in
  list-panes)  printf 'hub:0\\t%s\\n' "{pane_path}" ;;
  capture-pane) cat "{cap_file}" ;;
  *)           printf '%s\\n' "$*" >> "{log}" ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    return fake_bin, log


def _project_dir_for(projects_root: Path, wt_path: Path) -> Path:
    """Mirror the script's slug: non-alphanumerics in the worktree path → '-'."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── the factoring is self-contained ───────────────────────────────────────────


def test_sourcing_standalone_defines_the_primitives() -> None:
    fns = (
        "_spoke_project_dir _spoke_jsonl _transcript_mtime _spoke_pane_target inject_answer "
        "_composer_shows_text _transcript_sizes _answer_appended _answer_delivered "
        "_transcript_advanced inject_and_verify _pane_shows_permission_prompt "
        "approve_permission _deny_permission _answer_needle"
    )
    check = "; ".join(f"declare -F {f} >/dev/null || echo MISSING {f}" for f in fns.split())

    result = _call(check)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"undefined after standalone source: {result.stdout}"


def test_sourcing_standalone_pulls_wt_realpath() -> None:
    # hub-inject sources worktree-lib itself when no parent provided wt_realpath.
    result = _call("declare -F wt_realpath >/dev/null && echo present")

    assert result.stdout.strip() == "present", result.stderr


# ── the keystroke contract (the #251 core) ─────────────────────────────────────


def test_inject_answer_sends_escape_then_literal_text_then_separate_enter(tmp_path: Path) -> None:
    fake_bin, log = _recording_tmux(tmp_path)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "AFK_INJECT_MENU_PAUSE": "0"}

    result = _call("inject_answer 'hub:0' 'use Redis'", env=env)

    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    esc_idx = next(i for i, ln in enumerate(lines) if "Escape" in ln)
    text_idx = next(i for i, ln in enumerate(lines) if "use Redis" in ln)
    enter_idx = next(i for i, ln in enumerate(lines) if ln.split() and ln.split()[-1] == "Enter")
    assert esc_idx < text_idx < enter_idx, f"expected Esc → text → Enter, got: {lines}"
    # The paste is literal: `-l --` guards a leading-dash answer from key-name interpretation.
    assert any("-l --" in ln and "use Redis" in ln for ln in lines), lines


def test_approve_permission_sends_digit_one_then_separate_enter(tmp_path: Path) -> None:
    # Permission dialogs are numbered; option 1 is "Yes (this once)", never option 2.
    fake_bin, log = _pane_tmux(tmp_path, pane_path=tmp_path / "wt")
    (tmp_path / "wt").mkdir()
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",  # don't block on transcript advance
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    _call(f"approve_permission '{tmp_path / 'wt'}'", env=env)

    sends = [ln for ln in log.read_text().splitlines() if "send-keys" in ln]
    assert any(ln.split()[-1] == "1" for ln in sends), f"expected a bare '1', got: {sends}"
    assert any(ln.split()[-1] == "Enter" for ln in sends), f"expected a separate Enter: {sends}"
    # never option 2 ("Yes, don't ask again") — nothing is silently broadened.
    assert not any(ln.split()[-1] == "2" for ln in sends), sends


def test_inject_and_verify_retries_with_bare_enter_never_repastes(tmp_path: Path) -> None:
    # No transcript ever appears, so verification never confirms; the retry MUST be a bare
    # Enter, and the literal paste must happen exactly once (#133: a re-paste duplicated the
    # answer on top of a buffered one).
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, capture="")
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    _call(f"inject_and_verify '{wt}' 'hub:0' 'use Redis'", env=env)

    lines = log.read_text().splitlines()
    pastes = [ln for ln in lines if "use Redis" in ln]
    bare_enters = [ln for ln in lines if ln.split() and ln.split()[-1] == "Enter"]
    assert len(pastes) == 1, f"answer must be pasted exactly once, got {len(pastes)}: {lines}"
    assert len(bare_enters) >= 2, f"expected a submit Enter + a bare-Enter retry: {lines}"


# ── pane + permission detection ────────────────────────────────────────────────


def test_spoke_pane_target_resolves_by_canonical_path(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, _ = _pane_tmux(tmp_path, pane_path=wt)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = _call(f"_spoke_pane_target '{wt}'", env=env)

    assert result.stdout.strip() == "hub:0", result.stderr


def test_pane_shows_permission_prompt_matches_then_misses(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "n").mkdir()
    fake_yes, _ = _pane_tmux(tmp_path / "y", pane_path=wt, capture="Do you want to proceed?")
    fake_no, _ = _pane_tmux(tmp_path / "n", pane_path=wt, capture="just working, nothing to see")

    hit = _call(
        f"_pane_shows_permission_prompt '{wt}'",
        env={"PATH": f"{fake_yes}:{os.environ['PATH']}"},
    )
    miss = _call(
        f"_pane_shows_permission_prompt '{wt}'",
        env={"PATH": f"{fake_no}:{os.environ['PATH']}"},
    )

    assert hit.returncode == 0, "prompt present ⇒ rc 0"
    assert miss.returncode == 1, "prompt absent ⇒ rc 1"


def test_transcript_mtime_reads_newest_jsonl(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(json.dumps({"type": "user"}) + "\n")
    os.utime(pd / "session.jsonl", (1_700_000_000, 1_700_000_000))

    result = _call(f"_transcript_mtime '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)})

    assert result.stdout.strip() == "1700000000", result.stderr


# ── the CLI (direct invocation, no bash-source seam) ───────────────────────────


def test_cli_pane_target_prints_target(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, _ = _pane_tmux(tmp_path, pane_path=wt)

    result = _run_cli("pane-target", str(wt), env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.stdout.strip() == "hub:0", result.stderr


def test_cli_unknown_command_exits_two(tmp_path: Path) -> None:
    result = _run_cli("frobnicate")

    assert result.returncode == 2
    assert "unknown command" in result.stderr
