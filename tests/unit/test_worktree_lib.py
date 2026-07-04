"""Unit tests for the portable date/time helpers in scripts/worktree-lib.sh.

``wt_date_ymd`` / ``wt_epoch_at`` live here so the unattended supervisor
(``hub-afk.sh``) and any future caller share one copy of the portable date/time
helpers. These tests source the lib and call the helpers directly, pinning
``TZ=UTC`` so the conversion is deterministic regardless of the host timezone.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

WT_LIB = Path(__file__).resolve().parents[2] / "scripts" / "worktree-lib.sh"


def _call(fn_call: str) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh under TZ=UTC and invoke a shell expression."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC"},
    )


def _epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """UTC wall-clock to epoch seconds (paired with TZ=UTC in _call)."""
    return int(datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC).timestamp())


def test_wt_date_ymd_formats_epoch_as_local_date() -> None:
    # 2026-06-17 13:45 UTC -> the date component only.
    result = _call(f"wt_date_ymd {_epoch(2026, 6, 17, 13, 45)}")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026-06-17"


def test_wt_epoch_at_pins_seconds_to_zero() -> None:
    # The helper must return the :00 second of the given HH:MM, never leak the
    # invocation second (the BSD `date -j -f` footgun the comment warns about).
    expected = _epoch(2026, 6, 17, 7, 0)

    result = _call("wt_epoch_at 2026-06-17 07:00")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_wt_epoch_at_round_trips_with_wt_date_ymd() -> None:
    # Composing the two (the minutes_until idiom) must land on the same midnight.
    now = _epoch(2026, 6, 17, 23, 30)
    expected_midnight = _epoch(2026, 6, 17, 0, 0)

    result = _call(f'wt_epoch_at "$(wt_date_ymd {now})" 00:00')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected_midnight)


# --- native-OTel message-bridge preflight (auto-populate) ---------------------
# wt_otel_bridge_preflight brings the Langfuse message bridge up idempotently for
# an opted-in spoke. The decision (start / skip / warn) is unit-tested here by
# sourcing the lib and overriding the two collaborators — wt_port_listening (the
# ":4319 already up?" probe) and wt_bridge_launch (the actual nohup-python start) —
# so the branch logic is exercised with no live socket and no real server spawned.


def _preflight(*, gate: bool, auth: bool, port_up: bool) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_bridge_preflight with the two collaborators stubbed.

    wt_port_listening is forced to the desired up/down verdict and wt_bridge_launch
    is replaced with a marker echo, so a started bridge prints ``LAUNCHED <repo>``
    and nothing real is spawned. The gate (AI_TOOLKIT_OTEL) and LANGFUSE_BASIC_AUTH
    are set/unset explicitly so a host value can never steer the decision.
    wt_bridge_pid is stubbed to "no running bridge" so the already-up path is a
    clean no-op here (its restart behaviour is covered by the dedicated tests
    below) and never probes the real :4319 socket.
    """
    parts = [
        f"wt_port_listening() {{ return {0 if port_up else 1}; }}",
        'wt_bridge_launch() { echo "LAUNCHED $1"; }',
        'wt_bridge_pid() { printf ""; }',
        "export AI_TOOLKIT_OTEL=1" if gate else "unset AI_TOOLKIT_OTEL",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_bridge_preflight /repo",
    ]
    return _call("; ".join(parts))


def _bridge_preflight_up(
    *, proc_start: int, source_mtime: int, pid: str = "4242", auth: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_bridge_preflight with the bridge UP and staleness stubbed.

    The port probe is forced up and the staleness signal is driven directly:
    wt_bridge_pid (the :4319 listener pid), wt_proc_start_epoch (when it started),
    and wt_bridge_source_mtime (newest mtime of the bridge's source bundle).
    wt_bridge_kill / wt_bridge_launch are marker echoes, so a recycle prints
    ``KILLED <pid>`` then ``LAUNCHED <repo>`` and nothing real is signalled or
    spawned.
    """
    parts = [
        "wt_port_listening() { return 0; }",
        f'wt_bridge_pid() {{ printf "%s" "{pid}"; }}',
        f'wt_proc_start_epoch() {{ printf "%s" "{proc_start}"; }}',
        f'wt_bridge_source_mtime() {{ printf "%s" "{source_mtime}"; }}',
        'wt_bridge_kill() { echo "KILLED $1"; }',
        'wt_bridge_launch() { echo "LAUNCHED $1"; }',
        "export AI_TOOLKIT_OTEL=1",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_bridge_preflight /repo",
    ]
    return _call("; ".join(parts))


def test_bridge_preflight_launches_when_down_and_authed() -> None:
    # Gate on, bridge down, auth present → start exactly one bridge.
    result = _preflight(gate=True, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED /repo" in result.stdout


def test_bridge_preflight_warns_when_auth_missing() -> None:
    # Gate on, bridge down, auth absent → warn (audit #93 + LLM I/O won't land) but
    # never start a bridge and never fail the spawn.
    result = _preflight(gate=True, auth=False, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_bridge_preflight_idempotent_when_already_up() -> None:
    # Gate on, auth present, but the bridge is already listening → never a second one.
    result = _preflight(gate=True, auth=True, port_up=True)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_bridge_preflight_noop_without_gate() -> None:
    # Gate off → a no-op regardless of auth/port (opt-in is strictly explicit).
    result = _preflight(gate=False, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_bridge_launch_forwards_required_env_to_child(tmp_path: Path) -> None:
    # wt_bridge_launch must forward the child's REQUIRED config — LANGFUSE_BASIC_AUTH
    # (the bridge KeyErrors without it) and BRIDGE_PORT — even when the operator set
    # them as shell-internal (non-exported) values. A python3 stub on PATH records
    # the env it was launched with; the assignments in the expression are deliberately
    # NOT exported, so a pass proves the re-export inside wt_bridge_launch.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dump = tmp_path / "child-env.txt"
    stub = bindir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        '{ echo "LANGFUSE_BASIC_AUTH=$LANGFUSE_BASIC_AUTH"; '
        'echo "BRIDGE_PORT=$BRIDGE_PORT"; } > "$STUB_ENV_DUMP"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_ENV_DUMP": str(dump),
    }
    # Shell-internal (un-exported) operator values, then launch.
    expr = "LANGFUSE_BASIC_AUTH=tok-xyz; BRIDGE_PORT=4321; wt_bridge_launch /repo"
    subprocess.run(["bash", "-c", f'source "{WT_LIB}"; {expr}'], env=env, check=True)

    # The child is backgrounded via nohup; poll briefly for its env dump.
    deadline = time.monotonic() + 5.0
    while not dump.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert dump.exists(), "bridge child never launched / wrote its env"
    recorded = dump.read_text()
    assert "LANGFUSE_BASIC_AUTH=tok-xyz" in recorded
    assert "BRIDGE_PORT=4321" in recorded


def test_bridge_preflight_recycles_stale_source() -> None:
    # Up, gate on, auth present, but the bridge's source bundle was modified after
    # the running process started (a bridge-code change landed) → kill + relaunch.
    result = _bridge_preflight_up(proc_start=1000, source_mtime=2000, pid="4242")

    assert result.returncode == 0, result.stderr
    assert "KILLED 4242" in result.stdout
    assert "LAUNCHED /repo" in result.stdout


def test_bridge_preflight_leaves_current_process() -> None:
    # Up and the process started AFTER its newest source mtime → no churn: never
    # kill/relaunch an up-to-date bridge (no restart loop).
    result = _bridge_preflight_up(proc_start=2000, source_mtime=1000, pid="4242")

    assert result.returncode == 0, result.stderr
    assert "KILLED" not in result.stdout
    assert "LAUNCHED" not in result.stdout


def test_bridge_preflight_noop_when_no_pid() -> None:
    # Up but no listener pid resolves (lsof found nothing / unavailable) → can't
    # prove staleness, so leave it untouched (best-effort).
    result = _bridge_preflight_up(proc_start=1000, source_mtime=2000, pid="")

    assert result.returncode == 0, result.stderr
    assert "KILLED" not in result.stdout
    assert "LAUNCHED" not in result.stdout


def test_bridge_preflight_stale_but_auth_missing_leaves_process() -> None:
    # Stale source but LANGFUSE_BASIC_AUTH unset → don't kill a working bridge for
    # an un-authable replacement; warn and leave it running.
    result = _bridge_preflight_up(proc_start=1000, source_mtime=2000, pid="4242", auth=False)

    assert result.returncode == 0, result.stderr
    assert "KILLED" not in result.stdout
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_bridge_pid_resolves_via_lsof_not_pgrep(tmp_path: Path) -> None:
    # The bridge pid MUST be found via lsof on :4319, never pgrep -f — pgrep
    # false-negatives on non-ASCII argv under a non-UTF8 locale. Stub both on PATH:
    # lsof yields a pid; pgrep records if it was (wrongly) consulted.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pgrep_marker = tmp_path / "pgrep-called.txt"
    lsof = bindir / "lsof"
    lsof.write_text("#!/bin/sh\necho 9999\n")
    lsof.chmod(0o755)
    pgrep = bindir / "pgrep"
    pgrep.write_text(f'#!/bin/sh\necho called > "{pgrep_marker}"\n')
    pgrep.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_bridge_pid 4319'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "9999", result.stderr
    assert not pgrep_marker.exists(), "wt_bridge_pid must not consult pgrep"


def test_proc_start_epoch_is_locale_independent() -> None:
    # `ps -o lstart=` is locale-formatted (fr_FR emits "lun. 29 juin"), which
    # `date -f "%a %b %e %T %Y"` cannot parse — that would strand the epoch empty
    # and stop the bridge staleness check from ever firing. The helper must force
    # LC_ALL=C internally. Run the REAL helper against this test's own shell pid
    # (read-only — never kills) under a deliberately non-C inherited locale.
    env = {**os.environ, "LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8"}
    result = subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_proc_start_epoch $$'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out.isdigit() and int(out) > 0, f"expected an epoch, got {out!r} ({result.stderr!r})"


# --- native-OTel collector preflight (auto-ensure) ----------------------------
# wt_otel_collector_preflight brings the otelcol collector (:4317, the port CC
# exports to and that forks to the bridge) up idempotently for an opted-in spoke.
# It mirrors the bridge preflight, so the decision (start / skip / warn) is unit-
# tested the same way: source the lib and override the two collaborators —
# wt_port_listening (the ":4317 already up?" probe) and wt_collector_launch (the
# actual `docker run` start) — so the branch logic runs with no live socket and
# no real container spawned. The collector must come up BEFORE the bridge (it
# forks to the bridge), so the preflight runs first at spawn.


def _collector_preflight(
    *, gate: bool, auth: bool, port_up: bool, container_status: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_collector_preflight with the two collaborators stubbed.

    wt_port_listening is forced to the desired up/down verdict and
    wt_collector_launch is replaced with a marker echo, so a started collector
    prints ``LAUNCHED <repo>`` and nothing real is spawned. The gate
    (AI_TOOLKIT_OTEL) and LANGFUSE_BASIC_AUTH are set/unset explicitly so a host
    value can never steer the decision. The staleness collaborators are stubbed
    to a benign "no running container" verdict so the already-up path is a clean
    no-op here (its restart behaviour is covered by the dedicated tests below) and
    never probes real docker.

    ``container_status`` drives the down-path recover decision: the docker
    inspect probe (wt_collector_container_status) is stubbed to this value, so a
    dead/exited container ("exited"/"created"/"dead") must be REMOVED before the
    LAUNCH, while an absent one ("") launches with no REMOVE. Default "" keeps the
    absent case (no stopped container in the way) for the pre-recover tests.
    """
    parts = [
        f"wt_port_listening() {{ return {0 if port_up else 1}; }}",
        'wt_collector_launch() { echo "LAUNCHED $1"; }',
        'wt_collector_running_version() { printf ""; }',
        'wt_collector_remove() { echo "REMOVED"; }',
        f'wt_collector_container_status() {{ printf "%s" "{container_status}"; }}',
        "export AI_TOOLKIT_OTEL=1" if gate else "unset AI_TOOLKIT_OTEL",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_collector_preflight /repo",
    ]
    return _call("; ".join(parts))


def _collector_preflight_up(
    *, running_version: str, current_version: str, auth: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run wt_otel_collector_preflight with the collector UP and staleness stubbed.

    The port probe is forced up and the staleness signal is driven directly:
    wt_collector_config_version (the CURRENT signature from on-disk config + the
    expected port/image set) and wt_collector_running_version (the signature the
    running container was stamped with). wt_collector_remove / wt_collector_launch
    are marker echoes, so a recycle prints ``REMOVED`` then ``LAUNCHED <repo>`` and
    nothing real is torn down or spawned.
    """
    parts = [
        "wt_port_listening() { return 0; }",
        f'wt_collector_config_version() {{ printf "%s" "{current_version}"; }}',
        f'wt_collector_running_version() {{ printf "%s" "{running_version}"; }}',
        'wt_collector_remove() { echo "REMOVED"; }',
        'wt_collector_launch() { echo "LAUNCHED $1"; }',
        "export AI_TOOLKIT_OTEL=1",
        "export LANGFUSE_BASIC_AUTH=Basic-xyz" if auth else "unset LANGFUSE_BASIC_AUTH",
        "wt_otel_collector_preflight /repo",
    ]
    return _call("; ".join(parts))


def test_collector_preflight_launches_when_down_and_authed() -> None:
    # Gate on, collector down, auth present → start exactly one collector.
    result = _collector_preflight(gate=True, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED /repo" in result.stdout


def test_collector_preflight_recovers_dead_container() -> None:
    # Gate on, auth present, :4317 not listening BUT a stopped lf-collector
    # container still exists (Exited/Created/Dead). A bare `docker run --name
    # lf-collector` would hit a name conflict (swallowed) and never recover, so the
    # ensure path must REMOVE the dead container first, then LAUNCH a fresh one.
    result = _collector_preflight(gate=True, auth=True, port_up=False, container_status="exited")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" in result.stdout
    assert "LAUNCHED /repo" in result.stdout
    # Order matters: remove must precede the relaunch or the --name still clashes.
    assert result.stdout.index("REMOVED") < result.stdout.index("LAUNCHED")


def test_collector_preflight_launches_when_absent_without_recover() -> None:
    # Gate on, auth present, down, and NO container in the way ("" = absent) →
    # start exactly one collector and never rm (nothing to recover, no churn).
    result = _collector_preflight(gate=True, auth=True, port_up=False, container_status="")

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED /repo" in result.stdout
    assert "REMOVED" not in result.stdout


def test_collector_preflight_running_container_not_removed() -> None:
    # Down path entered while docker still reports the container `running` (a
    # startup race before :4317 binds, or a wrong-interface bind). The running
    # guard in wt_collector_recover_dead must decline to rm — never tear down a
    # possibly-healthy collector — even though it then relaunches. Pins the guard
    # so a "rm whenever a container exists" refactor can't slip through.
    result = _collector_preflight(gate=True, auth=True, port_up=False, container_status="running")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" not in result.stdout


def test_collector_preflight_dead_but_auth_missing_leaves_container() -> None:
    # Down with a dead container present but LANGFUSE_BASIC_AUTH unset → warn and
    # leave it: recovering (rm) without being able to relaunch an authed collector
    # only strands the port. No REMOVE, no LAUNCH, spawn not failed.
    result = _collector_preflight(gate=True, auth=False, port_up=False, container_status="exited")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" not in result.stdout
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_collector_preflight_warns_when_auth_missing() -> None:
    # Gate on, collector down, auth absent → warn (telemetry won't reach Langfuse)
    # but never start a collector and never fail the spawn.
    result = _collector_preflight(gate=True, auth=False, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_collector_preflight_idempotent_when_already_up() -> None:
    # Gate on, auth present, but :4317 already listens → never a second collector.
    result = _collector_preflight(gate=True, auth=True, port_up=True)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_collector_preflight_noop_without_gate() -> None:
    # Gate off → a no-op regardless of auth/port (opt-in is strictly explicit; this
    # is the AI_TOOLKIT_OTEL=0 full opt-out path — no collector started).
    result = _collector_preflight(gate=False, auth=True, port_up=False)

    assert result.returncode == 0, result.stderr
    assert "LAUNCHED" not in result.stdout


def test_collector_preflight_recycles_stale_config() -> None:
    # Up, gate on, auth present, but the running container's stamped config-version
    # no longer matches the current one (an otelcol.yaml / port / image change
    # landed) → tear it down and relaunch through the current code.
    result = _collector_preflight_up(running_version="oldsha", current_version="newsha")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" in result.stdout
    assert "LAUNCHED /repo" in result.stdout


def test_collector_preflight_leaves_current_instance() -> None:
    # Up and the stamped config-version equals the current one → no churn: never
    # rm/relaunch an up-to-date collector (no restart loop).
    result = _collector_preflight_up(running_version="samesha", current_version="samesha")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" not in result.stdout
    assert "LAUNCHED" not in result.stdout


def test_collector_preflight_leaves_unlabeled_instance() -> None:
    # Up but the running container carries no config-version label (started by
    # pre-feature code, or docker inspect unreadable) → staleness can't be PROVEN,
    # so leave it untouched (conservative; fire only on a genuine, detected change).
    result = _collector_preflight_up(running_version="", current_version="newsha")

    assert result.returncode == 0, result.stderr
    assert "REMOVED" not in result.stdout
    assert "LAUNCHED" not in result.stdout


def test_collector_preflight_stale_but_auth_missing_leaves_instance() -> None:
    # Stale config but LANGFUSE_BASIC_AUTH unset → don't tear down a working
    # instance for an un-authable replacement; warn and leave it running.
    result = _collector_preflight_up(running_version="oldsha", current_version="newsha", auth=False)

    assert result.returncode == 0, result.stderr
    assert "REMOVED" not in result.stdout
    assert "LAUNCHED" not in result.stdout
    assert "LANGFUSE_BASIC_AUTH" in result.stderr


def test_collector_launch_publishes_four_ports_and_unquoted_auth(tmp_path: Path) -> None:
    # wt_collector_launch must `docker run` lf-collector with ALL FOUR ports
    # published (4317/4318/4418/8889), mount the repo's otelcol.yaml, and forward
    # LANGFUSE_BASIC_AUTH verbatim — a quoted Authorization 401s while metrics
    # still flow (looks like a pipeline bug but is auth). The non-secret endpoints
    # default to the local stack when the operator leaves them unset. A `docker`
    # stub on PATH records its argv + the auth env it was launched with.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dump = tmp_path / "docker-invocation.txt"
    stub = bindir / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        '{ echo "ARGV=$*"; '
        'echo "LANGFUSE_BASIC_AUTH=[$LANGFUSE_BASIC_AUTH]"; '
        'echo "LANGFUSE_OTLP_ENDPOINT=$LANGFUSE_OTLP_ENDPOINT"; '
        'echo "BRIDGE_OTLP_ENDPOINT=$BRIDGE_OTLP_ENDPOINT"; } > "$STUB_DOCKER_DUMP"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_DOCKER_DUMP": str(dump),
        "TZ": "UTC",
        # Operator value set unquoted; the launch must not add extra quotes.
        "LANGFUSE_BASIC_AUTH": "Basic dG9rOnNlYw==",
    }
    # Drop the endpoints so the launch's defaults are exercised.
    env.pop("LANGFUSE_OTLP_ENDPOINT", None)
    env.pop("BRIDGE_OTLP_ENDPOINT", None)
    subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_collector_launch /repo'],
        env=env,
        check=True,
    )

    recorded = dump.read_text()
    argv = next(line for line in recorded.splitlines() if line.startswith("ARGV="))
    for port in ("4317:4317", "4318:4318", "4418:4418", "8889:8889"):
        assert f"-p {port}" in argv, f"missing published port {port} in: {argv}"
    assert "--name lf-collector" in argv
    # Stamp the config-version label so a later spawn can detect a stale container
    # and recycle it (the staleness signal is content+port+image, not just mtime).
    assert "--label ai-toolkit.config-version=" in argv
    assert "/repo/dashboard/langfuse/otelcol.yaml:/etc/otelcol-contrib/config.yaml" in argv
    # Forwarded verbatim — no surrounding quotes baked into the value.
    assert "LANGFUSE_BASIC_AUTH=[Basic dG9rOnNlYw==]" in recorded
    # Non-secret endpoints defaulted to the local stack.
    assert "LANGFUSE_OTLP_ENDPOINT=http://host.docker.internal:3000/api/public/otel" in recorded
    assert "BRIDGE_OTLP_ENDPOINT=http://host.docker.internal:4319" in recorded


# --- wt_base_branch / wt_base_start_point (issue #117) --------------------------
# One canonical resolver for the integration ("base") branch, shared by the
# worktree scripts AND the guard hooks: it is DEFINED in
# shared/hooks/lib/base-branch.sh (so hooks can source it standalone) and
# worktree-lib.sh sources it the same way it sources telemetry.sh. Precedence:
#   git config ai-toolkit.base-branch  >  AI_TOOLKIT_BASE_BRANCH env
#   >  origin/HEAD  >  init.defaultBranch (existing local ref)
#   >  local main  >  local master  >  literal "main".

BASE_BRANCH_LIB = (
    Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib" / "base-branch.sh"
)


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for base-branch tests: host git config + resolver env pinned out."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "TZ": "UTC",
    }
    env.pop("AI_TOOLKIT_BASE_BRANCH", None)
    env.pop("AFK_DEFAULT_BRANCH", None)
    if extra:
        env.update(extra)
    return env


def _git_in(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=_base_env())


def _make_base_repo(
    base: Path, *, branch: str = "main", with_remote: bool = False, origin_head: bool = False
) -> Path:
    """A one-commit repo on `branch`; optionally with a pushed origin remote.

    origin_head additionally points refs/remotes/origin/HEAD at `branch`
    (implies with_remote), mirroring what a clone gets for free.
    """
    repo = base / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(repo)],
        check=True,
        capture_output=True,
        env=_base_env(),
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git_in(repo, "config", k, v)
    (repo / "seed.txt").write_text("seed\n")
    _git_in(repo, "add", "seed.txt")
    _git_in(repo, "commit", "-qm", "seed")
    if with_remote or origin_head:
        bare = base / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(bare)],
            check=True,
            capture_output=True,
            env=_base_env(),
        )
        _git_in(repo, "remote", "add", "origin", str(bare))
        _git_in(repo, "push", "-q", "-u", "origin", branch)
        if origin_head:
            _git_in(
                repo,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                f"refs/remotes/origin/{branch}",
            )
    return repo


def _resolve_base(
    repo: Path, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh and run wt_base_branch against `repo`."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_base_branch "$1"', "bash", str(repo)],
        capture_output=True,
        text=True,
        env=_base_env(env_extra),
    )


def test_base_branch_config_beats_origin_head(tmp_path: Path) -> None:
    # git config ai-toolkit.base-branch is tier 1: it wins even when origin/HEAD
    # points elsewhere, and is honored without the branch existing yet (explicit
    # operator intent; existence checks belong to the call sites).
    repo = _make_base_repo(tmp_path, origin_head=True)
    _git_in(repo, "config", "ai-toolkit.base-branch", "develop")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "develop"


def test_base_branch_config_beats_env(tmp_path: Path) -> None:
    # The issue's numbered precedence: per-clone config (tier 1) over the
    # one-shot env override (tier 2).
    repo = _make_base_repo(tmp_path, origin_head=True)
    _git_in(repo, "config", "ai-toolkit.base-branch", "develop")

    result = _resolve_base(repo, env_extra={"AI_TOOLKIT_BASE_BRANCH": "release/1.0"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "develop"


def test_base_branch_env_beats_origin_head(tmp_path: Path) -> None:
    repo = _make_base_repo(tmp_path, origin_head=True)

    result = _resolve_base(repo, env_extra={"AI_TOOLKIT_BASE_BRANCH": "release/1.0"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "release/1.0"


def test_base_branch_uses_origin_head(tmp_path: Path) -> None:
    # Nothing configured: origin/HEAD names the base — today's behavior, which
    # an unset config must preserve exactly (AC).
    repo = _make_base_repo(tmp_path, branch="develop", origin_head=True)

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "develop"


def test_base_branch_uses_init_default_branch_ref(tmp_path: Path) -> None:
    # No config/env/origin-HEAD: an init.defaultBranch whose local ref exists is
    # honored — the tier the guard hooks always had, kept so pointing them at
    # this shared resolver changes nothing for them.
    repo = _make_base_repo(tmp_path, branch="trunk")
    _git_in(repo, "config", "init.defaultBranch", "trunk")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trunk"


def test_base_branch_falls_back_to_local_main(tmp_path: Path) -> None:
    repo = _make_base_repo(tmp_path, branch="main")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main"


def test_base_branch_falls_back_to_local_master(tmp_path: Path) -> None:
    repo = _make_base_repo(tmp_path, branch="master")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "master"


def test_base_branch_last_resort_is_main(tmp_path: Path) -> None:
    # No signal at all (repo on an unrelated branch, no remote, no config):
    # print "main" rather than fail — the guards' historical last resort.
    repo = _make_base_repo(tmp_path, branch="scratch")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main"


def test_base_branch_lib_sources_standalone(tmp_path: Path) -> None:
    # The guards source shared/hooks/lib/base-branch.sh directly (no
    # worktree-lib.sh in hook context) — the file must be self-contained.
    repo = _make_base_repo(tmp_path, origin_head=True)
    _git_in(repo, "config", "ai-toolkit.base-branch", "develop")

    result = subprocess.run(
        ["bash", "-c", f'source "{BASE_BRANCH_LIB}"; wt_base_branch "$1"', "bash", str(repo)],
        capture_output=True,
        text=True,
        env=_base_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "develop"


def _resolve_start_point(
    repo: Path, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source worktree-lib.sh and run wt_base_start_point against `repo`."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_base_start_point "$1"', "bash", str(repo)],
        capture_output=True,
        text=True,
        env=_base_env(env_extra),
    )


def test_base_start_point_prefers_origin_ref(tmp_path: Path) -> None:
    # New spokes branch from origin/<base> when the remote ref exists — the
    # hub's local base may lag or carry unpushed commits.
    repo = _make_base_repo(tmp_path, origin_head=True)

    result = _resolve_start_point(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "origin/main"


def test_base_start_point_uses_local_when_no_remote_ref(tmp_path: Path) -> None:
    repo = _make_base_repo(tmp_path)

    result = _resolve_start_point(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main"


def test_base_start_point_fails_when_base_missing(tmp_path: Path) -> None:
    # A configured base that exists nowhere (config typo) must FAIL, not
    # silently branch from something else — the caller dies with its own message.
    repo = _make_base_repo(tmp_path)
    _git_in(repo, "config", "ai-toolkit.base-branch", "develop")

    result = _resolve_start_point(repo)

    # Exactly 1: a deliberate refusal, not bash's 127 command-not-found.
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_base_branch_skips_init_default_branch_without_ref(tmp_path: Path) -> None:
    # Tier 4's existence guard, negative side: an init.defaultBranch naming a
    # branch that does NOT exist locally is skipped (tier 5 fires) — dropping
    # the guard would make every spoke branch from a nonexistent ref.
    repo = _make_base_repo(tmp_path, branch="main")
    _git_in(repo, "config", "init.defaultBranch", "ghost")

    result = _resolve_base(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main"


# --- SSH-keepalive push (issue #119) --------------------------------------------
# The ~6-minute pre-push suite runs INSIDE `git push`, between the SSH connection
# opening and the pack transfer; GitHub reaps the idle connection mid-gate, so a
# fully green push dies in the transfer phase. wt_git_push wraps `git push` with
# GIT_SSH_COMMAND keepalive options so the connection survives the gate, and
# wt_push_transport_died is the retry predicate worktree-land consults to tell
# that post-green transport death apart from a failed gate (which must never be
# retried with the suite skipped).

KEEPALIVE_OPTS = "-o ServerAliveInterval=15 -o ServerAliveCountMax=40"


def test_git_ssh_command_defaults_to_ssh_with_keepalive() -> None:
    # No pre-existing GIT_SSH_COMMAND → plain ssh plus the keepalive options.
    result = _call("unset GIT_SSH_COMMAND; wt_git_ssh_command")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ssh {KEEPALIVE_OPTS}"


def test_git_ssh_command_appends_to_existing_command() -> None:
    # A caller's GIT_SSH_COMMAND (custom binary, -i identity, its own -o options)
    # must be preserved verbatim as the PREFIX, with the keepalive options
    # appended — OpenSSH honors the FIRST occurrence of an option, so appending
    # also means a caller's own ServerAlive* settings keep winning.
    result = _call(
        'export GIT_SSH_COMMAND="ssh -i /tmp/key -o ConnectTimeout=5"; wt_git_ssh_command'
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ssh -i /tmp/key -o ConnectTimeout=5 {KEEPALIVE_OPTS}"


def test_git_push_injects_keepalive_and_passes_args_through(tmp_path: Path) -> None:
    # wt_git_push must exec `git push <args…>` with the keepalive GIT_SSH_COMMAND
    # in the child's env — proven with a `git` stub on PATH that records both.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dump = tmp_path / "git-invocation.txt"
    stub = bindir / "git"
    stub.write_text(
        "#!/bin/sh\n"
        '{ echo "ARGV=$*"; echo "GIT_SSH_COMMAND=$GIT_SSH_COMMAND"; } > "$STUB_GIT_DUMP"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_GIT_DUMP": str(dump),
        "TZ": "UTC",
    }
    env.pop("GIT_SSH_COMMAND", None)

    result = subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; wt_git_push -u origin fix/119-branch'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    recorded = dump.read_text()
    assert "ARGV=push -u origin fix/119-branch" in recorded
    assert f"GIT_SSH_COMMAND=ssh {KEEPALIVE_OPTS}" in recorded


def test_git_push_keepalive_does_not_leak_into_caller_env(tmp_path: Path) -> None:
    # The injection is scoped to the one git process — the caller's shell must
    # not end up with a mutated/exported GIT_SSH_COMMAND after the call.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "git"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "TZ": "UTC"}
    env.pop("GIT_SSH_COMMAND", None)

    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{WT_LIB}"; wt_git_push origin main; printf "AFTER=[%s]" "${{GIT_SSH_COMMAND:-}}"',
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("AFTER=[]")


def _transport_died(rc: int, stderr_text: str, tmp_path: Path) -> int:
    """Return wt_push_transport_died's exit code for a push rc + captured output."""
    capture = tmp_path / "push-output.txt"
    capture.write_text(stderr_text)
    result = _call(f'wt_push_transport_died {rc} "{capture}"')
    return result.returncode


def test_transport_died_on_sigpipe_exit(tmp_path: Path) -> None:
    # Exit 141 (SIGPIPE) is the spoke-side symptom — transport death regardless
    # of what the output says.
    assert _transport_died(141, "", tmp_path) == 0


@pytest.mark.parametrize(
    "line",
    [
        "Connection to ssh.github.com closed by remote host.",
        "packet_write_wait: Connection to 140.82.121.36 port 22: Broken pipe",
        "client_loop: send disconnect: Broken pipe",
        "fatal: the remote end hung up unexpectedly",
        "send-pack: unexpected disconnect while reading sideband packet",
    ],
    ids=["closed-by-remote", "packet-write-wait", "client-loop", "hung-up", "send-pack"],
)
def test_transport_died_on_ssh_disconnect_output(line: str, tmp_path: Path) -> None:
    # Each transport-death signature git/ssh emits when the connection dies
    # mid-transfer — a phase git only reaches AFTER the pre-push hook exited 0,
    # so any of these is proof the gate ran green.
    assert (
        _transport_died(1, f"some output\n{line}\nerror: failed to push some refs\n", tmp_path) == 0
    )


def test_transport_not_died_on_failed_gate(tmp_path: Path) -> None:
    # A failed pre-push gate (pytest failures + git's local refusal) must NOT
    # read as transport death — retrying it with TEST_SELECT_SKIP=1 would ship
    # a red tree. Includes a BrokenPipeError traceback line: pytest output
    # containing "Broken pipe" prose must not fool the predicate.
    gate_failure = (
        "FAILED tests/unit/test_x.py::test_y - BrokenPipeError: [Errno 32] Broken pipe\n"
        "=== 1 failed, 12 passed in 340.12s ===\n"
        "error: failed to push some refs to 'github.com:o/r.git'\n"
    )
    assert _transport_died(1, gate_failure, tmp_path) == 1


def test_transport_not_died_on_clean_remote_rejection(tmp_path: Path) -> None:
    # A remote policy rejection (branch protection etc.) is not transport death:
    # a retry would just fail again — roll back as today.
    rejection = (
        " ! [remote rejected] main -> main (protected branch hook declined)\n"
        "error: failed to push some refs to 'github.com:o/r.git'\n"
    )
    assert _transport_died(1, rejection, tmp_path) == 1


# --- hub-side Langfuse auth resolution (issue #127) ----------------------------
# wt_resolve_langfuse_auth generalizes hub-afk.sh's afk_resolve_telemetry_auth so
# ANY hub script (worktree-land.sh, worktree-quick.sh) can resolve Langfuse auth
# without the operator hand-exporting it: env wins each field independently, then
# the ~/.afk-telemetry conf fills the gaps. On success it exports the auth, the
# host, and the OTLP span endpoint (so telemetry.sh's script-span fan-out fires);
# on failure it exports nothing and returns 1 so callers keep their skip-WARN path.


def _resolve_auth(
    tmp_path: Path, *, conf: str | None, pre: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run wt_resolve_langfuse_auth hermetically and prove what it EXPORTED.

    AFK_TELEMETRY_CONF is always pinned — to a tmp conf when `conf` is given,
    else to a nonexistent path — and the LANGFUSE_* / span-endpoint env is
    cleared first, so a host shell's real credentials can never steer the
    resolution. `pre` re-exports the fields a test wants preset. A child bash
    echoes the three fields, so only values the resolver *exported* (not merely
    set) are visible in C_AUTH / C_HOST / C_EP.
    """
    conf_path = tmp_path / "afk-telemetry"
    if conf is not None:
        conf_path.write_text(conf)
    parts = [
        f'export AFK_TELEMETRY_CONF="{conf_path}"',
        "unset LANGFUSE_BASIC_AUTH LANGFUSE_HOST AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
        pre,
        'wt_resolve_langfuse_auth; echo "RC=$?"',
        "bash -c 'echo \"C_AUTH=$LANGFUSE_BASIC_AUTH C_HOST=$LANGFUSE_HOST"
        " C_EP=$AI_TOOLKIT_OTEL_SPAN_ENDPOINT\"'",
    ]
    return _call("; ".join(p for p in parts if p))


def test_resolve_langfuse_auth_from_conf_when_env_unset(tmp_path: Path) -> None:
    # Env auth absent but the conf supplies it ⇒ resolve + EXPORT it, defaulting
    # the host and the OTLP span endpoint to the local stack.
    result = _resolve_auth(tmp_path, conf='LANGFUSE_BASIC_AUTH="Basic-from-file"\n')

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH=Basic-from-file" in result.stdout, "auth resolved + exported from the conf"
    assert "C_HOST=http://localhost:3000" in result.stdout, "host defaults to the local stack"
    assert "C_EP=http://localhost:4318" in result.stdout, (
        "span endpoint defaults to the local collector's OTLP-HTTP port"
    )


def test_resolve_langfuse_auth_env_wins_over_conf(tmp_path: Path) -> None:
    # An explicit env LANGFUSE_BASIC_AUTH outranks the conf file — the same
    # precedence as afk_resolve_telemetry_auth.
    result = _resolve_auth(
        tmp_path,
        conf='LANGFUSE_BASIC_AUTH="Basic-from-file"\n',
        pre="export LANGFUSE_BASIC_AUTH=Basic-from-env",
    )

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH=Basic-from-env" in result.stdout, "env auth must win over the conf"


def test_resolve_langfuse_auth_conf_host_used_when_env_supplies_only_auth(
    tmp_path: Path,
) -> None:
    # Env supplies auth only; the conf supplies host ⇒ each field resolves
    # independently (env auth + conf host), never "conf only read when auth unset".
    result = _resolve_auth(
        tmp_path,
        conf='LANGFUSE_HOST="http://lf.example:3000"\n',
        pre="export LANGFUSE_BASIC_AUTH=Basic-env",
    )

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH=Basic-env" in result.stdout, "env auth is kept"
    assert "C_HOST=http://lf.example:3000" in result.stdout, "host resolves from the conf"


def test_resolve_langfuse_auth_rc1_and_no_exports_when_unresolvable(tmp_path: Path) -> None:
    # Conf absent + env absent ⇒ rc 1 and NOTHING exported — callers (the land
    # ingest, the span emits) keep their existing skip paths untouched.
    result = _resolve_auth(tmp_path, conf=None)

    assert "RC=1" in result.stdout, result.stderr + result.stdout
    assert "C_AUTH= " in result.stdout, "no auth may be invented"
    assert "C_EP=\n" in result.stdout or result.stdout.rstrip().endswith("C_EP="), (
        "the span endpoint must not be wired when auth is unresolvable"
    )


def test_resolve_langfuse_auth_span_endpoint_override_preserved(tmp_path: Path) -> None:
    # An operator's explicit span endpoint (env) outranks the default.
    result = _resolve_auth(
        tmp_path,
        conf='LANGFUSE_BASIC_AUTH="Basic-from-file"\n',
        pre="export AI_TOOLKIT_OTEL_SPAN_ENDPOINT=http://otel.example:9999",
    )

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_EP=http://otel.example:9999" in result.stdout, "env endpoint override is preserved"


def test_resolve_langfuse_auth_conf_may_supply_span_endpoint(tmp_path: Path) -> None:
    # The conf may pin the span endpoint too (same env-wins-per-field contract).
    result = _resolve_auth(
        tmp_path,
        conf=(
            'LANGFUSE_BASIC_AUTH="Basic-from-file"\n'
            'AI_TOOLKIT_OTEL_SPAN_ENDPOINT="http://conf.example:4318"\n'
        ),
    )

    assert "RC=0" in result.stdout, result.stderr + result.stdout
    assert "C_EP=http://conf.example:4318" in result.stdout, "conf endpoint is honored"


# --- review workspace file management (issue #134) ----------------------------
# The review "window" is a saved .code-workspace file; `code --add/--remove`
# target the last-focused window and routinely miss, so worktree-new/-done edit
# the file's `folders` array directly (VS Code hot-reloads it). The lib owns the
# three primitives: wt_workspace_file (location resolution), wt_workspace_add,
# and wt_workspace_remove (which also sweeps entries whose path is gone from
# disk — self-healing for past misses). A missing or unparseable file returns 1
# so callers fall back to the legacy `code` CLI path, file left untouched.


def _ws_env_call(fn_call: str, *, home: Path) -> subprocess.CompletedProcess[str]:
    """Source the lib with HOME pinned to a per-test dir and run an expression."""
    return subprocess.run(
        ["bash", "-c", f'source "{WT_LIB}"; {fn_call}'],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TZ": "UTC",
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def _write_workspace(ws: Path, folders: list[dict], settings: dict | None = None) -> str:
    """Write a VS Code-shaped workspace file (tab indent) and return its text."""
    ws.parent.mkdir(parents=True, exist_ok=True)
    doc = {"folders": folders, "settings": settings if settings is not None else {}}
    text = json.dumps(doc, indent="\t") + "\n"
    ws.write_text(text)
    return text


def _make_dirs(repos: Path, *names: str) -> list[Path]:
    """Create sibling worktree-like directories under a Repos/ parent."""
    made = []
    for name in names:
        d = repos / name
        d.mkdir(parents=True, exist_ok=True)
        made.append(d)
    return made


def test_workspace_file_defaults_to_home_claude_repo_basename(tmp_path: Path) -> None:
    # No git config override → ~/.claude/<repo-basename>.code-workspace.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    result = _ws_env_call(f'wt_workspace_file "{repo}"', home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(home / ".claude" / "myrepo.code-workspace")


def test_workspace_file_honors_git_config_override(tmp_path: Path) -> None:
    # `git config ai-toolkit.workspace-file` wins over the default, so synced
    # target repos can keep their own review workspace.
    repo = tmp_path / "myrepo"
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "ai-toolkit.workspace-file", "/x/review.code-workspace"],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )

    result = _ws_env_call(f'wt_workspace_file "{repo}"', home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/x/review.code-workspace"


def test_workspace_file_expands_leading_tilde_in_override(tmp_path: Path) -> None:
    # A `~/...` config value must resolve against HOME (git stores it verbatim).
    repo = tmp_path / "myrepo"
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "ai-toolkit.workspace-file",
            "~/ws/review.code-workspace",
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )

    result = _ws_env_call(f'wt_workspace_file "{repo}"', home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(home / "ws" / "review.code-workspace")


def test_workspace_add_appends_relative_entry(tmp_path: Path) -> None:
    # New worktree → one appended {"name", "path"} entry, path relative to the
    # workspace file's directory; unrelated entries, settings, and the tab
    # indent VS Code writes are all preserved.
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit", "ai-toolkit-42")
    main_entry = {"name": "ai-toolkit", "path": "../Repos/ai-toolkit"}
    _write_workspace(ws, [main_entry], settings={"files.exclude": {"**/.git": True}})

    result = _call(f'wt_workspace_add "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 0, result.stderr
    text = ws.read_text()
    doc = json.loads(text)
    assert doc["folders"] == [
        main_entry,
        {"name": "ai-toolkit-42", "path": "../Repos/ai-toolkit-42"},
    ]
    assert doc["settings"] == {"files.exclude": {"**/.git": True}}
    assert '\t"folders"' in text, "tab indentation (VS Code's own format) must be kept"


def test_workspace_add_stores_resolvable_path_under_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    # The workspace file addressed THROUGH a symlinked ancestor (NFS/corp homes):
    # the stored relative path must round-trip to the worktree via the physical
    # layout — a lexical relpath against the unresolved dir produces a `..`-chain
    # that resolves nowhere (and the next sweep would drop the live entry).
    # The link is SHALLOWER than the physical tree: a lexical `..`-count computed
    # from the unresolved side overshoots after the symlink is followed.
    phys = tmp_path / "a" / "b" / "phys"
    (phys / "claude").mkdir(parents=True)
    repos = phys / "Repos"
    _make_dirs(repos, "ai-toolkit-42")
    link = tmp_path / "link"
    link.symlink_to(phys)
    ws = link / "claude" / "review.code-workspace"
    _write_workspace(ws, [])

    result = _call(f'wt_workspace_add "{ws}" "{link / "Repos" / "ai-toolkit-42"}"')

    assert result.returncode == 0, result.stderr
    (entry,) = json.loads(ws.read_text())["folders"]
    resolved = (ws.parent / entry["path"]).resolve()
    assert resolved == (repos / "ai-toolkit-42").resolve()


def test_workspace_add_is_noop_when_entry_already_present(tmp_path: Path) -> None:
    # An entry already resolving to the worktree — even a name-less one — must
    # not be duplicated, and the file must not be rewritten.
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit", "ai-toolkit-42")
    before = _write_workspace(
        ws,
        [
            {"name": "ai-toolkit", "path": "../Repos/ai-toolkit"},
            {"path": "../Repos/ai-toolkit-42"},
        ],
    )

    result = _call(f'wt_workspace_add "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 0, result.stderr
    assert ws.read_text() == before, "a duplicate add must leave the file byte-identical"


def test_workspace_add_missing_file_signals_fallback(tmp_path: Path) -> None:
    # No workspace file → rc 1 (caller falls back to `code --add`) and the file
    # must NOT be conjured into existence.
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit-42")

    result = _call(f'wt_workspace_add "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 1, result.stderr
    assert not ws.exists()


def test_workspace_add_invalid_json_leaves_file_and_signals_fallback(tmp_path: Path) -> None:
    # A JSONC file (comments — VS Code tolerates them) fails strict parsing:
    # warn-and-fallback, never abort, never truncate or rewrite the file.
    ws = tmp_path / "claude" / "review.code-workspace"
    ws.parent.mkdir(parents=True)
    before = '{\n\t// hand-edited review window\n\t"folders": []\n}\n'
    ws.write_text(before)
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit-42")

    result = _call(f'wt_workspace_add "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 1
    assert ws.read_text() == before, "an unparseable file must be left untouched"
    assert "workspace" in result.stderr, "the parse failure must be surfaced as a warning"


def test_workspace_remove_drops_target_entry(tmp_path: Path) -> None:
    # Teardown removes exactly the target's entry; live siblings and the main
    # checkout stay, name-less entries stay name-less, settings survive.
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit", "ai-toolkit-42", "ai-toolkit-57")
    main_entry = {"name": "ai-toolkit", "path": "../Repos/ai-toolkit"}
    live_entry = {"path": "../Repos/ai-toolkit-57"}
    _write_workspace(
        ws,
        [main_entry, {"name": "ai-toolkit-42", "path": "../Repos/ai-toolkit-42"}, live_entry],
        settings={"window.title": "review"},
    )

    result = _call(f'wt_workspace_remove "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 0, result.stderr
    doc = json.loads(ws.read_text())
    assert doc["folders"] == [main_entry, live_entry]
    assert doc["settings"] == {"window.title": "review"}


def test_workspace_remove_sweeps_dead_paths(tmp_path: Path) -> None:
    # Entries whose path no longer exists on disk — relative or absolute — are
    # swept in the same pass (self-healing for past `code --remove` misses).
    # A path-less entry cannot be resolved and is conservatively kept.
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit", "ai-toolkit-42", "ai-toolkit-57")
    main_entry = {"name": "ai-toolkit", "path": "../Repos/ai-toolkit"}
    live_entry = {"name": "ai-toolkit-57", "path": "../Repos/ai-toolkit-57"}
    pathless = {"name": "weird"}
    _write_workspace(
        ws,
        [
            main_entry,
            {"path": "../Repos/ai-toolkit-99"},
            {"path": str(tmp_path / "gone-abs")},
            {"name": "ai-toolkit-42", "path": "../Repos/ai-toolkit-42"},
            live_entry,
            pathless,
        ],
    )

    result = _call(f'wt_workspace_remove "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 0, result.stderr
    doc = json.loads(ws.read_text())
    assert doc["folders"] == [main_entry, live_entry, pathless]


def test_workspace_remove_missing_file_signals_fallback(tmp_path: Path) -> None:
    ws = tmp_path / "claude" / "review.code-workspace"
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit-42")

    result = _call(f'wt_workspace_remove "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 1, result.stderr


def test_workspace_remove_invalid_json_leaves_file_and_signals_fallback(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "claude" / "review.code-workspace"
    ws.parent.mkdir(parents=True)
    before = '{\n\t"folders": [\n\t\t{"path": "../Repos/x"},\n\t],\n}\n'  # trailing commas
    ws.write_text(before)
    repos = tmp_path / "Repos"
    _make_dirs(repos, "ai-toolkit-42")

    result = _call(f'wt_workspace_remove "{ws}" "{repos / "ai-toolkit-42"}"')

    assert result.returncode == 1
    assert ws.read_text() == before, "an unparseable file must be left untouched"
    assert "workspace" in result.stderr, "the parse failure must be surfaced as a warning"
