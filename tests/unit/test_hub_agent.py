"""Unit tests for shared/skills/hub/scripts/hub-agent.sh.

Hub-side agent work (pre-land code-reviews, bug-scopers, delta re-reviews) must
run on a surface the operator can track (issue #245): a tmux window, a
``hub-status`` row, a durable log, and telemetry cost attribution. ``hub-agent.sh``
provides that. It has two modes:

- Dispatch (default): ``hub-agent.sh <label> [--purpose <t>] [--no-window] -- <cmd>``
  opens a tmux window ``hub:<label>`` that re-invokes the worker; degrades to an
  inline foreground run when tmux is unavailable or ``--no-window`` is passed.
- Worker (``--exec``): runs the command teed to a log, brackets it with start/end
  journal records, and emits a ``kind=agent`` telemetry span for cost attribution.

The tmux and claude binaries are stubbed so the tests never open a real window or
launch a real agent; telemetry is captured via AI_TOOLKIT_TELEMETRY + a tmp dir.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HUB_AGENT = (
    Path(__file__).resolve().parents[2] / "shared" / "skills" / "hub" / "scripts" / "hub-agent.sh"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture()
def hub(tmp_path: Path) -> Path:
    """A minimal hub (main checkout) — hub-agent runs from the repo root."""
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    _git(hub, "add", "README.md")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    return hub


def _tmux_stub(bindir: Path, log: Path, *, new_window_ok: bool = True) -> None:
    """A tmux stub that records every invocation's args (one line each) to `log`.

    new-window prints a fake window id and exits 0 when `new_window_ok`, else it
    prints nothing and exits 1 (simulating a spawn failure); every other subcommand
    succeeds."""
    nw = "printf '@9\\n'; exit 0" if new_window_ok else "exit 1"
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f"  new-window) {nw} ;;\n"
        "  display-message) printf 'hub-sess\\n' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)


def _env(hub: Path, tmp_path: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base env: a stub bin dir on PATH, OTel opted out (no collector), telemetry
    push off unless a test turns it on, and a hermetic hub-agents dir."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    env.pop("TMUX", None)
    env.pop("AI_TOOLKIT_TELEMETRY", None)
    env["AI_TOOLKIT_OTEL"] = "0"  # no collector/native stream in unit tests
    env["AI_TOOLKIT_HUB_AGENTS_DIR"] = str(tmp_path / "hub-agents")
    if extra:
        env.update(extra)
    return env


def _run(hub: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HUB_AGENT), *args],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )


def _journal(env: dict[str, str]) -> list[dict]:
    path = Path(env["AI_TOOLKIT_HUB_AGENTS_DIR"]) / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- Worker mode (--exec) -----------------------------------------------------


def test_exec_writes_log_and_journal_start_end(hub: Path, tmp_path: Path) -> None:
    env = _env(hub, tmp_path)
    log = tmp_path / "hub-agents" / "demo.log"

    result = _run(
        hub,
        "--exec",
        "demo",
        "--log",
        str(log),
        "--start-ms",
        "1000",
        "--purpose",
        "smoke check",
        "--",
        "printf",
        "hello-from-agent\n",
        env=env,
    )

    assert result.returncode == 0
    assert "hello-from-agent" in log.read_text()
    events = _journal(env)
    kinds = [e.get("event") for e in events]
    assert "start" in kinds and "end" in kinds
    start = next(e for e in events if e["event"] == "start")
    assert start["label"] == "demo"
    assert start["purpose"] == "smoke check"
    end = next(e for e in events if e["event"] == "end")
    assert end["status"] == "success"


def test_exec_propagates_command_exit_code(hub: Path, tmp_path: Path) -> None:
    env = _env(hub, tmp_path)
    log = tmp_path / "hub-agents" / "boom.log"

    result = _run(
        hub,
        "--exec",
        "boom",
        "--log",
        str(log),
        "--start-ms",
        "1000",
        "--",
        "sh",
        "-c",
        "exit 3",
        env=env,
    )

    assert result.returncode == 3
    end = next(e for e in _journal(env) if e["event"] == "end")
    assert end["status"] == "failure"


def test_exec_emits_agent_span(hub: Path, tmp_path: Path) -> None:
    tel_dir = tmp_path / "telemetry"
    env = _env(
        hub,
        tmp_path,
        extra={"AI_TOOLKIT_TELEMETRY": "1", "AI_TOOLKIT_TELEMETRY_DIR": str(tel_dir)},
    )
    log = tmp_path / "hub-agents" / "span.log"

    _run(
        hub,
        "--exec",
        "review-236",
        "--log",
        str(log),
        "--start-ms",
        "1000",
        "--",
        "true",
        env=env,
    )

    events_file = tel_dir / "events.jsonl"
    assert events_file.exists()
    spans = [json.loads(ln) for ln in events_file.read_text().splitlines() if ln.strip()]
    agent = [s for s in spans if s.get("kind") == "agent"]
    assert agent, "no kind=agent span emitted"
    assert any(s.get("name") == "hub-agent:review-236" for s in agent)


# --- Dispatch mode ------------------------------------------------------------


def test_dispatch_opens_named_tmux_window(hub: Path, tmp_path: Path) -> None:
    env = _env(hub, tmp_path)
    tmux_log = tmp_path / "tmux-calls.txt"
    _tmux_stub(tmp_path / "bin", tmux_log)

    result = _run(
        hub,
        "review-236",
        "--purpose",
        "pre-land review #236",
        "--",
        "true",
        env=env,
    )

    assert result.returncode == 0
    calls = tmux_log.read_text()
    assert "new-window" in calls
    assert "hub:review-236" in calls


def test_dispatch_degrades_to_inline_without_tmux(hub: Path, tmp_path: Path) -> None:
    # No tmux stub on PATH: dispatch must run the worker inline (foreground) so the
    # agent still runs, its log + journal are written, and a manual hint is printed.
    env = _env(hub, tmp_path)
    log = tmp_path / "hub-agents" / "review-240.log"

    result = _run(
        hub,
        "review-240",
        "--no-window",
        "--",
        "printf",
        "inline-run\n",
        env=env,
    )

    assert result.returncode == 0
    assert "inline-run" in log.read_text()
    events = _journal(env)
    assert any(e["event"] == "start" and e["label"] == "review-240" for e in events)
    assert any(e["event"] == "end" for e in events)


def test_dispatch_requires_command_after_double_dash(hub: Path, tmp_path: Path) -> None:
    env = _env(hub, tmp_path)

    result = _run(hub, "review-236", env=env)

    assert result.returncode != 0
    assert "--" in result.stderr or "command" in result.stderr.lower()


def test_dispatch_falls_back_inline_when_new_window_fails(hub: Path, tmp_path: Path) -> None:
    # tmux is present and the session resolves, but new-window fails: dispatch must
    # NOT report success — it falls through to the inline run so the agent actually
    # executes (and its log/journal are written).
    env = _env(hub, tmp_path)
    _tmux_stub(tmp_path / "bin", tmp_path / "tmux-calls.txt", new_window_ok=False)
    log = tmp_path / "hub-agents" / "review-236.log"

    result = _run(hub, "review-236", "--", "printf", "ran-inline\n", env=env)

    assert result.returncode == 0
    assert "ran-inline" in log.read_text()
    assert any(e["event"] == "end" for e in _journal(env))


def test_exec_applies_native_otel_prefix_when_enabled(hub: Path, tmp_path: Path) -> None:
    # With AI_TOOLKIT_OTEL=1 the worker must launch the command with the native-OTel
    # env prefix (so token cost streams). A fake `claude` echoes an OTel var it would
    # only see if the prefix were actually applied to its environment.
    env = _env(hub, tmp_path, extra={"AI_TOOLKIT_OTEL": "1"})
    fake_claude = tmp_path / "bin" / "claude"
    fake_claude.write_text("#!/bin/sh\nprintf 'exporter=%s\\n' \"$OTEL_TRACES_EXPORTER\"\n")
    fake_claude.chmod(0o755)
    log = tmp_path / "hub-agents" / "otelcheck.log"

    result = _run(
        hub, "--exec", "otelcheck", "--log", str(log), "--start-ms", "1000", "--", "claude", env=env
    )

    assert result.returncode == 0
    assert "exporter=otlp" in log.read_text()
