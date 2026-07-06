"""Unit tests for the global on/off switch (issue #154).

The toolkit runs enforcement at runtime on every commit / push / tool-call
(the pre-push + commit-msg git hooks and the Claude Code guard/marker/telemetry
hooks). ``shared/hooks/lib/enabled.sh`` is the ONE canonical resolver every hook
sources to decide whether to enforce or pass through, plus the ``on|off|status``
toggle CLI (also exposed as the ``scripts/ai-toolkit`` PATH shim).

Resolver precedence (first-decisive) — mirrors ``base-branch.sh`` but with the
MARKER made decisive over git config, because ``sync-to-repo.sh`` re-materializes
``git config ai-toolkit.enabled`` from the yaml on every sync and would silently
clobber a manual git-config override; the ``<git-common-dir>/ai-toolkit-off``
marker is sync-safe:

    1. ``<git-common-dir>/ai-toolkit-off`` present            ⇒ DISABLED (decisive)
    2. else ``git config --local --get ai-toolkit.enabled``   ⇒ false/0/off ⇒ DISABLED
                                                                  true/1/on   ⇒ ENABLED
    3. else                                                    ⇒ ENABLED (default)

``ai-toolkit off`` drops the marker (survives re-syncs); ``ai-toolkit on`` removes
it. The toggle is scoped to the CLONE — the marker lives at the shared
``git-common-dir`` and git config is read ``--local`` — so every linked worktree
of the clone sees the same state and it is never machine-global.

Hermetic: a throwaway git repo per test with HOME / git config sandboxed so no
real user config leaks in (mirrors test_install_git_hooks.py's _GIT_ENV).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
ENABLED_SH = _REPO_ROOT / "shared" / "hooks" / "lib" / "enabled.sh"
AI_TOOLKIT_CMD = _REPO_ROOT / "scripts" / "ai-toolkit"

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit, HOME/git config sandboxed."""
    home = tmp_path / "home"
    home.mkdir()
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _env(repo: Path) -> dict[str, str]:
    return {**_GIT_ENV, "HOME": str(repo.parent / "home")}


def _common_dir(repo: Path) -> Path:
    """Absolute <git-common-dir> for the repo (mirrors the resolver)."""
    raw = _git(repo, "rev-parse", "--git-common-dir").strip()
    p = Path(raw)
    return p if p.is_absolute() else (repo / p).resolve()


def _check(repo: Path, cwd: Path | None = None) -> int:
    """Run ``enabled.sh check`` — exit 0 when ENABLED, non-zero when DISABLED."""
    return subprocess.run(
        [str(ENABLED_SH), "check"],
        cwd=str(cwd or repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    ).returncode


def _cli(repo: Path, *args: str, cmd: Path = ENABLED_SH) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cmd), *args], cwd=str(repo), capture_output=True, text=True, env=_env(repo)
    )


# ── Resolver precedence ─────────────────────────────────────────────


def test_default_enabled_when_unset(repo: Path) -> None:
    """Nothing set (no marker, git config unset) ⇒ ENABLED (today's behavior)."""
    assert _check(repo) == 0


def test_git_config_false_disables(repo: Path) -> None:
    """git config ai-toolkit.enabled=false, no marker ⇒ DISABLED."""
    _git(repo, "config", "--local", "ai-toolkit.enabled", "false")
    assert _check(repo) != 0


def test_git_config_true_enabled(repo: Path) -> None:
    """git config ai-toolkit.enabled=true, no marker ⇒ ENABLED."""
    _git(repo, "config", "--local", "ai-toolkit.enabled", "true")
    assert _check(repo) == 0


def test_marker_disables(repo: Path) -> None:
    """A bare <git-common-dir>/ai-toolkit-off marker ⇒ DISABLED."""
    (_common_dir(repo) / "ai-toolkit-off").write_text("")
    assert _check(repo) != 0


def test_marker_beats_git_config_true(repo: Path) -> None:
    """The marker is DECISIVE: present even over git config=true ⇒ DISABLED.

    This is the #154 correction — the sync-safe marker wins so a re-sync can't
    silently clobber a manual off.
    """
    _git(repo, "config", "--local", "ai-toolkit.enabled", "true")
    (_common_dir(repo) / "ai-toolkit-off").write_text("")
    assert _check(repo) != 0


# ── Toggle CLI ──────────────────────────────────────────────────────


def test_off_creates_marker_and_disables(repo: Path) -> None:
    """``off`` drops the marker at git-common-dir and flips check to DISABLED."""
    result = _cli(repo, "off")
    assert result.returncode == 0, result.stderr
    assert (_common_dir(repo) / "ai-toolkit-off").exists()
    assert _check(repo) != 0


def test_on_removes_marker_and_enables(repo: Path) -> None:
    """``on`` removes the marker and reverts to the default (ENABLED)."""
    _cli(repo, "off")
    result = _cli(repo, "on")
    assert result.returncode == 0, result.stderr
    assert not (_common_dir(repo) / "ai-toolkit-off").exists()
    assert _check(repo) == 0


def test_on_with_config_false_reports_off(repo: Path) -> None:
    """``on`` removes the marker but must report the TRUE effective state: when
    git config still disables (a first-class precedence tier), it stays OFF —
    never a false "ON" that misleads an operator into thinking gates are back.
    """
    _git(repo, "config", "--local", "ai-toolkit.enabled", "false")
    result = _cli(repo, "on")
    assert result.returncode == 0, result.stderr
    assert "OFF" in result.stdout
    assert _check(repo) != 0


def test_resolver_from_subdirectory(repo: Path) -> None:
    """The resolver composes the relative git-common-dir from a nested subdir
    (the multi-``..`` path branch), so a marker still disables from deep in the tree.
    """
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    (_common_dir(repo) / "ai-toolkit-off").write_text("")
    assert _check(repo, cwd=sub) != 0


def test_status_reports_off(repo: Path) -> None:
    """``status`` reports the effective OFF state when disabled."""
    _cli(repo, "off")
    result = _cli(repo, "status")
    assert result.returncode == 0
    assert "OFF" in result.stdout


def test_status_reports_on(repo: Path) -> None:
    """``status`` reports the effective ON state by default."""
    result = _cli(repo, "status")
    assert result.returncode == 0
    assert "ON" in result.stdout


# ── Clone scope (shared across linked worktrees) ────────────────────


def test_marker_shared_across_worktrees(repo: Path, tmp_path: Path) -> None:
    """``off`` in the main checkout disables enforcement in a linked worktree too.

    The marker lives at the shared git-common-dir, so the toggle is clone-scoped,
    not per-worktree and not machine-global (AC: scoped to the clone).
    """
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/x", str(wt))
    _cli(repo, "off")
    assert _check(repo, cwd=wt) != 0


# ── PATH shim ───────────────────────────────────────────────────────


def test_ai_toolkit_shim_toggles(repo: Path) -> None:
    """The ``scripts/ai-toolkit`` PATH command drives the same on/off/check flow."""
    assert _cli(repo, "off", cmd=AI_TOOLKIT_CMD).returncode == 0
    assert _check(repo) != 0
    assert _cli(repo, "on", cmd=AI_TOOLKIT_CMD).returncode == 0
    assert _check(repo) == 0


# ── Subtask B: hook pass-through when disabled ──────────────────────
#
# The correctness bar: when DISABLED every hook must FULLY pass through — a
# commit/push that would normally be gated succeeds untouched; when ENABLED
# nothing changes vs today. Covered on both surfaces: the native git hooks
# (commit-msg + pre-push, emitted by install-git-hooks.sh) and the Claude Code
# hooks (which source lib/utils.sh, the telemetry-arming choke point).

INSTALL = _REPO_ROOT / "scripts" / "install-git-hooks.sh"
SHARED_HOOKS = _REPO_ROOT / "shared" / "hooks"
UTILS = SHARED_HOOKS / "lib" / "utils.sh"
BLOCK_NO_VERIFY = SHARED_HOOKS / "block-no-verify.sh"
PARENT_SPAN = SHARED_HOOKS / "parent-span-export.sh"

# Cage scripts the native hooks invoke, replaced by stubs so a hook's outcome is
# driven by the on/off guard, not the scripts' real logic. The split mirrors the
# wrappers: the blocking gates exit 1 (abort), the advisory ones exit 0 (their
# real behavior) — so an ENABLED pre-push is genuinely gated by test-select, not
# by an advisory stub that happens to run first.
_BLOCKING_STUBS = ("commit-quality", "commit-gauntlet", "test-select")
_ADVISORY_STUBS = ("red-proof-warn", "reviewer-sep-warn", "anti-gutting-scan")


@pytest.fixture()
def installed_repo(tmp_path: Path) -> Path:
    """A repo tracking a bare origin with the native cage hooks installed and
    every copied cage script replaced by an always-blocking stub."""
    home = tmp_path / "home"
    home.mkdir()
    remote = tmp_path / "remote.git"
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "chore: seed", "-m", "Refs #0")
    _git(r, "remote", "add", "origin", str(remote))
    _git(r, "push", "-q", "-u", "origin", "main")
    subprocess.run([str(INSTALL), str(r)], check=True, capture_output=True, text=True, env=_env(r))
    scripts = _common_dir(r) / "hooks" / "ai-toolkit-scripts"
    for name, code in [(n, 1) for n in _BLOCKING_STUBS] + [(n, 0) for n in _ADVISORY_STUBS]:
        stub = scripts / f"{name}.sh"
        stub.write_text(
            f"#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\necho RAN-{name} >&2\nexit {code}\n"
        )
        stub.chmod(0o755)
    return r


def _disable(repo: Path) -> None:
    """Drop the sync-safe off marker at the repo's git-common-dir."""
    (_common_dir(repo) / "ai-toolkit-off").write_text("")


def _commit_attempt(repo: Path) -> subprocess.CompletedProcess[str]:
    (repo / "f.txt").write_text("change\n")
    _git(repo, "add", "f.txt")
    return subprocess.run(
        ["git", "commit", "-m", "feat: x", "-m", "Refs #1"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )


def _make_pushable_commit(repo: Path) -> None:
    """A commit that bypasses the native commit-msg hook (via --no-verify), so a
    pre-push test can push it without the commit-msg stub interfering."""
    (repo / "p.txt").write_text("push me\n")
    _git(repo, "add", "p.txt")
    _git(repo, "commit", "--no-verify", "-qm", "chore: pushable", "-m", "Refs #1")


def _push(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push"], cwd=str(repo), capture_output=True, text=True, env=_env(repo)
    )


# native commit-msg hook


def test_native_commit_msg_enforces_when_enabled(installed_repo: Path) -> None:
    """Baseline: with the toolkit ON, the commit-msg cage stub blocks the commit."""
    assert _commit_attempt(installed_repo).returncode != 0


def test_native_commit_msg_passthrough_when_disabled(installed_repo: Path) -> None:
    """With the toolkit OFF, the commit-msg hook passes through — commit succeeds."""
    _disable(installed_repo)
    result = _commit_attempt(installed_repo)
    assert result.returncode == 0, result.stderr


# native pre-push hook


def test_native_pre_push_enforces_when_enabled(installed_repo: Path) -> None:
    """Baseline: with the toolkit ON, the advisory stubs pass (exit 0) and the
    blocking test-select stub aborts the push — the gate is genuinely reached."""
    _make_pushable_commit(installed_repo)
    assert _push(installed_repo).returncode != 0


def test_native_hook_degrades_to_enabled_when_switch_absent(installed_repo: Path) -> None:
    """Native wrapper: a stale install missing enabled.sh degrades to ENABLED —
    the [ -f ] guard skips, so the cage gate still fires (never crashes) even
    with the off marker present."""
    (_common_dir(installed_repo) / "hooks" / "ai-toolkit-scripts" / "lib" / "enabled.sh").unlink()
    _disable(installed_repo)  # marker present, but the switch file is gone
    assert _commit_attempt(installed_repo).returncode != 0


def test_native_pre_push_passthrough_when_disabled(installed_repo: Path) -> None:
    """With the toolkit OFF, the pre-push hook passes through — push succeeds."""
    _make_pushable_commit(installed_repo)
    _disable(installed_repo)
    result = _push(installed_repo)
    assert result.returncode == 0, result.stderr


def test_install_copies_enabled_lib(installed_repo: Path) -> None:
    """install-git-hooks.sh copies enabled.sh into the hooks lib dir, so the
    wrapper guard (and utils.sh, which now sources it) can resolve it."""
    lib = _common_dir(installed_repo) / "hooks" / "ai-toolkit-scripts" / "lib" / "enabled.sh"
    assert lib.exists()


# CC hooks (utils.sh choke point)


def _run_utils_probe(repo: Path) -> subprocess.CompletedProcess[str]:
    """Source utils.sh then echo a sentinel. When disabled, utils.sh must pass
    through (exit 0 at source-time) BEFORE arming telemetry, so the sentinel
    never prints — locking both the guard and the telemetry-off implication."""
    probe = repo / "probe.sh"
    probe.write_text(f'#!/usr/bin/env bash\nsource "{UTILS}"\necho REACHED_BODY\n')
    probe.chmod(0o755)
    return subprocess.run(
        ["bash", str(probe)], cwd=str(repo), capture_output=True, text=True, env=_env(repo)
    )


def test_cc_hook_runs_when_enabled(repo: Path) -> None:
    """Baseline: with the toolkit ON, utils.sh sources through to the hook body."""
    result = _run_utils_probe(repo)
    assert result.returncode == 0, result.stderr
    assert "REACHED_BODY" in result.stdout


def test_cc_hook_passthrough_when_disabled(repo: Path) -> None:
    """With the toolkit OFF, utils.sh exits 0 during source — the hook body and
    telemetry arming never run."""
    _disable(repo)
    result = _run_utils_probe(repo)
    assert result.returncode == 0
    assert "REACHED_BODY" not in result.stdout


def test_cc_hook_degrades_to_enabled_when_switch_absent(repo: Path, tmp_path: Path) -> None:
    """A stale install lacking enabled.sh must NOT crash: utils.sh sources the
    switch defensively and degrades to ENABLED (hook body runs). An absent switch
    cannot disable — even with the off marker present, enforcement stays on."""
    libdir = tmp_path / "stale_lib"
    libdir.mkdir()
    (libdir / "utils.sh").write_text(UTILS.read_text())
    (libdir / "telemetry.sh").write_text((UTILS.parent / "telemetry.sh").read_text())
    # deliberately NO enabled.sh — a pre-#154 install.
    probe = tmp_path / "probe.sh"
    probe.write_text(f'#!/usr/bin/env bash\nsource "{libdir}/utils.sh"\necho REACHED_BODY\n')
    probe.chmod(0o755)
    _disable(repo)  # marker present, but the switch file is absent, so it can't disable
    result = subprocess.run(
        ["bash", str(probe)], cwd=str(repo), capture_output=True, text=True, env=_env(repo)
    )
    assert result.returncode == 0, result.stderr
    assert "REACHED_BODY" in result.stdout


def _run_block_no_verify(repo: Path) -> subprocess.CompletedProcess[str]:
    payload = '{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}'
    return subprocess.run(
        [str(BLOCK_NO_VERIFY)],
        input=payload,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_env(repo),
    )


def test_block_no_verify_denies_when_enabled(repo: Path) -> None:
    """Baseline: with the toolkit ON, block-no-verify denies a --no-verify commit."""
    assert _run_block_no_verify(repo).returncode != 0


def test_block_no_verify_passthrough_when_disabled(repo: Path) -> None:
    """With the toolkit OFF, block-no-verify (a utils.sh-sourcing guard) no-ops."""
    _disable(repo)
    assert _run_block_no_verify(repo).returncode == 0


# parent-span-export (telemetry hook that does NOT source utils.sh)


def _run_parent_span(repo: Path, tool_id: str) -> Path:
    payload = f'{{"tool_use_id":"{tool_id}"}}'
    subprocess.run(
        [str(PARENT_SPAN)],
        input=payload,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**_env(repo), "AI_TOOLKIT_TELEMETRY": "1"},
    )
    return repo / ".ai-toolkit" / "parent-span"


def test_parent_span_writes_when_enabled(repo: Path) -> None:
    """Baseline: with telemetry opted in and the toolkit ON, the parent-span file
    is written."""
    marker_file = _run_parent_span(repo, "toolu_enabled")
    assert marker_file.exists()
    assert marker_file.read_text().strip() == "toolu_enabled"


def test_parent_span_passthrough_when_disabled(repo: Path) -> None:
    """OFF implies telemetry-off even for parent-span-export (which does not
    source utils.sh) — with the marker present it writes nothing."""
    _disable(repo)
    marker_file = _run_parent_span(repo, "toolu_disabled")
    assert not marker_file.exists()
