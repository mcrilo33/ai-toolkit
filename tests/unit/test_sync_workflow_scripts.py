"""Registration guard for scripts/travel-local.sh in the sync pipeline (issue #248).

`travel-local.sh` is a hub-side operational tool (like hub-afk.sh): it must ship into a
synced target's ``.ai-toolkit/scripts/`` so the travel-local flow resolves there too. The
mechanism is a single entry in ``sync_workflow_scripts()``'s ``for name in …`` loop in
``scripts/sync-to-repo.sh`` — travel-local.sh lives at the toolkit-root ``scripts/``, so it
takes the loop's default source case (``src="$SCRIPT_DIR/$name"``) with no extra mapping.

This is the source-level guard (the acceptance criterion: "Registered in
``sync_workflow_scripts()``"). The end-to-end sync outcome — the file landing executable in
``.ai-toolkit/scripts/`` — is covered by ``tests/integration/test_workflow_scripts_sync.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"
TRAVEL_LOCAL = REPO_ROOT / "scripts" / "travel-local.sh"
HUB_SCRIPTS_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"

# The gate-broker.sh core is split into functional modules (issue #275). Each must be
# registered exactly like a hub-skill sibling so it lands in a synced target's
# .ai-toolkit/scripts/ — otherwise the entry lib sources a missing sibling and the deny-wall
# fails CLOSED (a walled-shut spoke). Extend this tuple as each module is extracted.
GB_MODULES = ("markers", "detect", "classify", "danger", "answerer", "permission")


def _sync_workflow_scripts_body() -> str:
    """The body of the ``sync_workflow_scripts()`` function in sync-to-repo.sh."""
    text = SYNC_SCRIPT.read_text()
    match = re.search(r"^sync_workflow_scripts\(\) \{$(.*?)^\}$", text, re.MULTILINE | re.DOTALL)
    assert match, "sync_workflow_scripts() function not found in sync-to-repo.sh"
    return match.group(1)


def _for_name_list() -> list[str]:
    """The names in the ``for name in … ; do`` loop that drives the script copy."""
    body = _sync_workflow_scripts_body()
    match = re.search(r"for name in (.+?); do", body)
    assert match, "`for name in … ; do` loop not found in sync_workflow_scripts()"
    return match.group(1).split()


def _copy_telemetry_package_body() -> str:
    """The body of the ``copy_telemetry_package()`` helper in sync-to-repo.sh (issue #319)."""
    text = SYNC_SCRIPT.read_text()
    match = re.search(r"^copy_telemetry_package\(\) \{$(.*?)^\}$", text, re.MULTILINE | re.DOTALL)
    assert match, "copy_telemetry_package() function not found in sync-to-repo.sh"
    return match.group(1)


def test_travel_local_registered_in_sync_loop() -> None:
    assert "travel-local.sh" in _for_name_list(), (
        "travel-local.sh is not registered in sync_workflow_scripts() — it will not sync "
        "to .ai-toolkit/scripts/ of a synced target"
    )


def test_travel_local_takes_the_default_root_source_case() -> None:
    # A toolkit-root script has no per-name `case` mapping (those route to
    # shared/skills/hub/scripts/ or shared/hooks/lib/); it must fall through to the default.
    body = _sync_workflow_scripts_body()
    hub_case = re.search(r"\n\s*([\w.|-]*travel-local\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is None, (
        "travel-local.sh must not have an explicit case mapping — it takes the default "
        'src="$SCRIPT_DIR/$name" (toolkit-root scripts/)'
    )


def test_travel_local_source_exists_and_is_executable() -> None:
    # The default source case resolves to scripts/travel-local.sh; the sync chmods +x, so
    # the source must exist and be a real file.
    assert TRAVEL_LOCAL.is_file(), "scripts/travel-local.sh missing — sync would copy nothing"
    assert os.access(TRAVEL_LOCAL, os.X_OK), "scripts/travel-local.sh is not executable"


# ── hub-inject.sh registration (issue #251) ───────────────────────────────────
# gate-broker.sh now hard-depends on hub-inject.sh as a co-located sibling
# ($SCRIPT_DIR/hub-inject.sh) for the shared inject_and_verify primitive. If hub-inject.sh
# is not registered here it never lands in .ai-toolkit/scripts/, and the synced gate-broker
# sources a MISSING file — every synced /afk drain breaks at startup. Unlike travel-local.sh
# it is a hub-skill script, so it MUST take the hub-skill `case` mapping (not the default).
HUB_INJECT = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-inject.sh"


def test_hub_inject_registered_in_sync_loop() -> None:
    assert "hub-inject.sh" in _for_name_list(), (
        "hub-inject.sh is not registered in sync_workflow_scripts() — gate-broker.sh would "
        "source a missing sibling in a synced target and every /afk drain would break"
    )


def test_hub_inject_takes_the_hub_skill_source_case() -> None:
    # A hub-skill script must map to shared/skills/hub/scripts/, not the toolkit-root default.
    body = _sync_workflow_scripts_body()
    hub_case = re.search(r"\n\s*([\w.|-]*hub-inject\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        "hub-inject.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


def test_hub_inject_source_exists_and_is_executable() -> None:
    assert HUB_INJECT.is_file(), "shared/skills/hub/scripts/hub-inject.sh missing"
    assert os.access(HUB_INJECT, os.X_OK), "hub-inject.sh is not executable"


# ── transition-log.sh registration (issue #300, phase 1) ────────────────────────
# The per-spoke lifecycle transition log lib. Migration steps wire spoke-side scripts
# (spoke-ready.sh, spoke-push.sh) to source it as a co-located sibling in a synced
# target's .ai-toolkit/scripts/ — unregistered, those writers would source a missing
# file. Hub-skill script, so it takes the hub-skill case mapping.
TRANSITION_LOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "transition-log.sh"


def test_transition_log_registered_in_sync_loop() -> None:
    assert "transition-log.sh" in _for_name_list(), (
        "transition-log.sh is not registered in sync_workflow_scripts() — #300's actor "
        "writers would source a missing sibling in a synced target"
    )


def test_transition_log_takes_the_hub_skill_source_case() -> None:
    body = _sync_workflow_scripts_body()
    hub_case = re.search(r"\n\s*([\w.|-]*transition-log\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        "transition-log.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


def test_transition_log_source_exists_and_is_executable() -> None:
    assert TRANSITION_LOG.is_file(), "shared/skills/hub/scripts/transition-log.sh missing"
    assert os.access(TRANSITION_LOG, os.X_OK), "transition-log.sh is not executable"


# ── hub-watchdog.sh registration (issue #251) ─────────────────────────────────
# The tier-2 supervision daemon is a hub-skill script; it must sync into a target's
# .ai-toolkit/scripts/ so /afk / the hub skill can arm it, and take the hub-skill case mapping.
HUB_WATCHDOG = REPO_ROOT / "shared" / "skills" / "hub" / "scripts" / "hub-watchdog.sh"


def test_hub_watchdog_registered_in_sync_loop() -> None:
    assert "hub-watchdog.sh" in _for_name_list(), (
        "hub-watchdog.sh is not registered in sync_workflow_scripts() — it would not sync to a "
        "target's .ai-toolkit/scripts/ and /afk could not arm the tier-2 watchdog there"
    )


def test_hub_watchdog_takes_the_hub_skill_source_case() -> None:
    body = _sync_workflow_scripts_body()
    hub_case = re.search(r"\n\s*([\w.|-]*hub-watchdog\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        "hub-watchdog.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


def test_hub_watchdog_source_exists_and_is_executable() -> None:
    assert HUB_WATCHDOG.is_file(), "shared/skills/hub/scripts/hub-watchdog.sh missing"
    assert os.access(HUB_WATCHDOG, os.X_OK), "hub-watchdog.sh is not executable"


# ── gate-broker functional modules (issue #275) ───────────────────────────────
# gate-broker.sh sources each gate-broker-<stage>.sh as a co-located sibling; a module absent
# from a synced target fails the deny-wall CLOSED. So every module must be in the loop, take
# the hub-skill case mapping, and exist executable — mirroring the hub-inject/hub-watchdog guards.
@pytest.mark.parametrize("module", GB_MODULES)
def test_gate_broker_module_registered_in_sync_loop(module: str) -> None:
    assert f"gate-broker-{module}.sh" in _for_name_list(), (
        f"gate-broker-{module}.sh is not registered in sync_workflow_scripts() — the entry lib "
        "would source a missing sibling in a synced target and the deny-wall would fail closed"
    )


@pytest.mark.parametrize("module", GB_MODULES)
def test_gate_broker_module_takes_the_hub_skill_source_case(module: str) -> None:
    body = _sync_workflow_scripts_body()
    hub_case = re.search(rf"\n\s*([\w.|-]*gate-broker-{module}\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        f"gate-broker-{module}.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


@pytest.mark.parametrize("module", GB_MODULES)
def test_gate_broker_module_source_exists_and_is_executable(module: str) -> None:
    src = HUB_SCRIPTS_DIR / f"gate-broker-{module}.sh"
    assert src.is_file(), f"shared/skills/hub/scripts/gate-broker-{module}.sh missing"
    assert os.access(src, os.X_OK), f"gate-broker-{module}.sh is not executable"


# ── hub-afk functional modules (issue #307) ───────────────────────────────────
# hub-afk.sh sources each hub-afk-<lane>.sh as a co-located sibling; a module absent from a
# synced target sets _AFK_MODULES_OK=0 and the drain refuses to arm there. So every module
# must be in the loop, take the hub-skill case mapping, and exist executable — mirroring the
# gate-broker-module guards. Extend this tuple as each lane is extracted.
HUB_AFK_MODULES = ("land", "dispatch", "arm", "supervise", "recover")


@pytest.mark.parametrize("module", HUB_AFK_MODULES)
def test_hub_afk_module_registered_in_sync_loop(module: str) -> None:
    assert f"hub-afk-{module}.sh" in _for_name_list(), (
        f"hub-afk-{module}.sh is not registered in sync_workflow_scripts() — the entry lib "
        "would source a missing sibling in a synced target and the drain would refuse to arm"
    )


@pytest.mark.parametrize("module", HUB_AFK_MODULES)
def test_hub_afk_module_takes_the_hub_skill_source_case(module: str) -> None:
    body = _sync_workflow_scripts_body()
    hub_case = re.search(rf"\n\s*([\w.|-]*hub-afk-{module}\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        f"hub-afk-{module}.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


@pytest.mark.parametrize("module", HUB_AFK_MODULES)
def test_hub_afk_module_source_exists_and_is_executable(module: str) -> None:
    src = HUB_SCRIPTS_DIR / f"hub-afk-{module}.sh"
    assert src.is_file(), f"shared/skills/hub/scripts/hub-afk-{module}.sh missing"
    assert os.access(src, os.X_OK), f"hub-afk-{module}.sh is not executable"


# ── hub-watchdog functional modules (issue #308) ──────────────────────────────
# hub-watchdog.sh sources each hub-watchdog-<lane>.sh as a co-located sibling; a module
# absent from a synced target sets _WD_MODULES_OK=0 and the daemon refuses to run there
# (a silently-blind watchdog no-ops every detector — AFK Design Principle 2). So every
# module must be in the loop, take the hub-skill case mapping, and exist executable —
# mirroring the gate-broker/hub-afk module guards. Extend this tuple as each lane splits.
HUB_WATCHDOG_MODULES = ("detect", "intervene")


@pytest.mark.parametrize("module", HUB_WATCHDOG_MODULES)
def test_hub_watchdog_module_registered_in_sync_loop(module: str) -> None:
    assert f"hub-watchdog-{module}.sh" in _for_name_list(), (
        f"hub-watchdog-{module}.sh is not registered in sync_workflow_scripts() — the entry "
        "lib would source a missing sibling in a synced target and the daemon would refuse to run"
    )


@pytest.mark.parametrize("module", HUB_WATCHDOG_MODULES)
def test_hub_watchdog_module_takes_the_hub_skill_source_case(module: str) -> None:
    body = _sync_workflow_scripts_body()
    hub_case = re.search(rf"\n\s*([\w.|-]*hub-watchdog-{module}\.sh[\w.|-]*)\)\s+src=", body)
    assert hub_case is not None, (
        f"hub-watchdog-{module}.sh must have the hub-skill case mapping "
        '(src="$SHARED_DIR/skills/hub/scripts/$name"), not the toolkit-root default'
    )


@pytest.mark.parametrize("module", HUB_WATCHDOG_MODULES)
def test_hub_watchdog_module_source_exists_and_is_executable(module: str) -> None:
    src = HUB_SCRIPTS_DIR / f"hub-watchdog-{module}.sh"
    assert src.is_file(), f"shared/skills/hub/scripts/hub-watchdog-{module}.sh missing"
    assert os.access(src, os.X_OK), f"hub-watchdog-{module}.sh is not executable"


# ── telemetry package registration (issue #319) ───────────────────────────────
# The `for name in …` loop copies .sh FILES; the telemetry python PACKAGE needs its own
# recursive step. Without it .ai-toolkit/scripts/telemetry never exists, so the drain's
# self-copy (built from the synced scripts) has no package either and every drain land
# silently skipped the post-run ingestion — 51 lands, 4 days, no cycle-step scores. These
# are the source-level guards; the end-to-end outcome (complete + importable in a target)
# is covered by tests/integration/test_workflow_scripts_sync.py.
TELEMETRY_PKG = REPO_ROOT / "scripts" / "telemetry"


def test_telemetry_package_copy_invoked_in_sync_workflow_scripts() -> None:
    assert "copy_telemetry_package" in _sync_workflow_scripts_body(), (
        "sync_workflow_scripts() never copies the telemetry package — a synced target and "
        "the drain's self-copy would both lack it, and every land skips Langfuse ingestion"
    )


def test_telemetry_package_copy_enumerates_the_source_tree() -> None:
    # Complete BY CONSTRUCTION (#316's lesson, applied to a package): the copy must walk the
    # source tree, never a hand-listed set of module names. A manifest is a thing to forget,
    # and a forgotten module is an ImportError on a land nobody is watching.
    body = _copy_telemetry_package_body()
    assert "find " in body, (
        "copy_telemetry_package must enumerate the package with find, not a hand-listed manifest"
    )
    hardcoded = [m.name for m in TELEMETRY_PKG.glob("*.py") if m.name in body]
    assert hardcoded == [], (
        f"copy_telemetry_package hard-codes module names {hardcoded} — enumerate the tree instead"
    )


def test_telemetry_package_copy_prunes_pycache() -> None:
    # The hub EXECUTES the package in place, so __pycache__ is always present at the source;
    # copying it ships stale bytecode into every target and churns the sync manifest.
    assert "__pycache__" in _copy_telemetry_package_body(), (
        "copy_telemetry_package must prune __pycache__ — the hub's source tree always has one"
    )


def _extract_fn(name: str) -> str:
    """The verbatim source of a top-level function in sync-to-repo.sh.

    sync-to-repo.sh parses argv and runs at top level, so it cannot simply be sourced. The
    copy helpers are pure functions of their args + a few globals, so lift them into a
    harness and exercise the REAL code (not a paraphrase of it).
    """
    text = SYNC_SCRIPT.read_text()
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in sync-to-repo.sh"
    return match.group(0)


def _run_copy_telemetry_package(
    tmp_path: Path, script_dir: Path, *, dry_run: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run the real copy_telemetry_package against a fake SCRIPT_DIR, under `set -euo pipefail`."""
    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    harness = "\n".join(
        [
            "set -euo pipefail",
            f'TARGET="{target}"',
            f'SCRIPT_DIR="{script_dir}"',
            f"DRY_RUN={dry_run}",
            f'RECORD_FILE="{tmp_path / "record"}"',
            ': > "$RECORD_FILE"',
            "warn() { printf 'WARN: %s\\n' \"$*\" >&2; }",
            _extract_fn("record_file"),
            _extract_fn("make_dir"),
            _extract_fn("copy_file"),
            _extract_fn("copy_telemetry_package"),
            'copy_telemetry_package "$TARGET/.ai-toolkit/scripts/telemetry" && echo "RC=0" || echo "RC=$?"',
        ]
    )
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


def test_telemetry_package_copy_reports_a_missing_source(tmp_path: Path) -> None:
    # A silent skip dressed as success is the #319 bug itself: the caller prints a green
    # "scripts/telemetry/" on a 0 return, so a no-op MUST report non-zero.
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()  # no telemetry/ at all

    result = _run_copy_telemetry_package(tmp_path, script_dir)

    assert "RC=1" in result.stdout, f"missing package source must report failure: {result.stdout}"


def test_telemetry_package_copy_reports_an_empty_source(tmp_path: Path) -> None:
    # `[ -d ]` passes for an empty dir, so a presence check alone would no-op and still
    # report success — a target that cannot ingest, announced as a clean sync.
    pkg = tmp_path / "scripts" / "telemetry"
    pkg.mkdir(parents=True)

    result = _run_copy_telemetry_package(tmp_path, tmp_path / "scripts")

    assert "RC=1" in result.stdout, f"empty package source must report failure: {result.stdout}"


def test_telemetry_package_copy_recurses_and_prunes(tmp_path: Path) -> None:
    # The happy path, end to end through the real helpers: nested modules land, __pycache__
    # does not, and every copied file is recorded for the manifest GC.
    pkg = tmp_path / "scripts" / "telemetry"
    (pkg / "spoke_tree").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "__init__.py").write_text("# root\n")
    (pkg / "spoke_tree" / "steps.py").write_text("# nested\n")
    (pkg / "__pycache__" / "stale.pyc").write_bytes(b"stale\n")

    result = _run_copy_telemetry_package(tmp_path, tmp_path / "scripts")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    installed = tmp_path / "target" / ".ai-toolkit" / "scripts" / "telemetry"
    assert (installed / "spoke_tree" / "steps.py").is_file(), "a nested module must be copied"
    assert not (installed / "__pycache__").exists(), "__pycache__ must be pruned"
    recorded = (tmp_path / "record").read_text().splitlines()
    assert ".ai-toolkit/scripts/telemetry/spoke_tree/steps.py" in recorded, (
        "every copied module must be manifest-recorded or the GC cannot reclaim it"
    )


def test_sync_warns_when_the_telemetry_package_is_missing() -> None:
    # The caller must branch on the helper's status: calling it bare prints the success line
    # even when nothing was copied.
    body = _sync_workflow_scripts_body()
    assert re.search(r"if copy_telemetry_package .*; then", body), (
        "sync_workflow_scripts must branch on copy_telemetry_package's status (#319)"
    )
    assert re.search(r"else\s*\n\s*warn ", body), (
        "sync_workflow_scripts must warn LOUD when the telemetry package is missing (#319)"
    )


def test_telemetry_package_source_exists() -> None:
    # The view builder telemetry-ingest-spoke.sh runs, and the package marker that makes the
    # tree importable once PYTHONPATH points at the synced scripts dir.
    assert (TELEMETRY_PKG / "langfuse_spoke_tree.py").is_file(), (
        "scripts/telemetry/langfuse_spoke_tree.py missing — the sync would copy no view builder"
    )
    assert (TELEMETRY_PKG / "__init__.py").is_file(), (
        "scripts/telemetry/__init__.py missing — the synced tree would not be importable"
    )
