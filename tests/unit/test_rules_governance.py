"""Governance guards for the shared rules.

These tests do not judge rule *content* quality — they lock in two structural
invariants that are easy to regress silently:

1. The always-on rule payload (Cursor ``alwaysApply: true`` set) stays within a
   line budget, so the rules injected into every conversation can't quietly
   re-bloat over time.
2. The delegation policy stated in ``agent-orchestration.md`` stays consistent
   with what the ``delegation-gate-warn`` hook enforces: ``code-review`` is the
   only hard ship gate; ``planner`` / ``tdd-*`` are advisory nudges. The rule is
   the explanation; the hook is the enforcement — they must agree.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "shared" / "rules"
HOOKS_DIR = REPO_ROOT / "shared" / "hooks"

# Make scripts/ importable
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from metadata_parser import parse

# Cursor always-on payload was 285 lines (2026-06) before the agent-orchestration
# slim-down brought it to ~238. This ceiling ratchets it and stops it growing back
# toward the old triple-restatement bloat. Lower it as the set shrinks; never raise
# it without a deliberate decision.
MAX_ALWAYS_ON_LINES = 250

# The rules expected to be always-on. Membership is asserted so that flipping a
# rule's alwaysApply flag shows up as a conscious change here, not a silent drift.
EXPECTED_ALWAYS_ON = {
    "guidelines",
    "security",
    "agent-orchestration",
    "scientific-integrity",
}


def _always_on_rule_names() -> set[str]:
    """Return rule stems that are ``alwaysApply: true`` in the synced Cursor payload.

    Uses the production parser so per-tool override blocks (``cursor: {alwaysApply:
    true}``) count — a naive top-level read would miss exactly that silent flip.
    """
    items = parse(str(RULES_DIR / "metadata.yml"))
    return {
        name
        for name, data in items.items()
        # parse() keeps scalars as raw strings ("true"), not YAML booleans.
        if str(
            {**data["__defaults"], **data["__overrides"].get("cursor", {})}.get("alwaysApply")
        ).lower()
        == "true"
    }


class TestAlwaysOnBudget:
    """The always-on rule payload must stay small and well-defined."""

    def test_always_on_set_is_expected(self) -> None:
        """Flipping a rule's alwaysApply flag must be a conscious, visible change."""
        assert _always_on_rule_names() == EXPECTED_ALWAYS_ON

    def test_always_on_payload_within_budget(self) -> None:
        """Total lines of always-on rules stay within the ceiling."""
        per_file = {
            name: len((RULES_DIR / f"{name}.md").read_text().splitlines())
            for name in _always_on_rule_names()
        }
        total = sum(per_file.values())

        assert total <= MAX_ALWAYS_ON_LINES, (
            f"Always-on payload is {total} lines (budget {MAX_ALWAYS_ON_LINES}). "
            f"Per file: {per_file}"
        )


class TestShipGateContract:
    """code-review is the only hard ship gate; planner/tdd-* are advisory.

    Guards the rule <-> hook contract: the rule describes this split, and the hook
    must enforce exactly it — promoting only code-review to a hard block, never
    planner.
    """

    def _ship_gate_calls(self) -> list[str]:
        """Lines that promote a hint to the hard ship gate (excludes the def)."""
        hook = (HOOKS_DIR / "delegation-gate-warn.sh").read_text()
        return [line for line in hook.splitlines() if line.lstrip().startswith("ship_gate_add ")]

    def test_only_code_review_hard_blocks_the_ship(self) -> None:
        calls = self._ship_gate_calls()
        assert calls, "no ship_gate_add invocation — the code-review ship gate is missing"
        for line in calls:
            assert "code-review" in line, f"unexpected hard ship gate: {line.strip()}"
            assert "planner" not in line, (
                f"planner must not hard-block the ship (it is advisory): {line.strip()}"
            )

    def test_rule_states_the_same_contract(self) -> None:
        rule = (RULES_DIR / "agent-orchestration.md").read_text().lower()
        assert "code-review" in rule and "hard gate" in rule, (
            "agent-orchestration.md must name code-review as the hard ship gate"
        )
        assert "advisory" in rule, (
            "agent-orchestration.md must describe the advisory (planner/tdd) nudges"
        )
