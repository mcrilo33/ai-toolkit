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
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"

# The line budget for a single control-plane script. Chosen from the #275 split:
# the resulting gate-broker-*.sh modules are 580-900 lines; a healthy module lives
# well under this. Raise it only with a deliberate decision recorded here — not to
# quiet a growing monolith.
LINE_BUDGET = 1200

# Files knowingly over budget, each with the issue tracking its split. A file may sit
# here ONLY while its split is an open, tracked commitment — this is a countdown, not
# a parking lot. Removing the entry (by landing the split) is the goal.
KNOWN_OVER_BUDGET: dict[str, str] = {}


def _control_plane_scripts() -> list[Path]:
    return sorted(CONTROL_PLANE_DIR.glob("*.sh"))


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


def test_known_over_budget_entries_are_actually_over_budget() -> None:
    # Keep the allowlist honest: an entry that is no longer over budget (its split
    # landed) must be removed, so the list can never rot into a silent exemption.
    for name in KNOWN_OVER_BUDGET:
        script = CONTROL_PLANE_DIR / name
        assert script.is_file(), f"KNOWN_OVER_BUDGET names {name}, which does not exist"
        lines = script.read_text().count("\n")
        assert lines > LINE_BUDGET, (
            f"{name} is now {lines} lines (<= {LINE_BUDGET}) — its split landed. "
            f"Remove it from KNOWN_OVER_BUDGET."
        )
