"""Integration tests for syncing the hub/spoke/land workflow scripts.

`sync-to-repo.sh` must install the parallel-worktrees workflow into any target so
the hub/spoke/land flow works there with no manual setup. The four worktree
scripts and hub-status.sh land in the target's ``.ai-toolkit/scripts/`` — the
canonical location the hub/start-task/land skills reference in both the
ai-toolkit checkout and a synced target (consistent with ``.ai-toolkit/mcp/``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"

# Workflow scripts and their source locations in the toolkit checkout.
# spoke-push.sh ships alongside the worktree scripts so the spoke's PUSH step
# runs as one allowlistable process (issue #37); spoke-ready.sh ships too so
# marker emission (ready/N, gate/N) is one allowlistable command (issue #45).
# telemetry-ingest-spoke.sh ships so the synced worktree-land.sh can call it at
# teardown for the #87/#92 post-run Langfuse ingestion; worktree-quick.sh ships
# so the /quick express lane resolves in a synced target.
WORKTREE_SCRIPTS = (
    "worktree-new.sh",
    "worktree-land.sh",
    "worktree-done.sh",
    "worktree-lib.sh",
    "spoke-push.sh",
    "spoke-ready.sh",
    # The dead-pane relaunch script (issue #233) ships alongside its spoke siblings so a
    # crashed spoke can be relaunched with its identity + ledger + lifecycle span intact.
    "spoke-relaunch.sh",
    "telemetry-ingest-spoke.sh",
    "worktree-quick.sh",
    # The land tail's conditional post-land sweep worker (issue #124) — must
    # resolve at .ai-toolkit/scripts/ or every land logs "sweep failed to launch".
    "gate-sweep.sh",
    # The travel-local hotspot-drain control (issue #248) ships so `travel-local
    # on|off|status` resolves in a synced target like its hub-tool siblings.
    "travel-local.sh",
)
SOURCES = {name: REPO_ROOT / "scripts" / name for name in WORKTREE_SCRIPTS}
HUB_SCRIPTS_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"
SOURCES["hub-status.sh"] = HUB_SCRIPTS_DIR / "hub-status.sh"
SOURCES["hub-ready-watch.sh"] = HUB_SCRIPTS_DIR / "hub-ready-watch.sh"
# The hub OS-notifier (issue #146) MUST install alongside hub-ready-watch.sh:
# the ready-watch loop invokes it as a co-located sibling ($_script_dir), so if
# it is absent from .ai-toolkit/scripts/ the notifier is silently inert.
SOURCES["hub-notify.sh"] = HUB_SCRIPTS_DIR / "hub-notify.sh"
# The hub-side OTel watchdog (issue #115) ships alongside its siblings so a synced
# target can /loop it to keep the collector+bridge up for the whole spoke lifetime.
SOURCES["hub-otel-watch.sh"] = HUB_SCRIPTS_DIR / "hub-otel-watch.sh"
# The unattended-drain supervisor (issue #71) and the batch planner (issue #70)
# ship so /afk and /next-batch resolve in a synced target like their siblings.
SOURCES["hub-afk.sh"] = HUB_SCRIPTS_DIR / "hub-afk.sh"
# The shared gate-broker core (issue #155) MUST install alongside hub-afk.sh: the
# supervisor hard-depends on sourcing it as a co-located sibling ($SCRIPT_DIR) — if it
# is absent from .ai-toolkit/scripts/ every synced /afk drain fails at startup, unable
# to resolve log()/afk_now()/broker_service_gate.
SOURCES["gate-broker.sh"] = HUB_SCRIPTS_DIR / "gate-broker.sh"
# The gate-broker.sh core is split into functional modules (issue #275); each MUST install
# alongside gate-broker.sh so the entry lib can source it as a co-located sibling. A module
# absent from .ai-toolkit/scripts/ makes the deny-wall fail CLOSED (see gate-broker.sh's
# fail-closed source loop) — but it must never be absent, hence this end-to-end guard.
for _gb_mod in ("markers", "detect", "classify", "danger", "answerer", "permission"):
    SOURCES[f"gate-broker-{_gb_mod}.sh"] = HUB_SCRIPTS_DIR / f"gate-broker-{_gb_mod}.sh"
# The hardened tmux-inject primitive (issue #251) MUST install alongside gate-broker.sh: the
# broker now sources it as a co-located sibling ($SCRIPT_DIR/hub-inject.sh) for the ONE
# inject_and_verify both the /afk answerer and the hub-watchdog share — if it is absent from
# .ai-toolkit/scripts/ every synced /afk drain fails at startup, unable to resolve
# inject_and_verify / approve_permission / _spoke_pane_target.
SOURCES["hub-inject.sh"] = HUB_SCRIPTS_DIR / "hub-inject.sh"
# The tier-2 supervision daemon (issue #251) ships so `/afk` / the hub skill can arm it in a
# synced target like its hub-tool siblings — it cross-checks the drain and files afk-defects.
SOURCES["hub-watchdog.sh"] = HUB_SCRIPTS_DIR / "hub-watchdog.sh"
SOURCES["batch-plan.sh"] = HUB_SCRIPTS_DIR / "batch-plan.sh"
# The hub-side agent dispatcher (issue #245) ships alongside its siblings so the
# land/hub skills' `.ai-toolkit/scripts/hub-agent.sh` references resolve in a
# synced target — otherwise pre-land reviews/scopers have no trackable surface.
SOURCES["hub-agent.sh"] = HUB_SCRIPTS_DIR / "hub-agent.sh"
# Co-installed so the worktree scripts can source it as a sibling for lifecycle
# telemetry (it also lives under .claude/hooks/lib/ for the hooks).
SOURCES["telemetry.sh"] = REPO_ROOT / "shared" / "hooks" / "lib" / "telemetry.sh"

INSTALLED = {name: f".ai-toolkit/scripts/{name}" for name in SOURCES}


@pytest.fixture()
def target_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo to sync into."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _run_sync(target: Path, tool: str = "all") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SYNC_SCRIPT), str(target), tool],
        capture_output=True,
        text=True,
        check=True,
    )


class TestWorkflowScriptSync:
    """Sync installs the worktree scripts + hub-status.sh into .ai-toolkit/scripts/."""

    MANIFEST_NAME = ".ai-toolkit-manifest.json"

    def test_all_scripts_installed(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for rel in INSTALLED.values():
            assert (target_repo / rel).is_file(), f"{rel} not installed"

    def test_installed_scripts_are_executable(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for rel in INSTALLED.values():
            assert os.access(target_repo / rel, os.X_OK), f"{rel} not executable"

    def test_installed_files_match_source(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for name, rel in INSTALLED.items():
            assert (target_repo / rel).read_bytes() == SOURCES[name].read_bytes(), (
                f"{rel} differs from source"
            )

    def test_scripts_recorded_in_manifest_for_every_tool(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        for tool in ("copilot", "cursor", "claude"):
            for rel in INSTALLED.values():
                assert rel in manifest["tools"][tool], f"{tool}: {rel} missing from manifest"

    def test_resync_is_byte_identical(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = {rel: (target_repo / rel).read_bytes() for rel in INSTALLED.values()}

        _run_sync(target_repo, "all")

        second = {rel: (target_repo / rel).read_bytes() for rel in INSTALLED.values()}
        assert first == second

    def test_dry_run_does_not_install(self, target_repo: Path) -> None:
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "claude", "--dry-run"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert not (target_repo / ".ai-toolkit" / "scripts").exists()
        for rel in INSTALLED.values():
            assert f"[dry-run] would write {rel}" in result.stdout


# ── the telemetry python PACKAGE (issue #319) ─────────────────────────────────
# telemetry-ingest-spoke.sh resolves the view builder at $SCRIPT_DIR/telemetry when it is
# not running from a git checkout — which is exactly the drain's temp self-copy. The sync
# shipped the .sh files but never the PACKAGE, so that probe missed on every drain land and
# the post-run ingestion was silently skipped: 51 lands over 4 days produced no cycle-step
# scores. These pin the package into a synced target as a COMPLETE, IMPORTABLE unit.
TELEMETRY_SRC = REPO_ROOT / "scripts" / "telemetry"
TELEMETRY_DST = ".ai-toolkit/scripts/telemetry"


def _package_sources() -> set[str]:
    """Every source file of the telemetry package, as paths relative to the package root.

    Enumerated from the source tree — never a hand-listed manifest (#316's lesson: a list
    is a thing to forget). ``__pycache__`` is excluded: it is a build artifact, not part of
    the package, and the hub EXECUTES this package in place so it is always present there.
    """
    return {
        str(f.relative_to(TELEMETRY_SRC))
        for f in TELEMETRY_SRC.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    }


def _import_from_target(target: Path, module: str) -> subprocess.CompletedProcess[str]:
    """Import `module` in a subprocess whose only telemetry source is the synced target.

    Uses ``sys.executable`` (the interpreter running the suite) rather than a hardcoded
    python3.12: the ingest script pins 3.12 for the real run, but this asserts the SYNCED
    TREE is importable, which is interpreter-independent — and this host's python3 is 3.14.
    Runs with cwd inside the target and PYTHONPATH pinned to the synced scripts dir, and
    prints the resolved ``__file__`` so the caller can prove it loaded from the target and
    not from some other checkout on the path.
    """
    return subprocess.run(
        [sys.executable, "-c", f"import {module} as m; print(m.__file__)"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(target / ".ai-toolkit" / "scripts")},
        cwd=str(target),
    )


@pytest.fixture()
def source_pycache() -> Path:
    """A ``__pycache__`` in the source package — the hub's steady state.

    The hub runs the view builder straight out of ``scripts/telemetry``, so CPython writes
    bytecode caches there. A copy that does not prune them ships stale .pyc into every
    target and churns the manifest. Creates a uniquely-named sentinel and removes only
    that, so a real cache (and a parallel run) is left untouched.
    """
    sentinel = TELEMETRY_SRC / "__pycache__" / "sync_pin_319.cpython-39.pyc"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"stale bytecode that must never reach a target\n")
    yield sentinel
    sentinel.unlink(missing_ok=True)


class TestTelemetryPackageSync:
    """Sync installs the telemetry package COMPLETE and IMPORTABLE at .ai-toolkit/scripts/."""

    MANIFEST_NAME = ".ai-toolkit-manifest.json"

    def test_package_is_installed_complete(self, target_repo: Path) -> None:
        # Complete BY CONSTRUCTION: every source file, whatever its extension. A package
        # copied partially is a package that imports until it doesn't.
        _run_sync(target_repo, "claude")

        installed = {
            str(f.relative_to(target_repo / TELEMETRY_DST))
            for f in (target_repo / TELEMETRY_DST).rglob("*")
            if f.is_file()
        }
        assert _package_sources() <= installed, (
            f"missing from the synced package: {sorted(_package_sources() - installed)}"
        )

    def test_installed_package_matches_source(self, target_repo: Path) -> None:
        _run_sync(target_repo, "claude")

        for rel in _package_sources():
            assert (target_repo / TELEMETRY_DST / rel).read_bytes() == (
                TELEMETRY_SRC / rel
            ).read_bytes(), f"{rel} differs from source"

    def test_package_is_importable_in_target(self, target_repo: Path) -> None:
        # The acceptance criterion, asserted literally: present AND importable. Uses a
        # dependency-free leaf so the pin holds even where PyYAML is absent, and proves the
        # module resolved from the TARGET rather than from another checkout on the path.
        _run_sync(target_repo, "claude")

        result = _import_from_target(target_repo, "telemetry.spoke_tree.scores")

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(str(target_repo)), (
            f"resolved outside the target: {result.stdout!r}"
        )

    def test_view_builder_entrypoint_is_importable_in_target(self, target_repo: Path) -> None:
        # The module telemetry-ingest-spoke.sh actually runs. It reaches PyYAML through
        # spoke_tree.context_deltas' unguarded top-level `import yaml`, so gate the pin on
        # the dependency being present rather than assert a stdlib-only package it is not.
        # (A synced target WITHOUT PyYAML genuinely cannot ingest — that is a broken install,
        # and the #319 alarm in telemetry-ingest-spoke.sh is what surfaces it.)
        pytest.importorskip("yaml")
        _run_sync(target_repo, "claude")

        result = _import_from_target(target_repo, "telemetry.langfuse_spoke_tree")

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(str(target_repo)), (
            f"resolved outside the target: {result.stdout!r}"
        )

    def test_package_recorded_in_manifest_for_every_tool(self, target_repo: Path) -> None:
        # Recorded per-file so the GC reclaims a module that is later deleted upstream —
        # an unrecorded copy would silently strand removed modules in every target.
        _run_sync(target_repo, "all")

        manifest = json.loads((target_repo / self.MANIFEST_NAME).read_text())
        for tool in ("copilot", "cursor", "claude"):
            for rel in _package_sources():
                assert f"{TELEMETRY_DST}/{rel}" in manifest["tools"][tool], (
                    f"{tool}: {TELEMETRY_DST}/{rel} missing from manifest"
                )

    def test_pycache_is_not_synced(self, target_repo: Path, source_pycache: Path) -> None:
        assert source_pycache.exists(), "fixture failed to stage the source __pycache__"

        _run_sync(target_repo, "claude")

        # Guard against a vacuous pass: with no package installed the stray-scan below finds
        # nothing and the test would "pass" while proving nothing at all.
        assert (target_repo / TELEMETRY_DST).is_dir(), "no package installed — nothing to prune"
        strays = [
            str(f.relative_to(target_repo))
            for f in (target_repo / TELEMETRY_DST).rglob("*")
            if "__pycache__" in f.parts or f.suffix == ".pyc"
        ]
        assert strays == [], f"build artifacts leaked into the target: {strays}"

    def test_resync_is_byte_identical(self, target_repo: Path) -> None:
        _run_sync(target_repo, "all")
        first = {
            rel: (target_repo / TELEMETRY_DST / rel).read_bytes() for rel in _package_sources()
        }

        _run_sync(target_repo, "all")

        second = {
            rel: (target_repo / TELEMETRY_DST / rel).read_bytes() for rel in _package_sources()
        }
        assert first == second

    def test_dry_run_does_not_install_the_package(self, target_repo: Path) -> None:
        result = subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(target_repo), "claude", "--dry-run"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert not (target_repo / TELEMETRY_DST).exists()
        assert f"[dry-run] would write {TELEMETRY_DST}/langfuse_spoke_tree.py" in result.stdout
