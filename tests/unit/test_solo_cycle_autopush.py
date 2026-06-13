"""Spokes auto-push and auto-emit ``ready/<id>`` without an end-of-cycle prompt.

Issue #28: the end-of-cycle human approval prompt goes away. A mid-cycle subtask
push pushes the own branch silently; the final subtask (agent confirms all
acceptance criteria met from its ledger) pushes *and* emits ``ready/<id>`` — both
without asking. The single human checkpoint becomes ``/land <id>`` on the hub.

These tests guard the *wording* of the two source docs that future spokes are
seeded with: ``solo-cycle/SKILL.md`` (PUSH step + final-push marker) and the
``start-task`` kickoff template (scoped — not blanket — confirmation rule).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "shared" / "skills"

SOLO_CYCLE = SKILLS_DIR / "solo-cycle" / "SKILL.md"
START_TASK = SKILLS_DIR / "start-task" / "SKILL.md"

# The blanket rule #28 replaces with a scoped one. It must be gone from the
# kickoff so future spokes are not seeded to ask before the routine push.
BLANKET_RULE = "Ask me before any irreversible step"


def _text(path: Path) -> str:
    return path.read_text()


def _flat(path: Path) -> str:
    """Lowercased text with runs of whitespace collapsed to single spaces.

    The docs wrap prose at 88 chars, so a phrase like ``default branch`` may
    straddle a line break — collapse first so substring checks are not brittle.
    """
    return re.sub(r"\s+", " ", _text(path)).lower()


def test_solo_cycle_push_is_autonomous() -> None:
    """The PUSH step says the spoke pushes its own branch without prompting."""
    flat = _flat(SOLO_CYCLE)
    assert "without prompting" in flat, (
        "solo-cycle PUSH step must state the own-branch push needs no human prompt"
    )


def test_solo_cycle_final_push_emits_ready_without_prompt() -> None:
    """The final-push marker is emitted on the final subtask without asking."""
    flat = _flat(SOLO_CYCLE)
    assert "final subtask" in flat
    # ready emission is automatic on the final subtask, not a human decision.
    assert "no human prompt" in flat or "without prompting" in flat, (
        "the ready/<id> emission on the final subtask must not prompt the human"
    )


def test_solo_cycle_completion_is_agent_determined() -> None:
    """Completion is determined by the agent from the acceptance criteria/ledger."""
    flat = _flat(SOLO_CYCLE)
    assert "agent-determined" in flat, "the skill must document that completion is agent-determined"
    assert "acceptance criteria" in flat, (
        "completion is read from the issue's acceptance criteria / ledger"
    )


def test_start_task_kickoff_drops_blanket_rule() -> None:
    """The blanket 'ask before any irreversible step' is gone from the kickoff."""
    # Collapse whitespace so a line-wrapped occurrence still counts as present.
    assert BLANKET_RULE.lower() not in _flat(START_TASK), (
        "the blanket confirmation rule must be replaced by a scoped one"
    )


def test_start_task_kickoff_scopes_confirmation() -> None:
    """The kickoff still asks before genuinely dangerous/irreversible ops."""
    flat = _flat(START_TASK)
    assert "force-push" in flat, "still ask before force-push"
    assert "history rewrite" in flat, "still ask before history rewrites"
    # anything touching the default branch / main stays gated.
    assert "default branch" in flat or "default-branch" in flat, (
        "still ask before anything touching the default branch"
    )


def test_start_task_kickoff_does_not_gate_routine_push() -> None:
    """The routine own-branch push + ready emission is explicitly not gated."""
    flat = _flat(START_TASK)
    assert "own-branch push" in flat, (
        "the kickoff must call out the routine own-branch push as not needing approval"
    )
    assert "ready/" in flat, (
        "the kickoff must mention the ready marker as part of the routine, ungated flow"
    )
    # Bind the ready emission to the ungated context so a future edit can't move
    # the marker into a prompted sentence and still pass.
    assert "ready emission needs no approval" in flat, (
        "the ready emission must be explicitly marked as not needing approval"
    )
