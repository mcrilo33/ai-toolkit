"""Gated spokes — the PLAN gate by default (issue #34).

A spoke no longer runs blindly to ``ready/``; its human checkpoint moves to the
cheap, early PLAN gate. Gate level is declared per task (default = PLAN for
non-trivial, autonomous for very-clear), and the spoke parks in plan mode to
present a concrete plan before writing code. These tests guard the *wording* of
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


def test_solo_cycle_plan_gate_pauses_before_code_via_plan_mode() -> None:
    """The PLAN gate parks in plan mode and presents a plan before code."""
    flat = _flat(SOLO_CYCLE)
    assert "plan mode" in flat, "the PLAN gate uses plan mode"
    assert "open questions" in flat, "the presented plan surfaces open questions"
    assert "before writing code" in flat or "before green" in flat, (
        "the PLAN gate must pause before any implementation"
    )


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


def test_start_task_kickoff_pauses_at_the_plan_gate() -> None:
    """The kickoff tells a non-trivial spoke to park at the PLAN gate."""
    flat = _flat(START_TASK)
    assert "plan gate" in flat, "the kickoff must name the PLAN gate"
    assert "plan mode" in flat, "the PLAN gate is presented in plan mode"
    assert "before green" in flat or "before writing code" in flat, (
        "the kickoff must pause before implementation for non-trivial work"
    )
    # The marker the spoke emits when it parks (consumed by the hub watch).
    assert "gate/" in flat, "the kickoff references the gate/<id> park marker"
