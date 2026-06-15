"""Gated spokes — the PLAN gate by default (issue #34).

A spoke no longer runs blindly to ``ready/``; its human checkpoint moves to the
cheap, early PLAN gate. Gate level is declared per task (default = PLAN for
non-trivial, autonomous for very-clear), and the spoke **prints the plan as a
normal visible message** and parks on a ``gate/<id>`` tag — no harness plan mode
— before writing code. These tests guard the *wording* of
the two source docs future spokes are seeded with — ``solo-cycle/SKILL.md`` (the
gate spectrum + the PLAN gate step + the ``gate/<id>`` park-marker convention)
and the ``start-task`` kickoff (gate-level declaration).

Mirrors ``test_solo_cycle_autopush.py``: flatten the prose, then substring-assert
so an 88-char wrap can't split a checked phrase.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "shared" / "skills"

SOLO_CYCLE = SKILLS_DIR / "solo-cycle" / "SKILL.md"
# START_TASK is exercised by the subtask-2 kickoff assertions added below.
START_TASK = SKILLS_DIR / "start-task" / "SKILL.md"


def _flat(path: Path) -> str:
    """Lowercased text, runs of whitespace collapsed to single spaces."""
    return re.sub(r"\s+", " ", path.read_text()).lower()


# ── Subtask 1: solo-cycle gains the gate spectrum + PLAN gate ───────


def test_solo_cycle_documents_gate_spectrum() -> None:
    """solo-cycle names a per-task gate spectrum with its levels."""
    flat = _flat(SOLO_CYCLE)
    assert "gate spectrum" in flat or "gate level" in flat, (
        "solo-cycle must document a per-task gate spectrum / gate level"
    )
    # The spectrum spans no-gate through the PLAN+RED and behavioral combos.
    assert "plan gate" in flat
    assert "red gate" in flat
    assert "draft" in flat and "human-acceptance" in flat, (
        "the spectrum must name the RED, human-acceptance, and draft gates"
    )


def test_solo_cycle_plan_gate_parks_via_git_native_marker() -> None:
    """The PLAN gate prints the plan as a visible message, then parks on gate/<id>."""
    flat = _flat(SOLO_CYCLE)
    # The plan is a normal visible message — never a harness plan-mode / ExitPlanMode card.
    assert "plan mode" not in flat, "the PLAN gate must not use plan mode"
    assert "exitplanmode" not in flat, "the PLAN gate must not use ExitPlanMode"
    assert "visible message" in flat, "the plan is presented as a visible message"
    assert "open questions" in flat, "the presented plan surfaces open questions"
    assert "before writing code" in flat or "before green" in flat, (
        "the PLAN gate must pause before any implementation"
    )
    # The park + explicit stop is the git-native gate/<id> tag, not a harness primitive.
    assert "gate/<issue>" in flat or "gate/<id>" in flat, (
        "the gate is the git-native gate/<id> park marker"
    )
    assert "reply to approve" in flat, "the PLAN gate stops with an explicit reply-to-approve ask"


def test_solo_cycle_default_plan_for_nontrivial_autonomous_for_clear() -> None:
    """Default = PLAN for non-trivial; very-clear work runs autonomous."""
    flat = _flat(SOLO_CYCLE)
    assert "default" in flat and "non-trivial" in flat, (
        "PLAN must be stated as the default for non-trivial work"
    )
    assert "very-clear" in flat or "very clear" in flat, (
        "very-clear tasks must be called out as the autonomous (no-gate) lane"
    )
    assert "autonomous" in flat


def test_solo_cycle_runs_parallel_to_gate_not_serialized() -> None:
    """The spoke parks at its gate; the queue is reviewed non-serially."""
    flat = _flat(SOLO_CYCLE)
    assert "park" in flat, "the spoke parks at its gate rather than blocking"


def test_solo_cycle_defines_gate_marker_convention() -> None:
    """A ``gate/<id>`` tag carries the current park state, distinct from ready/."""
    flat = _flat(SOLO_CYCLE)
    assert "gate/<issue>" in flat or "gate/<id>" in flat, (
        "solo-cycle must define the gate/<id> park-marker convention"
    )
    # ready/<id> stays the distinct final-completion marker.
    assert "ready/<issue>" in flat or "ready/<id>" in flat


# ── Marker emission is the scripted spoke-ready.sh path, not hand-written git (#45)


def test_solo_cycle_emits_gate_via_script() -> None:
    """The gate marker emits via spoke-ready.sh, not a hand-written git tag/push."""
    flat = _flat(SOLO_CYCLE)
    assert "spoke-ready.sh --gate" in flat, "the gate marker must emit via `spoke-ready.sh --gate`"
    # The LLM-narrated chain that #45 replaces must be gone.
    assert "git tag -f -a gate" not in flat, (
        "the hand-written `git tag … && git push …` gate chain must be removed"
    )


def test_solo_cycle_retag_uses_script() -> None:
    """The re-tag-at-new-tip guidance points at the idempotent spoke-ready.sh."""
    flat = _flat(SOLO_CYCLE)
    assert "git tag -f ready/" not in flat, (
        "the hand-written ready re-tag chain must be replaced by spoke-ready.sh"
    )


def test_start_task_emits_gate_via_script() -> None:
    """The kickoff parks via spoke-ready.sh, not a hand-written git tag/push."""
    flat = _flat(START_TASK)
    assert "spoke-ready.sh --gate" in flat, (
        "the kickoff's gate marker must emit via `spoke-ready.sh --gate`"
    )
    assert "git tag -f -a gate" not in flat, (
        "the kickoff must not hand-write the git tag/push gate chain"
    )


# ── Subtask 2: start-task declares the gate level + wires the kickoff ───


def test_start_task_triages_a_gate_level() -> None:
    """start-task picks a gate level by risk/novelty, defaulting to PLAN."""
    flat = _flat(START_TASK)
    assert "gate level" in flat or "gate spectrum" in flat, (
        "start-task must triage a gate level for the dispatched task"
    )
    assert "default" in flat and "plan" in flat and "non-trivial" in flat, (
        "PLAN must be the declared default for non-trivial work"
    )
    assert "very-clear" in flat or "very clear" in flat, (
        "very-clear tasks must be called out as the autonomous lane"
    )


def test_start_task_records_gate_level_in_the_issue() -> None:
    """The gate level is recorded in the issue body — the durable contract."""
    flat = _flat(START_TASK)
    assert "gate:" in flat, "start-task must record the gate level as a Gate: line in the issue"


def test_start_task_kickoff_parks_at_the_plan_gate_git_native() -> None:
    """The kickoff has the spoke print the plan as a message, then park on gate/N."""
    flat = _flat(START_TASK)
    assert "plan gate" in flat, "the kickoff must name the PLAN gate"
    # No harness plan-mode / ExitPlanMode — the plan is a normal visible message.
    assert "plan mode" not in flat, "the kickoff must not invoke plan mode"
    assert "exitplanmode" not in flat, "the kickoff must not invoke ExitPlanMode"
    assert "visible message" in flat, "the plan is presented as a visible message"
    assert "before green" in flat or "before writing code" in flat, (
        "the kickoff must pause before implementation for non-trivial work"
    )
    # The marker the spoke emits when it parks (consumed by the hub watch).
    assert "gate/" in flat, "the kickoff references the gate/<id> park marker"


# ── Subtask: the gate-ACTION enum {human-pause | agent-review | none} (#40 ST4) ─
# A second dimension layered onto the gate spectrum: WHO services a parked gate.
# It is derived from MODE, not declared per task — day mode pauses for a human,
# night mode routes judgment gates to an independent adversarial reviewer and
# escalates to PARK. Shared with #34 as documented convention (substring-guarded),
# not a typed contract.


def test_solo_cycle_documents_gate_action_enum() -> None:
    flat = _flat(SOLO_CYCLE)
    assert "gate action" in flat, "solo-cycle must document the gate-action dimension"
    for value in ("human-pause", "agent-review", "none"):
        assert value in flat, f"the gate-action enum must name '{value}'"


def test_solo_cycle_gate_action_is_mode_derived() -> None:
    flat = _flat(SOLO_CYCLE)
    # Day mode = human-pause (today's behavior); night mode = agent-review.
    assert "day" in flat and "human-pause" in flat
    assert "night" in flat and "agent-review" in flat
    assert "mode" in flat, "the action is derived from mode, not declared per task"


def test_solo_cycle_night_reviewer_is_independent_adversarial_bounded() -> None:
    flat = _flat(SOLO_CYCLE)
    assert "independent" in flat and "adversarial" in flat
    assert "refute" in flat, "the night reviewer is prompted to refute, not rubber-stamp"
    assert "two round" in flat or "2-round" in flat or "two-round" in flat, (
        "the revise loop is bounded to two rounds"
    )
    assert "park" in flat, "the bounded loop escalates to park"


def test_solo_cycle_inherently_human_gate_always_parks() -> None:
    flat = _flat(SOLO_CYCLE)
    assert "always park" in flat, "draft/acceptance gates always park (agent cannot stand in)"
    assert "draft" in flat or "acceptance" in flat
    assert "accept/" in flat, "the inherently-human park emits the accept/<issue> marker"


def test_solo_cycle_documents_anti_gutting_clause() -> None:
    flat = _flat(SOLO_CYCLE)
    assert "sys.exit(0)" in flat, "the code-review reviewer checks for a sys.exit(0) cheat"
    assert "gut" in flat or "weaken" in flat
    assert "assert" in flat, "the cheat to catch is deleted/weakened assertions"


def test_solo_cycle_is_honest_about_enforceability() -> None:
    flat = _flat(SOLO_CYCLE)
    # The PLAN/RED adversarial review is policy (a spoke can narrate a review it
    # never ran); the mechanical backstops are named honestly.
    assert "policy" in flat or "behavioral" in flat
    assert "anti-gutting" in flat, "the anti-gutting tripwire is the mechanical backstop"
    assert "test-select" in flat, "test-select (tests must pass) is the other backstop"


def test_solo_cycle_three_terminal_markers_free_a_slot() -> None:
    flat = _flat(SOLO_CYCLE)
    for marker in ("ready/", "accept/", "blocked/"):
        assert marker in flat, f"the {marker} terminal marker must be documented"
    assert (
        "frees a slot" in flat
        or "free a supervisor slot" in flat
        or "frees a supervisor slot" in flat
    )


def test_start_task_documents_mode_derived_gate_action() -> None:
    flat = _flat(START_TASK)
    assert "gate action" in flat, "start-task must note the gate-action dimension"
    assert "mode" in flat
    assert "human-pause" in flat and "agent-review" in flat
