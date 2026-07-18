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

import re
import sys
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from metadata_parser import parse, query

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "shared" / "rules"
HOOKS_DIR = REPO_ROOT / "shared" / "hooks"
RULES_META = RULES_DIR / "metadata.yml"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-to-repo.sh"

# The two metadata fields that DEFINE a rule's intended Claude disposition (the
# #320 contract, mirrored in the issue's acceptance criteria). Named here as the
# specification — the *emission* is driven separately through the production
# field set read from sync-to-repo.sh, so a mapping drift shows up as a mismatch.
PATHS_FIELD = "paths"
ALWAYS_ON_FIELD = "alwaysApply"

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


def _claude_rule_field_mapping() -> tuple[list[str], str | None]:
    """Return the Claude rules ``(fields, always_on)`` as configured in the sync.

    Reads ``CLAUDE_RULE_FIELDS`` and the ``query_metadata`` always-on argument
    straight from ``sync-to-repo.sh`` so this governance check drives the *same*
    field mapping the real sync uses. A future edit that drops ``paths`` from the
    field set or removes the ``alwaysApply`` always-on arg flows into this test and
    turns the disposition assertions below red.
    """
    text = SYNC_SCRIPT.read_text()

    fields_m = re.search(r'CLAUDE_RULE_FIELDS="([^"]*)"', text)
    assert fields_m, "CLAUDE_RULE_FIELDS not found in sync-to-repo.sh"
    fields = [f.strip() for f in fields_m.group(1).split(",") if f.strip()]

    # The claude rules query: query_metadata "…/rules/metadata.yml" claude
    #   "$CLAUDE_RULE_FIELDS" "<config>" <always_on_field>
    # The always-on field is the trailing token — restricted to an identifier so a
    # dropped arg captures nothing (None) rather than swallowing the following shell
    # pipe, keeping the "absent → historic emit-only-if-field" contract reachable.
    call_m = re.search(
        r'query_metadata\s+"[^"]*rules/metadata\.yml"\s+claude\s+'
        r'"\$CLAUDE_RULE_FIELDS"(?:\s+"[^"]*")?(?:\s+([A-Za-z_]\w*))?',
        text,
    )
    assert call_m, "claude rules query_metadata invocation not found in sync-to-repo.sh"
    return fields, call_m.group(1)


def _merged_claude(data: dict) -> dict:
    """Merge a parsed item's shared defaults with its ``claude`` override block."""
    return {**data["__defaults"], **data["__overrides"].get("claude", {})}


def _intended_disposition(merged: dict) -> str:
    """The Claude disposition a rule *should* get, per the #320 contract.

    ``paths`` wins over ``alwaysApply`` — matching ``query``'s precedence, where a
    requested field present suppresses the always-on empty-frontmatter fallback.
    """
    if PATHS_FIELD in merged:
        return "conditional"
    if str(merged.get(ALWAYS_ON_FIELD, "")).lower() == "true":
        return "always-on"
    return "on-demand"


def _fm_keys(fm: str) -> list[str]:
    """Top-level frontmatter keys in an emitted (echo -e encoded) fm string."""
    return [line.split(":", 1)[0] for line in fm.split("\\n") if line]


def _emitted_glob(fm: str) -> str:
    """Recover the ``paths`` glob from a conditional rule's emitted frontmatter."""
    assert fm.startswith(f"{PATHS_FIELD}:"), f"expected a {PATHS_FIELD} frontmatter, got {fm!r}"
    raw = fm[len(PATHS_FIELD) + 1 :].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        # Reverse _emit_scalar's double-quoted escaping (globs carry no \ or ").
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


class TestClaudeRuleDelivery:
    """Every shared rule reaches Claude with the field its metadata dictates.

    Drives the production parser (:func:`query`) with the Claude field mapping read
    from ``sync-to-repo.sh`` — the same code path the sync runs — and compares each
    rule's emission against the disposition its ``metadata.yml`` entry declares. This
    is the check whose absence let the #320 delivery bug (always-on rules silently
    never reaching ``.claude/rules/``) go unnoticed for months.
    """

    def _emission(self) -> dict[str, str]:
        """``{rule_name: frontmatter}`` the Claude sync emits (unemitted rules absent)."""
        items = parse(str(RULES_META))
        fields, always_on = _claude_rule_field_mapping()
        return dict(query(items, "claude", fields, always_on=always_on))

    def _dispositions(self) -> dict[str, str]:
        """``{rule_name: intended_disposition}`` derived from metadata.yml."""
        items = parse(str(RULES_META))
        return {name: _intended_disposition(_merged_claude(data)) for name, data in items.items()}

    def test_always_on_rules_emit_no_paths(self) -> None:
        """Always-on rules reach Claude with empty frontmatter (no ``paths`` key)."""
        emission = self._emission()
        always_on = [n for n, d in self._dispositions().items() if d == "always-on"]
        assert always_on, "no always-on rules found — metadata regressed"
        for name in always_on:
            assert name in emission, f"always-on rule {name!r} missing from Claude output"
            assert emission[name] == "", (
                f"always-on rule {name!r} must emit empty frontmatter, got {emission[name]!r}"
            )
            assert PATHS_FIELD not in _fm_keys(emission[name]), (
                f"always-on rule {name!r} must not carry a {PATHS_FIELD}: key"
            )

    def test_conditional_rules_emit_exact_glob(self) -> None:
        """Each ``paths`` rule reaches Claude with its glob, byte-for-byte."""
        items = parse(str(RULES_META))
        emission = self._emission()
        conditional = [n for n, d in self._dispositions().items() if d == "conditional"]
        assert conditional, "no conditional rules found — metadata regressed"
        for name in conditional:
            assert name in emission, f"conditional rule {name!r} missing from Claude output"
            expected = _merged_claude(items[name])[PATHS_FIELD]
            assert _emitted_glob(emission[name]) == expected, (
                f"conditional rule {name!r} glob mismatch: "
                f"emitted {_emitted_glob(emission[name])!r} != metadata {expected!r}"
            )

    def test_on_demand_rules_not_emitted(self) -> None:
        """Rules that are neither always-on nor conditional never reach Claude."""
        emission = self._emission()
        on_demand = [n for n, d in self._dispositions().items() if d == "on-demand"]
        assert on_demand, "no on-demand rules found — metadata regressed"
        for name in on_demand:
            assert name not in emission, (
                f"on-demand rule {name!r} must not be emitted to Claude, got {emission[name]!r}"
            )

    def test_no_claude_rule_leaks_non_paths_key(self) -> None:
        """The Claude frontmatter carries only ``paths`` — never alwaysApply/globs/applyTo."""
        emission = self._emission()
        assert emission, "no rules emitted to Claude — sync regressed"
        for name, fm in emission.items():
            leaked = [k for k in _fm_keys(fm) if k != PATHS_FIELD]
            assert not leaked, f"Claude rule {name!r} leaked non-{PATHS_FIELD} key(s): {leaked}"

    def test_expected_always_on_all_reach_claude(self) -> None:
        """Cross-check: every EXPECTED_ALWAYS_ON rule is in the always-on Claude set.

        The specific regression the #320 delivery fix locks in — an always-on rule
        present in the sync output with empty (no ``paths``) frontmatter.
        """
        emission = self._emission()
        for name in EXPECTED_ALWAYS_ON:
            assert name in emission, f"expected always-on rule {name!r} missing from Claude output"
            assert emission[name] == "", (
                f"expected always-on rule {name!r} must emit empty frontmatter, "
                f"got {emission[name]!r}"
            )
