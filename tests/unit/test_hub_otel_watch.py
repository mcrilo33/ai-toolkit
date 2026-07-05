"""Unit tests for shared/skills/hub/scripts/hub-otel-watch.sh.

The hub-side OTel watchdog (issue #115): when ≥1 spoke pane is live it ensures the
otelcol collector (:4317) and the Langfuse message bridge (:4319) are up —
recycling a dead/stale one via the worktree-lib ensure paths — and is a silent
no-op when no spoke runs. Meant to be run on a loop from the hub (main checkout).

These tests source the script (a source-guard keeps ``main`` from running on
import) and drive its layers directly with the tmux/worktree probes and the
docker-touching preflights stubbed, so no real tmux, git, or docker is invoked:

  * ``main`` orchestration — ensure exactly when a spoke pane is live, else silent;
  * ``spoke_pane_live`` — the pane-vs-spoke-worktree correlation predicate.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_OTEL_WATCH = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-otel-watch.sh"


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    """Source hub-otel-watch.sh and invoke a shell expression against its functions."""
    return subprocess.run(
        ["bash", "-c", f'source "{HUB_OTEL_WATCH}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


# Stub the two docker-touching preflights so a run prints markers instead of
# spawning anything, force MAIN_ROOT so the ensure target is deterministic, and
# opt into native OTel (AI_TOOLKIT_OTEL=1) so main() takes the ensure path rather
# than the opted-out notice.
_ENSURE_STUBS = "; ".join(
    [
        "MAIN_ROOT=/repo",
        "export AI_TOOLKIT_OTEL=1",
        'wt_otel_collector_preflight() { echo "COLLECTOR $1"; }',
        'wt_otel_bridge_preflight() { echo "BRIDGE $1"; }',
    ]
)


# ── main orchestration ────────────────────────────────────────────────────────


def test_main_ensures_stack_when_spoke_pane_live() -> None:
    # A spoke pane is live → ensure BOTH collector and bridge against MAIN_ROOT.
    result = _call(f"spoke_pane_live() {{ return 0; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR /repo" in result.stdout
    assert "BRIDGE /repo" in result.stdout


def test_main_ensures_collector_before_bridge() -> None:
    # Ordering matters: the collector forks LLM I/O + audit events to the bridge,
    # so it must be ensured first — mirror wt_otel_collector_preflight's "run BEFORE
    # the bridge preflight" contract.
    result = _call(f"spoke_pane_live() {{ return 0; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert result.stdout.index("COLLECTOR") < result.stdout.index("BRIDGE")


def test_main_is_silent_noop_when_no_spoke_pane() -> None:
    # No spoke pane live → touch nothing (a quiet no-op; the stack need not run when
    # no spoke does). Never fail.
    result = _call(f"spoke_pane_live() {{ return 1; }}; {_ENSURE_STUBS}; main")

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR" not in result.stdout
    assert "BRIDGE" not in result.stdout


def test_main_warns_and_skips_when_otel_opted_out() -> None:
    # A spoke IS live but AI_TOOLKIT_OTEL != 1 → the preflights would silently
    # no-op and that spoke's traces are lost (the #115 footgun). main() must NOT
    # ensure, and must surface a one-line stderr notice rather than fail.
    parts = [
        "spoke_pane_live() { return 0; }",
        "MAIN_ROOT=/repo",
        "unset AI_TOOLKIT_OTEL",
        'wt_otel_collector_preflight() { echo "COLLECTOR $1"; }',
        'wt_otel_bridge_preflight() { echo "BRIDGE $1"; }',
        "main",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR" not in result.stdout
    assert "BRIDGE" not in result.stdout
    assert "AI_TOOLKIT_OTEL" in result.stderr


# ── spoke_pane_live predicate ─────────────────────────────────────────────────


def test_spoke_pane_live_true_when_pane_sits_in_spoke_worktree() -> None:
    # A tmux pane's path equals a spoke worktree path → live.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf "%s\\n" /repo/wt-115; }',
        '_pane_paths() { printf "%s\\n" /repo/wt-115; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LIVE"


def test_spoke_pane_live_false_when_only_hub_pane() -> None:
    # The only pane sits in the hub (not a spoke worktree) → idle, no ensure.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf "%s\\n" /repo/wt-115; }',
        '_pane_paths() { printf "%s\\n" /repo; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IDLE"


def test_spoke_pane_live_false_when_no_spoke_worktrees() -> None:
    # No linked spoke worktrees at all → idle regardless of panes.
    parts = [
        "MAIN_ROOT=/repo",
        '_spoke_worktree_paths() { printf ""; }',
        '_pane_paths() { printf "%s\\n" /repo/wt-115; }',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "IDLE"


# ── real-worktree coverage (porcelain parse, hub-exclusion, symlink resolve) ──


@pytest.fixture()
def hub_with_spoke(tmp_path: Path) -> tuple[Path, Path]:
    """A real git hub checkout with one linked spoke worktree.

    Returns (hub, spoke) as canonical paths so callers can assert against them
    without the tmp_path symlink (/var → /private/var on macOS) getting in the way.
    """
    hub = tmp_path / "hub"
    hub.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(hub), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    (hub / "f").write_text("x")
    git("add", "f")
    git("commit", "-qm", "init")
    spoke = tmp_path / "spoke"
    git("worktree", "add", "-q", "-b", "spoke", str(spoke))
    return Path(os.path.realpath(hub)), Path(os.path.realpath(spoke))


def test_spoke_worktree_paths_lists_spoke_excludes_hub(hub_with_spoke: tuple[Path, Path]) -> None:
    # The real porcelain parse + hub self-exclusion: only the spoke worktree is
    # listed, never the hub main checkout itself.
    hub, spoke = hub_with_spoke
    result = _call(f"MAIN_ROOT={hub}; _spoke_worktree_paths")

    assert result.returncode == 0, result.stderr
    resolved = [os.path.realpath(ln) for ln in result.stdout.splitlines() if ln.strip()]
    assert str(spoke) in resolved
    assert str(hub) not in resolved


def test_spoke_pane_live_resolves_symlinked_pane_path(
    hub_with_spoke: tuple[Path, Path], tmp_path: Path
) -> None:
    # A pane whose path reaches the spoke through a symlinked root (the /tmp →
    # /private/tmp trap) still correlates: spoke_pane_live canonicalizes both the
    # real spoke worktree path and the symlinked pane path. Would read IDLE if
    # wt_realpath resolution were dropped, so this genuinely exercises it.
    hub, spoke = hub_with_spoke
    link = tmp_path / "link"
    link.symlink_to(spoke)
    parts = [
        f"MAIN_ROOT={hub}",
        f'_pane_paths() {{ printf "%s\\n" {link}; }}',
        "spoke_pane_live && echo LIVE || echo IDLE",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LIVE"


# ── daemon mode (#138): _watch_loop ticks + --daemon pidfile singleton ───────
#
# The watchdog must survive a machine sleep with no human in the loop: worktree-
# new.sh arms `hub-otel-watch.sh --daemon`, which re-runs the ensure path every
# HUB_OTEL_WATCH_INTERVAL seconds while ≥1 spoke pane is live and exits itself
# after HUB_OTEL_WATCH_IDLE_TICKS consecutive idle ticks. These tests drive
# _watch_loop/_daemon with the pane probe and preflights stubbed (interval 0),
# so no real tmux/docker/sleep-wake is needed.

_LOOP_ENV = "; ".join(
    [
        "MAIN_ROOT=/repo",
        "export AI_TOOLKIT_OTEL=1",
        "export HUB_OTEL_WATCH_INTERVAL=0",
        "export HUB_OTEL_WATCH_IDLE_TICKS=3",
        'wt_otel_collector_preflight() { echo "COLLECTOR $1"; }',
        'wt_otel_bridge_preflight() { echo "BRIDGE $1"; }',
    ]
)


def _pane_pattern_stub(tmp_path: Path, pattern: str) -> str:
    """A spoke_pane_live stub scripted per tick: 'L' = live, anything else = idle.

    Ticks beyond the pattern read as idle, so every loop terminates. The tick
    count persists in a file the test can assert on.
    """
    ticks = tmp_path / "ticks"
    return (
        f'TICKS="{ticks}"; PATTERN="{pattern}"; '
        "spoke_pane_live() { "
        'n=$(( $(cat "$TICKS" 2>/dev/null || echo 0) + 1 )); printf "%s" "$n" > "$TICKS"; '
        'c="${PATTERN:$((n-1)):1}"; [ "$c" = "L" ]; }'
    )


def test_watch_loop_ensures_stack_on_each_live_tick(tmp_path: Path) -> None:
    # Two live ticks → the ensure pair runs twice, collector before bridge each
    # time (the one-shot ordering contract holds in daemon mode too).
    result = _call(f"{_pane_pattern_stub(tmp_path, 'LL')}; {_LOOP_ENV}; _watch_loop")

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("COLLECTOR /repo") == 2
    assert result.stdout.count("BRIDGE /repo") == 2
    assert result.stdout.index("COLLECTOR") < result.stdout.index("BRIDGE")


def test_watch_loop_exits_after_consecutive_idle_ticks(tmp_path: Path) -> None:
    # No spoke ever live → the loop tears itself down after exactly the idle
    # grace (3 ticks), never having ensured anything.
    result = _call(f"{_pane_pattern_stub(tmp_path, '')}; {_LOOP_ENV}; _watch_loop")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3"
    assert "COLLECTOR" not in result.stdout


def test_watch_loop_live_tick_resets_idle_counter(tmp_path: Path) -> None:
    # live, idle, idle, live, then idle to exhaustion → the mid-pattern live tick resets the idle
    # counter, so the loop survives to tick 7 and ensured twice. Would exit at
    # tick 4 if the counter never reset.
    result = _call(f"{_pane_pattern_stub(tmp_path, 'LIIL')}; {_LOOP_ENV}; _watch_loop")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "7"
    assert result.stdout.count("COLLECTOR /repo") == 2


def test_watch_loop_surfaces_preflight_recovery_output(tmp_path: Path) -> None:
    # AC (#138): the recovery is observable. When a tick's preflight recycles a
    # dead collector its "→ started lf-collector" line must pass through the
    # loop untouched (it lands in the daemon logfile).
    parts = [
        _pane_pattern_stub(tmp_path, "L"),
        "MAIN_ROOT=/repo",
        "export AI_TOOLKIT_OTEL=1",
        "export HUB_OTEL_WATCH_INTERVAL=0",
        "export HUB_OTEL_WATCH_IDLE_TICKS=1",
        'wt_otel_collector_preflight() { echo "→ started lf-collector (otelcol) on :4317/:4318/:4418/:8889"; }',
        "wt_otel_bridge_preflight() { :; }",
        "_watch_loop",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "→ started lf-collector" in result.stdout


def test_daemon_refuses_second_start_while_pid_alive(tmp_path: Path) -> None:
    # A pidfile naming a LIVE pid (the test shell's own $$) → the second --daemon
    # start must refuse: no loop tick, the other daemon's pidfile left intact.
    # Liveness must come from `kill -0`, not pgrep (locale trap).
    pidfile = tmp_path / "watch.pid"
    parts = [
        _pane_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_OTEL_WATCH_PIDFILE="{pidfile}"',
        f'export HUB_OTEL_WATCH_LOG="{tmp_path / "watch.log"}"',
        f'printf "%s" "$$" > "{pidfile}"',
        "_daemon",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr
    assert not (tmp_path / "ticks").exists()
    assert pidfile.exists()


def test_daemon_ignores_stale_pidfile_and_removes_own_on_exit(tmp_path: Path) -> None:
    # A pidfile naming a DEAD pid (reaped background child) must not block a new
    # daemon: the loop runs (all-idle → 3 ticks) and the pidfile is gone on exit.
    pidfile = tmp_path / "watch.pid"
    parts = [
        _pane_pattern_stub(tmp_path, ""),
        _LOOP_ENV,
        f'export HUB_OTEL_WATCH_PIDFILE="{pidfile}"',
        f'export HUB_OTEL_WATCH_LOG="{tmp_path / "watch.log"}"',
        f'sleep 0.01 & _dead=$!; wait "$_dead"; printf "%s" "$_dead" > "{pidfile}"',
        "_daemon",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ticks").read_text() == "3"
    assert not pidfile.exists()


def test_daemon_appends_loop_output_to_logfile(tmp_path: Path) -> None:
    # The loop's output (ensure markers, recovery lines) belongs in the logfile,
    # not on the daemon's stdout — that's what makes a recovery observable after
    # the fact.
    logfile = tmp_path / "watch.log"
    parts = [
        _pane_pattern_stub(tmp_path, "L"),
        _LOOP_ENV,
        f'export HUB_OTEL_WATCH_PIDFILE="{tmp_path / "watch.pid"}"',
        f'export HUB_OTEL_WATCH_LOG="{logfile}"',
        "_daemon",
    ]
    result = _call("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "COLLECTOR /repo" in logfile.read_text()
    assert "COLLECTOR /repo" not in result.stdout


def test_daemon_removes_pidfile_on_sigterm(tmp_path: Path) -> None:
    # `kill <daemon>` is the normal teardown, and bash does NOT run the EXIT trap
    # on an untrapped fatal signal — without TERM/INT traps the pidfile survives,
    # and after OS pid reuse every future arm would refuse to start (the silent
    # no-watchdog failure #138 exists to kill).
    pidfile = tmp_path / "watch.pid"
    parts = [
        "spoke_pane_live() { return 0; }",
        _LOOP_ENV,
        "export HUB_OTEL_WATCH_INTERVAL=0.05",
        f'export HUB_OTEL_WATCH_PIDFILE="{pidfile}"',
        f'export HUB_OTEL_WATCH_LOG="{tmp_path / "watch.log"}"',
        "_daemon",
    ]
    proc = subprocess.Popen(
        ["bash", "-c", f'source "{HUB_OTEL_WATCH}"; {"; ".join(parts)}'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ},
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists(), "daemon never wrote its pidfile"

        proc.terminate()
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not pidfile.exists()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_daemon_cli_dispatch_reports_already_running(tmp_path: Path) -> None:
    # `hub-otel-watch.sh --daemon` (real script, no sourcing) must reach the
    # pidfile singleton guard: with a live-pid pidfile it reports and exits 0
    # before touching tmux/docker.
    pidfile = tmp_path / "watch.pid"
    pidfile.write_text(str(os.getpid()))
    result = subprocess.run(
        ["bash", str(HUB_OTEL_WATCH), "--daemon"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "HUB_OTEL_WATCH_PIDFILE": str(pidfile), "AI_TOOLKIT_OTEL": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "already running" in result.stdout + result.stderr


# ── spawn-time auto-arm (#138 ST2): wt_otel_watch_arm in worktree-lib.sh ─────
#
# worktree-new.sh arms `hub-otel-watch.sh --daemon` at every spoke spawn via
# wt_otel_watch_arm, so the watchdog exists whenever a spoke does — no human,
# no /loop. The arm is best-effort (never fails the spawn), a no-op unless
# AI_TOOLKIT_OTEL=1 or while a live daemon already holds the pidfile, and must
# forward AI_TOOLKIT_OTEL + LANGFUSE_BASIC_AUTH to the detached child even when
# the caller never exported them (worktree-new.sh's assignment is unexported).
# Tests live here (not test_worktree_lib.py) per the issue #138 scope list.

WORKTREE_LIB = REPO_ROOT / "scripts" / "worktree-lib.sh"
WORKTREE_NEW = REPO_ROOT / "scripts" / "worktree-new.sh"

# Env vars that would let the HOST steer an arm test (a real daemon's pidfile,
# the operator's OTel opt-in/secret) — always popped, then set per test.
_ARM_HOST_VARS = (
    "AI_TOOLKIT_OTEL",
    "LANGFUSE_BASIC_AUTH",
    "HUB_OTEL_WATCH_BIN",
    "HUB_OTEL_WATCH_PIDFILE",
    "HUB_OTEL_WATCH_LOG",
)


def _write_arm_stub(path: Path, record: Path) -> None:
    """A hub-otel-watch.sh stand-in that records its argv and the env it saw."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s|%s|%s\\n" "$*" "${{AI_TOOLKIT_OTEL:-}}" "${{LANGFUSE_BASIC_AUTH:-}}" > "{record}"\n'
    )
    path.chmod(0o755)


def _call_arm(body: str) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh and run a shell body with host arm-vars stripped."""
    env = {**os.environ}
    for k in _ARM_HOST_VARS:
        env.pop(k, None)
    return subprocess.run(
        ["bash", "-c", f'source "{WORKTREE_LIB}"; {body}'],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def test_watch_arm_launches_daemon_with_env_forwarded(tmp_path: Path) -> None:
    # Opted in, no live daemon → launch `<bin> --daemon` detached, and the child
    # sees AI_TOOLKIT_OTEL + LANGFUSE_BASIC_AUTH even though the caller never
    # exported them (plain shell assignments, as in worktree-new.sh).
    record = tmp_path / "record"
    stub = tmp_path / "hub-otel-watch.sh"
    _write_arm_stub(stub, record)
    parts = [
        "AI_TOOLKIT_OTEL=1",
        "LANGFUSE_BASIC_AUTH=sekret",
        f'HUB_OTEL_WATCH_BIN="{stub}"',
        f'HUB_OTEL_WATCH_PIDFILE="{tmp_path / "watch.pid"}"',
        f'wt_otel_watch_arm "{tmp_path}"',
    ]
    result = _call_arm("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert _wait_for(record), "arm never invoked the watch script"
    argv, otel, auth = record.read_text().strip().split("|")
    assert "--daemon" in argv.split()
    assert otel == "1"
    assert auth == "sekret"


@pytest.mark.parametrize("optout", ["", "0"])
def test_watch_arm_noop_unless_opted_in(tmp_path: Path, optout: str) -> None:
    # AI_TOOLKIT_OTEL unset or an explicit 0 → never launch anything, still rc 0
    # (the arm must not fail the spawn).
    record = tmp_path / "record"
    stub = tmp_path / "hub-otel-watch.sh"
    _write_arm_stub(stub, record)
    setter = f"AI_TOOLKIT_OTEL={optout}" if optout else "unset AI_TOOLKIT_OTEL"
    parts = [
        setter,
        f'HUB_OTEL_WATCH_BIN="{stub}"',
        f'HUB_OTEL_WATCH_PIDFILE="{tmp_path / "watch.pid"}"',
        f'wt_otel_watch_arm "{tmp_path}"',
    ]
    result = _call_arm("; ".join(parts))

    assert result.returncode == 0, result.stderr
    time.sleep(0.5)
    assert not record.exists()


def test_watch_arm_skips_when_daemon_already_live(tmp_path: Path) -> None:
    # The pidfile names a live pid → an armed watchdog already runs; N spoke
    # spawns must not stack N daemons (nor even fork a doomed child).
    record = tmp_path / "record"
    stub = tmp_path / "hub-otel-watch.sh"
    _write_arm_stub(stub, record)
    pidfile = tmp_path / "watch.pid"
    parts = [
        "AI_TOOLKIT_OTEL=1",
        f'HUB_OTEL_WATCH_BIN="{stub}"',
        f'HUB_OTEL_WATCH_PIDFILE="{pidfile}"',
        f'printf "%s" "$$" > "{pidfile}"',
        f'wt_otel_watch_arm "{tmp_path}"',
    ]
    result = _call_arm("; ".join(parts))

    assert result.returncode == 0, result.stderr
    time.sleep(0.5)
    assert not record.exists()


def test_watch_arm_warns_but_never_fails_when_script_missing(tmp_path: Path) -> None:
    # No resolvable hub-otel-watch.sh under the repo root (and no override) →
    # warn on stderr, return 0: capture degrades loudly, the spawn proceeds.
    parts = [
        "AI_TOOLKIT_OTEL=1",
        f'HUB_OTEL_WATCH_PIDFILE="{tmp_path / "watch.pid"}"',
        f'wt_otel_watch_arm "{tmp_path}"',
    ]
    result = _call_arm("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert "hub-otel-watch" in result.stderr


@pytest.mark.parametrize(
    "layout",
    [
        "shared/skills/hub/scripts",
        ".ai-toolkit/scripts",
        ".claude/skills/hub/scripts",
    ],
)
def test_watch_arm_resolves_repo_layouts(tmp_path: Path, layout: str) -> None:
    # Without an override the arm finds the watch script in the ai-toolkit
    # checkout AND in synced-target layouts (hub-*.sh live in .ai-toolkit/scripts
    # and .claude/skills/hub/scripts there).
    record = tmp_path / "record"
    _write_arm_stub(tmp_path / layout / "hub-otel-watch.sh", record)
    parts = [
        "AI_TOOLKIT_OTEL=1",
        f'HUB_OTEL_WATCH_PIDFILE="{tmp_path / "watch.pid"}"',
        f'wt_otel_watch_arm "{tmp_path}"',
    ]
    result = _call_arm("; ".join(parts))

    assert result.returncode == 0, result.stderr
    assert _wait_for(record), f"arm did not resolve {layout}"


def test_worktree_new_arms_watchdog_after_preflights() -> None:
    # The spawn path must arm the daemon (unconditionally at top level — the
    # function self-gates on AI_TOOLKIT_OTEL), and must do so AFTER the one-shot
    # preflight pair / the tmux spawn so the daemon's first tick can already see
    # the new spoke pane.
    text = WORKTREE_NEW.read_text()
    assert 'wt_otel_watch_arm "$REPO_ROOT"' in text
    assert text.index("wt_otel_watch_arm") > text.index('wt_otel_bridge_preflight "$REPO_ROOT"')
