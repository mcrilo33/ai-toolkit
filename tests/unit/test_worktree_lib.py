"""Unit tests for the portable date/time helpers in scripts/worktree-lib.sh.

``wt_date_ymd`` / ``wt_epoch_at`` live here so the unattended supervisor
(``hub-afk.sh``) and any future caller share one copy of the portable date/time
helpers. These tests source the lib and call the helpers directly, pinning
``TZ=UTC`` so the conversion is deterministic regardless of the host timezone.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import time
from pathlib import Path

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
    *, gate: bool, auth: bool, port_up: bool
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
    """
    parts = [
        f"wt_port_listening() {{ return {0 if port_up else 1}; }}",
        'wt_collector_launch() { echo "LAUNCHED $1"; }',
        'wt_collector_running_version() { printf ""; }',
        'wt_collector_remove() { echo "REMOVED"; }',
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
