"""Unit tests for shared/skills/hub/scripts/hub-watchdog.sh.

hub-watchdog.sh is the tier-2 OS-level supervision daemon (issue #251): a self-looping,
pidfile-singleton, source-recycling process with its own heartbeat that cross-checks the
tier-1 /afk drain and (in later subtasks) turns each intervention into a defect signal. This
subtask is the daemon SKELETON — these tests pin the loop/pidfile/heartbeat/self-recycle
scaffold and the drain-state cross-check, modelled on test_hub_otel_watch.py's daemon suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _gate_broker_support import _project_dir_for, _resumed_gate_transcript

# hub-watchdog.sh cross-checks the macOS afk hub (kill -0, BSD tooling) like its siblings.
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="hub-watchdog.sh targets the macOS afk hub"
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_WATCHDOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-watchdog.sh"
WT_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"


def _call(fn_call: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source hub-watchdog.sh and invoke a shell expression against its functions.

    HUB_WATCHDOG_FILE defaults OFF here: this host has an authed `gh`, so a default-on defect
    file would fire a real hub-agent/gh against the live repo. The filing tests opt back in with
    HUB_WATCHDOG_FILE=1 + the HUB_WATCHDOG_SCOPER_CMD / _DEDUP_CMD / _LABEL_CMD stubs.
    """
    full_env = {**os.environ, "TZ": "UTC", "HUB_WATCHDOG_FILE": "0"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_WATCHDOG}"; {fn_call}'],
        capture_output=True,
        text=True,
        env=full_env,
    )


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke hub-watchdog.sh DIRECTLY (the CLI dispatch, BASH_SOURCE == $0)."""
    full_env = {**os.environ, "TZ": "UTC"}
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", str(HUB_WATCHDOG), *args], capture_output=True, text=True, env=full_env
    )


def _drain_pattern_stub(tmp_path: Path, pattern: str) -> str:
    """A _wd_drain_state stub scripted per tick: 'L'=live, 'S'=stale, anything else=off.

    Ticks beyond the pattern read as off, so every loop terminates. The tick count persists
    in a file the test can assert on.
    """
    ticks = tmp_path / "ticks"
    return (
        f'TICKS="{ticks}"; PATTERN="{pattern}"; '
        "_wd_drain_state() { "
        'n=$(( $(cat "$TICKS" 2>/dev/null || echo 0) + 1 )); printf "%s" "$n" > "$TICKS"; '
        'c="${PATTERN:$((n-1)):1}"; '
        'case "$c" in L) echo live ;; S) echo stale ;; *) echo off ;; esac; }'
    )


_LOOP_ENV = "; ".join(["export HUB_WATCHDOG_INTERVAL=0", "export HUB_WATCHDOG_IDLE_TICKS=3"])


# ── the drain-state cross-check ────────────────────────────────────────────────


def test_drain_state_off_when_no_state_file(tmp_path: Path) -> None:
    result = _call(
        "_wd_drain_state",
        env={"AFK_STATE": str(tmp_path / "absent"), "AFK_HEARTBEAT": str(tmp_path / "absent")},
    )

    assert result.stdout.strip() == "off", result.stderr


def test_drain_state_live_when_heartbeat_pid_alive(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")  # this test process is alive

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "live", result.stderr


def test_drain_state_stale_when_armed_but_pid_dead(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain until 23:00\n")
    hb = tmp_path / "afk-hb"
    # A pid that is (almost certainly) not a live process — a very high number.
    hb.write_text("999999 1783879781\n")

    result = _call("_wd_drain_state", env={"AFK_STATE": str(state), "AFK_HEARTBEAT": str(hb)})

    assert result.stdout.strip() == "stale", result.stderr


# ── the daemon loop ────────────────────────────────────────────────────────────


def test_loop_ticks_and_writes_heartbeat_on_each_live_tick(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_HEARTBEAT="{hb}"',
        "_wd_loop",  # empty baseline ⇒ no self-recycle
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    # Two live ticks then off to exhaustion: _wd_tick logged the drain state twice.
    assert result.stdout.count("drain supervisor is live") == 2
    assert hb.read_text().split()[0].isdigit(), "heartbeat stamped <pid> <epoch>"


def test_loop_exits_after_idle_ticks_when_drain_off(tmp_path: Path) -> None:
    parts = [_drain_pattern_stub(tmp_path, ""), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3", "torn down after exactly the idle grace"
    assert "drain supervisor is" not in result.stdout, "no tick ran while the drain was off"


def test_loop_live_tick_resets_idle_counter(tmp_path: Path) -> None:
    # live, off, off, live, then off to exhaustion → the mid-pattern live tick resets the idle
    # counter, so the loop survives to tick 7 and ticked twice. Would exit at tick 4 otherwise.
    parts = [_drain_pattern_stub(tmp_path, "LOOL"), _LOOP_ENV, "_wd_loop"]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "7"
    assert result.stdout.count("drain supervisor is live") == 2


def test_loop_reexecs_when_source_changed(tmp_path: Path) -> None:
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        "_wd_source_hash() { echo CHANGED; }",
        "_wd_reexec() { echo REEXEC; exit 0; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" in result.stdout


def test_loop_no_reexec_on_empty_hash(tmp_path: Path) -> None:
    # A transient hasher failure returns '' — must NOT be read as a change (that would
    # spuriously re-exec and re-stamp an empty baseline).
    parts = [
        _drain_pattern_stub(tmp_path, "LL"),
        _LOOP_ENV,
        "_wd_source_hash() { echo ''; }",
        "_wd_reexec() { echo REEXEC; exit 1; }",
        '_wd_loop "BASELINE"',
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "REEXEC" not in result.stdout


# ── the daemon singleton ───────────────────────────────────────────────────────


def test_daemon_refuses_second_start_while_pid_alive(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'printf "%s" "$$" > "{pidfile}"',  # a LIVE pid (our own shell)
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr
    assert not (tmp_path / "ticks").exists(), "the loop never ticked"
    assert pidfile.exists(), "the other daemon's pidfile is left intact"


def test_daemon_reclaims_stale_pidfile_and_removes_own_on_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, ""),  # all-off → 3 ticks then teardown
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        f'sleep 0.01 & _dead=$!; wait "$_dead"; printf "%s" "$_dead" > "{pidfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3"
    assert not pidfile.exists(), "the daemon removes its own pidfile on exit"


def test_daemon_appends_loop_output_to_logfile(tmp_path: Path) -> None:
    logfile = tmp_path / "wd.log"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{tmp_path / "wd.pid"}"',
        f'export HUB_WATCHDOG_LOG="{logfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "drain supervisor is live" in logfile.read_text()
    assert "drain supervisor is live" not in result.stdout, (
        "loop output goes to the log, not stdout"
    )


# ── singleton recycle of a live but STALE-GENERATION daemon (issue #296 AC2) ───
# #296 mechanism 2: a self-update redeploy resumes a FRESH copy, but its arm was singleton-
# guarded by liveness ALONE — a live pidfile always refused, even when the fresh copy can prove
# (via the recorded generation stamp) that the live daemon is running code an intervening land
# already replaced. These pin the recycle-instead-of-refuse path; a genfile the arming daemon
# never wrote (an old daemon that predates this stamp, or a genuinely current one) is
# unmeasurable and must NOT be touched — the existing refuse tests above cover that fail-safe.


def test_daemon_recycles_a_live_daemon_running_a_stale_generation(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    genfile = tmp_path / "wd.gen"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_GENFILE="{genfile}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        "export HUB_WATCHDOG_RECYCLE_GRACE=2",
        "_wd_source_hash() { echo FRESH; }",
        f'printf "%s" "STALE" > "{genfile}"',
        f'sleep 20 & printf "%s" "$!" > "{pidfile}"',
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" not in result.stdout + result.stderr, (
        "a live daemon PROVEN stale (recorded gen != current origin hash) must be recycled, "
        "not deferred to forever"
    )
    # "L" then off-to-exhaustion: one live tick + 3 idle ticks (the default idle grace) before
    # the loop tears itself down — same shape as test_daemon_reclaims_stale_pidfile's "3".
    assert (tmp_path / "ticks").read_text() == "4", "the reclaiming daemon actually ran the loop"
    assert genfile.read_text() == "FRESH", "the new daemon stamps its own (current) generation"


def test_daemon_leaves_a_live_daemon_alone_when_generation_is_unmeasurable(tmp_path: Path) -> None:
    # No genfile at all (an old daemon that predates the stamp, or the first arm ever) — must
    # NOT be treated as stale by default; that would let a fresh arm attempt kill a daemon it
    # has no positive proof is behind. Sibling of test_daemon_refuses_second_start_while_pid_alive,
    # using our own pid (like that test does) rather than a background job — the fix must never
    # reach for `kill` on this path at all when the generation is unmeasurable.
    pidfile = tmp_path / "wd.pid"
    parts = [
        _drain_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_GENFILE="{tmp_path / "absent.gen"}"',
        f'export HUB_WATCHDOG_LOG="{tmp_path / "wd.log"}"',
        "_wd_source_hash() { echo FRESH; }",
        f'printf "%s" "$$" > "{pidfile}"',  # a LIVE pid (our own shell)
        "_wd_daemon",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr
    assert not (tmp_path / "ticks").exists(), "the loop never ticked — the daemon refused"
    assert pidfile.exists(), "the other (unmeasurable) daemon's pidfile is left intact"


# ── the --arm entry point's OWN singleton guard (issue #296 AC2, real path) ────
# _wd_arm (not _wd_daemon) is what the drain actually calls every tick via `hub-watchdog.sh
# --arm`. It used to run its own independent liveness-only refusal that returned before ever
# reaching _wd_daemon's new stale-recycle logic — so mechanism 2 had zero coverage through its
# real entry point. These exercise _wd_arm directly, standing in _WD_SELF with a lightweight
# stub (never the real script) so a "relaunch happened" assertion doesn't require running an
# actual daemon loop.


def test_arm_refuses_when_a_live_daemon_generation_is_unmeasurable(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    marker = tmp_path / "relaunched"
    stub = tmp_path / "stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n')
    stub.chmod(0o755)
    parts = [
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_GENFILE="{tmp_path / "absent.gen"}"',
        f'_WD_SELF="{stub}"',
        f'printf "%s" "$$" > "{pidfile}"',  # a LIVE pid (our own shell)
        "_wd_arm",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already armed" in result.stdout + result.stderr
    assert not marker.exists(), "a live, unmeasurable-generation daemon must not be relaunched"


def test_arm_recycles_a_live_daemon_running_a_stale_generation(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    genfile = tmp_path / "wd.gen"
    marker = tmp_path / "relaunched"
    stub = tmp_path / "stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\ntouch "{marker}"\n')
    stub.chmod(0o755)
    parts = [
        f'export HUB_WATCHDOG_PIDFILE="{pidfile}"',
        f'export HUB_WATCHDOG_GENFILE="{genfile}"',
        "export HUB_WATCHDOG_RECYCLE_GRACE=2",
        f'_WD_SELF="{stub}"',
        "_wd_source_hash() { echo FRESH; }",
        f'printf "%s" "STALE" > "{genfile}"',
        f'sleep 20 & printf "%s" "$!" > "{pidfile}"',
        "_wd_arm",
    ]

    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already armed" not in result.stdout + result.stderr, (
        "a live daemon PROVEN stale must be recycled, not deferred to forever"
    )
    # nohup backgrounds the relaunch; give the (near-instant) stub a beat to run.
    for _ in range(20):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "the recycled slot must be relaunched with a fresh daemon"


# ── the self-recycle source bundle (issue #296) ────────────────────────────────
# A daemon armed by the drain EXECUTES from hub-afk.sh's frozen self-copy (/tmp/hub-afk-self.*/)
# — a bundle no land ever rewrites. Hashing THAT is structurally dead: the stamp can never move,
# so the #251/#190 contract ("a land of the watchdog's own code re-execs it live") never fires
# and a stale daemon patrols for the rest of the window, false-firing detectors the landed code
# already fixed (#296, the pre-#283 daemon on #278). The daemon must hash the ORIGIN bundle (the
# real checkout, handed over as HUB_WATCHDOG_ORIG_SCRIPT) while still executing from the copy.

_FRESH_STUB = '#!/usr/bin/env bash\necho "FRESH DAEMON argv=$* self=$0"\n'
_STALE_STUB = '#!/usr/bin/env bash\necho "STALE DAEMON self=$0"\n'


def _bundle(tmp_path: Path, *, origin_body: str, copy_body: str) -> dict[str, str]:
    """An origin checkout + a frozen self-copy of it, as the drain's arm leaves them.

    Returns the env pinning a daemon that executes from the copy and (post-#296) hashes the
    origin: HUB_WATCHDOG_SCRIPT_DIR is the frozen copy dir (so _WD_SELF resolves there, exactly
    as `_afk_find_script` leaves it), HUB_WATCHDOG_ORIG_SCRIPT the real checkout path. The
    wt-lib pin keeps wt_source_hash resolvable from a tmp copy dir that has no sibling ladder.
    """
    origin = tmp_path / "origin"
    copy = tmp_path / "copy"
    origin.mkdir()
    copy.mkdir()
    (origin / "hub-watchdog.sh").write_text(origin_body)
    (copy / "hub-watchdog.sh").write_text(copy_body)
    return {
        "HUB_WATCHDOG_SCRIPT_DIR": str(copy),
        "HUB_WATCHDOG_ORIG_SCRIPT": str(origin / "hub-watchdog.sh"),
        "HUB_WATCHDOG_WT_LIB": str(WT_LIB),
    }


def test_source_hash_tracks_the_origin_bundle(tmp_path: Path) -> None:
    env = _bundle(tmp_path, origin_body="# origin v1\n", copy_body="# frozen copy\n")

    before = _call("_wd_source_hash", env=env)
    (tmp_path / "origin" / "hub-watchdog.sh").write_text("# origin v2 — a land\n")
    after = _call("_wd_source_hash", env=env)

    assert before.stdout.strip(), before.stderr
    assert after.stdout.strip() != before.stdout.strip(), (
        "a land rewriting the ORIGIN bundle must move the stamp the self-recycle watches"
    )


def test_source_hash_ignores_the_frozen_copy(tmp_path: Path) -> None:
    # The other half of the pin: the frozen copy is not what a land rewrites, so it must not be
    # what the stamp is taken over. Hashing it is what made the recycle structurally dead.
    env = _bundle(tmp_path, origin_body="# origin v1\n", copy_body="# frozen copy\n")

    before = _call("_wd_source_hash", env=env)
    (tmp_path / "copy" / "hub-watchdog.sh").write_text("# the copy, scribbled on\n")
    after = _call("_wd_source_hash", env=env)

    assert before.stdout.strip(), before.stderr
    assert after.stdout.strip() == before.stdout.strip(), (
        "the frozen copy must not pin (nor move) the self-recycle stamp"
    )


def test_reexec_runs_the_landed_code_from_a_fresh_copy(tmp_path: Path) -> None:
    # The stub origin really is exec'd, so this reads which generation the daemon lands on and
    # where it runs from — no `exec` stubbing, the one thing that would hide the bug.
    env = _bundle(tmp_path, origin_body=_FRESH_STUB, copy_body=_STALE_STUB)

    result = _call("_wd_reexec", env=env)

    assert "FRESH DAEMON" in result.stdout, f"the re-exec must land on ORIGIN code: {result.stdout}"
    assert "STALE DAEMON" not in result.stdout, (
        "re-exec'ing the frozen copy re-runs the same stale code with a fresh baseline — the "
        "silent no-op that leaves a landed fix inert"
    )
    assert "argv=--daemon --reexec" in result.stdout, "the fresh daemon reclaims its own pidfile"
    ran = result.stdout.split("self=")[1].strip()
    assert ran != env["HUB_WATCHDOG_ORIG_SCRIPT"], (
        "must exec a FRESH COPY of the origin, never the rewritable checkout file itself (#133)"
    )
    assert Path(ran).name == "hub-watchdog.sh", ran


def test_reexec_refuses_a_parse_broken_landed_bundle(tmp_path: Path) -> None:
    # A DEAD watchdog is worse than a stale one, so a land shipping unparseable code keeps the
    # current daemon running. The `bash -n` guard only protects anything once the files it reads
    # are the ORIGIN's — over the frozen copy it would parse-check code no land can break.
    env = _bundle(tmp_path, origin_body="if [ ; then\n", copy_body=_STALE_STUB)

    result = _call("_wd_reexec; echo STILL-RUNNING", env=env)

    assert "fails to parse" in result.stdout, result.stdout
    assert "STILL-RUNNING" in result.stdout, "a parse-broken land must not kill the daemon"
    assert "STALE DAEMON" not in result.stdout, "and must not exec the frozen copy either"


# ── CLI dispatch ───────────────────────────────────────────────────────────────


def test_cli_status_reports_drain_and_watchdog_state(tmp_path: Path) -> None:
    state = tmp_path / "afk-state"
    state.write_text("drain\n")
    hb = tmp_path / "afk-hb"
    hb.write_text(f"{os.getpid()} 1783879781\n")

    result = _run(
        "--status",
        env={
            "AFK_STATE": str(state),
            "AFK_HEARTBEAT": str(hb),
            "HUB_WATCHDOG_PIDFILE": str(tmp_path / "absent.pid"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "drain=live" in result.stdout
    assert "watchdog=off" in result.stdout


def test_cli_daemon_dispatch_reports_already_running(tmp_path: Path) -> None:
    pidfile = tmp_path / "wd.pid"
    pidfile.write_text(str(os.getpid()))

    result = _run(
        "--daemon",
        env={"HUB_WATCHDOG_PIDFILE": str(pidfile), "HUB_WATCHDOG_LOG": str(tmp_path / "wd.log")},
    )

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr


def test_cli_once_stamps_heartbeat_and_ticks(tmp_path: Path) -> None:
    hb = tmp_path / "wd-hb"
    result = _run(
        "--once",
        env={"AFK_STATE": str(tmp_path / "absent"), "HUB_WATCHDOG_HEARTBEAT": str(hb)},
    )

    assert result.returncode == 0, result.stderr
    assert "tick: drain supervisor is off" in result.stdout
    fields = hb.read_text().split()
    assert len(fields) == 2 and fields[0].isdigit(), f"heartbeat is '<pid> <epoch>': {fields}"


# ── the 5 detectors + interventions (issue #251, subtask 3) ───────────────────
# The daemon skeleton observes; these detectors decide WHEN the drain fell short and fire the
# scripted intervention + a defect record. Detectors are pure predicates over the drain's own
# state readers (slot_state / read_answer_attempt / read_progress_epoch / _spoke_pane_target),
# stubbed inline here; the git-marker detectors run against a throwaway repo. `now` is pinned
# via AFK_NOW so the grace-margin arithmetic is deterministic.

NOW = "1783880000"


def _git_repo(tmp_path: Path, name: str = "wt") -> Path:
    wt = tmp_path / name
    wt.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "c1"]):
        subprocess.run(cmd, cwd=wt, check=True, env=env, capture_output=True)
    return wt


def _detect(
    prelude: str, call: str, *, env: dict[str, str] | None = None, state_dir: Path | None = None
) -> int:
    """Run a detector with the drain readers stubbed by `prelude`; return its rc.

    `state_dir` pins AFK_STATE_DIR: the park-unanswered detector notes the park EPISODE (a real
    write) before measuring, so without it a detector test would stamp the LIVE hub state dir.
    """
    e = {"AFK_NOW": NOW}
    if state_dir is not None:
        e["AFK_STATE_DIR"] = str(state_dir)
    if env:
        e.update(env)
    return _call(f"{prelude}; {call}", env=e).returncode


# Condition 1 — park unanswered
# AC1/AC2 (#265): the never-attempted branch measures against PARK ONSET, not zero. The
# answer-attempt epoch is stamped only at answer DELIVERY (minutes into the answerer's run), so
# a zero-grace floor false-fired 1s after every fresh park. A freshly parked spoke with no
# attempt stays quiet; only once the park itself outlives the ceiling may it fire.
# The lane stub (#283): the ceiling applies to ANSWER-lane parks only, so every ceiling-arithmetic
# test below declares one. The lane itself is pinned separately, against real probes.
_GATE_LANE = "_wd_park_lane() { echo gate; }"


def test_park_unanswered_quiet_when_fresh_park_never_attempted(tmp_path: Path) -> None:
    fresh = str(int(NOW) - 60)  # parked 60s ago (< 600s ceiling)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {fresh}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_fires_when_park_onset_stale_never_attempted(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)  # parked > 600s ago, still no answer → a real shortfall
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 0


def test_park_unanswered_fires_when_attempt_is_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)  # > 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 0


def test_park_unanswered_quiet_when_attempt_is_fresh(tmp_path: Path) -> None:
    fresh = str(int(NOW) - 60)  # < 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        f"read_answer_attempt() {{ echo {fresh}; }}; read_park_onset_epoch() {{ echo {fresh}; }}"
    )
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_quiet_when_not_waiting(tmp_path: Path) -> None:
    prelude = f'{_GATE_LANE}; slot_state() {{ echo busy; }}; read_answer_attempt() {{ echo ""; }}'
    assert _detect(prelude, "_wd_detect_park_unanswered /wt 5 " + NOW, state_dir=tmp_path) == 1


def test_park_unanswered_end_to_end_real_stamp_feeds_real_detector(tmp_path: Path) -> None:
    # End-to-end (no stubbed epoch reader): the REAL slot_state stamps park-onset on a gate park,
    # and the REAL detector reads it back — a one-sided rename of the park-onset file would make
    # the read miss and this fire fail. First tick stamps onset at an old AFK_NOW; a later tick
    # past the ceiling, with no answer ever delivered, fires the never-attempted branch.
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    statedir = tmp_path / "afk-state"
    old = str(int(NOW) - 700)  # onset 700s before NOW (> 600s ceiling)

    # Tick 1: real slot_state reads the gate park as waiting and stamps park-onset-5.epoch=old.
    stamp = _call(f"slot_state '{wt}' 5", env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": old})
    assert stamp.stdout.strip() == "waiting", stamp.stdout + stamp.stderr
    assert (statedir / "park-onset-5.epoch").read_text().strip() == old

    # Tick 2: the real detector (no read_park_onset_epoch stub) reads that onset and fires.
    fire = _call(
        f"_wd_detect_park_unanswered '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(statedir), "AFK_NOW": NOW},
    )
    assert fire.returncode == 0, fire.stdout + fire.stderr


# AC3 (#265): the firing reason reports the MEASURED age + which branch fired — never the
# constant "> 600s" that made a 1-second-old park's ledger + auto-filed defect claim a 600s
# breach. The never-attempted branch measures from park onset; the stale-attempt branch from the
# last delivery.
def test_park_unanswered_reason_never_attempted_reports_onset_age(tmp_path: Path) -> None:
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; read_answer_attempt() {{ echo ''; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(f"{prelude}; _wd_park_unanswered_reason /wt 5 {NOW}").stdout
    assert "never-attempted" in out
    assert "700s" in out  # the measured park age, not a constant ceiling


def test_park_unanswered_reason_stale_attempt_reports_delivery_age(tmp_path: Path) -> None:
    old = str(int(NOW) - 900)
    prelude = (
        f"{_GATE_LANE}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(f"{prelude}; _wd_park_unanswered_reason /wt 5 {NOW}").stdout
    assert "stale-attempt" in out
    assert "900s" in out


# ── issue #283: the ceiling measures the CURRENT park episode, not the last delivery ──────────
# The #276 false-fire: a spoke answered ONE plan gate, then worked productively for ten minutes
# under normal tier-3 judge traffic. The detector gated on `slot_state == waiting` (true for ANY
# park, including the permission dialogs the BROKER owns) and then measured from
# answer-attempt-<issue>.epoch — an epoch that ages monotonically while the spoke works. At the
# tick, the "park" was a transient permission dialog the broker cleared 12s later.


def _tag(wt: Path, name: str) -> None:
    subprocess.run(["git", "tag", "-f", name], cwd=wt, check=True, capture_output=True)


def _journal(state_dir: Path, issue: str, ts: int, park: str = "permission") -> None:
    """Append one broker decision-journal record — the drain's own 'I serviced this' evidence."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "decision-journal.jsonl").write_text(
        f'{{"ts":{ts},"issue":"{issue}","park":"{park}",'
        f'"decision":"hook auto-approved: pytest","reversibility":"reversible"}}\n'
    )


# The lane probe — structural, in _broker_park_signature's precedence order. NOT derived from the
# signature's output: a gate-tagged park whose plan artifact is unreadable hashes to EMPTY, which
# would read as `unknown` and (under "unknown never fires") silence #265's never-attempted branch.
def test_park_lane_reads_a_permission_dialog_first(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")  # a gate tag is ALSO at the tip — permission still wins, as the broker does
    out = _call(f"_permission_pending() {{ return 0; }}; _wd_park_lane '{wt}' 5").stdout
    assert out.strip() == "permission"


def test_park_lane_reads_a_gate_tag_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    out = _call(f"_permission_pending() {{ return 1; }}; _wd_park_lane '{wt}' 5").stdout
    assert out.strip() == "gate"


def test_park_lane_reads_a_pending_question(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    prelude = (
        "_permission_pending() { return 1; }; extract_pending_question() { echo 'which one?'; }"
    )
    assert _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip() == "question"


def test_park_lane_unknown_when_nothing_is_extractable(tmp_path: Path) -> None:
    # slot_state's `waiting` derives from these same three probes, so waiting-with-no-lane means
    # the park resolved between the two calls — a race, not a strand. The detector stays quiet.
    wt = _git_repo(tmp_path)
    prelude = "_permission_pending() { return 1; }; extract_pending_question() { echo ''; }"
    assert _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip() == "unknown"


def test_park_lane_gate_survives_an_unreadable_plan_artifact(tmp_path: Path) -> None:
    # The lane must NOT be read off _broker_park_signature: a gate tag with no readable artifact
    # hashes to empty. _gate_parked is a tag-at-tip check, immune to artifact readability.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    prelude = (
        "_permission_pending() { return 1; }; _read_gate_artifact() { echo ''; }; "
        "extract_pending_question() { echo ''; }"
    )
    sig = _call(f"{prelude}; _broker_park_signature '{wt}' 5").stdout.strip()
    lane = _call(f"{prelude}; _wd_park_lane '{wt}' 5").stdout.strip()
    assert sig == "", "precondition: this park's signature is genuinely unextractable"
    assert lane == "gate", "the lane comes from the tag at the tip, not from the signature"


def test_park_unanswered_fires_on_a_gate_park_with_an_unreadable_artifact(tmp_path: Path) -> None:
    # #265's never-attempted strand must keep firing through the new lane gate: a real gate park,
    # no delivery ever, onset past the ceiling, and nothing servicing it.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"_read_gate_artifact() {{ echo ''; }}; extract_pending_question() {{ echo ''; }}; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 0, "a genuinely stranded gate park still fires"


def test_park_unanswered_quiet_when_the_park_is_a_permission_dialog(tmp_path: Path) -> None:
    # The answer ceiling is the ANSWER lane's. Permission dialogs are the broker's lane, with its
    # own timers — the watchdog must never answer them (#271).
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 0; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1


def test_last_decision_ts_reads_a_record_written_by_the_real_journal_writer(
    tmp_path: Path,
) -> None:
    # End-to-end across the module boundary: the REAL _broker_journal_line writes the record and
    # the REAL extractor reads it back. _wd_last_decision_ts parses the journal's field ORDER (ts
    # first), so a writer-side reorder would otherwise silently return empty for every issue —
    # killing the servicing suppression and resurrecting the #276 false-fire with no test failure.
    sd = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW}
    _call('_broker_journal_line 5 permission "hook auto-approved: pytest" reversible', env=env)

    out = _call("_wd_last_decision_ts 5", env=env).stdout

    assert out.strip() == NOW, "the extractor must track the real journal writer's format"


def test_last_decision_ts_does_not_confuse_a_longer_issue_number(tmp_path: Path) -> None:
    sd = tmp_path / "sd"
    env = {"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW}
    _call('_broker_journal_line 15 permission "approved" reversible', env=env)

    assert _call("_wd_last_decision_ts 5", env=env).stdout.strip() == "", (
        "issue 15's record must not read as servicing for issue 5"
    )


def test_park_unanswered_quiet_when_the_drain_decided_recently(tmp_path: Path) -> None:
    # AC1: a broker decision for this issue since the delivery is proof the drain is SERVICING the
    # spoke. "Being handled" and "abandoned" must be distinguishable.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    _journal(sd, "5", int(NOW) - 12)  # the broker approved a tier-3 command 12s ago
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_unanswered_quiet_when_progress_advanced_recently(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    old = str(int(NOW) - 700)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}; "
        f"read_progress_epoch() {{ echo {int(NOW) - 30}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1, "a progress-epoch advance is servicing evidence too"


def test_park_unanswered_fires_when_the_delivery_is_genuinely_stranded(tmp_path: Path) -> None:
    # AC2: answer delivered, the SAME park persists past the ceiling, and nothing has touched the
    # issue since — no broker decisions, no progress. That is a real shortfall and must fire.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    old = int(NOW) - 700
    _journal(sd, "5", old, park="answer")  # the delivery's OWN record — not evidence of servicing
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0


def test_park_unanswered_ignores_a_delivery_that_predates_the_current_episode(
    tmp_path: Path,
) -> None:
    # An answer delivered BEFORE the current park began cannot count against it: the base is the
    # episode onset, so a fresh episode is quiet however old the last lifetime delivery is.
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 1; }}; "
        f"read_answer_attempt() {{ echo {int(NOW) - 5000}; }}; "
        f"read_park_onset_epoch() {{ echo {int(NOW) - 60}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=tmp_path / "sd")
    assert rc == 1, "the episode began 60s ago; a 5000s-old delivery belongs to a past park"


def test_park_unanswered_quiet_on_the_276_replay(tmp_path: Path) -> None:
    # The whole #276 shape end to end: the gate was answered at T0, the broker has been deciding
    # for the issue since (one 12s before this tick), a transient permission dialog is pending at
    # the tick, and the answer is 608s old — just past the 600s ceiling. It must NOT fire.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    t0 = int(NOW) - 608
    _journal(sd, "5", int(NOW) - 12)
    prelude = (
        f"slot_state() {{ echo waiting; }}; _permission_pending() {{ return 0; }}; "
        f"read_answer_attempt() {{ echo {t0}; }}; read_park_onset_epoch() {{ echo {t0 - 225}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1, "a healthy, actively-brokered spoke is not an afk defect"


# AC5: the ledger line must carry the MEASURED base — which epoch, the episode, the lane — so a
# future false positive is diagnosable from the ledger alone, without re-deriving the timeline.
def test_park_unanswered_reason_names_the_base_episode_and_lane(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    _tag(wt, "gate/5")
    sd = tmp_path / "sd"
    sd.mkdir()
    old = int(NOW) - 900
    (sd / "park-sig-5").write_text("deadbeef\tabc123def456\n")
    prelude = (
        f"_permission_pending() {{ return 1; }}; read_answer_attempt() {{ echo {old}; }}; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(
        f"{prelude}; _wd_park_unanswered_reason '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW},
    ).stdout

    assert "stale-attempt" in out and "900s" in out
    assert f"answer-attempt@{old}" in out, "names WHICH epoch was measured, and its value"
    assert "abc123de" in out, "names the current park episode's signature"
    assert "lane=gate" in out


def _head(wt: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True, check=True
    ).stdout.strip()


# ── issue #288 AC2: never-attempted suppression when the drain HAS attempted + is paced ────────
# answer-attempt-<issue>.epoch (stamped only at DELIVERY) is blind to every pre-inject drop path.
# Two records the re-answer-ceiling code ALREADY writes prove otherwise: reanswer-<issue> (an
# attempt genuinely ran on the CURRENT (tip, sig)) and an armed, not-yet-due warned-retry backoff
# (the drain is paced to retry, not abandoned). Together, firing "never-attempted" would be a lie.


def test_park_unanswered_never_attempted_suppressed_when_attempted_and_backing_off(
    tmp_path: Path,
) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "reanswer-5").write_text(f"{_head(wt)}\tsigA\t1\n")
    (sd / "warned-state-5").write_text(f"1\t{int(NOW) + 300}\n")  # armed, not due for 300s
    old = str(int(NOW) - 700)  # past the 600s ceiling
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1, "an attempted + backing-off park must not fire under the never-attempted label"


def test_park_unanswered_genuinely_unserviced_still_fires(tmp_path: Path) -> None:
    # AC2's counter-case: no reanswer record at all -> the suppression must not engage.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0, "a genuinely untouched park must still fire"


def test_park_unanswered_fires_when_attempted_but_backoff_already_due(tmp_path: Path) -> None:
    # A reanswer record alone is not enough: the retry must still be PENDING (not yet due) or the
    # drain has simply stopped, and the fire must go through.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "reanswer-5").write_text(f"{_head(wt)}\tsigA\t1\n")
    (sd / "warned-state-5").write_text(f"1\t{int(NOW) - 10}\n")  # already due
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0, (
        "a backoff that is already due is not 'paced to retry' — the fire must go through"
    )


# ── issue #288 AC3: park-undeliverable — serviced but never deliverable ────────────────────────
# A never-attempted park with a DROP on record (answers were computed but every one was dropped
# before injection) surfaces under its own honest reason instead of the misleading
# "never-attempted" label — and instead of silence, the other #277 failure mode.


def test_park_undeliverable_fires_when_drops_are_on_record(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(
        f"{_head(wt)}\tsigA\t3\tno longer parked on that prompt (spoke moved on)\n"
    )
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 0


def test_park_undeliverable_quiet_without_a_drop_record(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo ''; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_undeliverable_quiet_when_a_delivery_already_landed_in_episode(tmp_path: Path) -> None:
    # A delivery inside the current episode is the stale-attempt branch's turf, not this one.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t1\tstale tree\n")
    old = str(int(NOW) - 700)
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_answer_attempt() {{ echo {old}; }}; read_park_onset_epoch() {{ echo {old}; }}"
    )
    rc = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc == 1


def test_park_undeliverable_reason_names_the_count_and_last_verdict(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(
        f"{_head(wt)}\tsigA\t3\tno longer parked on that prompt (spoke moved on)\n"
    )
    old = int(NOW) - 700
    prelude = (
        "_broker_park_signature() { printf '%s' 'sigA'; }; "
        f"read_park_onset_epoch() {{ echo {old}; }}"
    )
    out = _call(
        f"{prelude}; _wd_park_undeliverable_reason '{wt}' 5 {NOW}",
        env={"AFK_STATE_DIR": str(sd), "AFK_NOW": NOW},
    ).stdout

    assert "park-undeliverable" in out
    assert "3" in out
    assert "no longer parked on that prompt (spoke moved on)" in out
    assert "700s" in out


def test_park_undeliverable_refreshes_a_stale_onset_before_checking_delivery(
    tmp_path: Path,
) -> None:
    # #288 review: _wd_detect_park_undeliverable must refresh park-onset (via _wd_park_base's
    # note_park_episode) BEFORE checking _wd_park_attempt_in_episode. A resolved prior episode
    # (A) left a stale onset + delivery on file; a NEW episode (B, a different signature) starts
    # with a fresh drop. Tick 1 (the transition) must refresh the onset and stay quiet (too
    # fresh); tick 2, once B itself ages past the ceiling, must fire — not stay silently masked
    # by A's stale delivery epoch, which a pre-fix direct read would still be comparing against.
    wt = _git_repo(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    t1 = int(NOW) - 700
    old_onset = t1 - 500  # episode A resolved well before B began
    (sd / "park-onset-5.epoch").write_text(f"{old_onset}\n")
    (sd / "park-sig-5").write_text(f"{_head(wt)}\told-sig\n")
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigB\t1\tspoke moved on\n")
    prelude = (
        f"{_GATE_LANE}; slot_state() {{ echo waiting; }}; "
        "_broker_park_signature() { printf '%s' 'sigB'; }; "
        f"read_answer_attempt() {{ echo {old_onset}; }}"
    )

    rc1 = _detect(
        prelude,
        f"_wd_detect_park_undeliverable '{wt}' 5 {t1}",
        env={"AFK_NOW": str(t1)},
        state_dir=sd,
    )
    assert rc1 == 1, "a just-transitioned episode must not fire before it ages"
    assert (sd / "park-onset-5.epoch").read_text().strip() == str(t1), (
        "the onset must be refreshed to the NEW episode's start, not left at episode A's"
    )

    rc2 = _detect(prelude, f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", state_dir=sd)
    assert rc2 == 0, "the fresh episode must fire once it ages past the ceiling, not stay masked"


def test_run_conditions_fires_park_undeliverable_instead_of_park_unanswered(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t2\tspoke moved on\n")
    onset = str(int(NOW) - 700)
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(sd),
        "AFK_NOW": NOW,
    }
    prelude = (
        f'inflight_worktrees() {{ printf "{wt}\\t5\\n"; }}; '
        f"{_GATE_LANE}; _broker_park_signature() {{ printf '%s' 'sigA'; }}; "
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    line = ledger.read_text()
    assert '"condition":"park-undeliverable"' in line
    assert '"condition":"park-unanswered"' not in line, (
        "a serviced-but-undeliverable park must not ALSO fire the misleading never-attempted label"
    )


def test_classify_park_undeliverable_defaults_to_afk_defect() -> None:
    assert _call("_wd_classify park-undeliverable 5").stdout.strip() == "afk-defect"


# ── issue #288 AC1: the #204 self-heal must end the park episode there, not at a lucky tick ────
def test_self_heal_clears_park_onset_so_a_following_tick_is_quiet(tmp_path: Path) -> None:
    # The #277 shape: a gate park, >=2 computed-then-dropped answer passes (recorded on
    # answer-drop-5, no injection landed), then an outside-broker typed approval lands. The
    # self-heal must clear park-onset right there so a watchdog tick immediately after — before
    # any lucky slot_state tick would otherwise have observed "not parked" — stays quiet.
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    sd = tmp_path / "sd"
    sd.mkdir()
    old = str(int(NOW) - 700)
    (sd / "park-onset-5.epoch").write_text(old + "\n")
    (sd / "answer-drop-5").write_text(f"{_head(wt)}\tsigA\t2\tno longer parked (spoke moved on)\n")
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, wt)
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "session.jsonl").write_text(_resumed_gate_transcript("stale PLAN prose"))
    env = {"AFK_STATE_DIR": str(sd), "CLAUDE_PROJECTS_DIR": str(projects), "AFK_NOW": NOW}

    heal = _call(f"broker_service_gate '{wt}' 5 unattended", env=env)
    assert heal.returncode == 0, heal.stderr
    assert not (sd / "park-onset-5.epoch").exists(), "the self-heal must clear the resolved onset"

    quiet_unanswered = _call(f"_wd_detect_park_unanswered '{wt}' 5 {NOW}", env=env)
    assert quiet_unanswered.returncode == 1, "a tick right after the self-heal must not fire"
    quiet_undeliverable = _call(f"_wd_detect_park_undeliverable '{wt}' 5 {NOW}", env=env)
    assert quiet_undeliverable.returncode == 1


# Condition 2 — dead / idle pane
# The #290 guards read the drain state, the last-action record and the drain state dir, so every
# condition-2 test pins all three off the LIVE host hub: unpinned, a real armed drain on this host
# would make `_wd_drain_state` read `live` and a stray done-<issue>.epoch would suppress a fire.
def _dead_pane_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "AFK_NOW": NOW,
        "AFK_STATE": str(tmp_path / "absent-afk-state"),  # no drain armed ⇒ _wd_drain_state = off
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_LAST_ACTION": str(tmp_path / "absent-last-action"),
    }
    env.update(extra)
    return env


_NO_DONE_EPOCH = 'read_done_epoch() { echo ""; }'


def test_dead_idle_fires_when_pane_dead_and_progress_stale(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)  # > 3600s ceiling
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo busy; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 0


def test_dead_idle_quiet_when_pane_alive(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo "hub:0"; }; slot_state() { echo busy; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 1


def test_dead_idle_quiet_when_done(tmp_path: Path) -> None:
    old = str(int(NOW) - 4000)
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo done; }; '
        f"{_NO_DONE_EPOCH}; read_progress_epoch() {{ echo {old}; }}"
    )
    assert _detect(prelude, "_wd_detect_dead_idle /wt 5 " + NOW, env=_dead_pane_env(tmp_path)) == 1


# ── #290: condition 2 is blind to a successful land's teardown window ──────────
# On #284 the watchdog fired "reaper missed a dead/idle pane" while auto_land was 13 minutes into
# the land's push gate and seconds from removing the worktree. The land had already consumed the
# ready/284 tag (so LIVE slot_state stopped reading `done`) and killed the tmux window, leaving
# exactly the detector's firing shape on a spoke that was landing successfully. Two facts each
# suppress it: the durable done-<N>.epoch (a terminal classification, checked in the detector), and
# a land running RIGHT NOW (a servicing defer, gated in the dispatcher so it cannot clear the
# firing-dedup marker mid-land — the #263 double-count hazard).
_LAND_IN_FLIGHT_284 = "_wd_land_in_flight 284 " + NOW


def test_land_in_flight_when_a_live_drain_is_landing_this_issue(tmp_path: Path) -> None:
    # auto_land stamps `land #<issue>` BEFORE the synchronous land, so it names the issue for the
    # land's whole duration — the primary signal.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    result = _call(f"_wd_drain_state() {{ echo live; }}; {_LAND_IN_FLIGHT_284}", env=env)

    assert result.returncode == 0, result.stderr


def test_land_in_flight_false_when_the_drain_is_landing_another_issue(tmp_path: Path) -> None:
    last_action = tmp_path / "last-action"
    last_action.write_text("land #999\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    assert (
        _call(f"_wd_drain_state() {{ echo live; }}; {_LAND_IN_FLIGHT_284}", env=env).returncode == 1
    )


def test_land_in_flight_false_when_the_drain_crashed_mid_land(tmp_path: Path) -> None:
    # A stale last-action outlives a crashed drain: the record still says `land #284` but nothing is
    # running it. Only a LIVE drain counts — else a crash would silence condition 2 forever.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))

    assert (
        _call(f"_wd_drain_state() {{ echo stale; }}; {_LAND_IN_FLIGHT_284}", env=env).returncode
        == 1
    )


def _land_log(tmp_path: Path, issue: str, age: int) -> Path:
    """Write <state-dir>/land-<issue>.log with an mtime `age` seconds before NOW."""
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / f"land-{issue}.log"
    log.write_text("running the push gate...\n")
    os.utime(log, (int(NOW) - age, int(NOW) - age))
    return log


def test_land_in_flight_when_the_land_log_is_fresh(tmp_path: Path) -> None:
    # The fallback signal for a clobbered/stale last-action: the land's own log is still being
    # written. The drain is off here, so ONLY the log mtime can carry the verdict.
    _land_log(tmp_path, "284", age=60)

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 0


def test_land_in_flight_false_when_the_land_log_is_stale(tmp_path: Path) -> None:
    # The defer is BOUNDED, not permanent: a land log untouched past HUB_WATCHDOG_LAND_ACTIVE
    # (900s) is a finished/abandoned land, not one in flight — else a single land log on disk would
    # silence condition 2 for that issue forever.
    _land_log(tmp_path, "284", age=1000)

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_land_in_flight_false_when_the_land_log_mtime_is_in_the_future(tmp_path: Path) -> None:
    # Clock skew / a corrupted timestamp: a future mtime is unmeasurable, not fresh. Negating the
    # staleness check on it would defer until wall-clock caught up to the bogus stamp — an
    # unbounded silence, not the documented 900s window. Fail toward firing.
    _land_log(tmp_path, "284", age=-86400)  # dated a day AHEAD of NOW

    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_land_in_flight_false_when_nothing_signals_a_land(tmp_path: Path) -> None:
    assert _call(_LAND_IN_FLIGHT_284, env=_dead_pane_env(tmp_path)).returncode == 1


def test_dead_idle_quiet_when_the_done_epoch_is_stamped(tmp_path: Path) -> None:
    # AC1 (the detector half): the stamped done epoch records that the spoke reached terminal
    # state. A land consuming the ready/284 tag flips LIVE slot_state off `done` while the worktree
    # still exists — the durable epoch does not follow it, so this shape is never a reaper miss.
    # Post-done staleness is condition 4's (mergeable-skipped) ceiling.
    prelude = (
        '_spoke_pane_target() { echo ""; }; slot_state() { echo busy; }; '
        f"read_done_epoch() {{ echo {int(NOW) - 4600}; }}; "
        f"read_progress_epoch() {{ echo {int(NOW) - 4459}; }}"
    )

    assert (
        _detect(prelude, "_wd_detect_dead_idle /wt 284 " + NOW, env=_dead_pane_env(tmp_path)) == 1
    )


# The #284 replay, through the DISPATCHER — the servicing defer lives there, not in the detector.
def _dead_pane_dispatch_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = _dead_pane_env(
        tmp_path,
        HUB_WATCHDOG_LEDGER=str(tmp_path / "l.jsonl"),
        HUB_WATCHDOG_LANDMARK_REPO=str(tmp_path / "no-landmark-repo"),
        # The revive seam MUST be stubbed: the default runs `nohup claude --continue` for real.
        HUB_WATCHDOG_REVIVE_CMD=f'printf "%s" "$2" > {tmp_path / "revived"}',
    )
    env.update(extra)
    return env


def _dead_pane_dispatch_prelude(*, done_epoch: str, drain: str = "off") -> str:
    """The #284 tick shape: worktree in-flight, ready tag consumed, window killed, progress stale."""
    return (
        'inflight_worktrees() { printf "/the/wt\\t284\\n"; }; '
        f"_wd_drain_state() {{ echo {drain}; }}; "
        '_spoke_pane_target() { echo ""; }; '  # the land killed the tmux window
        "slot_state() { echo busy; }; "  # the land consumed ready/284 ⇒ no longer `done`
        f'read_done_epoch() {{ echo "{done_epoch}"; }}; '
        f"read_progress_epoch() {{ echo {int(NOW) - 4459}; }}; "
        'read_answer_attempt() { echo ""; }'
    )


def test_run_conditions_defers_dead_pane_during_a_land_teardown(tmp_path: Path) -> None:
    # AC1: the full #284 shape — done epoch stamped, land mid-flight, ready tag consumed, window
    # killed, worktree still present, progress epoch past the ceiling ⇒ NO fire and NO revive.
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path, AFK_LAST_ACTION=str(last_action))
    prelude = _dead_pane_dispatch_prelude(done_epoch=str(int(NOW) - 4600), drain="live")

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert not ledger.exists(), "a successful land's teardown is not a reaper miss"
    assert not (tmp_path / "revived").exists(), "never revive into a worktree being removed"


def test_run_conditions_defer_keeps_the_dead_pane_dedup_marker_during_a_land(
    tmp_path: Path,
) -> None:
    # The amendment's rationale (#263): the defer must NOT clear a prior firing's dedup marker.
    # Clearing it mid-land would let a subsequently-failed land re-fire and double-count in the
    # ledger — which is exactly why the defer is gated in the dispatcher rather than the detector
    # (a detector returning 1 falls into the else-branch, which clears).
    marker = tmp_path / "wd-fire-dedup-dead-pane-284"  # dir == dirname(ledger)
    marker.write_text("")
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_dispatch_env(tmp_path, AFK_LAST_ACTION=str(last_action))
    prelude = _dead_pane_dispatch_prelude(done_epoch=str(int(NOW) - 4600), drain="live")

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert marker.exists(), "a servicing defer must neither fire nor clear the firing marker"


def test_run_conditions_reads_the_done_epoch_before_slot_state_clears_it(tmp_path: Path) -> None:
    # The ordering contract, against the REAL readers (no read_done_epoch / slot_state stub — those
    # are exactly what hid this). A non-terminal slot_state read DELETES the done epoch
    # (_afk_note_tip_progress -> clear_done_epoch, deliberate per #263), and the park detectors call
    # slot_state ahead of condition 2 every tick. So a done epoch read at condition-2 time is always
    # already gone: the dispatcher must pre-read it at the top of the iteration and pass it in.
    # Here the land has consumed ready/284, so real slot_state reads non-terminal and clears the
    # epoch mid-tick — yet the spoke must still be classified terminal and NOT fire.
    wt = _git_repo(tmp_path)
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir()
    (state_dir / "done-284.epoch").write_text("1784066007\n")  # stamped at the ready transition
    (state_dir / "progress-284.epoch").write_text(str(int(NOW) - 4459) + "\n")
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    # No pane (the land killed the window); the drain is off and no land log exists, so
    # _wd_land_in_flight CANNOT defer — the done-epoch guard is the only thing that can hold.
    prelude = (
        f"inflight_worktrees() {{ printf '{wt}\\t284\\n'; }}; "
        '_wd_drain_state() { echo off; }; _spoke_pane_target() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert not (state_dir / "done-284.epoch").exists(), (
        "precondition: the real slot_state must clear the epoch mid-tick, else this proves nothing"
    )
    assert not ledger.exists(), "the done epoch must be read before slot_state destroys it"
    assert not (tmp_path / "revived").exists()


def test_run_conditions_fires_dead_pane_on_a_genuine_reaper_miss(tmp_path: Path) -> None:
    # AC2: the complement — no done epoch, no land in flight, drain not servicing the issue, pane
    # gone with progress past the ceiling. A real abandoned spoke still fires AND still revives.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert '"condition":"dead-pane"' in ledger.read_text()
    assert (tmp_path / "revived").read_text() == "284"


# ── #303 (#300 step 4): dead-pane READS the transition log ─────────────────────
# The residual #290/#301 gap: when _wd_land_in_flight cannot see the land (a clobbered
# last-action, no fresh land log, drain off), the epoch-inference still fires "reaper
# missed a dead pane" on a spoke that is landing or pushing — those are RECORDED states,
# not silence. A recorded landing|pushing state suppresses the fire structurally, and the
# suppression is logged as a DIVERGENCE (never silent, #300). unknown ⇒ fall back to the
# inference (never a firing basis alone, never a suppression basis alone).
def _write_transition(state_dir: Path, issue: int, to: str, *, ts: str = NOW) -> None:
    """Append one complete transition record the read API parses (afk_current_state/onset)."""
    d = state_dir / "transitions"
    d.mkdir(parents=True, exist_ok=True)
    line = (
        f'{{"v":1,"ts":{ts},"issue":{issue},"kind":"transition",'
        f'"to":"{to}","actor":"test","cause":"pin"}}'
    )
    (d / f"{issue}.jsonl").write_text(line + "\n")


@pytest.mark.parametrize("state", ["landing", "pushing"])
def test_run_conditions_suppresses_dead_pane_when_state_is_landing_or_pushing(
    tmp_path: Path, state: str
) -> None:
    # The genuine-reaper-miss shape (no done epoch, drain off, no land log, pane gone,
    # progress past the ceiling) — today's inference FIRES. A recorded landing|pushing
    # transition is a known multi-minute phase, so #303 suppresses the fire and the revive.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    _write_transition(tmp_path / "afk-state", 284, state)
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert not ledger.exists(), f"a recorded {state} state is not a reaper miss"
    assert not (tmp_path / "revived").exists(), "never revive a spoke recorded landing/pushing"


@pytest.mark.parametrize("state", ["landing", "pushing"])
def test_run_conditions_logs_a_divergence_when_the_log_suppresses_a_dead_pane_fire(
    tmp_path: Path, state: str
) -> None:
    # The suppression is never silent (#300): when the epoch-inference WOULD have fired but
    # the recorded state vetoes it, a DIVERGENCE line names the disagreement (log wins).
    env = _dead_pane_dispatch_env(tmp_path)
    _write_transition(tmp_path / "afk-state", 284, state)
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    result = _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert "divergence" in result.stdout.lower(), result.stdout
    assert state in result.stdout


def test_run_conditions_dead_pane_still_fires_when_state_is_not_a_land_or_push(
    tmp_path: Path,
) -> None:
    # The veto is NARROW: only landing|pushing suppress. A recorded `dispatched` (a spoke
    # that crashed before ever pushing) is exactly the reaper miss condition 2 exists to
    # catch — the log must NOT suppress it, mirroring the unknown-log fallback.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    _write_transition(tmp_path / "afk-state", 284, "dispatched")
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert '"condition":"dead-pane"' in ledger.read_text()
    assert (tmp_path / "revived").read_text() == "284"


@pytest.mark.parametrize("state", ["landing", "pushing"])
def test_run_conditions_dead_pane_re_arms_when_a_recorded_phase_is_impossibly_old(
    tmp_path: Path, state: str
) -> None:
    # The suppression is BOUNDED, never permanent (#299's silent-stall class). spoke-push and
    # worktree-land record landing|pushing INTENT-FIRST and leave them stuck on a mid-phase crash, so
    # an unbounded defer would silence the dead-pane backstop forever — the exact hazard the adjacent
    # _wd_land_in_flight bounds with its mtime window ("must never silence condition 2 forever"). A
    # recorded phase whose onset is older than the dead-idle ceiling has run impossibly long: stop
    # deferring and let the reaper-miss backstop re-arm.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    _write_transition(
        tmp_path / "afk-state", 284, state, ts=str(int(NOW) - 4000)
    )  # > 3600s ceiling
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert '"condition":"dead-pane"' in ledger.read_text(), f"a stuck {state} phase must re-arm"
    assert (tmp_path / "revived").read_text() == "284"


# Condition 3 — stale blocked marker (real git)
def test_stale_marker_fires_when_blocked_is_ancestor_of_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "c2"],
        cwd=wt,
        check=True,
        env=env,
        capture_output=True,
    )

    assert _call(f"_wd_detect_stale_marker '{wt}' 5").returncode == 0


def test_stale_marker_quiet_when_blocked_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env, capture_output=True)

    assert _call(f"_wd_detect_stale_marker '{wt}' 5").returncode == 1


# Condition 4 — mergeable branch the drain skipped (escalate-only)
# Staleness is measured from the DONE epoch (stamped when slot_state first reads done, #263),
# not the progress epoch — a spoke parked > ceiling BEFORE going ready must not false-fire.
def test_mergeable_skipped_fires_when_done_open_and_stale(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)  # done > 900s ago, un-landed → a real skip
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 0


def test_mergeable_skipped_quiet_when_freshly_done(tmp_path: Path) -> None:
    # AC4 (the #263 false-fire): a spoke that parked long BEFORE going ready is `done` only
    # recently — its done epoch is within the ceiling, so condition 4 stays quiet even though a
    # progress-epoch base would already read stale. auto_land gets its full ceiling to land.
    wt = _git_repo(tmp_path)
    fresh = str(int(NOW) - 100)  # done 100s ago (< 900s ceiling)
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {fresh}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


def test_mergeable_skipped_quiet_when_blocked_at_tip(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    env0 = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "tag", "blocked/5"], cwd=wt, check=True, env=env0, capture_output=True)
    old = str(int(NOW) - 1000)
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


def test_mergeable_skipped_quiet_when_issue_closed(tmp_path: Path) -> None:
    wt = _git_repo(tmp_path)
    old = str(int(NOW) - 1000)
    prelude = f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    env = {"AFK_NOW": NOW, "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


# Condition 4 (#285) — probe mergeability, defer while the LAND lane is mid-backoff, and fire a
# DISTINCT `conflicted-land` reason (naming the conflicting files) instead of the false "mergeable
# branch un-landed" when the branch actually conflicts with the base.
def _git(wt: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(wt), *args], check=True, env=env, capture_output=True, text=True
    ).stdout


def _base_branch(wt: Path) -> str:
    return _git(wt, "symbolic-ref", "--short", "HEAD").strip()


def _conflicting_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose checked-out feature branch CONFLICTS with the base branch on README.md."""
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    _git(wt, "checkout", "-qb", "feature/5-x")
    (wt / "README.md").write_text("spoke side\n")
    _git(wt, "commit", "-qam", "feat: spoke readme")
    _git(wt, "checkout", "-q", base)
    (wt / "README.md").write_text("hub side\n")
    _git(wt, "commit", "-qam", "chore: hub readme")
    _git(wt, "checkout", "-q", "feature/5-x")
    return wt, base


def _mergeable_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose feature branch is cleanly mergeable into the base (disjoint files)."""
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    _git(wt, "checkout", "-qb", "feature/5-x")
    (wt / "spoke.txt").write_text("spoke only\n")
    _git(wt, "add", "spoke.txt")
    _git(wt, "commit", "-qm", "feat: spoke file")
    return wt, base


def test_land_conflicts_names_the_conflicting_file(tmp_path: Path) -> None:
    wt, base = _conflicting_repo(tmp_path)
    out = _call(f"_wd_land_conflicts '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout
    assert "README.md" in out


def test_land_conflicts_empty_when_mergeable(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    out = _call(f"_wd_land_conflicts '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout
    assert out.strip() == ""


def _run_conditions_ledger(
    wt: Path, base: str, tmp_path: Path, *, servicing_next: int | None = None
) -> str:
    """Drive _wd_run_conditions for one done+stale+open spoke; return the intervention ledger text.

    The ledger + state dir + fire-dedup marker are FIXED under tmp_path, so repeated calls
    accumulate in the same ledger — the dedup / servicing-toggle tests rely on that. When
    ``servicing_next`` is given, the drain's LAND-lane backoff is armed to that epoch (a future
    epoch models mid-service); otherwise any prior servicing record is cleared.
    """
    ledger = tmp_path / "ledger.jsonl"
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    land_lane = statedir / "warned-state-5-land"
    if servicing_next is not None:
        land_lane.write_text(f"2\t{servicing_next}\n")
    elif land_lane.exists():
        land_lane.unlink()
    old = str(int(NOW) - 1000)
    prelude = (
        f'inflight_worktrees() {{ printf "%s\\t5\\n" "{wt}"; }}; '
        f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    )
    env = {
        "AFK_NOW": NOW,
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_CMD": "true",  # stub the landmark so no real tag is written
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "nolandmarks"),
        "AI_TOOLKIT_BASE_BRANCH": base,
        "AFK_STATE_DIR": str(statedir),
    }
    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)
    return ledger.read_text() if ledger.exists() else ""


def test_run_conditions_fires_conflicted_land_naming_files_on_conflict(tmp_path: Path) -> None:
    wt, base = _conflicting_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path)
    assert '"condition":"conflicted-land"' in ledger
    assert "README.md" in ledger
    assert '"condition":"auto-land-skipped"' not in ledger


def test_run_conditions_fires_auto_land_skipped_when_truly_mergeable(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path)
    assert '"condition":"auto-land-skipped"' in ledger
    assert '"condition":"conflicted-land"' not in ledger


# ── issue #292: condition 4 is marker-kind-blind ──────────────────────────────
# slot_state returns `done` for BOTH ready/ and accept/ at the tip (its `for kind in ready accept`
# loop), so _wd_detect_mergeable_skipped conflates two opposite situations: ready/ means the drain
# SHOULD have landed and didn't (a real shortfall, #274's class), while accept/ is the deliberate
# human-eyeball terminal the drain MUST NOT land (spoke-ready's EYEBALL row; auto_land lands only
# _ready_at_tip). On #286 an accept/ spoke with ZERO commits of its own — the spoke concluded the
# work was already shipped and moved to close without code — escalated as "mergeable branch
# un-landed: human land". Three costs: the remediation misdirects (landing a zero-diff branch would
# close the issue as shipped when the pending decision is "confirm the duplicate, or re-kick"), the
# firing lands as class afk-defect and docks the #251 autonomy score for a by-design human wait, and
# it auto-spawns a bug-scoper per accept-spoke per run (this issue's own provenance).
def _accept_tag(wt: Path, issue: str = "5") -> None:
    """Tag accept/<issue> at the tip the way spoke-ready does — ANNOTATED (`git tag -f -a`)."""
    _git(wt, "tag", "-f", "-a", f"accept/{issue}", "-m", "built+reviewed; human sign-off")


def _ready_tag(wt: Path, issue: str = "5") -> None:
    _git(wt, "tag", "-f", "-a", f"ready/{issue}", "-m", "ready to land")


def _zero_diff_repo(tmp_path: Path) -> tuple[Path, str]:
    """The #286 shape: a branch at the base tip with NO commits of its own."""
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    _git(wt, "checkout", "-qb", "feature/5-x")  # branched, never committed
    return wt, base


def test_terminal_marker_kind_reads_accept(tmp_path: Path) -> None:
    wt, _ = _mergeable_repo(tmp_path)
    _accept_tag(wt)
    assert _call(f"_wd_terminal_marker_kind '{wt}' 5").stdout.strip() == "accept"


def test_terminal_marker_kind_reads_ready(tmp_path: Path) -> None:
    wt, _ = _mergeable_repo(tmp_path)
    _ready_tag(wt)
    assert _call(f"_wd_terminal_marker_kind '{wt}' 5").stdout.strip() == "ready"


def test_terminal_marker_kind_prefers_ready_when_both_sit_at_the_tip(tmp_path: Path) -> None:
    # Must agree with slot_state's own precedence (`for kind in ready accept` — ready wins), or the
    # watchdog would classify off a different marker than the one that made the spoke read `done`.
    wt, _ = _mergeable_repo(tmp_path)
    _accept_tag(wt)
    _ready_tag(wt)
    assert _call(f"_wd_terminal_marker_kind '{wt}' 5").stdout.strip() == "ready"


def test_terminal_marker_kind_empty_when_no_terminal_marker(tmp_path: Path) -> None:
    # Unreadable kind must fall through to the historical afk-defect path, never to the silent one.
    # returncode is asserted too: a missing/broken helper exits 127 with empty stdout, which would
    # satisfy a stdout-only assertion and leave this safety-critical fallthrough unguarded.
    wt, _ = _mergeable_repo(tmp_path)
    out = _call(f"_wd_terminal_marker_kind '{wt}' 5")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_accept_unsigned_reason_names_conflicts_too(tmp_path: Path) -> None:
    # #285's honesty contract applies to accept/ as well. accept/ is normally eyeball-THEN-land and
    # auto_land never touches it, so condition 4 is the ONLY mergeability probe it ever gets: drop
    # the file list and a human who approves the code walks straight into an unannounced conflict.
    wt, base = _conflicting_repo(tmp_path)
    _accept_tag(wt)
    ledger = _run_conditions_ledger(wt, base, tmp_path)

    assert '"condition":"accept-unsigned"' in ledger, ledger
    assert "README.md" in ledger, "an accept/ escalation must still name the conflicting files"


def test_own_commits_is_zero_when_the_local_base_lags_origin(tmp_path: Path) -> None:
    # Spokes branch from origin/<base> (wt_base_start_point: "the hub's local base may lag or carry
    # unpushed work"), while _wd_land_base_ref PREFERS the local ref. Measuring <local-base>..HEAD
    # then reports the base's OWN missing commits as the spoke's work, dropping the zero-diff clause
    # on exactly #292's headline scenario — the human lands an empty branch and closes it as shipped.
    wt = _git_repo(tmp_path)
    base = _base_branch(wt)
    (wt / "README.md").write_text("base\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-qm", "chore: base readme")
    # origin/<base> is two commits AHEAD of the local base; the spoke branched from origin's tip.
    _git(wt, "update-ref", f"refs/remotes/origin/{base}", "HEAD")
    _git(wt, "checkout", "-qb", "feature/5-x")
    for n in (1, 2):
        (wt / f"sibling{n}.txt").write_text("landed by someone else\n")
        _git(wt, "add", f"sibling{n}.txt")
        _git(wt, "commit", "-qm", f"feat: sibling work {n}")
    _git(wt, "update-ref", f"refs/remotes/origin/{base}", "HEAD")  # origin absorbed them
    # The local base still points at the old readme commit — it LAGS origin by two.

    out = _call(f"_wd_own_commits '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout.strip()

    assert out == "0", (
        "a branch holding only commits the base's remote already has authored nothing of its own"
    )


def test_own_commits_counts_zero_for_a_close_without_code_branch(tmp_path: Path) -> None:
    wt, base = _zero_diff_repo(tmp_path)
    out = _call(f"_wd_own_commits '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout.strip()
    assert out == "0"


def test_own_commits_counts_the_branchs_own_work(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    out = _call(f"_wd_own_commits '{wt}'", env={"AI_TOOLKIT_BASE_BRANCH": base}).stdout.strip()
    assert out == "1"


def test_classify_accept_unsigned_is_a_novel_decision() -> None:
    # The by-design human sign-off wait is not a drain shortfall: it must not dock the autonomy
    # score, and must not auto-file a bug against afk.
    assert _call("_wd_classify accept-unsigned 5").stdout.strip() == "novel-decision"


def test_run_conditions_fires_accept_unsigned_for_an_accept_spoke(tmp_path: Path) -> None:
    wt, base = _mergeable_repo(tmp_path)
    _accept_tag(wt)
    ledger = _run_conditions_ledger(wt, base, tmp_path)

    assert '"condition":"accept-unsigned"' in ledger, ledger
    assert '"class":"novel-decision"' in ledger, "a by-design human wait is not an afk defect"
    assert '"condition":"auto-land-skipped"' not in ledger, (
        "telling a human to LAND an accept/ spoke is the misdirection #292 is about"
    )


def test_accept_unsigned_reason_names_sign_off_and_the_zero_diff(tmp_path: Path) -> None:
    # The #286 shape end-to-end: the reason must say what the human actually owes — confirm the
    # close-without-code — not "land it", which would close the issue as shipped on an empty merge.
    wt, base = _zero_diff_repo(tmp_path)
    _accept_tag(wt)
    ledger = _run_conditions_ledger(wt, base, tmp_path)

    assert "sign-off" in ledger, ledger
    assert "no own commits" in ledger, ledger
    assert "do not land" in ledger, ledger


def test_run_conditions_files_no_defect_for_an_accept_spoke(tmp_path: Path) -> None:
    # The filing-spam consequence: every accept/ a human does not service within the ceiling was
    # spawning a fresh headless bug-scoper per run.
    wt, base = _mergeable_repo(tmp_path)
    _accept_tag(wt)
    scoped = tmp_path / "scoped"
    ledger = tmp_path / "ledger.jsonl"
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    old = str(int(NOW) - 1000)
    prelude = (
        f'inflight_worktrees() {{ printf "%s\\t5\\n" "{wt}"; }}; '
        f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    )
    env = {
        "AFK_NOW": NOW,
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_CMD": "true",
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "nolandmarks"),
        "AI_TOOLKIT_BASE_BRANCH": base,
        "AFK_STATE_DIR": str(statedir),
        "HUB_WATCHDOG_FILE": "1",  # opt filing back on — the point of the test
        "HUB_WATCHDOG_DEDUP_CMD": "true",
        "HUB_WATCHDOG_LABEL_CMD": "true",
        "HUB_WATCHDOG_SCOPER_CMD": f'printf "%s %s" "$1" "$2" >> {scoped}',
    }

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert not scoped.exists(), (
        "an accept/ human-sign-off wait must not auto-file a bug against afk"
    )


def test_run_conditions_still_escalates_the_landmark_for_an_accept_spoke(tmp_path: Path) -> None:
    # Quieter classification must NOT mean invisible: the human still needs pointing at the spoke.
    # Reuses needs-human-land/<N> per #272 (no second tripwire-racing tag) — and the existing
    # _wd_clear_landed_landmarks sweep then self-clears it once the human closes the issue.
    wt, base = _mergeable_repo(tmp_path)
    _accept_tag(wt)
    marked = tmp_path / "marked"
    ledger = tmp_path / "ledger.jsonl"
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    old = str(int(NOW) - 1000)
    prelude = (
        f'inflight_worktrees() {{ printf "%s\\t5\\n" "{wt}"; }}; '
        f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    )
    env = {
        "AFK_NOW": NOW,
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_CMD": f'printf "%s" "$2" > {marked}',
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "nolandmarks"),
        "AI_TOOLKIT_BASE_BRANCH": base,
        "AFK_STATE_DIR": str(statedir),
    }

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert marked.read_text() == "5", "the escalation must stay visible to a human"


def test_run_conditions_keeps_auto_land_skipped_for_a_ready_spoke(tmp_path: Path) -> None:
    # The complement, and the load-bearing half: ready/ at tip IS a genuine drain shortfall (#274)
    # and must keep firing afk-defect. A novel-decision here would hide a real bug and flatter the
    # autonomy score — the direction that must never widen.
    wt, base = _mergeable_repo(tmp_path)
    _ready_tag(wt)
    ledger = _run_conditions_ledger(wt, base, tmp_path)

    assert '"condition":"auto-land-skipped"' in ledger, ledger
    assert '"class":"afk-defect"' in ledger, (
        "a drain that should have landed and didn't IS a defect"
    )
    assert '"condition":"accept-unsigned"' not in ledger


# ── #303 (#300 step 4): auto-land-skipped READS the transition log ─────────────
# Two conversions, each with a fallback so an unknown/absent log never fires nor suppresses alone:
#   1. Staleness is measured from the RECORDED terminal transition (ready/accepted onset), the
#      log-native replacement for the done-epoch proxy — done epoch is the fallback when unknown.
#   2. accept-vs-ready is classified from the RECORDED state (accepted is DISTINCT from ready at the
#      source, spoke-ready's _tlog_state_for_kind), so a human-sign-off close can never escalate as
#      an un-landed branch (#292) — the tip-tag probe (_wd_terminal_marker_kind) is the fallback.
# A log-vs-tag disagreement logs a divergence line (never silent, #300); the log wins.
def test_mergeable_skipped_measures_staleness_from_the_ready_transition_onset(
    tmp_path: Path,
) -> None:
    # #303: the ceiling is measured from the RECORDED ready transition, not the done epoch. A FRESH
    # ready onset means auto_land still has its full window, even when a STALE done epoch (the proxy
    # this replaces) would already read past the ceiling — so the detector stays quiet.
    wt = _git_repo(tmp_path)
    _write_transition(tmp_path / "afk-state", 5, "ready", ts=str(int(NOW) - 100))  # fresh onset
    prelude = (
        "slot_state() { echo done; }; "
        f"read_done_epoch() {{ echo {int(NOW) - 4000}; }}"  # stale proxy — would fire today
    )
    env = {
        "AFK_NOW": NOW,
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
    }
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 1


def test_mergeable_skipped_fires_when_the_ready_transition_onset_is_stale(tmp_path: Path) -> None:
    # The complement: a ready transition older than the ceiling IS a genuine un-landed skip — and it
    # fires from the transition ALONE, with no done epoch on record (the log is now the primary read).
    wt = _git_repo(tmp_path)
    _write_transition(tmp_path / "afk-state", 5, "ready", ts=str(int(NOW) - 4000))  # stale onset
    prelude = "slot_state() { echo done; }; read_done_epoch() { echo ''; }"  # no done epoch at all
    env = {
        "AFK_NOW": NOW,
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
    }
    assert _call(f"{prelude}; _wd_detect_mergeable_skipped '{wt}' 5 {NOW}", env=env).returncode == 0


def test_run_conditions_accepted_in_the_log_never_escalates_even_against_a_ready_tag(
    tmp_path: Path,
) -> None:
    # The #292 structural proof: the transition log is AUTHORITATIVE over the tip tag. A recorded
    # `accepted` routes to accept-unsigned (human sign-off), NEVER auto-land-skipped — even when a
    # (misleading) ready/ tag sits at the tip that today's tag-only probe would classify as a ready
    # drain shortfall. This is the fix the log makes structural: accepted is a distinct recorded
    # state, not something inferred from a tag that can be stale or mid-move.
    wt, base = _mergeable_repo(tmp_path)
    _ready_tag(wt)  # tip tag says ready — today's inference would escalate as un-landed
    (tmp_path / "statedir").mkdir(exist_ok=True)
    _write_transition(tmp_path / "statedir", 5, "accepted", ts=str(int(NOW) - 1000))
    ledger = _run_conditions_ledger(wt, base, tmp_path)

    assert '"condition":"accept-unsigned"' in ledger, ledger
    assert '"condition":"auto-land-skipped"' not in ledger, (
        "a recorded accepted state must win over a ready tag — the #292 misdirection"
    )


def test_run_conditions_logs_a_classify_divergence_when_log_and_tag_disagree(
    tmp_path: Path,
) -> None:
    # Never silent (#300): the log (accepted) and the tip tag (ready) disagree about the terminal
    # kind, so a DIVERGENCE line names it; the log wins. Mirrors _run_conditions_ledger's setup but
    # captures stdout (where _wd_log writes) instead of only the ledger.
    wt, base = _mergeable_repo(tmp_path)
    _ready_tag(wt)
    statedir = tmp_path / "statedir"
    statedir.mkdir(exist_ok=True)
    _write_transition(statedir, 5, "accepted", ts=str(int(NOW) - 1000))
    old = str(int(NOW) - 1000)
    prelude = (
        f'inflight_worktrees() {{ printf "%s\\t5\\n" "{wt}"; }}; '
        f"slot_state() {{ echo done; }}; read_done_epoch() {{ echo {old}; }}"
    )
    env = {
        "AFK_NOW": NOW,
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo open",
        "HUB_WATCHDOG_LEDGER": str(tmp_path / "ledger.jsonl"),
        "HUB_WATCHDOG_LANDMARK_CMD": "true",
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "nolandmarks"),
        "AI_TOOLKIT_BASE_BRANCH": base,
        "AFK_STATE_DIR": str(statedir),
    }

    result = _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert "divergence" in result.stdout.lower(), result.stdout
    assert "accept" in result.stdout and "ready" in result.stdout


def test_sweep_clears_the_accept_unsigned_marker_for_a_closed_issue(tmp_path: Path) -> None:
    # The landmark sweep already re-arms auto-land-skipped/conflicted-land once the issue closes;
    # the new condition needs the same treatment or a later recurrence stays deduped into silence.
    root = _git_repo(tmp_path, name="landmarks")
    _git(root, "tag", "needs-human-land/5")
    ledger = tmp_path / "ledger.jsonl"
    # Co-located with the ledger, because _wd_fired_marker resolves off dirname(_wd_ledger_file) —
    # the same placement the sibling dead-pane sweep tests use.
    marker = tmp_path / "wd-fire-dedup-accept-unsigned-5"
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(root),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed",
    }

    _call("_wd_clear_landed_landmarks", env=env)

    assert not marker.exists(), "a closed accept/ issue must re-arm the condition for a recurrence"


def test_run_conditions_defers_while_land_lane_mid_backoff(tmp_path: Path) -> None:
    # AC5: while the drain's LAND lane has a FRESH armed retry (future-dated warned-state-5-land),
    # the watchdog must NOT fire condition 4 — no false "conflicted-land"/"skipped" escalation while
    # the drain is still servicing the land. Mirrors the answer-lane servicing defer.
    wt, base = _conflicting_repo(tmp_path)
    ledger = _run_conditions_ledger(wt, base, tmp_path, servicing_next=int(NOW) + 500)
    assert "conflicted-land" not in ledger and "auto-land-skipped" not in ledger


def test_conflicted_land_dedups_across_servicing_toggle(tmp_path: Path) -> None:
    # #285 review / #263: a single persistent conflict must ledger EXACTLY ONCE across an
    # arm->service->elapse toggle. The servicing defer must not clear the fire-dedup marker, or the
    # post-service tick would re-fire and double-count — corrupting the autonomy score.
    wt, base = _conflicting_repo(tmp_path)
    _run_conditions_ledger(wt, base, tmp_path)  # tick 1: not servicing → fire once
    _run_conditions_ledger(
        wt, base, tmp_path, servicing_next=int(NOW) + 500
    )  # tick 2: servicing → defer
    ledger = _run_conditions_ledger(wt, base, tmp_path)  # tick 3: elapsed → still deduped
    assert ledger.count('"condition":"conflicted-land"') == 1


# Condition 5 — supervisor dead
def test_supervisor_dead_fires_when_drain_state_stale(tmp_path: Path) -> None:
    prelude = "_wd_drain_state() { echo stale; }"
    assert _call(f"{prelude}; _wd_detect_supervisor_dead").returncode == 0
    prelude_live = "_wd_drain_state() { echo live; }"
    assert _call(f"{prelude_live}; _wd_detect_supervisor_dead").returncode == 1


# ── interventions fire the seam with the worktree + issue ─────────────────────
def test_intervene_answer_invokes_the_seam(tmp_path: Path) -> None:
    marker = tmp_path / "answered"
    # An idle drain (no fresh attempt, no live supervisor) is not mid-service → the seam fires.
    # AFK_STATE_DIR isolates read_answer_attempt off the live host state dir (else a real drain's
    # answer-attempt-5.epoch could read "fresh" under the past-pinned AFK_NOW and defer the seam).
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f'printf "%s %s" "$1" "$2" > {marker}',
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_intervene_answer /the/wt 5", env=env)

    assert marker.read_text() == "/the/wt 5"


# AC4 (#265): the watchdog must NOT run a second decide_and_act while the supervisor is
# mid-service on the SAME park — a duplicate answer races the in-flight answerer (the #89
# stale-answer/strand hazard + a wasted high-effort run). Two mid-service signals defer it.
def test_intervene_answer_defers_when_attempt_stamp_fresh(tmp_path: Path) -> None:
    # A fresh answer-delivery stamp ⇒ the supervisor delivered within the ceiling → defer.
    marker = tmp_path / "answered"
    fresh = str(int(NOW) - 60)  # delivered 60s ago (< 600s ceiling)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_NOW": NOW,
    }

    _call(f"read_answer_attempt() {{ echo {fresh}; }}; _wd_intervene_answer /the/wt 5", env=env)

    assert not marker.exists()


def test_intervene_answer_defers_when_live_drain_last_action_names_issue(tmp_path: Path) -> None:
    # No delivery stamp yet (the never-attempted window), but the drain is live and its
    # last-action names this issue ⇒ the answerer is mid-reasoning → defer.
    marker = tmp_path / "answered"
    last_action = tmp_path / "last-action"
    last_action.write_text("answer #5\n")
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_LAST_ACTION": str(last_action),
        "AFK_NOW": NOW,
    }
    prelude = '_wd_drain_state() { echo live; }; read_answer_attempt() { echo ""; }'

    _call(f"{prelude}; _wd_intervene_answer /the/wt 5", env=env)

    assert not marker.exists()


def test_intervene_answer_fires_when_last_action_names_other_issue(tmp_path: Path) -> None:
    # A live drain whose last-action names a DIFFERENT issue is not servicing this park → fire.
    marker = tmp_path / "answered"
    last_action = tmp_path / "last-action"
    last_action.write_text("answer #9\n")
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_LAST_ACTION": str(last_action),
        "AFK_NOW": NOW,
    }
    prelude = '_wd_drain_state() { echo live; }; read_answer_attempt() { echo ""; }'

    _call(f"{prelude}; _wd_intervene_answer /the/wt 5", env=env)

    assert marker.read_text() == "x"


# AC4 (#283): the answer intervention belongs to the ANSWER lane. A permission dialog is the
# broker's, with its own classifier, timers and re-answer ceiling — injecting an answer into one is
# how the watchdog ends up answering a park it does not own (#271), interrupting a live tool call
# (#89). The detector already refuses to fire on a permission park; this is the second lock, for
# any direct caller.
def test_intervene_answer_defers_on_a_permission_dialog(tmp_path: Path) -> None:
    marker = tmp_path / "answered"
    wt = _git_repo(tmp_path)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),  # drain off ⇒ the #265 guard never defers
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    prelude = "_permission_pending() { return 0; }"

    _call(f"{prelude}; _wd_intervene_answer '{wt}' 5", env=env)

    assert not marker.exists(), "a permission dialog is the broker's lane — never inject into it"


def test_intervene_answer_answers_a_gate_park(tmp_path: Path) -> None:
    # The complement: a real answer-lane park still gets the intervention.
    marker = tmp_path / "answered"
    wt = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "gate/5"], cwd=wt, check=True, capture_output=True)
    env = {
        "HUB_WATCHDOG_ANSWER_CMD": f"printf x > {marker}",
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    prelude = "_permission_pending() { return 1; }"

    _call(f"{prelude}; _wd_intervene_answer '{wt}' 5", env=env)

    assert marker.read_text() == "x"


# AC3 (#290): the revive must never `nohup claude --continue` into a worktree a land is about to
# remove — on #284 it did exactly that, resuming a headless session against a vanishing cwd. The
# detector guard upstream already stops the dispatcher path; this is the second lock, for any
# direct caller (mirroring _wd_intervene_answer's permission-lane re-check).
def test_intervene_revive_defers_when_the_issue_is_done_stamped(tmp_path: Path) -> None:
    marker = tmp_path / "revived"
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir()
    (state_dir / "done-284.epoch").write_text("1784066007\n")  # reached terminal state
    env = _dead_pane_env(tmp_path, HUB_WATCHDOG_REVIVE_CMD=f"printf x > {marker}")

    _call("_wd_intervene_revive /the/wt 284", env=env)

    assert not marker.exists(), "a done-stamped spoke is finished — never revive it"


def test_intervene_revive_defers_when_a_land_is_in_flight(tmp_path: Path) -> None:
    marker = tmp_path / "revived"
    last_action = tmp_path / "last-action"
    last_action.write_text("land #284\n")
    env = _dead_pane_env(
        tmp_path,
        AFK_LAST_ACTION=str(last_action),
        HUB_WATCHDOG_REVIVE_CMD=f"printf x > {marker}",
    )

    _call("_wd_drain_state() { echo live; }; _wd_intervene_revive /the/wt 284", env=env)

    assert not marker.exists(), "never revive into a worktree the land is removing"


def test_intervene_revive_runs_for_a_genuinely_dead_spoke(tmp_path: Path) -> None:
    # The complement: no done epoch, no land in flight ⇒ a real abandoned spoke still gets revived.
    marker = tmp_path / "revived"
    env = _dead_pane_env(tmp_path, HUB_WATCHDOG_REVIVE_CMD=f'printf "%s" "$2" > {marker}')

    _call("_wd_intervene_revive /the/wt 284", env=env)

    assert marker.read_text() == "284"


# ── issue #297 defect 1: the revive intervention re-fired on EVERY tick ────────
# The wd-fire-dedup marker gated only the LEDGER append (_wd_fire); the intervention itself ran
# every tick the condition held. Nothing in the revive path advances the epoch the detector
# measures, and `nohup claude --continue` creates no tmux pane — so pane-absence stayed true and a
# stale drain plus one crashed spoke past the ceiling launched a fresh headless run into the same
# worktree EVERY MINUTE: dozens of concurrent claudes racing each other's writes in one checkout.
# The revive is now budgeted once per armed window, mirroring the drain's own resumed-<issue>
# stamp (_afk_already_resumed / _afk_mark_resumed, hub-afk.sh): revive once, and a second crash
# escalates to a human rather than re-spawning.
_REVIVE_BUDGET_MARKER = "wd-fire-dedup-revive-284"


def _revive_counter_env(tmp_path: Path, counter: Path, **extra: str) -> dict[str, str]:
    """A dead-pane env whose revive seam APPENDS one line per spawn, so spawns are countable."""
    return _dead_pane_env(
        tmp_path, HUB_WATCHDOG_REVIVE_CMD=f'printf "spawn\\n" >> {counter}', **extra
    )


def _spawn_count(counter: Path) -> int:
    return counter.read_text().count("spawn") if counter.exists() else 0


def _await_file(path: Path, timeout: float = 15.0) -> str | None:
    """Wait for a detached process to write `path`; its content, or None if it never landed.

    Returning None rather than raising keeps a slow spawn under a loaded push gate reporting the
    caller's assertion message instead of a bare FileNotFoundError naming a tmp path.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        time.sleep(0.05)
    return None


def test_intervene_revive_spends_its_budget_once_per_window(tmp_path: Path) -> None:
    # The core of the defect: three ticks with the condition still holding must launch ONE run.
    counter = tmp_path / "spawns"
    env = _revive_counter_env(tmp_path, counter)

    for _ in range(3):
        _call("_wd_intervene_revive /the/wt 284", env=env)

    assert _spawn_count(counter) == 1, (
        "the revive is budgeted once per armed window — a second crash escalates to a human"
    )


def test_run_conditions_revives_a_persistent_dead_pane_only_once(tmp_path: Path) -> None:
    # The same bound through the DISPATCHER — the path that actually burned the subscription: a
    # persistent dead pane across N ticks is one ledger firing AND one spawn, not N.
    counter = tmp_path / "spawns"
    env = _dead_pane_dispatch_env(
        tmp_path, HUB_WATCHDOG_REVIVE_CMD=f'printf "spawn\\n" >> {counter}'
    )
    prelude = _dead_pane_dispatch_prelude(done_epoch="")

    for _ in range(3):
        _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert _spawn_count(counter) == 1, "a persistent dead pane must not re-spawn a claude per tick"


def test_revive_budget_marker_records_the_spawned_run(tmp_path: Path) -> None:
    # Liveness bookkeeping: the budget marker carries `<ts>\t<pid>` of the run it launched, so an
    # operator returning to a burnt window can find and kill the orphan instead of hunting stray
    # `claude` processes by hand. A fake `claude` on PATH records its own $$ — that must be the pid
    # the marker names, which also proves the recorded pid IS the spawned run and not the daemon's.
    wt = tmp_path / "wt"
    wt.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude_pid = tmp_path / "claude-pid"
    fake = bindir / "claude"
    fake.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$$" > "{claude_pid}"\n')
    fake.chmod(0o755)
    env = _dead_pane_env(tmp_path, PATH=f"{bindir}:{os.environ['PATH']}")

    _call(f"_wd_intervene_revive '{wt}' 284", env=env)

    marker = tmp_path / "afk-state" / _REVIVE_BUDGET_MARKER
    assert marker.exists(), "the spawn must be recorded so the next tick sees the budget spent"
    ts, pid = marker.read_text().strip().split("\t")
    assert ts.isdigit(), f"the marker must carry the spawn ts, got {ts!r}"
    assert _await_file(claude_pid) == pid, "the marker must name the pid of the run it spawned"


def test_a_fresh_arm_clears_the_revive_budget(tmp_path: Path) -> None:
    # The budget is per-WINDOW, not per-run: it rides the `wd-fire-dedup-` family precisely so the
    # REAL _clear_progress_state glob drops it on a fresh arm. Pinned against the actual function
    # (not a copy of its glob) so a rename there fails here instead of silently stranding a spoke
    # un-revivable for every future window.
    counter = tmp_path / "spawns"
    env = _revive_counter_env(tmp_path, counter)

    _call("_wd_intervene_revive /the/wt 284", env=env)
    assert (tmp_path / "afk-state" / _REVIVE_BUDGET_MARKER).exists()
    _call("_clear_progress_state", env=env)
    _call("_wd_intervene_revive /the/wt 284", env=env)

    assert _spawn_count(counter) == 2, "a fresh arm must re-grant the revive budget"


def test_the_revive_lane_survives_the_drains_give_up_label(tmp_path: Path) -> None:
    # A watchdog revive must NOT be gated on _wd_last_action naming this issue. That record is the
    # drain's LAST action, not its CURRENT one, and is never cleared mid-window — so the drain's own
    # give-up label (_warn_parked_last stamps `warn-park #284` when its resume budget is spent)
    # would read as "the drain is busy here" and disable this lane for the rest of the window, on
    # precisely the abandoned spoke tier-2 exists to catch. Pinned so the defer cannot come back.
    counter = tmp_path / "spawns"
    last_action = tmp_path / "last-action"
    last_action.write_text("warn-park #284\n")
    env = _revive_counter_env(tmp_path, counter, AFK_LAST_ACTION=str(last_action))

    _call("_wd_drain_state() { echo live; }; _wd_intervene_revive /the/wt 284", env=env)

    assert _spawn_count(counter) == 1, (
        "the drain having GIVEN UP on this spoke is the watchdog's cue to act, not to stand down"
    )


def test_a_relocated_ledger_still_leaves_the_budget_clearable(tmp_path: Path) -> None:
    # HUB_WATCHDOG_LEDGER is a documented override. Minting the budget beside the ledger (as the
    # wd-fire-dedup- FIRING markers do) would put it outside the only glob that clears it —
    # _clear_progress_state globs _afk_state_dir alone — stranding it past every future arm and
    # leaving the spoke permanently un-revivable. The budget must live where its clearer looks.
    counter = tmp_path / "spawns"
    far_ledger = tmp_path / "elsewhere" / "ledger.jsonl"
    far_ledger.parent.mkdir()
    env = _revive_counter_env(tmp_path, counter, HUB_WATCHDOG_LEDGER=str(far_ledger))

    _call("_wd_intervene_revive /the/wt 284", env=env)
    assert (tmp_path / "afk-state" / _REVIVE_BUDGET_MARKER).exists(), (
        "the budget must be minted in the state dir its clearer globs, not beside the ledger"
    )
    _call("_clear_progress_state", env=env)
    _call("_wd_intervene_revive /the/wt 284", env=env)

    assert _spawn_count(counter) == 2, "a relocated ledger must not strand the budget past an arm"


def test_intervene_revive_refuses_to_spawn_when_the_budget_cannot_be_recorded(
    tmp_path: Path,
) -> None:
    # The record is the ONLY thing bounding this lane, so its failure directions are asymmetric: an
    # unwritable state dir must cost one revive, never restore the every-tick spawn storm. Fail
    # closed — the pre-#297 behaviour was to spawn regardless, which is the bug.
    counter = tmp_path / "spawns"
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    env = _revive_counter_env(tmp_path, counter, AFK_STATE_DIR=str(readonly / "state"))
    readonly.chmod(0o555)
    try:
        _call("_wd_intervene_revive /the/wt 284", env=env)
    finally:
        readonly.chmod(0o755)

    assert _spawn_count(counter) == 0, "an unrecordable budget is an unbounded one — do not spawn"


def test_intervene_revive_keeps_its_budget_when_the_worktree_is_gone(tmp_path: Path) -> None:
    # The spawn is detached, so a vanished worktree is the one launch failure observable in time to
    # keep the budget unspent. Burning it here would let a torn-down-then-restored path (or simply a
    # wrong wt) consume the window's only revive on a run that never started.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/usr/bin/env bash\ntrue\n")
    fake.chmod(0o755)
    env = _dead_pane_env(tmp_path, PATH=f"{bindir}:{os.environ['PATH']}")

    _call(f"_wd_intervene_revive '{tmp_path / 'no-such-worktree'}' 284", env=env)

    assert not (tmp_path / "afk-state" / _REVIVE_BUDGET_MARKER).exists(), (
        "a revive that could not even start must not spend the window's budget"
    )


# ── issue #297 defect 2: condition 2 is blind to a spoke that dies before its first commit ────
# The progress epoch is stamped ONLY on a branch-tip ADVANCE (_afk_note_tip_progress), so a pane
# that crashes before its first commit leaves it empty forever — and _wd_epoch_stale reads an empty
# epoch as "unmeasurable ⇒ never fire". The spoke sits dead for the whole window and condition 2,
# whose entire job is catching a reaper miss, is structurally incapable of seeing it. This is the
# inverse face of defect 1: an empty epoch never fires, a stale one fired forever.
# The drain's own reaper already solved this — _afk_ceiling_epoch measures max(dispatch, progress),
# so a never-committed spoke is still measured from the moment it was dispatched. Condition 2 now
# uses the same base, via a helper that also names WHICH epoch won (the reason string needs it).
def test_dead_idle_base_falls_back_to_dispatch_before_the_first_commit(tmp_path: Path) -> None:
    sd = tmp_path / "afk-state"
    sd.mkdir()
    dispatched = str(int(NOW) - 4000)
    (sd / "dispatch-284.epoch").write_text(dispatched + "\n")  # never committed ⇒ no progress epoch

    out = _call("_wd_dead_idle_base 284", env=_dead_pane_env(tmp_path)).stdout.strip()

    assert out == f"dispatch\t{dispatched}", "a never-committed spoke measures from its dispatch"


def test_dead_idle_base_prefers_progress_once_the_spoke_commits(tmp_path: Path) -> None:
    # max(dispatch, progress): a committing spoke's ceiling restarts from its last real progress,
    # exactly as the reaper's does — else every long-running spoke would fire off its dispatch.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    progressed = str(int(NOW) - 100)
    (sd / "dispatch-284.epoch").write_text(f"{int(NOW) - 4000}\n")
    (sd / "progress-284.epoch").write_text(progressed + "\n")

    out = _call("_wd_dead_idle_base 284", env=_dead_pane_env(tmp_path)).stdout.strip()

    assert out == f"progress\t{progressed}"


def test_dead_idle_base_is_empty_when_nothing_is_measurable(tmp_path: Path) -> None:
    # Neither epoch ⇒ no base ⇒ the detector cannot fire. Preserves _afk_ceiling_epoch's contract
    # ("can't measure → never reap"): the watchdog must not invent a ceiling it has no clock for.
    (tmp_path / "afk-state").mkdir()

    out = _call("_wd_dead_idle_base 284", env=_dead_pane_env(tmp_path)).stdout.strip()

    assert out == ""


@pytest.mark.parametrize(
    "dispatch_age,progress_age",
    [
        pytest.param(4000, None, id="dispatch-only"),
        pytest.param(None, 900, id="progress-only"),
        pytest.param(4000, 900, id="both-progress-newer"),
        pytest.param(900, 4000, id="both-dispatch-newer"),
        pytest.param(900, 900, id="tie"),
        pytest.param(None, None, id="neither"),
    ],
)
def test_dead_idle_base_epoch_agrees_with_the_real_reaper_ceiling(
    tmp_path: Path, dispatch_age: int | None, progress_age: int | None
) -> None:
    # The parity pin: this helper duplicates _afk_ceiling_epoch's max() so it can name the winner.
    # Pinned against the REAL reaper across EVERY combination of present/absent epochs — the two
    # max() expressions are independently written (different operators, different emptiness
    # handling), so pinning only the case where progress wins outright would let a future edit to
    # either one diverge unnoticed. A divergence means the watchdog and the reaper contradict each
    # other about whether the same spoke is over its ceiling.
    # NOT parametrized over future-dated epochs: the watchdog deliberately screens those and the
    # reaper does not (see _wd_dead_idle_base). That divergence is the point, and is pinned
    # separately by test_dead_idle_base_screens_a_future_dated_epoch.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    if dispatch_age is not None:
        (sd / "dispatch-284.epoch").write_text(f"{int(NOW) - dispatch_age}\n")
    if progress_age is not None:
        (sd / "progress-284.epoch").write_text(f"{int(NOW) - progress_age}\n")
    env = _dead_pane_env(tmp_path)

    base = _call(f"_wd_dead_idle_base 284 {NOW}", env=env).stdout.strip()
    reaper = _call("_afk_ceiling_epoch 284", env=env).stdout.strip()

    mine = base.split("\t")[-1] if base else ""
    assert mine == reaper, f"base {base!r} must carry the reaper's ceiling epoch {reaper!r}"


def test_dead_idle_base_screens_a_future_dated_epoch(tmp_path: Path) -> None:
    # A future-dated epoch is unmeasurable, not fresh. Taking max() blindly would let a skewed
    # dispatch stamp OUTRANK a genuinely stale progress clock: now-epoch goes negative, which is
    # never > the ceiling, so condition 2 would go silent for the whole window — the unbounded
    # silence of #284, worse than the blindness this subtask removes. Fail toward firing, the same
    # rule _wd_land_in_flight applies to its log mtime. Reachable via a clock that steps forward
    # (VM resume, bad NTP) while spokes stamp dispatch, then corrects back.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    stale_progress = str(int(NOW) - 9000)
    (sd / "dispatch-284.epoch").write_text(f"{int(NOW) + 100000}\n")  # clock skew
    (sd / "progress-284.epoch").write_text(stale_progress + "\n")

    base = _call(f"_wd_dead_idle_base 284 {NOW}", env=_dead_pane_env(tmp_path)).stdout.strip()

    assert base == f"progress\t{stale_progress}", "a skewed epoch must not outrank a real one"


def test_dead_idle_fires_despite_a_future_dated_dispatch_epoch(tmp_path: Path) -> None:
    # The screen's whole point, end to end: the dead pane still fires rather than being silenced
    # for the window by a bogus stamp.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "dispatch-284.epoch").write_text(f"{int(NOW) + 100000}\n")
    (sd / "progress-284.epoch").write_text(f"{int(NOW) - 9000}\n")
    prelude = (
        f'_spoke_pane_target() {{ echo ""; }}; slot_state() {{ echo busy; }}; {_NO_DONE_EPOCH}'
    )

    assert (
        _detect(prelude, "_wd_detect_dead_idle /wt 284 " + NOW, env=_dead_pane_env(tmp_path)) == 0
    ), "clock skew must not silence a genuinely dead pane"


def test_dead_idle_quiet_for_a_parked_spoke_that_never_committed(tmp_path: Path) -> None:
    # `waiting` is PARKED, not hung, and must be excluded now that the base falls back to dispatch.
    # A spoke parked at a gate before its first commit has no progress epoch, so the old
    # progress-only base made condition 2 structurally silent for it; measuring from dispatch arms
    # it — and _wd_intervene_revive checks slot_state nowhere, so it would `claude --continue` a
    # spoke parked at an UNAPPROVED plan gate straight into implementing unreviewed work.
    # Parked spokes are conditions 1/3's, exactly as the drain's recover_dead_panes skips them.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "dispatch-284.epoch").write_text(f"{int(NOW) - 4000}\n")
    prelude = (
        f'_spoke_pane_target() {{ echo ""; }}; slot_state() {{ echo waiting; }}; {_NO_DONE_EPOCH}'
    )

    assert (
        _detect(prelude, "_wd_detect_dead_idle /wt 284 " + NOW, env=_dead_pane_env(tmp_path)) == 1
    ), "never revive a parked spoke — its gate may be waiting on a human"


def test_dead_idle_reason_reports_the_base_it_was_handed_not_a_fresh_read(tmp_path: Path) -> None:
    # The reason takes the detector's measured base for the same reason it takes the pre-read done
    # epoch: a live re-read can disagree with the epoch that actually fired (a concurrent revive
    # stamping progress, a fresh arm clearing both), emitting a line whose age contradicts its own
    # ceiling. Here disk says "progress 5s ago" while the detector fired on dispatch@4000s.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "progress-284.epoch").write_text(f"{int(NOW) - 5}\n")  # what a fresh read would find
    dispatched = str(int(NOW) - 4000)
    env = _dead_pane_env(tmp_path)

    out = _call(
        f'slot_state() {{ echo busy; }}; _wd_dead_idle_reason /the/wt 284 {NOW} "" '
        f"$'dispatch\\t{dispatched}'",
        env=env,
    ).stdout

    assert f"base=dispatch@{dispatched}" in out, out
    assert "4000s" in out, "the age must come from the base that fired, not a racing re-read"


def test_dead_idle_reason_does_not_claim_progress_when_there_is_no_base(tmp_path: Path) -> None:
    # The fallback arm must not print the progress noun beside base=none: telling a human "last
    # progress, unknown time" for a spoke with no epoch at all reads as a BROKEN clock rather than
    # an absent one — the #290 AC4 diagnosability contract failing in the exact way it must not.
    (tmp_path / "afk-state").mkdir()

    out = _call(
        f'slot_state() {{ echo busy; }}; _wd_dead_idle_reason /the/wt 284 {NOW} "" ""',
        env=_dead_pane_env(tmp_path),
    ).stdout

    assert "base=none@?" in out, out
    assert "last progress" not in out, "do not report progress for a spoke that has no progress"


def test_dead_idle_fires_for_a_spoke_that_died_before_its_first_commit(tmp_path: Path) -> None:
    # The defect end-to-end, through the detector: dead pane, non-terminal, never committed, past
    # the ceiling since dispatch. Today the empty progress epoch makes this permanently quiet.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    (sd / "dispatch-284.epoch").write_text(f"{int(NOW) - 4000}\n")  # > 3600s ceiling
    prelude = (
        f'_spoke_pane_target() {{ echo ""; }}; slot_state() {{ echo busy; }}; {_NO_DONE_EPOCH}'
    )

    assert (
        _detect(prelude, "_wd_detect_dead_idle /wt 284 " + NOW, env=_dead_pane_env(tmp_path)) == 0
    ), "a spoke that crashed before its first commit is exactly the reaper miss condition 2 is for"


def test_dead_idle_reason_names_dispatch_when_it_is_the_measured_base(tmp_path: Path) -> None:
    # #290 AC4's measured-base contract, extended: the ledger line must name WHICH epoch it
    # measured, so `base=dispatch@N` tells a human this spoke never committed at all — a materially
    # different story from a stalled-after-progress one, and the first thing to know when triaging.
    sd = tmp_path / "afk-state"
    sd.mkdir()
    dispatched = str(int(NOW) - 4000)
    (sd / "dispatch-284.epoch").write_text(dispatched + "\n")
    env = _dead_pane_env(tmp_path)

    out = _call(
        f'slot_state() {{ echo busy; }}; _wd_dead_idle_reason /the/wt 284 {NOW} ""', env=env
    )

    assert f"base=dispatch@{dispatched}" in out.stdout, out.stdout
    assert "4000s" in out.stdout, "the measured age must come from the base that actually won"


# AC4 (#290): a dead-pane ledger line must carry the base it was MEASURED from, so a future false
# positive is diagnosable from the line alone rather than by re-deriving the timeline from four
# state files. Mirrors #283 AC5's measured-base reason for park-unanswered.
def test_dead_idle_reason_names_the_measured_base(tmp_path: Path) -> None:
    last_action = tmp_path / "last-action"
    last_action.write_text("answer #7\n")
    env = _dead_pane_env(tmp_path, AFK_LAST_ACTION=str(last_action))
    progress = str(int(NOW) - 4459)
    prelude = f"slot_state() {{ echo busy; }}; read_progress_epoch() {{ echo {progress}; }}"

    out = _call(f'{prelude}; _wd_dead_idle_reason /the/wt 284 {NOW} ""', env=env).stdout

    assert "4459s" in out  # the measured age
    assert f"base=progress@{progress}" in out  # WHICH epoch it measured from
    assert "slot_state=busy" in out  # the live state at firing time
    assert "done-epoch=none" in out  # the terminal classification it checked
    assert "last-action=answer #7" in out  # what the drain was doing
    assert "ceiling 3600s" in out


def test_dead_idle_reason_falls_back_to_a_live_done_epoch_read_when_omitted(tmp_path: Path) -> None:
    # The omitted-4th-arg branch (`${4-...}`, not `${4:-...}`): a direct caller that passes no
    # pre-read epoch gets a live read, while an explicitly-EMPTY 4th arg stays empty. Pinned so the
    # two are never collapsed to `:-`, which would silently re-read on every dispatcher call.
    state_dir = tmp_path / "afk-state"
    state_dir.mkdir()
    (state_dir / "done-284.epoch").write_text("1784066007\n")
    env = _dead_pane_env(tmp_path)
    prelude = f"slot_state() {{ echo busy; }}; read_progress_epoch() {{ echo {int(NOW) - 4459}; }}"

    omitted = _call(f"{prelude}; _wd_dead_idle_reason /the/wt 284 {NOW}", env=env).stdout
    explicit_empty = _call(f'{prelude}; _wd_dead_idle_reason /the/wt 284 {NOW} ""', env=env).stdout

    assert "done-epoch=1784066007" in omitted  # unset ⇒ live read finds the stamp
    assert "done-epoch=none" in explicit_empty  # set-but-empty ⇒ honored as empty


def test_run_conditions_dead_pane_ledger_line_carries_the_measured_base(tmp_path: Path) -> None:
    # End-to-end: the reason reaches the LEDGER, not just stdout — AC4 is about the ledger line.
    ledger = tmp_path / "l.jsonl"
    env = _dead_pane_dispatch_env(tmp_path)
    prelude = _dead_pane_dispatch_prelude(done_epoch="", drain="off")

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    line = ledger.read_text()
    assert '"condition":"dead-pane"' in line
    assert f"base=progress@{int(NOW) - 4459}" in line
    assert "slot_state=busy" in line
    assert "done-epoch=none" in line


def test_landmark_intervention_never_lands_only_marks(tmp_path: Path) -> None:
    # Escalate-only: the default raises a needs-human-land tag and NEVER runs a land.
    wt = _git_repo(tmp_path)

    _call(f"_wd_intervene_landmark '{wt}' 5")

    tags = subprocess.run(["git", "tag"], cwd=wt, capture_output=True, text=True).stdout
    assert "needs-human-land/5" in tags


# ── #263: the escalation self-clears once the drain lands the branch ───────────
def test_clear_landed_landmarks_removes_tag_when_issue_closed(tmp_path: Path) -> None:
    # A needs-human-land/<issue> raised by condition 4 dangles after the drain lands the branch;
    # the sweep drops it once the issue is CLOSED (landed), so a human is not pointed at
    # already-shipped work. A still-open issue keeps its tag — a human still owes that land.
    repo = _git_repo(tmp_path)
    for issue in (5, 7):
        subprocess.run(
            ["git", "tag", f"needs-human-land/{issue}"], cwd=repo, check=True, capture_output=True
        )
    env = {
        "HUB_WATCHDOG_LANDMARK_REPO": str(repo),
        # issue 5 has landed (closed); issue 7 is still open.
        "HUB_WATCHDOG_ISSUE_STATE_CMD": '[ "$1" = 5 ] && echo closed || echo open',
    }

    _call("_wd_clear_landed_landmarks", env=env)

    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout
    assert "needs-human-land/5" not in tags  # closed/landed → swept
    assert "needs-human-land/7" in tags  # still open → kept


def test_clear_landed_landmarks_keeps_tag_when_issue_state_ambiguous(tmp_path: Path) -> None:
    # Fail-safe: an ambiguous issue-state read (gh down, query failure, empty output) must KEEP
    # the escalation — never delete an un-landed human-land marker on a transient outage. Here
    # the state command echoes nothing, mirroring _wd_issue_open's fail-open-to-open path.
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "needs-human-land/9"], cwd=repo, check=True, capture_output=True)
    env = {"HUB_WATCHDOG_LANDMARK_REPO": str(repo), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo ''"}

    _call("_wd_clear_landed_landmarks", env=env)

    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout
    assert "needs-human-land/9" in tags  # ambiguous state → kept (fail-safe)


# ── _wd_fire writes the intervention-ledger ───────────────────────────────────
def test_fire_appends_a_ledger_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    result = _call("_wd_fire park-unanswered 5 'drain fell short'", env=env)

    assert result.returncode == 0, result.stderr
    line = ledger.read_text().strip()
    assert '"condition":"park-unanswered"' in line
    assert '"issue":"5"' in line
    assert f'"ts":{NOW}' in line
    assert "FIRING [park-unanswered] #5" in result.stdout  # _wd_log → stdout (daemon tees to log)


# ── #263: one ledger firing per condition+issue while unresolved ───────────────
def _ledger_lines(ledger: Path) -> list[str]:
    return [ln for ln in ledger.read_text().splitlines() if ln.strip()]


def test_fire_dedupes_repeat_firing_of_same_condition_issue(tmp_path: Path) -> None:
    # A persistent condition (or an in-flight land racing condition 4) must log ONE intervention,
    # not one per tick — else the #251 autonomy score is double-penalized (#263).
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)
    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)  # a later tick, same unresolved cond

    assert len(_ledger_lines(ledger)) == 1


def test_fire_reappends_after_the_firing_marker_is_cleared(tmp_path: Path) -> None:
    # Dedup is scoped to "while unresolved": once the condition clears, a genuine recurrence fires
    # afresh.
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)
    _call("_wd_clear_fired dead-pane 5", env=env)  # condition resolved
    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)  # recurs → re-fires

    assert len(_ledger_lines(ledger)) == 2


def test_clear_landed_landmarks_clears_the_condition4_firing_marker(tmp_path: Path) -> None:
    # A landed issue drops out of the in-flight loop, so the dispatcher's else-clear never runs for
    # it — the sweep must re-arm its condition-4 firing marker so a future genuine skip re-fires.
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "tag", "needs-human-land/5"], cwd=repo, check=True, capture_output=True)
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-auto-land-skipped-5"  # dir == dirname(ledger)
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LANDMARK_REPO": str(repo),
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed",
    }

    _call("_wd_clear_landed_landmarks", env=env)

    assert not marker.exists()


# ── #290 AC5: the dangling dead-pane firing marker is swept once the issue lands ──
# A dead-pane fire raises no needs-human-land tag, so _wd_clear_landed_landmarks never revisits it.
# The dispatcher's else-clear only runs for IN-FLIGHT worktrees, and a landed issue's worktree is
# gone next tick — so wd-fire-dedup-dead-pane-<N> dangles forever (the #284 marker still on disk).
def test_sweep_drops_the_dead_pane_marker_for_a_landed_issue(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    landed = tmp_path / "wd-fire-dedup-dead-pane-284"  # dir == dirname(ledger)
    still_open = tmp_path / "wd-fire-dedup-dead-pane-7"
    landed.write_text("")
    still_open.write_text("")
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": '[ "$1" = 284 ] && echo closed || echo open',
    }

    _call('_wd_sweep_dead_pane_markers ""', env=env)

    assert not landed.exists()  # closed/landed → swept
    assert still_open.exists()  # still open → a genuine unresolved condition, kept


def test_sweep_keeps_the_dead_pane_marker_for_an_in_flight_issue(tmp_path: Path) -> None:
    # An issue whose worktree is still in-flight this tick is the DISPATCHER's to clear — its
    # detector may legitimately still be firing. Sweeping it here would re-arm the dedup mid-fire
    # and let the same unresolved condition double-count in the ledger (#263).
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-dead-pane-284"
    marker.write_text("")
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}

    _call("_wd_sweep_dead_pane_markers 284", env=env)

    assert marker.exists(), "an in-flight issue is the dispatcher's to clear, not the sweep's"


def test_sweep_in_flight_check_does_not_substring_match_a_longer_issue(tmp_path: Path) -> None:
    # The in-flight membership test is a substring match over a space-joined list. Both the list and
    # the pattern are space-padded so #4 cannot match an in-flight list of "14 284" — an unpadded
    # match would skip #4's genuinely dangling marker and leave it deduped into silence forever.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-dead-pane-4"
    marker.write_text("")
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}

    _call('_wd_sweep_dead_pane_markers "14 284"', env=env)

    assert not marker.exists(), "#4 is not in flight — 14/284 must not mask it"


def test_sweep_keeps_the_dead_pane_marker_when_the_issue_state_is_ambiguous(tmp_path: Path) -> None:
    # Fail-safe, mirroring _wd_clear_landed_landmarks: a gh outage / empty read must never be
    # treated as "landed". _wd_issue_open reads an unknown state as OPEN.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-dead-pane-9"
    marker.write_text("")
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo ''"}

    _call('_wd_sweep_dead_pane_markers ""', env=env)

    assert marker.exists()


def test_sweep_ignores_a_non_numeric_marker_stem(tmp_path: Path) -> None:
    # The supervisor-dead condition files its firing under the issue "-"; the glob must not try to
    # resolve that as an issue number.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-dead-pane--"
    marker.write_text("")
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed"}

    result = _call('_wd_sweep_dead_pane_markers ""', env=env)

    assert result.returncode == 0
    assert marker.exists()


def test_run_conditions_sweeps_a_landed_dead_pane_marker(tmp_path: Path) -> None:
    # End-to-end: the #284 shape after the land completed — the worktree is gone, so the issue is
    # no longer in-flight and the dispatcher's else-clear can never run for it again.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-dead-pane-284"
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "HUB_WATCHDOG_ISSUE_STATE_CMD": "echo closed",
        "AFK_NOW": NOW,
    }
    prelude = 'inflight_worktrees() { printf ""; }'  # #284 landed → nothing in flight

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert not marker.exists()


# ── the dispatcher: firing dedup re-arms when a condition resolves (#263) ──────
def test_run_conditions_clears_firing_marker_when_condition_resolves(tmp_path: Path) -> None:
    # #263: a detector that does NOT fire this tick clears its firing marker, so a genuinely
    # resolved-then-recurring condition re-fires rather than staying deduped forever.
    ledger = tmp_path / "l.jsonl"
    marker = tmp_path / "wd-fire-dedup-park-unanswered-5"  # a firing from a prior tick
    marker.write_text("")
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "AFK_NOW": NOW,
    }
    # The spoke is now busy (no park, not done) → no detector fires → the else-clears run.
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        'slot_state() { echo busy; }; _spoke_pane_target() { echo "hub:0"; }; '
        'read_answer_attempt() { echo ""; }; read_progress_epoch() { echo ' + NOW + "; }; "
        'read_done_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    assert not marker.exists()


def test_run_conditions_dedupes_ledger_but_keeps_intervening(tmp_path: Path) -> None:
    # The safety invariant of the dedup: across repeated ticks of the SAME unresolved condition,
    # the ledger records ONE firing but the scripted intervention still runs EVERY tick (#263) —
    # log once, keep intervening. A refactor folding the intervene call into _wd_fire's early
    # return would silently break this.
    ledger = tmp_path / "l.jsonl"
    answers = tmp_path / "answers"  # one line per answer intervention
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ANSWER_CMD": f"printf 'x\\n' >> {answers}",
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        # Drain off so the AC4 mid-service guard never defers (hermetic vs a real armed drain).
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        # #283: pin the state dir so a REAL decision-journal record cannot read as servicing.
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    # A parked spoke never answered, parked past the ceiling → park-unanswered fires; the same
    # park persists across ticks (onset stays stale, #265).
    onset = str(int(NOW) - 700)
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        f"{_GATE_LANE}; "  # #283: the answer ceiling applies to answer-lane parks only
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        # Progress stays EMPTY: a fresh progress epoch now reads as the drain servicing the spoke.
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)  # tick 1: fire + intervene
    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)  # tick 2: deduped fire, intervene

    assert len(_ledger_lines(ledger)) == 1  # one ledger firing across both ticks
    assert answers.read_text().count("x") == 2  # but intervened on both ticks


# ── the dispatcher: detect → fire → intervene, end to end ─────────────────────
def test_run_conditions_fires_supervisor_dead_from_passed_state(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rearm = tmp_path / "rearmed"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_REARM_CMD": f"touch {rearm}",
        # Isolate the landmark sweep off the real repo (a nonexistent path → git no-ops).
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        "AFK_NOW": NOW,
    }
    # inflight_worktrees stubbed empty so only the global supervisor check runs.
    prelude = "inflight_worktrees() { :; }"

    _call(f"{prelude}; _wd_run_conditions {NOW} stale", env=env)

    assert '"condition":"supervisor-dead"' in ledger.read_text()
    assert rearm.exists(), "supervisor-dead must re-arm the drain"


def test_run_conditions_fires_park_and_invokes_answer_seam(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    answered = tmp_path / "answered"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_ANSWER_CMD": f"touch {answered}",
        # Isolate the landmark sweep off the real repo (a nonexistent path → git no-ops).
        "HUB_WATCHDOG_LANDMARK_REPO": str(tmp_path / "no-landmark-repo"),
        # Drain off so the AC4 mid-service guard never defers (hermetic vs a real armed drain).
        "AFK_STATE": str(tmp_path / "absent-afk-state"),
        # #283: the servicing check reads the drain's decision journal out of the state dir —
        # pin it at a scratch path so a REAL journal record for this issue cannot suppress the fire.
        "AFK_STATE_DIR": str(tmp_path / "afk-state"),
        "AFK_NOW": NOW,
    }
    onset = str(
        int(NOW) - 700
    )  # parked past the ceiling (#265) so the never-attempted branch fires
    prelude = (
        'inflight_worktrees() { printf "/the/wt\\t5\\n"; }; '
        f"{_GATE_LANE}; "  # #283: the answer ceiling applies to answer-lane parks only
        'slot_state() { echo waiting; }; read_answer_attempt() { echo ""; }; '
        f"read_park_onset_epoch() {{ echo {onset}; }}; "
        # A live pane is what keeps dead-idle quiet here; progress must stay EMPTY, since a fresh
        # progress epoch now reads as the drain servicing the spoke and would suppress the fire.
        '_spoke_pane_target() { echo "hub:0"; }; read_progress_epoch() { echo ""; }'
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} live", env=env)

    line = ledger.read_text()
    assert '"condition":"park-unanswered"' in line
    assert answered.exists(), "a park firing must invoke the answer intervention"
    # #283 AC5: the MEASURED base reaches the ledger line itself — not just the reason function —
    # so a future false positive is diagnosable from the ledger alone.
    assert f"base=park-onset@{onset}" in line, "the ledger line names which epoch was measured"
    assert "lane=gate" in line


# ── the instrument: classify + file the defect (issue #251, subtask 4) ─────────
# Every firing is classified {afk-defect|novel-decision} so a genuine human-call escalation is
# not mis-filed as an afk bug; afk-defects file (deduped) via a headless bug-scoper. Filing is
# gated by HUB_WATCHDOG_FILE (default OFF in _call) so no test hits the live gh/hub-agent.


# ── issue #297 defect 3: _wd_classify reads the push-failure-only blocked record ──────────────
# A park the reasoner deliberately escalated is a novel-decision (a real human call), not an afk
# bug. But _wd_classify detected that solely via _afk_blocked_record — the DURABLE LOCAL record
# gate-broker-markers.sh writes ONLY when the `git push` of the blocked/<issue> tag FAILS (the #109
# fallback). The common case — the tag pushes fine — leaves no file at all, so the watchdog called
# every successful escalation an afk-defect: it auto-filed a bogus bug against afk and docked the
# #251 autonomy score for the reasoner doing its job correctly. The authoritative signal is the tag
# itself, which the dispatcher already trusts 150 lines earlier (_wd_detect_mergeable_skipped's
# `_wd_tag_at_tip "$wt" blocked`); _wd_classify simply never got the wt to check it with.
def _blocked_tag(wt: Path, issue: str = "5") -> None:
    """Tag blocked/<issue> at the tip the way production does — ANNOTATED (spoke-ready's `-a`).

    Lightweight tags resolve straight to the commit, so they cannot catch a `_wd_tag_at_tip` that
    forgets its `^{commit}` peel; an annotated tag resolves to the TAG OBJECT and would never equal
    HEAD. The fixture has to be the real shape or the peel is unpinned.
    """
    subprocess.run(
        ["git", "tag", "-f", "-a", f"blocked/{issue}", "-m", "needs a human"],
        cwd=wt,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _blocked_tag_epoch(wt: Path, issue: str = "5") -> int:
    """The blocked tag's own creation epoch — the clock _wd_escalation_is_live compares against."""
    return int(
        subprocess.run(
            ["git", "for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/blocked/{issue}"],
            cwd=wt,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


def test_classify_reads_the_blocked_tag_at_tip_as_a_novel_decision(tmp_path: Path) -> None:
    # The common escalation: spoke-ready pushed blocked/5 successfully, so NO durable record exists.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    env = {"AFK_STATE_DIR": str(tmp_path / "state")}  # no blocked-5.txt — the push succeeded

    out = _call(f"_wd_classify park-unanswered 5 '{wt}'", env=env).stdout.strip()

    assert out == "novel-decision", (
        "a deliberately escalated park is a human call, not an afk defect to file a bug against"
    )


def test_classify_files_a_defect_for_a_park_that_began_after_the_escalation(
    tmp_path: Path,
) -> None:
    # A blocked tag is only ever cleared by a later COMMIT — a human answering clears nothing, the
    # spoke resuming clears nothing. So a spoke that escalates, gets answered, resumes and re-parks
    # on a NEW question (all before its first commit — the common shape, since escalations usually
    # precede any RED/GREEN) carries its old tag at the tip. Reading that as "a human call" would
    # exempt the spoke from defect filing for the rest of the window and flatter the autonomy score
    # — the dangerous direction, since the score exists to be honest about afk's shortfalls.
    # The park ONSET names the episode actually pending: newer than the tag ⇒ a different question.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    state = tmp_path / "state"
    state.mkdir()
    # Relative to the tag's OWN epoch, not the suite's fake NOW: git stamps the tag with the real
    # wall clock, so a fixed constant would land on whichever side of it the calendar happens to put.
    (state / "park-onset-5.epoch").write_text(f"{_blocked_tag_epoch(wt) + 500}\n")
    env = {"AFK_STATE_DIR": str(state)}

    out = _call(f"_wd_classify park-unanswered 5 '{wt}'", env=env).stdout.strip()

    assert out == "afk-defect", (
        "a park that began AFTER the escalation is a new question — its non-answer is a real defect"
    )


def test_classify_keeps_novel_decision_for_the_escalations_own_episode(tmp_path: Path) -> None:
    # The complement, so the episode bound cannot widen into "never novel-decision": the escalation
    # is raised DURING the episode it belongs to, so that onset precedes the tag.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    state = tmp_path / "state"
    state.mkdir()
    (state / "park-onset-5.epoch").write_text(f"{_blocked_tag_epoch(wt) - 500}\n")
    env = {"AFK_STATE_DIR": str(state)}

    out = _call(f"_wd_classify park-unanswered 5 '{wt}'", env=env).stdout.strip()

    assert out == "novel-decision"


def test_classify_park_undeliverable_still_files_despite_a_blocked_tag(tmp_path: Path) -> None:
    # The blocked tag is emitted BY a delivery failure (gate-broker's _escalate_blocked when the
    # inject cannot be verified), so reading it as a human call here would silence exactly the
    # defect class #288 AC3 added park-undeliverable to surface. The drain being unable to deliver
    # an answer is afk's shortfall, not a novel decision — it must keep filing.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    state = tmp_path / "state"
    state.mkdir()
    (state / "blocked-5.txt").write_text("1784066007\tcould not inject\n")  # same writer, same lane
    env = {"AFK_STATE_DIR": str(state)}

    out = _call(f"_wd_classify park-undeliverable 5 '{wt}'", env=env).stdout.strip()

    assert out == "afk-defect", (
        "the escalation IS the delivery failure — it must not excuse afk from the defect"
    )


def test_classify_still_reads_the_durable_record_when_the_tag_push_failed(tmp_path: Path) -> None:
    # The #109 fallback must keep working: no tag (the push failed), but the durable local record
    # says the reasoner escalated. Both signals mean the same thing — a real human call.
    wt = _git_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "blocked-5.txt").write_text("1784066007\tneeds a human\n")
    env = {"AFK_STATE_DIR": str(state)}

    out = _call(f"_wd_classify park-unanswered 5 '{wt}'", env=env).stdout.strip()

    assert out == "novel-decision"


def test_classify_is_an_afk_defect_when_the_blocked_tag_is_behind_the_tip(tmp_path: Path) -> None:
    # A blocked/ tag the spoke has since committed on top of is STALE (the #103 coexistence
    # _wd_blocked_stale exists for) — live state wins, so this is not a live escalation and the
    # firing is a real afk defect. Pins that the fix uses tag-AT-TIP, not merely tag-exists.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "c2"],
        cwd=wt,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    env = {"AFK_STATE_DIR": str(tmp_path / "state")}

    out = _call(f"_wd_classify park-unanswered 5 '{wt}'", env=env).stdout.strip()

    assert out == "afk-defect", "a stale blocked tag behind the tip is not a live escalation"


def test_fire_threads_the_worktree_into_classify(tmp_path: Path) -> None:
    # The plumbing: _wd_fire is where the class is decided, so it must carry the wt through or the
    # tag can never be read at the only place it matters — the real firing path.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    ledger = tmp_path / "l.jsonl"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "AFK_STATE_DIR": str(tmp_path / "state"),
        "AFK_NOW": NOW,
    }

    _call(f"_wd_fire park-unanswered 5 'parked too long' '{wt}'", env=env)

    assert '"class":"novel-decision"' in ledger.read_text()


def test_classify_seam_receives_the_worktree(tmp_path: Path) -> None:
    # The override seam owns the whole decision, so it must be handed every input the built-in has
    # — else an operator's classifier cannot reach the authoritative signal either.
    seen = tmp_path / "seen"
    env = {
        "HUB_WATCHDOG_CLASSIFY_CMD": f'printf "%s %s %s" "$1" "$2" "$3" > {seen}; echo afk-defect'
    }

    _call("_wd_classify park-unanswered 5 /the/wt", env=env)

    assert seen.read_text() == "park-unanswered 5 /the/wt"


def test_run_conditions_does_not_file_a_defect_for_a_deliberate_escalation(tmp_path: Path) -> None:
    # End to end through the DISPATCHER, with filing switched on: a blocked-at-tip park must reach
    # the ledger as novel-decision and dispatch NO bug-scoper. This is the defect's real cost — a
    # bogus auto-filed issue against afk every time the reasoner correctly escalates.
    wt = _git_repo(tmp_path)
    _blocked_tag(wt)
    ledger = tmp_path / "l.jsonl"
    scoped = tmp_path / "scoped"
    env = _dead_pane_env(
        tmp_path,
        HUB_WATCHDOG_LEDGER=str(ledger),
        HUB_WATCHDOG_LANDMARK_REPO=str(tmp_path / "no-landmark-repo"),
        HUB_WATCHDOG_FILE="1",
        HUB_WATCHDOG_DEDUP_CMD="true",
        HUB_WATCHDOG_LABEL_CMD="true",
        HUB_WATCHDOG_SCOPER_CMD=f'printf "%s %s" "$1" "$2" >> {scoped}',
        AFK_STATE_DIR=str(tmp_path / "state"),
    )
    prelude = (
        f"inflight_worktrees() {{ printf '%s\\t5\\n' '{wt}'; }}; "
        "_wd_detect_park_unanswered() { return 0; }; "
        "_wd_detect_park_undeliverable() { return 1; }; "
        "_wd_detect_dead_idle() { return 1; }; "
        "_wd_detect_stale_marker() { return 1; }; "
        "_wd_detect_mergeable_skipped() { return 1; }; "
        '_wd_park_unanswered_reason() { echo "parked too long"; }; '
        "_wd_intervene_answer() { :; }"
    )

    _call(f"{prelude}; _wd_run_conditions {NOW} off", env=env)

    assert '"class":"novel-decision"' in ledger.read_text(), ledger.read_text()
    assert not scoped.exists(), "a correct escalation must never auto-file a bug against afk"


def test_classify_defaults_to_afk_defect() -> None:
    assert _call("_wd_classify dead-pane 5").stdout.strip() == "afk-defect"


def test_classify_seam_overrides_to_novel_decision(tmp_path: Path) -> None:
    env = {"HUB_WATCHDOG_CLASSIFY_CMD": "echo novel-decision"}
    assert _call("_wd_classify park-unanswered 5", env=env).stdout.strip() == "novel-decision"


def test_fire_records_the_class_in_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    env = {"HUB_WATCHDOG_LEDGER": str(ledger), "AFK_NOW": NOW}

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)

    assert '"class":"afk-defect"' in ledger.read_text()


def test_afk_defect_firing_dispatches_the_scoper(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_FILE": "1",  # opt back in
        "HUB_WATCHDOG_DEDUP_CMD": "true",  # no open dup (empty stdout)
        "HUB_WATCHDOG_LABEL_CMD": "true",
        "HUB_WATCHDOG_SCOPER_CMD": f'printf "%s %s" "$1" "$2" > {scoped}',
        "AFK_STATE_DIR": str(tmp_path / "state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_fire dead-pane 5 'reaper missed it'", env=env)

    assert scoped.read_text() == "dead-pane 5", "afk-defect must dispatch the bug-scoper"


def test_novel_decision_firing_does_not_file(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_LEDGER": str(tmp_path / "l.jsonl"),
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_CLASSIFY_CMD": "echo novel-decision",
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
        "AFK_NOW": NOW,
    }

    _call("_wd_fire park-unanswered 5 'a real human call'", env=env)

    assert not scoped.exists(), "a novel human decision must NOT be filed as an afk bug"


def test_file_defect_dedups_within_the_run(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    env = {
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_DEDUP_CMD": "true",
        "HUB_WATCHDOG_LABEL_CMD": "true",
        "HUB_WATCHDOG_SCOPER_CMD": f"echo x >> {calls}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call(
        "_wd_file_defect dead-pane 5 r; _wd_file_defect dead-pane 5 r; _wd_file_defect dead-pane 5 r",
        env=env,
    )

    assert calls.read_text().count("x") == 1, "one defect per condition+issue per run"


def test_file_defect_skips_when_open_defect_exists(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_FILE": "1",
        "HUB_WATCHDOG_DEDUP_CMD": "echo 999",  # a matching open issue exists
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call("_wd_file_defect dead-pane 5 r", env=env)

    assert not scoped.exists(), "an already-open afk-defect must not be duplicated"


def test_file_defect_off_by_gate_does_not_dispatch(tmp_path: Path) -> None:
    scoped = tmp_path / "scoped"
    env = {
        "HUB_WATCHDOG_FILE": "0",  # gated off
        "HUB_WATCHDOG_SCOPER_CMD": f"touch {scoped}",
        "AFK_STATE_DIR": str(tmp_path / "state"),
    }

    _call("_wd_file_defect dead-pane 5 r", env=env)

    assert not scoped.exists(), "HUB_WATCHDOG_FILE=0 suppresses filing"


# ── the autonomy score + report (issue #251, subtask 5) ────────────────────────
# score = 1 - interventions/spokes; a ZERO-firing run scores 1.000 - the pass criterion for
# "afk autonomous on this backlog". These pin the arithmetic + the morning report.


def _seed_spokes(state_dir: Path, issues: list[int]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for n in issues:
        (state_dir / f"dispatch-{n}.epoch").write_text("1783880000\n")


def _seed_ledger(ledger: Path, lines: list[str]) -> None:
    ledger.write_text("".join(f"{line}\n" for line in lines))


def test_spokes_serviced_counts_dispatch_epochs(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9])

    result = _call("_wd_spokes_serviced", env={"AFK_STATE_DIR": str(state)})

    assert result.stdout.strip() == "3", result.stderr


def test_intervention_count_counts_ledger_lines(tmp_path: Path) -> None:
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"condition":"a"}', '{"condition":"b"}'])

    result = _call("_wd_intervention_count", env={"HUB_WATCHDOG_LEDGER": str(ledger)})

    assert result.stdout.strip() == "2", result.stderr


def test_autonomy_score_is_one_on_a_zero_firing_run(tmp_path: Path) -> None:
    # The pass criterion: spokes serviced, no firing ⇒ afk was autonomous ⇒ 1.000.
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9, 11])
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(tmp_path / "absent.jsonl")}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "1.000"


def test_autonomy_score_reflects_intervention_ratio(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9, 11])  # 4 spokes
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"condition":"dead-pane"}'])  # 1 firing → 1 - 1/4 = 0.75
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(ledger)}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "0.750"


def test_autonomy_score_clamps_at_zero(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5])  # 1 spoke
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"c":1}', '{"c":2}', '{"c":3}'])  # 3 firings → 1 - 3 < 0 ⇒ 0.000
    env = {"AFK_STATE_DIR": str(state), "HUB_WATCHDOG_LEDGER": str(ledger)}

    assert _call("_wd_autonomy_score", env=env).stdout.strip() == "0.000"


def test_report_prints_the_summary_line(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7])
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"class":"afk-defect"}'])
    env = {
        "AFK_STATE_DIR": str(state),
        "HUB_WATCHDOG_LEDGER": str(ledger),
        "HUB_WATCHDOG_NO_TELEMETRY": "1",
    }

    result = _call("_wd_report", env=env)

    assert "interventions=1" in result.stdout
    assert "defects_filed=1" in result.stdout
    assert "spokes_serviced=2" in result.stdout
    assert "autonomy_score=0.500" in result.stdout


def test_cli_report_on_zero_firing_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _seed_spokes(state, [5, 7, 9])
    result = _run(
        "--report",
        env={
            "AFK_STATE_DIR": str(state),
            "HUB_WATCHDOG_LEDGER": str(tmp_path / "none.jsonl"),
            "HUB_WATCHDOG_NO_TELEMETRY": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "autonomy_score=1.000" in result.stdout


def test_defect_count_is_single_line_when_no_afk_defects(tmp_path: Path) -> None:
    # An existing ledger with zero afk-defect lines: grep -c prints "0" AND exits 1, so a naive
    # `&& grep || echo 0` would double-print "0\n0" and corrupt the report. Must be exactly "0".
    ledger = tmp_path / "l.jsonl"
    _seed_ledger(ledger, ['{"class":"novel-decision"}'])

    result = _call("_wd_defect_count", env={"HUB_WATCHDOG_LEDGER": str(ledger)})

    assert result.stdout.strip() == "0"
    assert result.stdout.count("0") == 1, f"exactly one '0', not a double-print: {result.stdout!r}"
