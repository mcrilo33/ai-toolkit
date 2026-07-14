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
