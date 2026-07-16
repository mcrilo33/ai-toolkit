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


# The agent-liveness probe (#301) reads the pane's pid from tmux, then looks for the agent
# among that pid's DESCENDANTS in a `ps` snapshot. Both halves are stubbed here.
_PANE_PID = 4242
_AGENT_PID = 4243
# Every tmux stub answers `display-message` with the pane pid, so the probe can resolve it.
_DISPLAY_CASE = f'  display-message) printf "{_PANE_PID}\\n" ;;\n'


def _agent_ps_stub(fake_bin: Path, *, agent_alive: bool = True) -> None:
    """PATH-stub `ps` for the #301 agent probe: is the agent a descendant of the pane pid?

    The pane pid is ALWAYS a bare shell — that is the whole point of the incident. A live
    spoke's pane reports `pane_current_command=zsh` exactly like a dead one (the launcher
    shell is the pgrp leader; claude runs as its child), so the ONLY thing separating the
    two shapes is whether a `claude` descendant exists. `agent_alive=False` is the incident:
    a pane whose agent died but whose shell survived `exec $SHELL`.

    Only the probe's exact `-eo pid=,ppid=,comm=` form is answered; every other `ps` call
    (hub-afk's `-o comm= -p`, `-o command= -p`, the hang-capture snapshot) execs the REAL
    ps, so a stub on PATH can never silently corrupt an unrelated process read.
    """
    table = f"{_PANE_PID} 1 -zsh\n"
    if agent_alive:
        table += f"{_AGENT_PID} {_PANE_PID} claude\n"
    # A foreign claude that is NOT under this pane: the probe must not mistake it for ours.
    table += "999 1 /Applications/Other.app/Contents/MacOS/claude\n"
    tbl = fake_bin / "ps_table.txt"
    tbl.write_text(table)
    (fake_bin / "ps").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  "-eo pid=,ppid=,comm=") cat "{tbl}" ;;\n'
        '  *) exec /bin/ps "$@" ;;\n'
        "esac\n"
    )
    (fake_bin / "ps").chmod(0o755)


def _recording_tmux(tmp_path: Path, *, agent_alive: bool = True) -> tuple[Path, Path]:
    """A tmux stub that appends each invocation's args to a log and exits 0."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    (fake_bin / "tmux").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f"{_DISPLAY_CASE}"
        "esac\n"
        "exit 0\n"
    )
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin, agent_alive=agent_alive)
    return fake_bin, log


def _pane_tmux(
    tmp_path: Path, *, pane_path: Path, capture: str = "", agent_alive: bool = True
) -> tuple[Path, Path]:
    """A tmux stub that answers list-panes/capture-pane from fixtures and logs send-keys.

    list-panes -> one pane `hub:0<TAB><pane_path>`; capture-pane -> the `capture` text;
    display-message -> the pane pid (the #301 agent probe); every other call (send-keys) is
    appended to the log. Enough to drive _spoke_pane_target, _pane_shows_permission_prompt,
    and the keystroke ordering without a live tmux server.
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
{_DISPLAY_CASE.rstrip()}
  *)           printf '%s\\n' "$*" >> "{log}" ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin, agent_alive=agent_alive)
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


# ── issue #299 finding 3: approve_permission's lost-keypress retry ─────────────
# The sibling inject_and_verify retries a lost Enter; approve_permission gave up after one
# attempt. The retry canNOT be a copy of the sibling's, though: a bare Enter into a *composer*
# is a harmless no-op, but approve_permission drives a *menu*, where Enter selects whatever is
# CURRENTLY highlighted. Option 2 is "Yes, don't ask again" and must never be selected, so the
# retry re-asserts "1" instead of trusting a highlight it never set — and is gated on the same
# dialog still being pending, so a "1" can never leak into a composer whose dialog was consumed.
_PROMPT = "Do you want to proceed?"


def _pane_tmux_landing_on_nth_enter(
    tmp_path: Path, *, pane_path: Path, capture: str, jsonl: Path, n: int
) -> tuple[Path, Path]:
    """A tmux stub modelling a LOST keypress: the transcript appears only on the nth Enter.

    Nothing here touches the real clock — the transcript's mere APPEARANCE is the advance
    (_transcript_advanced reads an empty `before` as "any mtime is progress"), so the outcome is
    pinned by the stub rather than by wall-time (#294: an approve's only success signal is an
    mtime move, which must never be raced against a real second boundary in a test).
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    counter = tmp_path / "enters"
    cap_file = tmp_path / "capture.txt"
    cap_file.write_text(capture)
    script = f"""#!/usr/bin/env bash
case "$1" in
  list-panes)   printf 'hub:0\\t%s\\n' "{pane_path}" ;;
  capture-pane) cat "{cap_file}" ;;
{_DISPLAY_CASE.rstrip()}
  *)
    printf '%s\\n' "$*" >> "{log}"
    case "$*" in
      *Enter)
        printf 'x' >> "{counter}"
        if [ "$(wc -c < "{counter}" | tr -d ' ')" -ge {n} ]; then
          mkdir -p "$(dirname "{jsonl}")"
          printf '{{}}\\n' >> "{jsonl}"
        fi ;;
    esac ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin)
    return fake_bin, log


def _sends(log: Path) -> list[str]:
    return [ln for ln in log.read_text().splitlines() if "send-keys" in ln]


def _keys(log: Path) -> list[str]:
    return [ln.split()[-1] for ln in _sends(log)]


def test_approve_permission_re_asserts_the_selection_when_the_first_attempt_is_lost(
    tmp_path: Path,
) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    # The dialog is STILL on the pane after the first attempt: the keypress was lost, not consumed.
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, capture=_PROMPT)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",  # never confirms: force the retry path
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    _call(f"approve_permission '{wt}'", env=env)

    assert _keys(log) == ["1", "Enter", "1", "Enter"], (
        "the retry must re-assert '1' before its Enter, never send a bare Enter into a menu"
    )


def test_approve_permission_never_sends_an_enter_it_did_not_precede_with_one(
    tmp_path: Path,
) -> None:
    # The "never option 2" invariant (hub-inject.sh:375-378), pinned on the keystroke sequence
    # itself rather than by reading the code: every Enter this function sends is IMMEDIATELY
    # preceded by an explicit '1', so a highlight that has drifted off option 1 cannot be
    # submitted. Asserting the invariant (not just the count) is what would catch a future
    # bare-Enter retry being reintroduced.
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, capture=_PROMPT)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    _call(f"approve_permission '{wt}'", env=env)

    keys = _keys(log)
    assert "2" not in keys, f"option 2 broadens the approval and must never be sent: {keys}"
    for i, key in enumerate(keys):
        if key == "Enter":
            assert i > 0 and keys[i - 1] == "1", f"bare Enter at index {i}: {keys}"


def test_approve_permission_sends_nothing_more_once_the_dialog_is_gone(tmp_path: Path) -> None:
    # An empty pane capture = no dialog pending. The first attempt may well have landed and the
    # spoke moved on, so there is nothing to approve: retrying would either approve a dialog no
    # classifier ever saw, or type a stray '1' into the spoke's composer.
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, capture="")
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    result = _call(f"approve_permission '{wt}'", env=env)

    assert _keys(log) == ["1", "Enter"], "no dialog on the pane ⇒ no retry keys at all"
    assert result.returncode == 1, "unconfirmed stays rc 1 so the caller re-serves next tick"


def test_approve_permission_does_not_retry_into_a_dialog_it_never_classified(
    tmp_path: Path,
) -> None:
    # The hazard the byte-identity gate exists for: a permission prompt is STILL on the pane, but
    # it is a DIFFERENT one (the #269 unflushed-dialog window renders a dialog the transcript does
    # not yet reflect, so "no transcript advance" alone does not prove nothing moved). Retrying
    # here would approve a command no classifier ever read. A changed pane must draw no keys.
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    counter = tmp_path / "captures"
    # Every capture-pane returns a DIFFERENT dialog, both matching the permission-prompt regex.
    script = f"""#!/usr/bin/env bash
case "$1" in
  list-panes)   printf 'hub:0\\t%s\\n' "{wt}" ;;
  capture-pane) printf 'x' >> "{counter}"
                printf 'rm -rf /tmp/run-%s\\n{_PROMPT}\\n' "$(wc -c < "{counter}" | tr -d ' ')" ;;
{_DISPLAY_CASE.rstrip()}
  *)            printf '%s\\n' "$*" >> "{log}" ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    result = _call(f"approve_permission '{wt}'", env=env)

    assert _keys(log) == ["1", "Enter"], (
        "a pane showing a DIFFERENT dialog must draw no retry — approving it would authorize a "
        "command the classifier never saw"
    )
    assert result.returncode == 1


def test_approve_permission_recovers_a_lost_enter_on_the_retry(tmp_path: Path) -> None:
    # The payoff: the first Enter is lost, the retry lands, and the approve reports success —
    # instead of costing a whole tick of drain latency and a re-run of the classifier.
    wt = tmp_path / "wt"
    wt.mkdir()
    projects = tmp_path / "projects"
    jsonl = _project_dir_for(projects, wt) / "session.jsonl"
    jsonl.unlink(missing_ok=True)
    fake_bin, log = _pane_tmux_landing_on_nth_enter(
        tmp_path, pane_path=wt, capture=_PROMPT, jsonl=jsonl, n=2
    )
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(projects),
    }

    result = _call(f"approve_permission '{wt}'", env=env)

    assert result.returncode == 0, f"the retry landed; approve must report success: {result.stderr}"
    assert _keys(log) == ["1", "Enter", "1", "Enter"]


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
{_DISPLAY_CASE.rstrip()}
  send-keys)
    case "$*" in
      *Escape*) printf '%s\\n' '{decline}' >> "{jsonl}" ;;
    esac ;;
esac
exit 0
"""
    (fake_bin / "tmux").write_text(script)
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin)
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


# ── #289: stat flavor ordering (the CI-red bug class already fixed once in #132) ──
#
# Both stat fallbacks here must probe the GNU spelling FIRST. GNU stat's `-f` means
# "display filesystem status" and takes no inline format, so under a BSD-first
# `stat -f %m F || stat -c %Y F` chain GNU reads `%m` as a missing file operand: it errors
# on %m yet still PRINTS a multi-line filesystem-status block for F and exits nonzero, so
# the `||` fallback fires too and the capture holds the garbage block AND the real value.
# GNU-first inverts this: BSD fails the `-c` probe CLEANLY (usage error, empty stdout), so
# exactly one answer is ever captured. The stubs simulate BOTH flavors, so these pin the
# ordering regardless of host -- this module is darwin-gated, so they guard the dev host
# where such a regression would be authored, while test_gate_broker_detect.py (ungated)
# carries the same contract on the ubuntu CI runner.

_GNU_STAT_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "-c" ]; then\n'
    '  case "$2" in\n'
    "    %Y) echo 1700000000; exit 0 ;;\n"
    "    %s) echo 4096; exit 0 ;;\n"
    "  esac\n"
    "fi\n"
    'if [ "$1" = "-f" ]; then\n'
    '  echo "  File: \\"$3\\""\n'
    '  echo "    ID: b505c8e079f9471 Namelen: 255     Type: ext2/ext3"\n'
    '  echo "  Block size: 4096       Fundamental block size: 4096"\n'
    '  echo "stat: cannot read file system information for $2" >&2\n'
    "  exit 1\n"
    "fi\n"
    "exit 1\n"
)

_BSD_STAT_STUB = (
    "#!/bin/sh\n"
    'if [ "$1" = "-c" ]; then echo "stat: illegal option -- c" >&2; exit 1; fi\n'
    'if [ "$1" = "-f" ]; then\n'
    '  case "$2" in\n'
    "    %m) echo 1700000000; exit 0 ;;\n"
    "    %z) echo 4096; exit 0 ;;\n"
    "  esac\n"
    "fi\n"
    "exit 1\n"
)


def _stat_stub_path(tmp_path: Path, stub_body: str) -> str:
    """Install a fake `stat` and return a PATH with it in front of the real one."""
    bindir = tmp_path / "stat-stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "stat"
    stub.write_text(stub_body)
    stub.chmod(0o755)
    return f"{bindir}:{os.environ['PATH']}"


def _wt_with_transcript(tmp_path: Path) -> tuple[Path, Path]:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    projects = tmp_path / "projects"
    _project_dir_for(projects, wt).joinpath("session.jsonl").write_text("{}\n")
    return wt, projects


@pytest.mark.parametrize("stub", [_GNU_STAT_STUB, _BSD_STAT_STUB], ids=["gnu", "bsd"])
def test_transcript_mtime_survives_both_stat_flavors(tmp_path: Path, stub: str) -> None:
    # The registration signal for inject verification: a polluted capture makes the
    # caller's `[ "$now" -gt "$before" ]` compare error out, so a delivered answer reads as
    # unregistered and inject_and_verify returns the wrong RC (the red CI nodes).
    wt, projects = _wt_with_transcript(tmp_path)

    result = _call(
        f"_transcript_mtime '{wt}'",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": _stat_stub_path(tmp_path, stub),
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1700000000", (
        f"mtime must be a bare epoch: {result.stdout!r}{result.stderr}"
    )


@pytest.mark.parametrize("stub", [_GNU_STAT_STUB, _BSD_STAT_STUB], ids=["gnu", "bsd"])
def test_transcript_sizes_survives_both_stat_flavors(tmp_path: Path, stub: str) -> None:
    # The size-sort helper takes the `-c %s` / `-f %z` variant of the same chain; its
    # snapshot feeds _answer_appended's byte-level delivery proof, so a leaked fs block
    # would corrupt the pre-inject baseline rather than any mtime compare.
    wt, projects = _wt_with_transcript(tmp_path)

    result = _call(
        f"_transcript_sizes '{wt}'",
        env={
            "CLAUDE_PROJECTS_DIR": str(projects),
            "PATH": _stat_stub_path(tmp_path, stub),
        },
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 1, (
        f"one size<TAB>path line per jsonl: {result.stdout!r}{result.stderr}"
    )
    assert result.stdout.split("\t")[0] == "4096", (
        f"size must be a bare byte count: {result.stdout!r}{result.stderr}"
    )


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


# ── #301: the agent-liveness probe + the never-inject-into-a-shell precondition ─
#
# The 2026-07-15 incident: a reboot killed both spokes' `claude` processes, but the terminal
# restored their tmux panes running a bare zsh in the worktree. The answerer then typed its
# answer into that shell, where zsh's autocorrect read the prose as a COMMAND:
#
#     > pproved — proceed to ST4. Answers to your three questions: ...
#     zsh: correct 'pproved' to 'prove' [nyae]?
#
# It was harmless only by luck. An answer whose first word happened to be a real command —
# or one carrying shell metacharacters — would have EXECUTED inside a git worktree. So the
# precondition lives in the inject PRIMITIVES, not in the callers: every lane (answer,
# permission approve, nudge, watchdog) inherits it by construction.


def test_pane_agent_alive_finds_the_agent_below_a_bare_shell_pane(tmp_path: Path) -> None:
    """A LIVE spoke pane runs a bare shell — the agent is its CHILD.

    This is the pin that kills the tempting-but-wrong probe. On a real hub a live spoke's
    pane reports `pane_current_command=zsh`, byte-identical to the dead pane in the
    incident, because the launcher shell is the process-group leader and claude runs
    beneath it. Anything reading the pane's own command would declare every healthy spoke
    dead and kill it. Liveness is a property of the pane's process TREE, not its foreground
    command.
    """
    fake_bin, _ = _recording_tmux(tmp_path, agent_alive=True)

    result = _call("_pane_agent_alive 'hub:0'", env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode == 0, (
        f"a claude child of the pane pid is a LIVE agent: {result.stderr}"
    )


def test_pane_agent_alive_reports_dead_for_a_pane_whose_agent_exited(tmp_path: Path) -> None:
    """The incident shape: pane alive (`exec $SHELL` kept it), agent gone."""
    fake_bin, _ = _recording_tmux(tmp_path, agent_alive=False)

    result = _call("_pane_agent_alive 'hub:0'", env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode == 1, (
        f"a pane with no agent descendant must read DEAD, not alive: {result.stdout}{result.stderr}"
    )


def test_pane_agent_alive_ignores_a_claude_outside_the_pane_tree(tmp_path: Path) -> None:
    """A foreign claude (another spoke, the IDE's own) must not vouch for THIS pane."""
    fake_bin, _ = _recording_tmux(tmp_path, agent_alive=False)

    result = _call("_pane_agent_alive 'hub:0'", env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode == 1, "only a DESCENDANT of the pane pid counts as this pane's agent"


def test_pane_agent_alive_is_unprovable_when_the_pane_pid_is_unreadable(tmp_path: Path) -> None:
    """rc 2 = no evidence either way — distinct from a proven-dead rc 1.

    The two consumers take OPPOSITE safe directions from this: a write (inject/approve)
    refuses, while liveness reads it as alive rather than kill a spoke it cannot observe.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    # tmux answers display-message with nothing: no pane pid, so the tree cannot be walked.
    (fake_bin / "tmux").write_text("#!/usr/bin/env bash\nexit 0\n")
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin)

    result = _call("_pane_agent_alive 'hub:0'", env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode == 2, (
        f"an unreadable pane pid is UNPROVABLE, not dead: {result.stderr}"
    )


def test_inject_answer_sends_no_keys_when_the_agent_is_dead(tmp_path: Path) -> None:
    """THE safety pin (AC3 + AC4b): never type prose into a bare shell.

    Asserts on the keystrokes, not just the return code: a non-zero rc that had already sent
    the text would still have executed it. Zero send-keys is the only honest proof.
    """
    fake_bin, log = _recording_tmux(tmp_path, agent_alive=False)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "AFK_INJECT_MENU_PAUSE": "0"}

    result = _call("inject_answer 'hub:0' 'Approved — proceed to ST4.'", env=env)

    assert result.returncode != 0, (
        "injecting into a pane with no agent must FAIL, not silently pass"
    )
    assert _sends(log) == [], (
        f"a dead-agent pane must receive ZERO keystrokes — these would have run as shell "
        f"commands in the worktree: {_sends(log)}"
    )


def test_inject_answer_refuses_when_the_agent_probe_is_unprovable(tmp_path: Path) -> None:
    """Writes fail CLOSED: an unobservable pane is never typed into.

    Not silent — inject_and_verify's non-zero rc routes to the existing #281 escalation.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "tmux.log"
    (fake_bin / "tmux").write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{log}"\nexit 0\n')
    (fake_bin / "tmux").chmod(0o755)
    _agent_ps_stub(fake_bin)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "AFK_INJECT_MENU_PAUSE": "0"}

    result = _call("inject_answer 'hub:0' 'use Redis'", env=env)

    assert result.returncode != 0, "an unprovable agent must not be typed into"
    assert _sends(log) == [], f"no keys may reach an unobservable pane: {_sends(log)}"


def test_approve_permission_sends_no_keys_when_the_agent_is_dead(tmp_path: Path) -> None:
    """The permission lane inherits the precondition too.

    approve_permission does NOT go through inject_answer — it drives the menu itself — so a
    guard placed only in the answer path would leave this lane typing "1<Enter>" into a
    shell. Guarding both send-keys primitives is what makes "every lane inherits it" true.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, capture=_PROMPT, agent_alive=False)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    result = _call(f"approve_permission '{wt}'", env=env)

    assert result.returncode != 0, "approving a dead pane's stale dialog must fail"
    assert _sends(log) == [], (
        f"a dead-agent pane must draw no selection keys — the rendered dialog is scrollback "
        f"left behind by an agent that is gone: {_sends(log)}"
    )


def test_inject_and_verify_refuses_a_dead_agent_without_touching_the_pane(tmp_path: Path) -> None:
    """The wrapper every answer lane calls inherits the guard from inject_answer."""
    wt = tmp_path / "wt"
    wt.mkdir()
    fake_bin, log = _pane_tmux(tmp_path, pane_path=wt, agent_alive=False)
    env = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AFK_INJECT_MENU_PAUSE": "0",
        "AFK_INJECT_VERIFY_SECONDS": "0",
        "AFK_INJECT_POLL_SECONDS": "0",
        "CLAUDE_PROJECTS_DIR": str(tmp_path / "projects"),
    }

    result = _call(f"inject_and_verify '{wt}' 'hub:0' 'use Redis'", env=env)

    assert result.returncode != 0, "a dead-agent pane cannot register an answer"
    assert _sends(log) == [], f"not even the bare-Enter retry may reach a dead pane: {_sends(log)}"


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
