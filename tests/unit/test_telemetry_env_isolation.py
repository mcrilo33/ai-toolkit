"""Regression test for issue #49 — the suite leaking spans into the REAL telemetry log.

The span recorder in ``shared/hooks/lib/telemetry.sh`` writes to
``${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl`` whenever
``AI_TOOLKIT_TELEMETRY=1``. A dev shell (and the pre-push full-suite gate) commonly
exports ``=1``, so a test that shells out to a hook — or, like
``test_worktree_new.py``, snapshots ``os.environ`` at module import — passed that
opt-in down to ``worktree-new.sh`` and leaked fixture spoke spans
(``feature/7-bare+<ts>`` …) into ``~/.ai-toolkit/telemetry/events.jsonl``, where they
then surfaced as fake spokes in the observability dashboard. Same class as the
GIT_DIR leak (#29 / #30); the capturing happens at import time, so the cure lives in
``tests/conftest.py`` at import time too.

Two guards, mirroring ``test_git_env_isolation.py``:

* **In-process** — after the conftest import, ``AI_TOOLKIT_TELEMETRY`` is not enabled
  and ``AI_TOOLKIT_TELEMETRY_DIR`` is redirected to a sandbox outside
  ``$HOME/.ai-toolkit``. This is the direct assertion that the sanitizer ran.
* **End-to-end** — drive the proven leak path (``worktree-new.sh``) with
  ``AI_TOOLKIT_TELEMETRY=1`` re-injected and ``$HOME`` pointed at a decoy. With the
  conftest's ``AI_TOOLKIT_TELEMETRY_DIR`` redirect inherited from ``os.environ`` the
  spawn span lands in the sandbox and the decoy's real-default log
  (``$HOME/.ai-toolkit/telemetry/events.jsonl``) gains ZERO new lines; without the
  redirect the re-injected opt-in writes straight to that default and the assertion
  below fails — the exact incident.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

WORKTREE_NEW = Path(__file__).resolve().parents[2] / "scripts" / "worktree-new.sh"
TELEMETRY_LIB = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib" / "telemetry.sh"

# The OTLP/Langfuse fan-out sink in telemetry.sh (Issue #83) is a SECOND, INDEPENDENT
# sink: telemetry_emit_span curl-POSTs a span to AI_TOOLKIT_OTEL_SPAN_ENDPOINT gated on
# that var ALONE — not on AI_TOOLKIT_TELEMETRY. The events.jsonl strip (#49) never
# covered it, so a test inheriting a spoke/dev shell's exported endpoint POSTed fixture
# spans straight to the live collector -> Langfuse (the recurring fake-spoke leak). These
# are the vars whose presence opens a real export channel; the conftest must drop the lot.
_OTLP_EXPORT_VARS = (
    "AI_TOOLKIT_OTEL_SPAN_ENDPOINT",
    "AI_TOOLKIT_OTEL",
    "AI_TOOLKIT_OTEL_BODY_DIR",
    "BRIDGE_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)

# Pin git config to nothing so a host's global/system config never reaches the
# commits the harness drives (this repo itself ships installable git hooks), and
# so git ignores the decoy $HOME below.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
# The host's base-branch override (#117) must never steer the script under test.
_GIT_ENV.pop("AI_TOOLKIT_BASE_BRANCH", None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


def _make_hub(tmp_path: Path) -> Path:
    """A minimal main checkout with a local bare ``origin`` — enough to spawn from."""
    remote = tmp_path / "remote.git"
    hub = tmp_path / "hub"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(hub)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(hub, "config", k, v)
    (hub / "README.md").write_text("seed\n")
    # .ai-toolkit/ holds synced runtime state (incl. the minted spoke-run-id) and is
    # gitignored in every real repo, so it never counts as a dirty worktree.
    (hub / ".gitignore").write_text(".ai-toolkit/\n")
    _git(hub, "add", "README.md", ".gitignore")
    _git(hub, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(hub, "remote", "add", "origin", str(remote))
    _git(hub, "push", "-q", "-u", "origin", "main")
    return hub


def test_conftest_neutralizes_telemetry_opt_in() -> None:
    # AI_TOOLKIT_TELEMETRY=1 is the recorder's opt-in; the conftest must drop it so
    # no test inherits a live telemetry channel.
    assert os.environ.get("AI_TOOLKIT_TELEMETRY") != "1"

    # The dir must be redirected to a throwaway sandbox so even a test that re-enables
    # telemetry without its own dir can never reach $HOME/.ai-toolkit/telemetry.
    dir_override = os.environ.get("AI_TOOLKIT_TELEMETRY_DIR")
    assert dir_override, "conftest must set AI_TOOLKIT_TELEMETRY_DIR to a sandbox"
    real_default = (Path.home() / ".ai-toolkit").resolve()
    resolved = Path(dir_override).resolve()
    assert real_default not in (resolved, *resolved.parents), (
        f"AI_TOOLKIT_TELEMETRY_DIR ({resolved}) must resolve outside {real_default}"
    )


def test_worktree_new_with_telemetry_on_never_writes_real_default(tmp_path: Path) -> None:
    hub = _make_hub(tmp_path)

    # A decoy $HOME standing in for the real one — its .ai-toolkit/telemetry is the
    # path the recorder defaults to when AI_TOOLKIT_TELEMETRY_DIR is unset.
    home = tmp_path / "home"
    sentinel = home / ".ai-toolkit" / "telemetry" / "events.jsonl"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("PRE-EXISTING REAL EVENT\n")
    before = sentinel.read_text()

    # Re-inject the opt-in the conftest just stripped, and inherit os.environ so the
    # conftest's AI_TOOLKIT_TELEMETRY_DIR redirect (present only after the fix) flows
    # through. Do NOT set AI_TOOLKIT_TELEMETRY_DIR here — that inheritance IS the test.
    env = {**_GIT_ENV, "AI_TOOLKIT_TELEMETRY": "1", "HOME": str(home)}
    for var in ("TMUX", "WT_SPOKE"):
        env.pop(var, None)

    res = subprocess.run(
        ["bash", str(WORKTREE_NEW), "7", "bare", "--no-code", "--no-terminal"],
        cwd=str(hub),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"worktree-new.sh failed:\n{res.stdout}\n{res.stderr}"

    # The real-default log must be byte-for-byte unchanged: zero new fixture spans.
    assert sentinel.read_text() == before, (
        "worktree-new.sh leaked telemetry into the real-default $HOME/.ai-toolkit log — "
        "the conftest AI_TOOLKIT_TELEMETRY_DIR redirect is missing or ineffective"
    )


# ── OTLP / Langfuse fan-out sink isolation (the recurring fake-spoke leak) ──


def test_conftest_strips_otlp_export_endpoints() -> None:
    # In-process guard: after the conftest import, NO var that opens a live OTLP export
    # channel survives in os.environ. AI_TOOLKIT_OTEL_SPAN_ENDPOINT is the one the shell
    # sink reads; the rest are the native-OTel family a spoke may also export. Any of
    # them present means a test that inherits os.environ can POST to the real collector.
    for var in _OTLP_EXPORT_VARS:
        assert not os.environ.get(var), (
            f"conftest must strip {var} so no test inherits a live OTLP export channel; "
            f"it is still set to {os.environ.get(var)!r}"
        )


def _stub_curl(bin_dir: Path) -> Path:
    """A fake ``curl`` on PATH that records each invocation to a sentinel file.

    The real sink runs ``curl`` backgrounded + output-redirected; the stub only
    touches the sentinel so it stays invisible. Presence of the sentinel == the OTLP
    sink fired a network POST.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    sentinel = bin_dir / "curl_invoked"
    stub = bin_dir / "curl"
    stub.write_text(f'#!/usr/bin/env bash\necho called >> "{sentinel}"\ncat >/dev/null 2>&1\n')
    stub.chmod(0o755)
    return sentinel


def _emit_with_stub_curl(env: dict[str, str], bin_dir: Path, sentinel: Path) -> bool:
    """Source telemetry.sh, emit one span under ``env``, return whether curl was hit."""
    env = {**env, "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"}
    subprocess.run(
        ["bash", "-c", f'source "{TELEMETRY_LIB}"; telemetry_emit_span --kind step --name t'],
        capture_output=True,
        text=True,
        env=env,
    )
    # The sink backgrounds curl; poll briefly for the sentinel to appear.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sentinel.exists():
            return True
        time.sleep(0.02)
    return False


def test_emit_inheriting_test_env_opens_no_network_connection(tmp_path: Path) -> None:
    # The cure: a test that inherits the (conftest-neutralized) os.environ and shells out
    # to telemetry_emit_span must NOT reach a collector — the OTLP endpoint was stripped,
    # so the sink no-ops and curl is never invoked.
    sentinel = _stub_curl(tmp_path / "bin")
    fired = _emit_with_stub_curl(dict(os.environ), tmp_path / "bin", sentinel)
    assert not fired, (
        "telemetry_emit_span opened a network connection under the inherited test env — "
        "AI_TOOLKIT_OTEL_SPAN_ENDPOINT leaked through the conftest sanitizer"
    )


def test_emit_does_fire_when_endpoint_explicitly_set(tmp_path: Path) -> None:
    # Positive control: with the endpoint explicitly present, the OTLP sink DOES curl.
    # Proves the no-network assertion above is real (stripping is what suppresses it),
    # not a stub/path artifact that would pass regardless.
    sentinel = _stub_curl(tmp_path / "bin")
    env = {**os.environ, "AI_TOOLKIT_OTEL_SPAN_ENDPOINT": "http://127.0.0.1:4318"}
    fired = _emit_with_stub_curl(env, tmp_path / "bin", sentinel)
    assert fired, "OTLP sink did not fire even with AI_TOOLKIT_OTEL_SPAN_ENDPOINT set"


def test_conftest_pins_langfuse_auth_resolution() -> None:
    # In-process guard for the issue #127 resolver: wt_resolve_langfuse_auth
    # (worktree-lib.sh) re-defaults AI_TOOLKIT_OTEL_SPAN_ENDPOINT once auth resolves,
    # so the endpoint strip above is NOT enough — a test shelling out to
    # worktree-land/worktree-quick would read the operator's real ~/.afk-telemetry
    # and re-open the live export channel. conftest must strip the auth pair AND pin
    # AFK_TELEMETRY_CONF at a path that cannot exist.
    assert not os.environ.get("LANGFUSE_BASIC_AUTH"), (
        "conftest must strip LANGFUSE_BASIC_AUTH so the resolver can't resolve from env"
    )
    assert not os.environ.get("LANGFUSE_HOST"), "conftest must strip LANGFUSE_HOST"
    conf = os.environ.get("AFK_TELEMETRY_CONF")
    assert conf, (
        "conftest must pin AFK_TELEMETRY_CONF so the resolver never reads ~/.afk-telemetry"
    )
    assert not Path(conf).exists(), "the pinned conf must not exist (sandbox no-such-conf)"
