"""Anti-monolith guardrail (AFK Design Principle 7).

Drain concurrency is scope-graph-bound: two issues run in parallel only if their
`Scope:` files are disjoint. So a control-plane script that every change must touch
silently serializes the whole backlog — the cost stays invisible until nothing runs
concurrently. This test makes "the file got too big to schedule around" a RED test
that forces a split, instead of pain noticed months later.

`gate-broker.sh` was 3,700 lines before #275 split it into ~600-900-line modules;
`hub-afk.sh` then grew to ~4,400 and became the new bottleneck. The budget below is
set above the healthy post-split module size and below the monolith range, so a file
crossing it is a signal to decompose by responsibility (dispatch / recover / land /
tick loop) behind a thin entry lib.

Control-plane boundary (what this guard measures, and why it is an explicit list):
The drain runs a hub side and a spoke side, so the guard measures both.

- Hub side: every ``*.sh`` in ``shared/skills/hub/scripts`` — that directory holds
  only control-plane scripts, so an unfiltered glob is the right boundary there.
- Spoke side: ``scripts/`` is mixed — it holds the spoke lifecycle scripts AND
  unrelated install/build utilities. Only the lifecycle scripts become a common
  ``Scope:`` token that serializes the backlog, so the guard measures an EXPLICIT
  allowlist rather than an incidental glob:
    * the growing ``worktree-*.sh`` and ``spoke-*.sh`` families (matched by prefix so
      a newly added lifecycle script is measured automatically), plus
    * the standalone lifecycle scripts ``sync-to-repo.sh``, ``gate-sweep.sh``,
      ``telemetry-ingest-spoke.sh``.
  Everything else in ``scripts/`` is deliberately OUT: install/build/test-harness and
  travel helpers (``install.sh``, ``install-git-hooks.sh``, ``build-cursor-plugin.sh``,
  ``list-cursor-rules.sh``, ``ensure-test-venv.sh``, ``test-run.sh``,
  ``test-budget-watch.sh``, ``afk-travel.sh``, ``travel-local.sh``) are operator
  tooling, never a shared spoke ``Scope:``, so a scheduling-bottleneck budget has no
  business measuring them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_CONTROL_PLANE_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"
SPOKE_CONTROL_PLANE_DIR = REPO_ROOT / "scripts"

# Spoke-side control-plane boundary. Prefix families cover the two growing lifecycle
# families so a new worktree-*/spoke-* script is measured without touching this list;
# the explicit names are the standalone lifecycle scripts. See the module docstring
# for the rationale on what is in and what is out.
_SPOKE_CONTROL_PLANE_PREFIXES = ("worktree-", "spoke-")
_SPOKE_CONTROL_PLANE_NAMES = frozenset(
    {
        "sync-to-repo.sh",
        "gate-sweep.sh",
        "telemetry-ingest-spoke.sh",
    }
)

# The line budget for a single control-plane script. Chosen from the #275 split:
# the resulting gate-broker-*.sh modules are 580-900 lines; a healthy module lives
# well under this. Raise it only with a deliberate decision recorded here — not to
# quiet a growing monolith.
LINE_BUDGET = 1200

# Files knowingly over budget, each with the issue tracking its split. A file may sit
# here ONLY while its split is an open, tracked commitment — this is a countdown, not
# a parking lot. Removing the entry (by landing the split) is the goal.
KNOWN_OVER_BUDGET: dict[str, str] = {
    "worktree-lib.sh": "1346 lines; split behind a thin entry lib tracked in #353",
}


def _is_spoke_control_plane(name: str) -> bool:
    """Return True if a ``scripts/`` filename is spoke-side control plane.

    The boundary is the documented allowlist: the ``worktree-*.sh`` / ``spoke-*.sh``
    families, plus the standalone lifecycle scripts. Everything else in ``scripts/``
    (install/build/test-harness/travel helpers) is out.
    """
    if not name.endswith(".sh"):
        return False
    if name in _SPOKE_CONTROL_PLANE_NAMES:
        return True
    return name.startswith(_SPOKE_CONTROL_PLANE_PREFIXES)


def _control_plane_scripts() -> list[Path]:
    hub = HUB_CONTROL_PLANE_DIR.glob("*.sh")
    spoke = (p for p in SPOKE_CONTROL_PLANE_DIR.glob("*.sh") if _is_spoke_control_plane(p.name))
    return sorted([*hub, *spoke], key=lambda p: p.name)


def _script_by_name() -> dict[str, Path]:
    return {p.name: p for p in _control_plane_scripts()}


@pytest.mark.parametrize("script", _control_plane_scripts(), ids=lambda p: p.name)
def test_control_plane_script_under_line_budget(script: Path) -> None:
    lines = script.read_text().count("\n")
    name = script.name
    if name in KNOWN_OVER_BUDGET:
        pytest.skip(f"{name} over budget by design: {KNOWN_OVER_BUDGET[name]}")
    assert lines <= LINE_BUDGET, (
        f"{name} is {lines} lines (> {LINE_BUDGET}). A control-plane file that every "
        f"change must touch serializes the drain's backlog on its scope (AFK Design "
        f"Principle 7). Split it by responsibility behind a thin entry lib, or — if a "
        f"split is genuinely tracked — add it to KNOWN_OVER_BUDGET with its issue."
    )


def test_spoke_lifecycle_scripts_are_measured() -> None:
    # The spoke side used to be a blind spot: the guard globbed only the hub dir, so
    # scripts/worktree-lib.sh drifted 146 lines over budget unnoticed (#350). Pin that
    # the lifecycle scripts are now measured.
    measured = {p.name for p in _control_plane_scripts()}
    for name in (
        "worktree-lib.sh",
        "worktree-land.sh",
        "worktree-new.sh",
        "sync-to-repo.sh",
        "gate-sweep.sh",
        "telemetry-ingest-spoke.sh",
        "spoke-push.sh",
    ):
        assert name in measured, (
            f"{name} is spoke-side control plane but is not measured by the guard"
        )


def test_non_control_plane_utilities_are_excluded() -> None:
    # The boundary must exclude scripts/ utilities that are not a shared spoke Scope:
    # token, so a scheduling-bottleneck budget never measures install/build tooling.
    measured = {p.name for p in _control_plane_scripts()}
    for name in ("install.sh", "build-cursor-plugin.sh", "list-cursor-rules.sh"):
        assert name not in measured, (
            f"{name} is not control plane and must not sit in the line budget"
        )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("worktree-lib.sh", True),
        ("worktree-newly-added.sh", True),
        ("spoke-push.sh", True),
        ("sync-to-repo.sh", True),
        ("gate-sweep.sh", True),
        ("telemetry-ingest-spoke.sh", True),
        ("install.sh", False),
        ("install-git-hooks.sh", False),
        ("build-cursor-plugin.sh", False),
        ("list-cursor-rules.sh", False),
        ("ensure-test-venv.sh", False),
        ("afk-travel.sh", False),
        ("README.md", False),
    ],
)
def test_is_spoke_control_plane_classifies_by_documented_boundary(
    name: str, expected: bool
) -> None:
    assert _is_spoke_control_plane(name) is expected


def test_known_over_budget_entries_are_actually_over_budget() -> None:
    # Keep the allowlist honest: an entry that is no longer over budget (its split
    # landed) must be removed, so the list can never rot into a silent exemption.
    by_name = _script_by_name()
    for name in KNOWN_OVER_BUDGET:
        script = by_name.get(name)
        assert script is not None and script.is_file(), (
            f"KNOWN_OVER_BUDGET names {name}, which is not a measured control-plane script"
        )
        lines = script.read_text().count("\n")
        assert lines > LINE_BUDGET, (
            f"{name} is now {lines} lines (<= {LINE_BUDGET}) — its split landed. "
            f"Remove it from KNOWN_OVER_BUDGET."
        )
