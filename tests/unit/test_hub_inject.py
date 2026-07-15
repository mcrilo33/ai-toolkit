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
        "approve_permission _deny_permission _answer_needle _transcript_finished_turn_idle"
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


def _decline_tmux(tmp_path: Path, *, wt: Path, jsonl: Path) -> tuple[Path, Path]:
    """The #281 pane: Esc DECLINES a pending AskUserQuestion, the paste never submits.

    Models the real sequence in the #271 spoke pane. inject_answer sends Escape first (the
    #74 menu-cancel, built for the PLAN-gate QCM); against a live AskUserQuestion that Escape
    CANCELS the menu, and Claude Code records the cancel as its own type:"user" turn ("User
    declined to answer questions") — which bumps the transcript mtime. The free-text answer
    typed after it then sits in the composer and its Enter is swallowed, so the answer's own
    needle NEVER lands in a user record.

    capture-pane returns empty: a 40-char needle is wrapped across lines by the composer's
    box, so the grep -qF in _composer_shows_text misses it and the pane reads as "no text
    here" even though the paste is sitting right there. That false-negative is what let the
    unsubmitted paste score as delivered.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    decline = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": "User declined"}]}}
    )
    script = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
case "$1" in
  list-panes)   printf 'hub:0\\t%s\\n' "{wt}" ;;
  capture-pane) : ;;
  send-keys)
    case "$*" in
      *Escape*) printf '%s\\n' '{decline}' >> "{jsonl}" ;;
    esac ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    return fake_bin, log


def test_inject_and_verify_rejects_qcm_decline_advance_as_delivery(tmp_path: Path) -> None:
    """#281 head (b): an Esc-cancelled QCM must never score as the answer's own delivery.

    The decline record advances the transcript, so _transcript_advanced fires — but it is the
    INJECTOR's own Escape that wrote it, not the spoke reading the answer. Delivery proof has
    to be the answer's needle landing in an appended type:"user" record; anything else lets a
    paste nobody submitted be logged as "injected answer into #271" (four times, against an
    answer the spoke never saw).
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    jsonl = _project_dir_for(projects, wt) / "session.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (1_000_000_000, 1_000_000_000))  # stale: the decline reads as an advance
    fake_bin, log = _decline_tmux(tmp_path, wt=wt, jsonl=jsonl)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(projects),
    }
    answer = "Approved — the push is correct; emit the ready marker."

    result = _call(
        f'inject_and_verify "{wt}" hub:0 "$ANSWER"; echo RC=$?', env={**env, "ANSWER": answer}
    )

    rc = result.stdout.strip().splitlines()[-1]
    assert rc == "RC=3", f"an unsubmitted paste must be REFUTED, not delivered: {result.stdout}"
    # The retry is a bare Enter, never a re-paste (#133): a second paste would stack the
    # answer on top of the first in the composer.
    pastes = [ln for ln in log.read_text().splitlines() if answer in ln]
    assert len(pastes) == 1, f"answer must be pasted exactly once, got {len(pastes)}"


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


# ── #255: the finished-turn-idle transcript read ───────────────────────────────


def _bash_tool_record(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "id": "tu_1", "input": {"command": command}}
            ]
        },
    }


def test_finished_turn_idle_true_when_last_turn_is_completed_assistant(tmp_path: Path) -> None:
    # A completed tool cycle that ends on an assistant text turn: the spoke stopped at the
    # prompt with no pending tool_use → nudge-able, not a hang.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _bash_tool_record("pytest -q"),
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
                },
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done."}]}},
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 0, result.stderr


def test_finished_turn_idle_false_on_trailing_pending_tool_use(tmp_path: Path) -> None:
    # A trailing UNRESOLVED tool_use is a pane hung MID-TOOL_USE (incl. the #240 flushed shape),
    # NOT finished-turn-idle → the reaper revives it, never nudges.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "run"}]}},
                _bash_tool_record("pytest -q"),
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 1, result.stderr


def test_finished_turn_idle_true_with_trailing_task_notification(tmp_path: Path) -> None:
    # #255 review: a spoke that finished its turn, awaited a background task, and then received
    # a <task-notification> user turn (synthetic: no promptSource, not isMeta) is STILL nudge-
    # able — the notification does not mean the spoke moved on. Skipped, so the completed
    # assistant turn before it is the last genuine turn.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "stop"}]}},
                {
                    "type": "user",
                    "message": {"content": "<task-notification>done</task-notification>"},
                },
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 0, result.stderr


def test_finished_turn_idle_true_with_trailing_meta_turn(tmp_path: Path) -> None:
    # An isMeta user turn (a skill/meta injection) after a completed assistant turn is likewise
    # synthetic and skipped → the spoke is still nudge-able.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "stop"}]}},
                {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 0, result.stderr


def test_finished_turn_idle_false_with_trailing_typed_reply(tmp_path: Path) -> None:
    # A GENUINE typed user reply after the assistant turn means the spoke got real input and is
    # about to work — not idle at the prompt → rc 1 (not nudged).
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "stop"}]}},
                {"type": "user", "promptSource": "typed", "message": {"content": "keep going"}},
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 1, result.stderr


def test_finished_turn_idle_false_when_last_turn_is_tool_result(tmp_path: Path) -> None:
    # The transcript ends on a tool_result user turn: the spoke stopped mid-turn (about to
    # produce the next assistant message), not idle at the prompt → not finished-turn-idle.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    (pd / "session.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _bash_tool_record("pytest -q"),
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
                },
            ]
        )
        + "\n"
    )

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 1, result.stderr


def test_finished_turn_idle_false_when_no_transcript(tmp_path: Path) -> None:
    # Fail-CLOSED: an absent transcript reads as "not proven finished-turn-idle" so the reaper
    # falls through to its existing revive rather than nudging a pane it cannot observe.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()

    result = _call(
        f"_transcript_finished_turn_idle '{wt}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 1, result.stderr


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
