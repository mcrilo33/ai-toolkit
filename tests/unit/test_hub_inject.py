"""Unit tests for shared/skills/hub/scripts/hub-inject.sh, the Orca delivery module (#251, #365).

Every answer, approval and nudge reaches a spoke through deliver_reply (the ack is the proof)
or deliver_text (the stage Orca observed is the proof), both rc 0 / 1, never resent within the
call. The module is sourced STANDALONE to prove it is self-contained; the transcript locators the
reaper's idle clock still reads and the #300 delivery events are pinned too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _orca_stub import install_forbidden_stubs, orca_calls, orca_scenario

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="hub-inject.sh targets the macOS BSD-stat control plane"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_INJECT = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-inject.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-inject.sh standalone and invoke a shell expression against its functions."""
    full_env = {**os.environ, "TZ": "UTC", "AFK_INJECT_MENU_PAUSE": "0"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_INJECT}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _project_dir_for(projects_root: Path, wt_path: Path) -> Path:
    """Mirror the script's slug: non-alphanumerics in the worktree path -> '-'."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt_path))
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- the module is self-contained and carries no pane code ----------------------------------


def test_sourcing_standalone_defines_the_delivery_and_locator_functions() -> None:
    fns = (
        "deliver_reply deliver_text deliver_answer approve_permission _deny_permission "
        "_spoke_project_dir _spoke_jsonl _transcript_mtime _transcript_sizes"
    )
    check = "; ".join(f"declare -F {f} >/dev/null || echo MISSING {f}" for f in fns.split())

    result = _call(check)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"undefined after standalone source: {result.stdout}"


def test_the_pane_primitives_are_gone() -> None:
    gone = (
        "inject_and_verify inject_answer _spoke_pane_target _pane_agent_alive _composer_shows_text"
    )
    check = "; ".join(f"declare -F {f} >/dev/null && echo STILL {f}" for f in gone.split())

    result = _call(check)

    assert result.stdout.strip() == "", result.stdout


def test_hub_inject_source_has_no_tmux_transport() -> None:
    src = HUB_INJECT.read_text()

    for token in ("tmux", "capture-pane", "send-keys", "pane_pid", "pane_current_path"):
        assert token not in src, token


# -- delivery through Orca --------------------------------------------------------------------


def _wt(tmp_path: Path, **identity: str) -> Path:
    wt = tmp_path / "wt"
    (wt / ".ai-toolkit").mkdir(parents=True, exist_ok=True)
    (wt / ".ai-toolkit" / "identity").write_text(
        "".join(f"{k}={v}\n" for k, v in {"issue": "311", **identity}.items())
    )
    return wt


def _worker(wt: Path, handle: str = "term_w") -> dict:
    return {
        "dispatchId": "ctx_1",
        "taskId": "task_1",
        "agentTerminalHandle": handle,
        "resource": {"worktreeId": f"repo::{wt}"},
        "projection": {"liveness": {"verdict": "live"}},
    }


def _send(stages: list[str] | None, accepted: bool = True) -> dict:
    prompt = {"stages": stages} if stages is not None else None
    return {"out": {"ok": True, "result": {"send": {"accepted": accepted, "prompt": prompt}}}}


def _scenario(orca_bin: Path, wt: Path, **extra: list[dict]) -> None:
    workers = {"out": {"ok": True, "result": {"workers": [_worker(wt)]}}}
    orca_scenario(orca_bin, {"orchestration worker-list": [workers], **extra})


def _sends(orca_bin: Path) -> list[list[str]]:
    return [c for c in orca_calls(orca_bin) if c[:2] == ["terminal", "send"]]


def _tlog(state_dir: Path, issue: int) -> str:
    p = state_dir / "transitions" / f"{issue}.jsonl"
    return p.read_text() if p.is_file() else ""


def test_deliver_reply_acks_the_recorded_message_id(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path, run_id="run_1")
    _scenario(orca_bin, wt)

    result = _call(f'deliver_reply "{wt}" m_42 "use Redis"')

    assert result.returncode == 0, result.stderr
    reply = next(c for c in orca_calls(orca_bin) if c[:2] == ["orchestration", "reply"])
    assert reply[reply.index("--id") + 1] == "m_42"
    assert reply[reply.index("--body") + 1] == "use Redis"
    assert reply[reply.index("--run") + 1] == "run_1"


def test_deliver_reply_refused_or_unknown_is_rc1(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"orchestration reply": [{"rc": 1, "out": {"ok": False}}]})

    result = _call(f'deliver_reply "{wt}" m_42 x')

    assert result.returncode == 1


def test_deliver_text_is_rc0_only_when_the_requested_stage_was_observed(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"terminal send": [_send(["input_accepted", "turn_started"])]})
    started = _call(f'deliver_text "{wt}" "go on"')
    _scenario(orca_bin, wt, **{"terminal send": [_send(["input_accepted"])]})
    only_accepted = _call(f'deliver_text "{wt}" "go on"')
    accepted_proof = _call(f'deliver_text "{wt}" 1 input_accepted')

    assert started.returncode == 0, started.stderr
    assert only_accepted.returncode == 1
    assert accepted_proof.returncode == 0


def test_deliver_text_silence_is_rc1_and_sends_exactly_once(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"terminal send": [_send(["input_accepted"])]})

    result = _call(f'deliver_text "{wt}" "go on"')

    assert result.returncode == 1
    (call,) = _sends(orca_bin)
    assert call[call.index("--text") + 1] == "go on"
    assert "--enter" in call
    assert "--wait-submit" in call


def test_deliver_text_refuses_a_worker_orca_reports_exited(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    exited = {**_worker(wt), "projection": {"liveness": {"verdict": "exited"}}}
    orca_scenario(
        orca_bin,
        {"orchestration worker-list": [{"out": {"ok": True, "result": {"workers": [exited]}}}]},
    )

    result = _call(f'deliver_text "{wt}" hi')

    assert result.returncode == 1
    assert _sends(orca_bin) == []


@pytest.mark.parametrize(
    ("since", "want"),
    [("400000", "600000"), ("1970-01-01T00:06:40Z", "600000"), ("nonsense", ""), ("", "")],
)
def test_wait_ms_reads_epoch_ms_and_iso_timestamps(since: str, want: str) -> None:
    result = _call(f'_hi_wait_ms "{since}"', env={"AFK_NOW": "1000"})

    assert result.stdout.strip() == want


def test_deliver_text_without_a_recorded_terminal_sends_nothing(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)

    result = _call(f'deliver_text "{wt}" hi')

    assert result.returncode == 1
    assert _sends(orca_bin) == []


def test_deliver_text_with_escape_cancels_the_menu_before_the_text(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt)

    _call(f'deliver_text "{wt}" "pick Redis" turn_started 1')

    esc, text = _sends(orca_bin)
    assert esc[esc.index("--text") + 1] == "\x1b"
    assert "--enter" not in esc
    assert text[text.index("--text") + 1] == "pick Redis"


def test_approve_permission_sends_one_and_counts_input_accepted(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"terminal send": [_send(["input_accepted"])]})
    state = tmp_path / "sd"

    result = _call(f'approve_permission "{wt}"', env={"AFK_STATE_DIR": str(state)})

    assert result.returncode == 0, result.stderr
    (call,) = _sends(orca_bin)
    assert call[call.index("--text") + 1] == "1"
    assert '"delivered":true' in _tlog(state, 311)


def test_approve_permission_silence_is_rc1_and_never_resent(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"terminal send": [_send(None, accepted=False)]})
    state = tmp_path / "sd"

    result = _call(f'approve_permission "{wt}"', env={"AFK_STATE_DIR": str(state)})

    assert result.returncode == 1
    assert len(_sends(orca_bin)) == 1
    assert '"delivered":false' in _tlog(state, 311)


def test_deny_permission_cancels_the_dialog_then_sends_the_guidance(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt)

    result = _call(f'_deny_permission "{wt}" "use a worktree-local path"')

    assert result.returncode == 0, result.stderr
    esc, text = _sends(orca_bin)
    assert esc[esc.index("--text") + 1] == "\x1b"
    assert text[text.index("--text") + 1] == "use a worktree-local path"


def test_deliver_answer_replies_by_id_and_types_only_for_a_stray_dialog(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt)

    _call(f'deliver_answer "{wt}" 311 m_9 "approved"')
    by_id = [c[:2] for c in orca_calls(orca_bin)]
    _call(f'deliver_answer "{wt}" 311 "" "pick Redis"')

    assert ["orchestration", "reply"] in by_id
    assert ["terminal", "send"] not in by_id
    assert len(_sends(orca_bin)) == 2  # Escape + the text


def test_delivery_never_touches_tmux(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt)
    bindir = tmp_path / "forbidden-bin"
    bindir.mkdir()
    log = install_forbidden_stubs(bindir)

    _call(
        f'deliver_text "{wt}" hi; deliver_reply "{wt}" m x; approve_permission "{wt}"',
        env={"PATH": f"{bindir}:{os.environ['PATH']}"},
    )

    assert log.read_text() == ""


@pytest.mark.parametrize(
    ("rc", "event"), [("0", "answer_delivered"), ("1", "answer_not_registered")]
)
def test_hi_delivery_verdict_maps_each_rc(rc: str, event: str) -> None:
    result = _call(f"_hi_delivery_verdict {rc}")

    assert result.stdout.strip() == event


def test_deliver_text_records_injected_then_the_verdict(tmp_path: Path, orca_bin: Path) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt)
    state = tmp_path / "sd"

    _call(f'deliver_text "{wt}" hi', env={"AFK_STATE_DIR": str(state)})

    log = _tlog(state, 311)
    assert log.index('"event":"answer_injected"') < log.index('"event":"answer_delivered"')


def test_a_refused_reply_records_only_the_not_registered_verdict(
    tmp_path: Path, orca_bin: Path
) -> None:
    wt = _wt(tmp_path)
    _scenario(orca_bin, wt, **{"orchestration reply": [{"rc": 1, "out": {"ok": False}}]})
    state = tmp_path / "sd"

    _call(f'deliver_reply "{wt}" m x', env={"AFK_STATE_DIR": str(state)})

    log = _tlog(state, 311)
    assert '"event":"answer_not_registered"' in log
    assert '"event":"answer_injected"' not in log


# -- transcript locators (the reaper's idle clock) ---------------------------------------------


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


# -- #300 step 3b: delivery-event writers ---------------------------------------------------


def _tlog(state_dir: Path, issue: int) -> str:
    p = state_dir / "transitions" / f"{issue}.jsonl"
    return p.read_text() if p.is_file() else ""


def test_hi_issue_for_wt_prefers_the_explicit_override(tmp_path: Path) -> None:
    # AFK_TLOG_ISSUE wins over any branch derivation (the answer path passes no issue, so tests
    # and future callers that already know it thread it in without a git worktree).
    result = _call(
        f'_hi_issue_for_wt "{tmp_path}"; echo rc=$?',
        env={"AFK_TLOG_ISSUE": "311"},
    )

    assert "311" in result.stdout
    assert "rc=0" in result.stdout


def test_hi_issue_for_wt_empty_on_a_non_numeric_slug(tmp_path: Path) -> None:
    # A non-numeric override (an ad-hoc /quick slug) yields no issue — the caller then skips the
    # write rather than keying a log by a bad id.
    result = _call(
        f'out="$(_hi_issue_for_wt "{tmp_path}")"; echo "rc=$? out=[$out]"',
        env={"AFK_TLOG_ISSUE": "quick-slug"},
    )

    assert "rc=1 out=[]" in result.stdout


def test_hi_tlog_delivery_records_the_event_with_lane_and_episode(tmp_path: Path) -> None:
    state = tmp_path / "sd"

    _call(
        f'_hi_tlog_delivery "{tmp_path / "wt"}" answer_delivered answer \'{{"rc":0}}\'',
        env={
            "AFK_STATE_DIR": str(state),
            "AFK_TLOG_ISSUE": "311",
            "AFK_TLOG_LANE": "permission",
            "AFK_TLOG_EPISODE": "abc123:1700",
        },
    )

    line = _tlog(state, 311)
    assert '"event":"answer_delivered"' in line
    assert '"actor":"hub-inject.sh"' in line
    assert '"lane":"permission"' in line  # AFK_TLOG_LANE overrides the default lane
    assert '"episode":"abc123:1700"' in line  # a caller that owns the park context threads it in


def test_hi_tlog_delivery_defaults_the_lane_when_env_unset(tmp_path: Path) -> None:
    state = tmp_path / "sd"

    _call(
        f'_hi_tlog_delivery "{tmp_path / "wt"}" answer_injected answer',
        env={"AFK_STATE_DIR": str(state), "AFK_TLOG_ISSUE": "311"},
    )

    assert '"lane":"answer"' in _tlog(state, 311)


def test_hi_tlog_delivery_is_best_effort_without_the_transition_log(tmp_path: Path) -> None:
    # The #300 contract: a write NEVER fails the injector. With the transition-log lib absent
    # (both wrappers unset), the helper is a clean no-op returning success — no file, no error.
    state = tmp_path / "sd"

    result = _call(
        f"unset -f wt_tlog_event afk_tlog_event; "
        f'_hi_tlog_delivery "{tmp_path / "wt"}" answer_delivered answer; echo rc=$?',
        env={"AFK_STATE_DIR": str(state), "AFK_TLOG_ISSUE": "311"},
    )

    assert "rc=0" in result.stdout
    assert not (state / "transitions").exists()


# ── issue #361 (S2b): _hi_issue_for_wt reads the identity record ──────────────
# Identity first, then the branch slug. AFK_TLOG_ISSUE still wins over both.


def _branch_wt(tmp_path: Path, branch: str, record: str | None = None) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(wt)], check=True)
    if record is not None:
        (wt / ".ai-toolkit").mkdir()
        (wt / ".ai-toolkit" / "identity").write_text(record)
    return wt


def _issue_for(wt: Path, *, env: dict[str, str] | None = None) -> str:
    result = _call(f'out="$(_hi_issue_for_wt "{wt}")"; echo "rc=$? out=[$out]"', env=env)
    return result.stdout.strip()


def test_hi_issue_for_wt_reads_the_record_on_a_non_conforming_branch(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "orca-migration", "issue=361\n")

    assert _issue_for(wt) == "rc=0 out=[361]"


def test_hi_issue_for_wt_prefers_the_record_over_the_branch_slug(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "feature/5-x", "issue=361\n")

    assert _issue_for(wt) == "rc=0 out=[361]"


def test_hi_issue_for_wt_override_still_beats_the_record(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "orca-migration", "issue=361\n")

    assert _issue_for(wt, env={"AFK_TLOG_ISSUE": "311"}) == "rc=0 out=[311]"


def test_hi_issue_for_wt_without_a_record_keeps_the_branch_slug_read(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "feature/223-slug")

    assert _issue_for(wt) == "rc=0 out=[223]"


def test_hi_issue_for_wt_without_a_record_is_empty_on_a_bare_branch(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "orca-migration")

    assert _issue_for(wt) == "rc=1 out=[]"


def test_hi_issue_for_wt_ignores_a_non_numeric_record(tmp_path: Path) -> None:
    wt = _branch_wt(tmp_path, "feature/223-slug", "issue=quick-slug\n")

    assert _issue_for(wt) == "rc=0 out=[223]"


def test_hi_tlog_delivery_logs_a_bare_branch_worktree_against_its_recorded_issue(
    tmp_path: Path,
) -> None:
    wt = _branch_wt(tmp_path, "orca-migration", "issue=361\n")
    state = tmp_path / "sd"

    _call(
        f'_hi_tlog_delivery "{wt}" answer_delivered answer \'{{"rc":0}}\'',
        env={"AFK_STATE_DIR": str(state)},
    )

    assert '"event":"answer_delivered"' in _tlog(state, 361)


@pytest.mark.parametrize(
    ("scripts_dir", "lib_dir"),
    [
        # synced target: identity.sh co-located with hub-inject.sh
        pytest.param(".ai-toolkit/scripts", ".ai-toolkit/scripts", id="toolkit"),
        # synced .claude layout: the hook libs live under hooks/scripts/lib
        pytest.param(".claude/skills/hub/scripts", ".claude/hooks/scripts/lib", id="claude"),
    ],
)
def test_identity_loader_resolves_in_a_synced_target_layout(
    tmp_path: Path, scripts_dir: str, lib_dir: str
) -> None:
    # The permission hook and danger wall run from <target>/.claude/skills/hub/scripts/ with a
    # CLAUDE_PROJECT_DIR that is NOT the ai-toolkit checkout: the loader must still find the lib.
    scripts = tmp_path / scripts_dir
    libs = tmp_path / lib_dir
    scripts.mkdir(parents=True)
    libs.mkdir(parents=True, exist_ok=True)
    (scripts / "hub-inject.sh").write_text(HUB_INJECT.read_text())
    (libs / "identity.sh").write_text(
        (REPO_ROOT / "shared" / "hooks" / "lib" / "identity.sh").read_text()
    )
    empty_top = tmp_path / "no-toplevel"
    empty_top.mkdir()

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'cd "{empty_top}" && source "{scripts / "hub-inject.sh"}" && '
            "declare -F ai_toolkit_identity_issue_at",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "_AFK_TOPLEVEL": str(empty_top)},
    )

    assert "ai_toolkit_identity_issue_at" in result.stdout, result.stderr
