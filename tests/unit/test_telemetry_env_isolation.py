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
from pathlib import Path

WORKTREE_NEW = Path(__file__).resolve().parents[2] / "scripts" / "worktree-new.sh"

# Pin git config to nothing so a host's global/system config never reaches the
# commits the harness drives (this repo itself ships installable git hooks), and
# so git ignores the decoy $HOME below.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


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
